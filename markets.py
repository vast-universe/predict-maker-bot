"""Predict.fun market data: REST discovery plus the real-time WebSocket feed.

REST (``api.predict.fun``) discovers open Up/Down rounds and resolves them.
Real-time prices come from ``wss://ws.predict.fun/ws``: the socket subscribes to
``predictOrderbook/{marketId}``, ``predictTradingStatus/{marketId}``,
``predictMarketStatus/{marketId}`` and ``predictMarketChanged/{marketId}``,
echoes the 15s ``heartbeat``, and reconnects with backoff (resubscribing every
time). Each message is the whole Yes book, and the Down side is its complement at
the market's ``decimalPrecision`` -- the book frame itself carries no precision,
so the value discovered over REST is threaded through by market id.

The feed is push driven: :meth:`PredictWebSocketFeed.wait_for_update` blocks the
scan loop until a streamed message arrives (or a timeout expires), so a fresh
book is acted on without polling. ``predictMarketChanged`` additionally forces a
rediscovery when a round rolls over or resolves.

``websockets`` is optional: when it is missing the feed logs why and the scan
keeps using the REST ``markets/quotes`` endpoint, which is also the fallback for
any market whose streamed quote is older than ``quote_max_age``.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from math import isfinite
from decimal import Decimal
import threading
import time
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class MarketSnapshot:
    market_id: str
    asset: str
    window_minutes: int
    up_ask: Optional[float]
    down_ask: Optional[float]
    observed_at: str
    price_to_beat: Optional[float] = None
    seconds_left: Optional[float] = None
    fee_rate_bps: float = 0.0
    spot_price: Optional[float] = None
    sigma_1m: Optional[float] = None
    price_feed_provider: Optional[str] = None
    price_feed_symbol: Optional[str] = None
    market_title: Optional[str] = None


def down_ask_from_yes_bid(yes_bid: float, decimal_precision: int) -> float:
    """Convert the best Yes bid into the complementary No/Down ask price."""
    tick = Decimal(1).scaleb(-decimal_precision)
    return float((Decimal(1) - Decimal(str(yes_bid))).quantize(tick))


def valid_price(price: Optional[float]) -> bool:
    return price is not None and isfinite(price) and 0.0 < price < 1.0


def quote_price(level: Optional[dict]) -> Optional[float]:
    if not isinstance(level, dict):
        return None
    try:
        price = float(level["price"])
        size = float(level["size"]) if "size" in level else None
    except (KeyError, TypeError, ValueError):
        return None
    if not valid_price(price) or (size is not None and (not isfinite(size) or size <= 0)):
        return None
    return price


def _load_websockets():
    try:
        import websockets  # type: ignore
    except ImportError:
        return None
    return websockets


@dataclass
class BookQuote:
    """Best prices on both outcomes, as derived from the Yes book."""

    market_id: str
    up_bid: float | None = None
    up_ask: float | None = None
    down_bid: float | None = None
    down_ask: float | None = None
    timestamp_ms: int = 0
    trading_status: str = "OPEN"
    market_status: str = "OPEN"

    @property
    def received_at(self) -> float:
        return self.timestamp_ms / 1000.0

    @classmethod
    def from_orderbook(cls, payload: dict, decimal_precision: int | None = None) -> "BookQuote":
        asks = payload.get("asks") or []
        bids = payload.get("bids") or []
        up_ask = float(asks[0][0]) if asks else None
        up_bid = float(bids[0][0]) if bids else None
        # The streamed book frame has no decimalPrecision field, so the caller
        # passes the value discovered over REST; payload/3 are last resorts.
        precision = int(
            decimal_precision
            if decimal_precision is not None
            else payload.get("decimalPrecision") or 3
        )
        factor = 10 ** precision
        down_ask = (factor - round(up_bid * factor)) / factor if up_bid is not None else None
        down_bid = (factor - round(up_ask * factor)) / factor if up_ask is not None else None
        return cls(
            market_id=str(payload.get("marketId", "")),
            up_bid=up_bid, up_ask=up_ask, down_bid=down_bid, down_ask=down_ask,
            timestamp_ms=int(payload.get("updateTimestampMs") or 0),
        )

    @classmethod
    def from_top_of_book(cls, payload: dict, decimal_precision: int | None = None) -> "BookQuote":
        def price(level) -> float | None:
            if isinstance(level, dict):
                value = level.get("price")
            elif isinstance(level, (list, tuple)) and level:
                value = level[0]
            else:
                value = None
            return float(value) if value is not None else None

        up_bid = price(payload.get("bestBid") or payload.get("bid"))
        up_ask = price(payload.get("bestAsk") or payload.get("ask"))
        precision = int(
            decimal_precision
            if decimal_precision is not None
            else payload.get("decimalPrecision") or 3
        )
        factor = 10 ** precision
        down_ask = (factor - round(up_bid * factor)) / factor if up_bid is not None else None
        down_bid = (factor - round(up_ask * factor)) / factor if up_ask is not None else None
        return cls(
            market_id=str(payload.get("marketId", "")),
            up_bid=up_bid, up_ask=up_ask, down_bid=down_bid, down_ask=down_ask,
            timestamp_ms=int(payload.get("updateTimestampMs") or 0),
        )


@dataclass
class LiveQuoteStore:
    """Thread-safe latest quote per market, written by the socket thread."""

    quotes: dict[str, BookQuote] = field(default_factory=dict)
    trading_status: dict[str, str] = field(default_factory=dict)
    market_status: dict[str, str] = field(default_factory=dict)
    books: dict[str, dict] = field(default_factory=dict)
    market_changed: set[str] = field(default_factory=set)
    connected: bool = False
    messages: int = 0
    reconnects: int = 0

    def put_book(self, quote: BookQuote, payload: dict) -> None:
        self.quotes[quote.market_id] = quote
        self.books[quote.market_id] = payload
        self.messages += 1


class PredictWebSocketFeed:
    """One background connection multiplexing every tracked market's topics."""

    def __init__(
        self,
        *,
        api_key: str | None,
        url: str = "wss://ws.predict.fun/ws",
        on_error: Callable[[str], None] | None = None,
        max_age: float = 10.0,
    ):
        self.api_key = api_key
        self.url = url
        self.on_error = on_error or (lambda message: None)
        self.max_age = max_age
        self.store = LiveQuoteStore()
        self._topics: list[str] = []
        self._precision: dict[str, int] = {}
        self._request_id = 0
        self._socket = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_message_at = 0.0
        # Wakes the scan loop as soon as a streamed message lands.
        self._update = threading.Condition()
        self._update_seq = 0

    # ------------------------------------------------------------- lifecycle
    @property
    def available(self) -> bool:
        return _load_websockets() is not None

    def start(self) -> bool:
        """Start the socket thread. Returns False when websockets is missing."""
        if not self.available:
            self.on_error("websockets is not installed; live quotes fall back to REST")
            return False
        if self._thread is not None:
            return True
        self._thread = threading.Thread(target=self._run, name="predict-ws", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._loop is not None and self._socket is not None:
            asyncio.run_coroutine_threadsafe(self._close(), self._loop)
        if self._thread is not None:
            self._thread.join(timeout)

    # ---------------------------------------------------------- subscription
    def sync_markets(
        self,
        market_ids: list[str],
        *,
        books: bool = True,
        precision: dict[str, int] | None = None,
    ) -> None:
        """Subscribe to topics for new markets and drop the ones that closed."""
        if precision:
            self._precision.update({str(key): int(value) for key, value in precision.items()})
        book_topic = "predictOrderbook" if books else "predictTopOfBook"
        wanted = []
        for market_id in market_ids:
            wanted += [
                f"{book_topic}/{market_id}",
                f"predictTradingStatus/{market_id}",
                f"predictMarketStatus/{market_id}",
                f"predictMarketChanged/{market_id}",
            ]
        added = [topic for topic in wanted if topic not in self._topics]
        removed = [topic for topic in self._topics if topic not in wanted]
        self._topics = wanted
        for topic in removed:
            market_id = topic.split("/", 1)[1]
            self.store.quotes.pop(market_id, None)
            self.store.books.pop(market_id, None)
        if self._loop is not None and self._socket is not None:
            asyncio.run_coroutine_threadsafe(
                self._send([("subscribe", topic) for topic in added]
                           + [("unsubscribe", topic) for topic in removed]),
                self._loop,
            )

    def _precision_for(self, market_id: str) -> int | None:
        return self._precision.get(market_id)

    # -------------------------------------------------------------- reads
    def quotes_ready(self, market_ids: list[str], max_age: float | None = None) -> bool:
        """True when every market has a quote fresher than ``max_age``."""
        if not self.store.connected:
            return False
        now = time.time()
        limit = max_age if max_age is not None else self.max_age
        for market_id in market_ids:
            quote = self.store.quotes.get(market_id)
            if quote is None:
                return False
            if limit and quote.received_at and now - quote.received_at > limit:
                return False
        return True

    def quote(self, market_id: str) -> BookQuote | None:
        return self.store.quotes.get(market_id)

    def book_payload(self, market_id: str) -> dict | None:
        return self.store.books.get(market_id)

    def stale(self, seconds: float) -> bool:
        return not self._last_message_at or time.time() - self._last_message_at > seconds

    def last_message_age(self) -> float | None:
        """Seconds since the last frame arrived, or None before the first one."""
        if not self._last_message_at:
            return None
        return max(0.0, time.time() - self._last_message_at)

    def wait_for_update(self, timeout: float) -> bool:
        """Block until a streamed message arrives. True when a push woke us."""
        timeout = max(0.0, timeout)
        with self._update:
            sequence = self._update_seq
            return self._update.wait_for(
                lambda: self._update_seq != sequence, timeout
            )

    def wait_ready(self, market_ids: list[str], timeout: float = 3.0) -> bool:
        """Wait (bounded) for the socket to deliver a first book per market."""
        if not market_ids:
            return self.store.connected
        deadline = time.monotonic() + max(0.0, timeout)

        def ready() -> bool:
            return self.store.connected and all(
                market_id in self.store.quotes for market_id in market_ids
            )

        while not ready():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with self._update:
                self._update.wait(min(0.2, remaining))
        return True

    def take_market_changed(self) -> set[str]:
        """Return and clear the markets whose details changed since the last call."""
        changed = set(self.store.market_changed)
        self.store.market_changed.clear()
        return changed

    def status(self) -> str:
        """One-line connection summary for the console banner."""
        if not self.store.connected:
            return "ws=down rest-fallback"
        age = self.last_message_age()
        age_text = f"{age:.1f}s" if age is not None else "n/a"
        return (
            f"ws=live msgs={self.store.messages} "
            f"reconn={self.store.reconnects} last={age_text}"
        )

    def _notify_update(self) -> None:
        with self._update:
            self._update_seq += 1
            self._update.notify_all()

    # ------------------------------------------------------------- internals
    def _endpoint(self) -> str:
        if not self.api_key:
            return self.url
        join = "&" if "?" in self.url else "?"
        return f"{self.url}{join}apiKey={quote(self.api_key)}"

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as error:  # noqa: BLE001 - keep the bot running on REST
            self.store.connected = False
            self.on_error(f"Live WebSocket loop stopped: {type(error).__name__}: {error}")

    async def _main(self) -> None:
        websockets = _load_websockets()
        if websockets is None:
            return
        self._loop = asyncio.get_running_loop()
        attempt = 0
        while not self._stop.is_set():
            try:
                headers = {"x-api-key": self.api_key} if self.api_key else None
                async with websockets.connect(
                    self._endpoint(), additional_headers=headers,
                    ping_interval=None, max_size=None,
                ) as socket:
                    self._socket = socket
                    self.store.connected = True
                    attempt = 0
                    await self._send([("subscribe", topic) for topic in list(self._topics)])
                    async for raw in socket:
                        await self._handle(socket, raw)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - reconnect on any failure
                self.store.connected = False
                self.on_error(f"Live WebSocket closed: {type(error).__name__}: {error}")
            finally:
                self._socket = None
                self.store.connected = False
            if self._stop.is_set():
                return
            self.store.reconnects += 1
            wait = min(1000 * 2 ** max(0, attempt - 1), 10_000) / 1000 if attempt else 0
            attempt += 1
            await asyncio.sleep(wait + 0.25)

    async def _send(self, actions: list[tuple[str, str]]) -> None:
        if self._socket is None:
            return
        for method, topic in actions:
            self._request_id += 1
            await self._socket.send(json.dumps(
                {"method": method, "requestId": self._request_id, "params": [topic]}
            ))

    async def _close(self) -> None:
        if self._socket is not None:
            await self._socket.close()

    def _handle_sync_put(self, payload: dict, decimal_precision: int | None = None) -> None:
        """Store one order-book payload without a live socket (tests, REST warm-up)."""
        payload = dict(payload)
        market_id = str(payload.get("marketId", ""))
        payload.setdefault("marketId", market_id)
        quote = BookQuote.from_orderbook(payload, decimal_precision)
        quote.market_id = market_id
        self.store.put_book(quote, payload)
        self._last_message_at = time.time()
        self._notify_update()

    def _handle_text(self, raw: str) -> None:
        """Parse one raw frame with no socket attached (heartbeat stays unsent)."""
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict):
            return
        self._last_message_at = time.time()
        if message.get("topic") == "heartbeat":
            return
        self._route(message)

    def _route(self, message: dict) -> None:
        self._dispatch(None, message)

    async def _handle(self, socket, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(message, dict):
            return
        self._last_message_at = time.time()
        if message.get("topic") == "heartbeat":
            # Echo the server's value before the next heartbeat (15s) or it closes.
            await socket.send(json.dumps({"method": "heartbeat", "data": message.get("data")}))
            return
        self._dispatch(None, message)

    def _dispatch(self, socket, message: dict) -> None:
        topic = message.get("topic") or ""
        data = message.get("data")
        if topic == "system":
            if isinstance(data, dict) and data.get("event") == "reconnect":
                self.on_error("Server asked to reconnect; dropping the connection")
                if self._loop is not None:
                    asyncio.run_coroutine_threadsafe(self._close(), self._loop)
            return
        if message.get("type") == "R" and not message.get("success"):
            error = message.get("error") or {}
            self.on_error(f"WebSocket request failed: {error.get('code')}: {error.get('message')}")
            return
        if message.get("type") != "M":
            return
        data = data if isinstance(data, dict) else {}
        if topic.startswith("predictOrderbook/"):
            market_id = topic.split("/", 1)[1]
            payload = dict(data)
            payload.setdefault("marketId", market_id)
            quote = BookQuote.from_orderbook(payload, self._precision_for(market_id))
            quote.market_id = market_id
            self.store.put_book(quote, payload)
        elif topic.startswith("predictTopOfBook/"):
            market_id = topic.split("/", 1)[1]
            payload = dict(data)
            payload.setdefault("marketId", market_id)
            quote = BookQuote.from_top_of_book(payload, self._precision_for(market_id))
            quote.market_id = market_id
            self.store.quotes[market_id] = quote
            self.store.messages += 1
        elif topic.startswith("predictTradingStatus/"):
            market_id = topic.split("/", 1)[1]
            status = data.get("tradingStatus") or data.get("status") or "OPEN"
            self.store.trading_status[market_id] = status
        elif topic.startswith("predictMarketStatus/"):
            market_id = topic.split("/", 1)[1]
            status = data.get("status") or "OPEN"
            self.store.market_status[market_id] = status
        elif topic.startswith("predictMarketChanged/"):
            # The round's details changed (roll, resolution, params): the scan
            # rediscovers instead of waiting for the TTL to expire.
            self.store.market_changed.add(topic.split("/", 1)[1])
        self._notify_update()


class PredictMarketFeed:
    """Read active Predict.fun crypto up/down rounds and their Yes order books."""

    def __init__(
        self,
        assets: tuple[str, ...],
        windows: tuple[int, ...],
        *,
        api_key: Optional[str],
        base_url: str = "https://api.predict.fun",
        timeout: float = 10.0,
        discovery_ttl: float = 10.0,
        live: bool = True,
        quote_max_age: float = 15.0,
    ):
        self.assets = assets
        self.windows = windows
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.discovery_ttl = discovery_ttl
        self.quote_max_age = quote_max_age
        self._discovered_at: Optional[float] = None
        self._markets: list[dict] = []
        self._precision: dict[str, int] = {}
        self.diagnostics: list[str] = []
        self._discovery_diagnostics: list[str] = []
        self.live: Optional[PredictWebSocketFeed] = None
        if live:
            self.live = PredictWebSocketFeed(
                api_key=api_key,
                on_error=lambda message: self.diagnostics.append(f"[WS] {message}"),
            )
            if not self.live.start():
                self.live = None

    def _get(self, path: str, params: dict[str, str]) -> dict:
        url = f"{self.base_url}/v1/{path}?{urlencode(params)}"
        headers = {"Accept": "application/json", "User-Agent": "predictfun-paper-bot/0.1"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            if error.code == 404 and path.startswith("series/"):
                return {"success": True, "data": []}
            raise RuntimeError(f"Predict API returned HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"Could not reach Predict API: {error}") from error
        if not payload.get("success"):
            raise RuntimeError(f"Predict API request failed: {payload}")
        return payload

    def get_snapshots(self) -> list[MarketSnapshot]:
        now = time.monotonic()
        # predictMarketChanged reports a roll or a resolution; rediscover at once
        # instead of waiting for the TTL. Throttled so a burst of notices cannot
        # turn into a burst of REST calls.
        if self.live is not None and self.live.take_market_changed():
            if self._discovered_at is None or now - self._discovered_at >= 2.0:
                self._discovered_at = None
        if self._discovered_at is None or now - self._discovered_at >= self.discovery_ttl:
            self._discover_markets()
            self._discovered_at = now
        self.diagnostics = list(self._discovery_diagnostics)
        if not self._markets:
            return []

        self._precision = {
            str(market["id"]): int(market.get("decimal_precision", 3))
            for market in self._markets
        }
        market_ids = [str(market["id"]) for market in self._markets]
        live_quotes = self._live_quotes(market_ids)
        rest_quotes = {}
        missing_live = [market_id for market_id in market_ids if market_id not in live_quotes]
        if missing_live:
            rest_quotes = self._rest_quotes(missing_live)
        snapshots = []
        for market in self._markets:
            market_id = str(market["id"])
            live = live_quotes.get(market_id)
            if live is not None:
                up_ask, down_ask = live.up_ask, live.down_ask
                timestamp_ms = live.timestamp_ms
                if up_ask is None and down_ask is None:
                    self.diagnostics.append(
                        f"market={market_id} live book has no two-sided quote"
                    )
                    continue
            else:
                quote = rest_quotes.get(market_id)
                if not quote:
                    self.diagnostics.append(f"market={market_id} missing quote response")
                    continue
                up_ask = quote_price(quote.get("bestAsk"))
                yes_bid = quote_price(quote.get("bestBid"))
                down_ask = round(1.0 - yes_bid, 6) if yes_bid is not None else None
                timestamp_ms = quote.get("updateTimestampMs")
                if up_ask is None and down_ask is None:
                    self.diagnostics.append(
                        f"market={market_id} neither side has a valid quote"
                    )
                    continue
            if not valid_price(down_ask):
                down_ask = None
            if up_ask is None or down_ask is None:
                missing = "UP bestAsk" if up_ask is None else "DOWN quote (Yes bestBid)"
                self.diagnostics.append(
                    f"market={market_id} missing {missing}; evaluating the quoted side only"
                )
            observed_at = (
                datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat(timespec="seconds")
                if timestamp_ms
                else datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
            ends_at = market.get("ends_at")
            seconds_left = None
            if ends_at:
                end_time = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                seconds_left = max(0.0, (end_time - datetime.now(timezone.utc)).total_seconds())
                if seconds_left <= 0:
                    self.diagnostics.append(f"market={market['id']} round has ended")
                    continue
            snapshots.append(
                MarketSnapshot(
                    market_id=str(market["id"]),
                    asset=market["asset"],
                    window_minutes=market["window_minutes"],
                    up_ask=up_ask,
                    down_ask=down_ask,
                    observed_at=observed_at,
                    price_to_beat=market.get("price_to_beat"),
                    seconds_left=seconds_left,
                    fee_rate_bps=float(market.get("fee_rate_bps", 0.0)),
                    price_feed_provider=market.get("price_feed_provider"),
                    price_feed_symbol=market.get("price_feed_symbol"),
                    market_title=market.get("title"),
                )
            )
        return snapshots

    def _live_quotes(self, market_ids: list[str]) -> dict[str, "BookQuote"]:
        """Return fresh streamed quotes and (re)subscribe the socket to the set."""
        if self.live is None:
            return {}
        try:
            self.live.sync_markets(market_ids, precision=self._precision)
        except Exception as error:  # noqa: BLE001 - a dead socket must not stop the scan
            self.diagnostics.append(f"[WS] subscription failed: {error}")
        fresh = {}
        for market_id in market_ids:
            quote = self.live.quote(market_id)
            if quote is None:
                continue
            if self.live.stale(self.quote_max_age) or (
                quote.received_at and time.time() - quote.received_at > self.quote_max_age
            ):
                continue
            fresh[market_id] = quote
        if fresh:
            self.diagnostics.append(
                f"[WS] streamed {len(fresh)}/{len(market_ids)} tracked markets"
            )
        return fresh

    def _rest_quotes(self, market_ids: list[str]) -> dict[str, dict]:
        """REST fallback for markets without a fresh streamed quote."""
        quotes = self._get("markets/quotes", {"ids": ",".join(market_ids)}).get("data", [])
        return {str(quote["marketId"]): quote for quote in quotes}

    def close(self) -> None:
        """Stop the WebSocket thread when the scan loop ends."""
        if self.live is not None:
            self.live.stop()

    # ------------------------------------------------------------- live loop
    def warm_up(self, timeout: float = 3.0) -> bool:
        """Discover the tracked rounds and give the socket a bounded head start.

        Returns True when every discovered market already has a streamed book,
        so the first scan can run on WebSocket data instead of REST.
        """
        if self.live is None:
            return False
        self._discover_markets()
        self._discovered_at = time.monotonic()
        market_ids = [str(market["id"]) for market in self._markets]
        if not market_ids:
            return False
        self._precision = {
            str(market["id"]): int(market.get("decimal_precision", 3))
            for market in self._markets
        }
        self.live.sync_markets(market_ids, precision=self._precision)
        return self.live.wait_ready(market_ids, timeout)

    def wait_for_update(self, timeout: float) -> bool:
        """Block until the socket pushes a message; False when only time passed."""
        if self.live is None:
            return False
        return self.live.wait_for_update(timeout)

    def ws_status(self) -> str:
        """Compact WebSocket summary for the console banner."""
        return self.live.status() if self.live is not None else "ws=off"

    def get_resolution(self, market_id: str) -> Optional[dict[str, object]]:
        """Return the resolved binary outcome when Predict has finalized it."""
        payload = self._get(f"markets/{market_id}", {})
        market = payload.get("data", {}) or {}
        if market.get("status") != "RESOLVED":
            return None
        outcomes = market.get("outcomes", [])
        resolution = market.get("resolution") or {}
        resolved_index = resolution.get("indexSet")
        if resolved_index == 1:
            return {"winner": "UP", "status": market.get("status")}
        if resolved_index == 2:
            return {"winner": "DOWN", "status": market.get("status")}
        resolved = {
            int(outcome.get("indexSet", -1)): outcome.get("status")
            for outcome in outcomes
        }
        if resolved.get(1) == "WON" and resolved.get(2) == "WON":
            winner = "TIE"
        elif resolved.get(1) == "WON":
            winner = "UP"
        elif resolved.get(2) == "WON":
            winner = "DOWN"
        elif resolved.get(1) == "LOST" and resolved.get(2) == "LOST":
            winner = "TIE"
        else:
            return None
        return {"winner": winner, "status": market.get("status")}

    def get_positions_by_address(self, address: str) -> list[dict]:
        """Read any wallet's Predict positions; this endpoint only needs the API key."""
        positions = []
        cursor = None
        while True:
            params = {"first": "100", "isResolved": "false"}
            if cursor:
                params["after"] = cursor
            payload = self._get(f"positions/{address}", params)
            positions.extend(payload.get("data", []))
            cursor = payload.get("cursor")
            if not cursor:
                return positions

    def _discover_markets(self) -> None:
        markets = []
        self._discovery_diagnostics = []
        now = datetime.now(timezone.utc)
        for asset in self.assets:
            for window in self.windows:
                slug = f"{asset.lower()}-updown-{window}m"
                rounds = self._get(f"series/{slug}/rounds", {"status": "ACTIVE", "first": "5"})
                reasons = []
                selected = None
                for category in rounds.get("data", []):
                    ends_at = category.get("endsAt")
                    starts_at = category.get("startsAt")
                    if not ends_at:
                        reasons.append("round missing end time")
                        continue
                    end_time = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                    if end_time <= now:
                        reasons.append("round has ended")
                        continue
                    if starts_at and datetime.fromisoformat(
                        starts_at.replace("Z", "+00:00")
                    ) > now:
                        reasons.append("round has not started")
                        continue
                    for market in category.get("markets", []):
                        if market.get("tradingStatus") != "OPEN":
                            continue
                        variant = market.get("variantData") or {}
                        if (market.get("marketVariant") != "CRYPTO_UP_DOWN"
                                and variant.get("type") != "CRYPTO_UP_DOWN"):
                            continue
                        if not any(outcome.get("indexSet") == 1
                                   for outcome in market.get("outcomes", [])):
                            continue
                        details = market.get("variantDetails") or {}
                        details = details.get("crypto") or details.get("upDown") or variant
                        if details.get("priceFeedProvider") != "CHAINLINK":
                            reasons.append(f"market={market['id']} unsupported price provider")
                            continue
                        if details.get("startPrice") is None:
                            reasons.append(f"market={market['id']} missing start price")
                            continue
                        rewards = market.get("rewards") or {}
                        current_reward = rewards.get("current") or {}
                        selected = {
                            "id": market["id"], "asset": asset, "window_minutes": window,
                            "title": category.get("title") or market.get("title"),
                            "ends_at": ends_at, "price_to_beat": float(details["startPrice"]),
                            "fee_rate_bps": float(market.get("feeRateBps", 0.0)),
                            "price_feed_provider": details.get("priceFeedProvider"),
                            "price_feed_symbol": details.get("priceFeedSymbol"),
                            "decimal_precision": int(market.get("decimalPrecision", 3)),
                            "trading_status": market.get("tradingStatus", "OPEN"),
                            "status": market.get("status", "OPEN"),
                            # A market can be REGISTERED and still accept and match
                            # orders; only pause/close states should block quoting,
                            # so normalize the boolean here.
                            "book_active": market.get("tradingStatus") == "OPEN"
                            and (market.get("status") or "OPEN").upper() != "PAUSED"
                            and (market.get("status") or "OPEN").upper() != "CLOSED",
                            "seconds_left": max(
                                0.0, (end_time - now).total_seconds()
                            ),
                            "reward_rate": current_reward.get("hourlyRate"),
                            "spread_threshold": market.get("spreadThreshold"),
                            "share_threshold": market.get("shareThreshold"),
                            "series_slug": slug,
                        }
                        break
                    if selected:
                        break
                if selected:
                    markets.append(selected)
                else:
                    reason = "; ".join(dict.fromkeys(reasons)) or "no eligible open rounds"
                    self._discovery_diagnostics.append(f"{asset} {window}m: {reason}")
        self._markets = markets
