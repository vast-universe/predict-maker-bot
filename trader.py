import json
import time
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from eth_account import Account
from eth_account.messages import encode_defunct

from paper_portfolio import PaperPortfolio
from strategy import Opportunity


SIDE_LABELS = {"UP": "Up", "DOWN": "Down", "PAIR": "Up+Down"}


class PaperTrader:
    """Print screenshot-style fills and mirror them into the paper ledger."""

    def __init__(self, portfolio: PaperPortfolio):
        self.portfolio = portfolio

    def execute(self, opportunity: Opportunity, *, maker: bool = False) -> bool:
        if opportunity.sell:
            return self._execute_sell(opportunity)
        if opportunity.side == "PAIR":
            return self._execute_pair(opportunity)
        cost = self.portfolio.record_trade(opportunity, maker=maker)
        if cost <= 0:
            return False
        label = SIDE_LABELS[opportunity.side]
        print(
            f"[PAPER] BUY {label} @ {opportunity.ask:.2f} "
            f"x {opportunity.quantity:.1f} (~${cost:.2f}) - "
            f"{opportunity.strategy} {label}@{opportunity.ask:.3f}",
            flush=True,
        )
        return True

    def _execute_sell(self, opportunity: Opportunity) -> bool:
        proceeds = self.portfolio.record_sell(opportunity)
        if proceeds <= 0:
            return False
        label = SIDE_LABELS[opportunity.side]
        print(
            f"[PAPER] SELL {label} @ {opportunity.ask:.2f} "
            f"x {opportunity.quantity:.1f} (~${proceeds:.2f}) - "
            f"rebal sell_{opportunity.side.lower()}@{opportunity.ask:.2f} "
            f"{opportunity.condition}".rstrip(),
            flush=True,
        )
        return True

    def _execute_pair(self, opportunity: Opportunity) -> bool:
        cost = self.portfolio.record_trade(opportunity)
        if cost <= 0:
            return False
        for label, ask, unit_cost in (
            ("Up", opportunity.up_ask, opportunity.up_cost_per_share),
            ("Down", opportunity.down_ask, opportunity.down_cost_per_share),
        ):
            if ask is None:
                continue
            leg_cost = opportunity.quantity * unit_cost
            print(
                f"[PAPER] BUY {label} @ {ask:.2f} "
                f"x {opportunity.quantity:.1f} (~${leg_cost:.2f}) - "
                f"arb {label} comb={opportunity.comb_ratio:.3f}@{ask:.3f}",
                flush=True,
            )
        return True


# ---------------------------------------------------------------------------
# Live trading
#
# Sends real signed orders when the three credentials are present:
#   PREDICT_API_KEY, PREDICT_ACCOUNT_ADDRESS, PRIVY_WALLET_PRIVATE_KEY
# Explicit --live needs all credentials. The flow follows the Predict docs:
# GET /v1/auth/message -> account signature -> POST /v1/auth -> JWT (24h),
# then SDK-built EIP-712 orders posted to POST /v1/orders.
# ---------------------------------------------------------------------------

WEI = 10 ** 18


class PredictAPIError(RuntimeError):
    """An HTTP error, keeping whether a write may have reached the exchange."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 ambiguous: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.ambiguous = ambiguous
        self.order_hash: str | None = None


class LiveTrader:
    """Signs and posts limit orders through the Predict REST API."""

    def __init__(self, *, api_key: str, account_address: str, private_key: str,
                 base_url: str = "https://api.predict.fun", timeout: float = 20.0):
        self.api_key = api_key
        self.account_address = account_address
        self.private_key = private_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None
        self.token_deadline = 0.0
        self.builder = None
        self.errors: list[str] = []

    # ---------------------------------------------------------------- auth
    def _request(self, method: str, path: str, payload: dict | None = None,
                 *, auth: bool = True) -> dict:
        headers = {"Accept": "application/json",
                   "User-Agent": "predict-maker-bot/0.1",
                   "x-api-key": self.api_key}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        if auth:
            headers["Authorization"] = f"Bearer {self._jwt()}"
        request = Request(f"{self.base_url}/v1/{path}", data=body,
                          headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise PredictAPIError(
                f"Predict {method} /v1/{path} -> HTTP {error.code}: {detail}",
                status_code=error.code, ambiguous=method != "GET" and error.code >= 500,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise PredictAPIError(
                f"Predict {method} /v1/{path} failed: {error}", ambiguous=method != "GET",
            ) from error
        except (ValueError, UnicodeError) as error:
            raise PredictAPIError(
                f"Predict {method} /v1/{path} returned an unreadable response",
                ambiguous=method != "GET",
            ) from error
        if not isinstance(result, dict) or result.get("success") is False:
            raise PredictAPIError(
                f"Predict {method} /v1/{path} failed: {result}",
                ambiguous=method != "GET" and not isinstance(result, dict),
            )
        return result

    def _jwt(self) -> str:
        if self.token and time.time() < self.token_deadline:
            return self.token
        message = self._request("GET", "auth/message", auth=False)["data"]["message"]
        if Account.from_key(self.private_key).address.lower() == self.account_address.lower():
            signature = Account.sign_message(
                encode_defunct(text=message), private_key=self.private_key
            ).signature.to_0x_hex()
        else:
            signature = self._order_builder().sign_predict_account_message(message)
        payload = self._request("POST", "auth", {
            "signer": self.account_address,
            "message": message,
            "signature": signature,
        }, auth=False)
        self.token = payload["data"]["token"]
        # A JWT lives 24h; refresh an hour early so an order never races expiry.
        self.token_deadline = time.time() + 23 * 3600
        return self.token

    def _order_builder(self):
        """Use the configured account identity; never silently switch wallets."""
        if self.builder is not None:
            return self.builder
        from predict_sdk import ChainId, OrderBuilder, OrderBuilderOptions
        try:
            signer_address = Account.from_key(self.private_key).address
            if signer_address.lower() != self.account_address.lower():
                self.builder = OrderBuilder.make(
                    ChainId.BNB_MAINNET, self.private_key,
                    OrderBuilderOptions(predict_account=self.account_address),
                )
            else:
                self.builder = OrderBuilder.make(ChainId.BNB_MAINNET, self.private_key)
        except Exception as error:  # noqa: BLE001 - SDK/RPC errors have varied types
            raise RuntimeError(
                f"Could not initialize the configured Predict signer: {type(error).__name__}"
            ) from error
        return self.builder

    # --------------------------------------------------------------- orders
    def place_limit(self, *, token_id: str, price: float, quantity: float,
                    fee_rate_bps: float, side: str = "BUY",
                    is_neg_risk: bool = False, is_yield_bearing: bool = False) -> dict:
        """Submit a limit order that may take liquidity at its signed price."""
        if (not isfinite(price) or not 0.0 < price < 1.0
                or not isfinite(quantity) or quantity <= 0
                or not isfinite(fee_rate_bps) or fee_rate_bps < 0):
            raise ValueError("Order price, quantity and fee must be finite and valid")
        if side.upper() not in {"BUY", "SELL"}:
            raise ValueError("Predict order side must be BUY or SELL")
        from predict_sdk import BuildOrderInput, LimitHelperInput, Side
        builder = self._order_builder()
        side_enum = Side.BUY if side.upper() == "BUY" else Side.SELL
        price_wei = int(Decimal(str(price)) * WEI)
        quantity_wei = int(Decimal(str(quantity)) * WEI)
        # Let the SDK derive the amounts: maker_amount is the collateral paid on a
        # BUY ((price * qty) / precision) and taker_amount is the share count, not
        # 1e18-scaled copies of both. It also enforces the 1e16 minimum quantity
        # and truncates to the digit counts the exchange accepts.
        amounts = builder.get_limit_order_amounts(LimitHelperInput(
            side=side_enum,
            price_per_share_wei=price_wei,
            quantity_wei=quantity_wei,
        ))
        order = builder.build_order("LIMIT", BuildOrderInput(
            side=side_enum,
            token_id=str(token_id),
            maker_amount=str(amounts.maker_amount),
            taker_amount=str(amounts.taker_amount),
            fee_rate_bps=str(int(fee_rate_bps)),
            signer=self.account_address,
        ))
        typed_data = builder.build_typed_data(
            order, is_neg_risk=is_neg_risk, is_yield_bearing=is_yield_bearing
        )
        signed = builder.sign_typed_data_order(typed_data)
        # sign_typed_data_order leaves `hash` unset; the order hash the API wants
        # comes from the typed-data hash.
        order_hash = builder.build_typed_data_hash(typed_data)
        payload = {"data": {
            "order": {
                "hash": order_hash,
                "salt": str(signed.salt),
                "maker": signed.maker,
                "signer": signed.signer,
                "taker": signed.taker,
                "tokenId": str(signed.token_id),
                "makerAmount": str(signed.maker_amount),
                "takerAmount": str(signed.taker_amount),
                "expiration": str(signed.expiration),
                "nonce": str(signed.nonce),
                "feeRateBps": str(signed.fee_rate_bps),
                "side": int(signed.side.value) if hasattr(signed.side, "value") else int(signed.side),
                "signatureType": int(signed.signature_type.value)
                if hasattr(signed.signature_type, "value") else int(signed.signature_type),
                "signature": signed.signature,
            },
            "pricePerShare": str(price_wei),
            "strategy": "LIMIT",
            "isPostOnly": False,
            "selfTradePrevention": "CANCEL_TAKER",
        }}
        try:
            response = self._request("POST", "orders", payload)
        except PredictAPIError as error:
            error.order_hash = order_hash
            raise
        result = response.get("data") or {}
        if not result.get("orderId") or str(result.get("orderHash", "")).lower() != order_hash.lower():
            error = PredictAPIError("Accepted order response lacks its ID or signed hash", ambiguous=True)
            error.order_hash = order_hash
            raise error
        return result

    def cancel(self, order_ids: list[str]) -> dict:
        if not 1 <= len(order_ids) <= 100:
            raise ValueError("Predict removes between 1 and 100 orders per request")
        return self._request("POST", "orders/remove", {
            "data": {"ids": [str(i) for i in order_ids]}
        })

    def open_orders(self) -> list[dict]:
        orders, seen, cursor = [], set(), None
        while True:
            params = {"status": "OPEN", "first": "100"}
            if cursor:
                params["after"] = cursor
            payload = self._request("GET", f"orders?{urlencode(params)}", auth=True)
            page = payload.get("data")
            if not isinstance(page, list):
                raise RuntimeError("Predict returned an invalid open-orders page")
            orders.extend(page)
            cursor = payload.get("cursor")
            if not cursor:
                return orders
            if cursor in seen:
                raise RuntimeError("Predict returned a repeated orders cursor")
            seen.add(cursor)

    def get_order(self, order_hash: str) -> dict:
        return self._request("GET", f"orders/{quote(order_hash.lower(), safe='')}").get("data") or {}

    def order_matches(self, order_hashes: list[str]) -> list[dict]:
        """Read every settlement for owned hashes, without signing or trading."""
        hashes = list(dict.fromkeys(str(value).lower() for value in order_hashes))
        events, identities = [], {}
        for start in range(0, len(hashes), 25):
            cursor, seen = None, set()
            while True:
                params = {"orderHashes": ",".join(hashes[start:start + 25]), "first": "150"}
                if cursor:
                    params["after"] = cursor
                payload = self._request("GET", f"orders/matches?{urlencode(params)}", auth=False)
                page = payload.get("data")
                if not isinstance(page, list):
                    raise RuntimeError("Predict returned an invalid order-matches page")
                for event in page:
                    if not isinstance(event, dict) or not event.get("settlementId"):
                        raise RuntimeError("Predict match lacks a stable settlement identity")
                    identity = str(event["settlementId"])
                    encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
                    if identity in identities and identities[identity] != encoded:
                        raise RuntimeError("Predict returned conflicting settlement records")
                    if identity not in identities:
                        identities[identity] = encoded
                        events.append(event)
                cursor = payload.get("cursor")
                if not cursor:
                    break
                if cursor in seen:
                    raise RuntimeError("Predict returned a repeated matches cursor")
                seen.add(cursor)
        return events

    def collateral_balance(self) -> float:
        """Read on-chain USDT only; this never submits approvals or transactions."""
        try:
            return float(Decimal(self._order_builder().balance_of(address=self.account_address)) / WEI)
        except Exception as error:  # noqa: BLE001 - SDK/RPC errors have varied types
            raise RuntimeError(f"Could not read USDT balance: {type(error).__name__}") from error

    def positions(self) -> list[dict]:
        positions, seen, cursor = [], set(), None
        while True:
            params = {"first": "100", "isResolved": "false"}
            if cursor:
                params["after"] = cursor
            payload = self._request(
                "GET", f"positions/{quote(self.account_address, safe='')}?{urlencode(params)}",
                auth=False,
            )
            page = payload.get("data")
            if not isinstance(page, list):
                raise RuntimeError("Predict returned an invalid positions page")
            positions.extend(page)
            cursor = payload.get("cursor")
            if not cursor:
                return positions
            if cursor in seen:
                raise RuntimeError("Predict returned a repeated positions cursor")
            seen.add(cursor)


class LiveBook:
    """Reconcile only this process's orders, preserving unchanged queue entries.

    An OPEN partial fill retains its remaining shares. A fully closed order can
    be replenished, but a failed read or unconfirmed removal blocks new orders in
    that market. Unknown submissions retain their signed hash for reconciliation
    rather than being retried as a second order.
    """

    def __init__(self, live: LiveTrader, log=None):
        self.live = live
        self.log = log or (lambda message: None)
        self.orders: dict[str, list[dict]] = {}  # market_id -> placed orders
        # Keep terminal fills for this process. Positions are authoritative;
        # these counters verify that its indexer has caught up, never add stock.
        self._history: dict[str, dict] = {}
        self._tokens: dict[str, dict[str, str]] = {}
        self._positions: dict[str, dict] = {}
        self._baseline: dict[tuple[str, str], int] | None = None
        self._baseline_costs: dict[str, float] | None = None
        self._resolved: set[str] = set()
        self._pending_observations: dict[str, tuple[bool, int, int]] = {}
        self._position_cost = 0.0
        self._generation = 0
        self._snapshot_generation = -1
        self._snapshot_valid = False

    def _invalidate(self) -> None:
        self._generation += 1
        self._snapshot_valid = False

    @staticmethod
    def _wei(value, name: str) -> int:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number != number.to_integral_value():
            raise RuntimeError(f"Invalid {name}")
        return int(number)

    @staticmethod
    def _token_id(value) -> str:
        encoded = str(value)
        if (value is None or isinstance(value, bool) or not encoded.isascii()
                or not encoded.isdigit() or int(encoded) <= 0):
            raise RuntimeError("Invalid outcome token ID")
        return str(int(encoded))

    def _remember(self, market_id: str, order: dict) -> None:
        order.setdefault("marketId", str(market_id))
        order.setdefault("grossFilledWei", 0)
        order.setdefault("netFilledWei", 0)
        order.setdefault("cashNumerator", 0)
        order.setdefault("positionFilledWei", 0)
        order.setdefault("observedFilledWei", 0)
        order.setdefault("stateKnown", not order.get("uncertain", False))
        order.setdefault("matchKnown", True)
        self._history[str(order["orderHash"]).lower()] = order

    def ready(self, market_id: str | None = None) -> bool:
        return (self._snapshot_valid and self._snapshot_generation == self._generation
                and (market_id is None or (str(market_id) in self._tokens
                                          and str(market_id) not in self._resolved))
                and all(order.get("stateKnown") and order.get("matchKnown")
                        and (not order.get("terminalHoldWei") or order.get("pendingClearConfirmed"))
                        for orders in self.orders.values() for order in orders))

    def observe_pending(self, market_id: str, *, clear: bool, timestamp_ms: int) -> None:
        """Accept only an explicit full WS book's pending-settlement observation."""
        if not isinstance(clear, bool) or isinstance(timestamp_ms, bool):
            raise ValueError("Pending observation requires a boolean and an epoch timestamp")
        timestamp = self._wei(timestamp_ms, "pending timestamp")
        if timestamp <= 0:
            raise ValueError("Pending observation requires a positive epoch timestamp")
        key = str(market_id)
        previous = self._pending_observations.get(key)
        if previous is None or timestamp > previous[1]:
            # Keep the first local observation for this distinct frame. A
            # cached frame from a slightly fast server clock is not new proof.
            self._pending_observations[key] = clear, timestamp, int(time.time() * 1000)

    def inventory_cost(self, market_id: str | None = None) -> float:
        return (self._position_cost if market_id is None
                else float(self._positions.get(str(market_id), {}).get("cost", 0.0)))

    def total_position_cost(self) -> float:
        return self._position_cost

    def position_state(self, market_id: str) -> dict:
        state = {"up_quantity": 0.0, "down_quantity": 0.0,
                 "up_cost": 0.0, "down_cost": 0.0, "cost": 0.0, "net_quantity": 0.0}
        state.update(self._positions.get(str(market_id), {}))
        for side in ("UP", "DOWN"):
            pending = sum((order.get("terminalHoldWei", 0)
                           + max(0, order.get("grossFilledWei", 0) - order.get("positionFilledWei", 0))) / WEI
                          for order in self.orders.get(str(market_id), []) if order["side"] == side)
            prefix = side.lower()
            state["pending_" + prefix + "_quantity"] = pending
            state["worst_" + prefix + "_quantity"] = state[prefix + "_quantity"] + pending
        state["ready"] = self.ready(str(market_id))
        return state

    def filled_totals(self, market_id: str | None = None) -> dict:
        totals = {"up_quantity": 0.0, "down_quantity": 0.0,
                  "up_cost": 0.0, "down_cost": 0.0, "cost": 0.0, "gross_quantity": 0.0}
        for order in self._history.values():
            if market_id is not None and str(order["marketId"]) != str(market_id):
                continue
            prefix = str(order["side"]).lower()
            quantity = order["netFilledWei"] / WEI
            cost = order["cashNumerator"] / (WEI * WEI)
            totals[prefix + "_quantity"] += quantity
            totals[prefix + "_cost"] += cost
            totals["cost"] += cost
            totals["gross_quantity"] += order["grossFilledWei"] / WEI
        return totals

    def resolve_market(self, market_id: str) -> bool:
        """Retire known terminal liabilities after caller-confirmed resolution."""
        market_id = str(market_id)
        orders = self.orders.get(market_id, [])
        if any(not order.get("terminal") or not order.get("stateKnown")
               or not order.get("matchKnown") for order in orders):
            return False
        self._resolved.add(market_id)
        self.orders.pop(market_id, None)
        self._invalidate()
        return True

    def reconcile_positions(self, rows: list[dict], *, token_ids_by_market: dict | None = None) -> bool:
        """Accept one complete unresolved-position snapshot or fail closed.

        The startup snapshot is the baseline, including zero holdings for absent
        tokens. Subsequent snapshots must include all locally settled net buys.
        Extra shares (for example maker rebates) are accepted as real inventory.
        """
        try:
            if not isinstance(rows, list):
                raise RuntimeError("Positions snapshot is not a complete list")
            tokens = {key: dict(value) for key, value in self._tokens.items()}
            for key, mapping in (token_ids_by_market or {}).items():
                key = str(key)
                candidate = {side: self._token_id(mapping[side]) for side in ("UP", "DOWN")}
                if (not all(candidate.values()) or candidate["UP"] == candidate["DOWN"]
                        or (key in tokens and tokens[key] != candidate)):
                    raise RuntimeError("Invalid or changed market token mapping")
                tokens[key] = candidate
            quantities, states, total_cost = {}, {}, 0.0
            for row in rows:
                if not isinstance(row, dict):
                    raise RuntimeError("Invalid position row")
                market_id = str(row["market"]["id"])
                token_id = self._token_id(row["outcome"]["onChainId"])
                amount = self._wei(row["amount"], "position amount")
                average = Decimal(str(row["averageBuyPriceUsd"]))
                if not average.is_finite() or not 0 <= average <= 1:
                    raise RuntimeError("Invalid position cost basis")
                quantity = amount / WEI
                cost = quantity * float(average)
                if not isfinite(quantity) or not isfinite(cost):
                    raise RuntimeError("Non-finite position quantity or cost")
                if row.get("valueUsd") is not None and row.get("pnlUsd") is not None:
                    value = Decimal(str(row["valueUsd"]))
                    pnl = Decimal(str(row["pnlUsd"]))
                    basis = value - pnl
                    if (not value.is_finite() or not pnl.is_finite() or basis < 0
                            or not isfinite(float(basis))
                            or abs(float(basis) - cost) > max(0.01, cost * 0.001)):
                        raise RuntimeError("Position value/PnL disagrees with its buy cost basis")
                total_cost += cost
                if not isfinite(total_cost):
                    raise RuntimeError("Non-finite total position cost")
                pair = market_id, token_id
                if pair in quantities:
                    raise RuntimeError("Duplicate position token in complete snapshot")
                quantities[pair] = amount
                state = states.setdefault(market_id, {"up_quantity": 0.0, "down_quantity": 0.0,
                                                       "up_cost": 0.0, "down_cost": 0.0,
                                                       "cost": 0.0, "net_quantity": 0.0})
                state["cost"] += cost
                mapping = tokens.get(market_id)
                if mapping:
                    side = next((side for side, token in mapping.items() if token == token_id), None)
                    if side is None:
                        raise RuntimeError("Position outcome differs from discovered market tokens")
                    prefix = side.lower()
                    state[prefix + "_quantity"] += quantity
                    state[prefix + "_cost"] += cost
            baseline = dict(quantities) if self._baseline is None else dict(self._baseline)
            for market_id, mapping in tokens.items():
                for token_id in mapping.values():
                    baseline.setdefault((market_id, token_id), 0)
            baseline_costs = ({key: value["cost"] for key, value in states.items()}
                              if self._baseline_costs is None else dict(self._baseline_costs))
            fills, cash_fills = {}, {}
            for order in self._history.values():
                pair = str(order["marketId"]), str(order["tokenId"])
                fills[pair] = fills.get(pair, 0) + order["netFilledWei"]
                cash_fills[pair[0]] = cash_fills.get(pair[0], 0) + order["cashNumerator"]
            for pair, amount in fills.items():
                if pair[0] not in self._resolved and quantities.get(pair, 0) + 1000 < baseline.get(pair, 0) + amount:
                    raise RuntimeError("Positions indexer has not caught up with settled BUY fills")
            for market_id, numerator in cash_fills.items():
                if market_id in self._resolved:
                    continue
                minimum_cost = baseline_costs.get(market_id, 0.0) + numerator / (WEI * WEI)
                actual_cost = states.get(market_id, {}).get("cost", 0.0)
                if (not isfinite(minimum_cost)
                        or actual_cost + max(0.01, minimum_cost * 0.001) < minimum_cost):
                    raise RuntimeError("Position basis has not caught up with exact settled BUY cash")
            # Observed extra shares (including rebates) become part of the next
            # lower bound rather than masking the following fill's indexer lag.
            for pair, amount in quantities.items():
                baseline[pair] = max(baseline.get(pair, 0), amount - fills.get(pair, 0))
            for market_id, state in states.items():
                baseline_costs[market_id] = max(
                    baseline_costs.get(market_id, 0.0),
                    state["cost"] - cash_fills.get(market_id, 0) / (WEI * WEI))
            for state in states.values():
                state["net_quantity"] = state["up_quantity"] - state["down_quantity"]
                if not all(isfinite(value) for value in state.values()):
                    raise RuntimeError("Non-finite aggregated position inventory")
            if any(not order.get("stateKnown") or not order.get("matchKnown")
                   for orders in self.orders.values() for order in orders):
                raise RuntimeError("Owned order or fill state is still unknown")
            if any(order.get("terminalHoldWei") and not order.get("pendingClearConfirmed")
                   for orders in self.orders.values() for order in orders):
                raise RuntimeError("Terminal order still has unproven pending-settlement exposure")
            self._tokens, self._baseline = tokens, baseline
            self._baseline_costs = baseline_costs
            self._positions, self._position_cost = states, total_cost
            self._snapshot_generation, self._snapshot_valid = self._generation, True
            for order in self._history.values():
                order["positionFilledWei"] = order["grossFilledWei"]
            self._release_terminals()
            return True
        except (RuntimeError, KeyError, ValueError, TypeError, InvalidOperation, OverflowError) as error:
            self._snapshot_valid = False
            self.log(f"[LIVE] positions not reconciled; holding new orders: {error}")
            return False

    def _release_terminals(self) -> None:
        if not self.ready():
            return
        for key in list(self.orders):
            retained = [order for order in self.orders[key] if not order.get("terminal")]
            if retained:
                self.orders[key] = retained
            else:
                self.orders.pop(key, None)

    @staticmethod
    def _key(order: dict) -> tuple:
        return order["side"], Decimal(str(order["price"]))

    def reserved_cost(self, market_id: str | None = None) -> float:
        """Reserve unmatched/pending shares and fills absent from the snapshot."""
        return sum(
            (float(order.get("remaining", order["shares"]))
             + order.get("terminalHoldWei", 0) / WEI
             + max(0, order.get("grossFilledWei", 0) - order.get("positionFilledWei", 0)) / WEI)
            * float(order["price"])
            for key, orders in self.orders.items()
            if market_id is None or str(key) == str(market_id)
            for order in orders
        )

    def refresh(self, market_id: str | None = None) -> bool:
        """Read tracked order statuses before refreshing the on-chain balance."""
        known = True
        markets = [str(market_id)] if market_id is not None else list(self.orders)
        for key in markets:
            if not self._refresh(key):
                known = False
        return known

    def _refresh(self, market_id: str) -> bool:
        known, pending = True, []
        for order in self.orders.get(market_id, []):
            self._remember(market_id, order)
            try:
                data = self.live.get_order(order["orderHash"])
                status = str(data.get("status", "")).upper()
                if status not in {"OPEN", "FILLED", "CANCELLED", "EXPIRED", "INVALIDATED"}:
                    raise RuntimeError(f"Unknown order status {status!r}")
                raw = data.get("order") or {}
                if (str(raw.get("hash", "")).lower() != order["orderHash"].lower()
                        or str(raw.get("tokenId")) != str(order["tokenId"])
                        or int(raw.get("side", -1)) != 0
                        or str(data.get("marketId")) != str(market_id)):
                    raise RuntimeError("Order identity differs from the submitted bid")
                amount = self._wei(data["amount"], "order amount")
                filled = self._wei(data["amountFilled"], "settled amount")
                if amount <= 0 or filled > amount or filled < order["observedFilledWei"]:
                    raise RuntimeError("Invalid order amount or settled amount")
                if status == "FILLED" and filled != amount:
                    raise RuntimeError("FILLED order has unsettled shares")
                remaining = (amount - filled) / WEI if status == "OPEN" else 0.0
                # OPEN with no remaining shares can be an in-flight transition;
                # retain the full previous reservation until status is final.
                if status == "OPEN" and remaining <= 0:
                    raise RuntimeError("OPEN order has no confirmed remaining shares")
                if (status != "OPEN" and not order.get("terminalObserved")):
                    self._invalidate()
                    order["terminalObserved"] = True
                    order["terminalObservedAt"] = int(time.time() * 1000)
                if abs(remaining - float(order.get("remaining", order["shares"]))) > 1e-12:
                    self._invalidate()
                if filled != order["observedFilledWei"]:
                    self._invalidate()
                    order["observedFilledWei"] = filled
                order["shares"] = amount / WEI
                order["amountWei"] = amount
                order["orderId"] = str(data["id"])
                order["uncertain"] = False
                order["stateKnown"] = True
                if data.get("removalLockedUntil") is not None:
                    order["removalLockedUntil"] = data["removalLockedUntil"]
                # Terminal responses must also be checked for settlements; a
                # final fill is often first visible on this very response.
                if filled != order["grossFilledWei"] or status != "OPEN":
                    order["matchKnown"] = False
                    pending.append((order, status, remaining, filled))
                else:
                    order["remaining"] = remaining
            except (RuntimeError, KeyError, ValueError, TypeError, InvalidOperation, OverflowError) as error:
                # A just-accepted order can return 404 briefly. Keep it, and
                # retry its existing hash on the next scan without replacing it.
                known = False
                order["stateKnown"] = False
                self._invalidate()
                self.log(f"[LIVE] order status unknown market={market_id}: {error}")
        if pending:
            try:
                events = self.live.order_matches([order["orderHash"] for order, *_ in pending])
                for order, status, remaining, filled in pending:
                    gross, net, cash = self._match_totals(order, events)
                    if gross != filled:
                        raise RuntimeError("Match history disagrees with settled order quantity")
                    if (gross < order["grossFilledWei"] or net < order["netFilledWei"]
                            or cash < order["cashNumerator"]):
                        raise RuntimeError("Cumulative BUY fill history moved backwards")
                    if (gross, net, cash) != (order["grossFilledWei"], order["netFilledWei"], order["cashNumerator"]):
                        self._invalidate()
                    hold = order["amountWei"] - filled if status != "OPEN" else 0
                    observation = self._pending_observations.get(market_id)
                    terminal_after = max(order.get("cancelAckAt", 0), order.get("terminalObservedAt", 0))
                    cleared = bool(not hold or (observation and observation[0]
                                   and observation[1] > terminal_after
                                   and observation[2] > terminal_after))
                    order.update(grossFilledWei=gross, netFilledWei=net,
                                 cashNumerator=cash, remaining=remaining,
                                 terminalHoldWei=hold, pendingClearConfirmed=cleared,
                                 terminal=status != "OPEN", matchKnown=True)
            except (RuntimeError, KeyError, ValueError, TypeError, InvalidOperation, OverflowError) as error:
                known = False
                self._invalidate()
                self.log(f"[LIVE] fill accounting unknown market={market_id}: {error}")
        self._release_terminals()
        return known

    def _match_totals(self, order: dict, events: list[dict]) -> tuple[int, int, int]:
        gross = net = cash = 0
        seen = set()
        if not isinstance(events, list):
            raise RuntimeError("Invalid match history")
        for event in events:
            if not isinstance(event, dict):
                raise RuntimeError("Invalid settlement record")
            identity = str(event.get("settlementId", ""))
            if not identity:
                raise RuntimeError("Match lacks a stable settlement identity")
            if identity in seen:
                continue
            seen.add(identity)
            taker, makers = event.get("taker") or {}, event.get("makers") or []
            if (not isinstance(taker, dict) or not isinstance(makers, list)
                    or any(not isinstance(entity, dict) for entity in makers)):
                raise RuntimeError("Invalid settlement participants")
            entities = [taker, *makers]
            for entity in entities:
                if str(entity.get("hash", "")).lower() != order["orderHash"].lower():
                    continue
                if (str((event.get("market") or {}).get("id")) != str(order["marketId"])
                        or str(entity.get("quoteType", "")).upper() != "BID"
                        or (entity.get("signer") and str(entity["signer"]).lower()
                            != self.live.account_address.lower())):
                    raise RuntimeError("Match identity differs from the owned BUY order")
                amount = self._wei(entity["amount"], "match amount")
                price = self._wei(entity["price"], "match price")
                if amount <= 0 or not 0 < price < WEI:
                    raise RuntimeError("Invalid match price or quantity")
                fee = entity.get("fee")
                if not isinstance(fee, dict):
                    raise RuntimeError("Match lacks exact fee accounting")
                fee_amount = self._wei(fee["amount"], "match fee")
                fee_type = str(fee.get("type", "")).upper()
                if fee_amount and fee_type not in {"SHARES", "COLLATERAL"}:
                    raise RuntimeError("Unknown match fee unit")
                shares_fee = fee_amount if fee_type == "SHARES" else 0
                if shares_fee > amount:
                    raise RuntimeError("Match fee exceeds bought shares")
                gross += amount
                net += amount - shares_fee
                cash += amount * price + (fee_amount * WEI if fee_type == "COLLATERAL" else 0)
        return gross, net, cash

    @staticmethod
    def _locked(order: dict) -> bool:
        value = order.get("removalLockedUntil")
        if not value:
            return False
        try:
            # API timestamps are ISO-8601; tolerate numeric epoch values too.
            from datetime import datetime
            if isinstance(value, str) and ("T" in value or "-" in value):
                unlocks = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            else:
                unlocks = float(value)
                if unlocks > 10 ** 11:
                    unlocks /= 1000.0
            return not isfinite(unlocks) or unlocks > time.time()
        except (ValueError, TypeError):
            return True

    def _remove(self, market_id: str, stale: list[dict]) -> bool:
        """Forget only IDs explicitly acknowledged as removed or already final."""
        if not stale:
            return True
        # An acknowledged cancellation is reconciled via its hash; resending it
        # would keep moving the timestamp that the WS pending-clear must exceed.
        actionable = [order for order in stale
                      if not order.get("cancelPending") and not order.get("terminal")]
        if not actionable:
            return False
        eligible = [order for order in actionable
                    if order.get("orderId") and not self._locked(order)]
        if len(eligible) != len(actionable):
            self.log(f"[LIVE] removal pending or locked market={market_id}; holding new orders")
        if not eligible:
            return False
        all_removed = len(eligible) == len(actionable)
        for start in range(0, len(eligible), 100):
            batch = eligible[start:start + 100]
            ids = {str(order["orderId"]) for order in batch}
            try:
                result = self.live.cancel(sorted(ids))
                removed = {str(value) for value in (result.get("removed") or [])}
                noop = {str(value) for value in (result.get("noop") or [])}
                rejected = {str(value) for value in (result.get("rejected") or [])}
                acknowledged = ((removed | noop) & ids) - rejected
                for order in batch:
                    if str(order["orderId"]) in acknowledged:
                        # Removal acceptance is not a settled-order snapshot.
                        # Keep its reservation until GET confirms terminal state.
                        order["cancelPending"] = True
                        order["cancelAckAt"] = int(time.time() * 1000)
                if acknowledged != ids:
                    all_removed = False
                    self.log(f"[LIVE] removal incomplete market={market_id}; holding new orders")
            except RuntimeError as error:
                all_removed = False
                self.log(f"[LIVE] cancel failed market={market_id}: {error}")
        return all_removed and not any(order in self.orders.get(market_id, []) for order in stale)

    def sync(self, market_id: str, token_ids: dict, ladder: dict, *,
             fee_rate_bps: float, is_neg_risk: bool = False,
             is_yield_bearing: bool = False, budget: float | None = None,
             expires_at: float | None = None, stop_new_seconds: float = 60.0,
             stop_all_seconds: float = 10.0) -> int:
        """Keep matching orders; remove stale prices; add affordable missing bids.

        ``budget`` limits this market's total resting notional, including retained
        partial orders and unsettled matches. A complete target level is placed
        only when affordable; the configured share size is never silently scaled.
        """
        market_id = str(market_id)
        if budget is not None and (not isfinite(budget) or budget < 0):
            raise ValueError("Live resting-order budget must be finite and nonnegative")
        if (expires_at is not None and not isfinite(expires_at)
                or not isfinite(stop_new_seconds) or not isfinite(stop_all_seconds)
                or stop_new_seconds < stop_all_seconds or stop_all_seconds < 0):
            raise ValueError("Live round cutoffs must be finite and correctly ordered")
        try:
            canonical_tokens = {side: self._token_id(token_ids.get(side)) for side in ("UP", "DOWN")}
            if canonical_tokens != self._tokens.get(market_id):
                raise RuntimeError("Live quote tokens lack a reconciled position baseline")
        except (RuntimeError, ValueError, TypeError) as error:
            self._invalidate()
            self.log(f"[LIVE] market tokens not reconciled market={market_id}: {error}")
            self._refresh(market_id)
            self._remove(market_id, list(self.orders.get(market_id, [])))
            return 0
        token_ids = canonical_tokens
        wanted, ranks = {}, {}
        for side, levels in ladder.items():
            if side not in {"UP", "DOWN"}:
                raise ValueError("Live maker supports only UP/DOWN BUY bids")
            token = token_ids.get(side)
            if not token:
                continue
            for index, level in enumerate(levels):
                if (not isfinite(level.price) or not 0 < level.price < 1
                        or not isfinite(level.shares) or level.shares <= 0):
                    raise ValueError("Live ladder contains an invalid price or quantity")
                key = side, Decimal(str(level.price))
                wanted[key] = float(level.shares)
                ranks[key] = index
        refreshed = self._refresh(market_id)
        if not refreshed or not self.ready(market_id):
            self._remove(market_id, list(self.orders.get(market_id, [])))
            return 0
        existing = self.orders.get(market_id, [])
        stale, kept_keys, kept_cost = [], set(), 0.0
        for order in existing:
            key = self._key(order)
            cost = order["remaining"] * float(order["price"])
            # A partially filled order at a desired price keeps its queue place.
            # A deliberate size reduction requires a cancel and a new order.
            retain = (not order.get("cancelPending") and key in wanted and key not in kept_keys
                      and order["remaining"] <= wanted[key] + 1e-9
                      and (budget is None or kept_cost + cost <= budget + 1e-9))
            if retain:
                kept_keys.add(key)
                kept_cost += cost
            else:
                stale.append(order)
        if not self._remove(market_id, stale):
            return 0
        placed = 0
        # Alternate outcomes at each depth so limited cash can quote both sides.
        candidates = sorted(wanted, key=lambda key: (ranks[key], key[0]))
        for side, price_decimal in candidates:
            tail_room = None
            if expires_at is not None:
                seconds_left = expires_at - time.time()
                if seconds_left <= stop_all_seconds:
                    self._remove(market_id, list(self.orders.get(market_id, [])))
                    break
                if seconds_left <= stop_new_seconds:
                    net = self.position_state(market_id)["net_quantity"]
                    missing = "DOWN" if net > 1e-9 else "UP" if net < -1e-9 else None
                    unsafe = [order for order in self.orders.get(market_id, [])
                              if order["side"] != missing]
                    if unsafe:
                        self._remove(market_id, unsafe)
                        break
                    if side != missing:
                        continue
                    committed = sum(float(order.get("remaining", order["shares"]))
                                    + (order.get("terminalHoldWei", 0)
                                       + max(0, order.get("grossFilledWei", 0)
                                             - order.get("positionFilledWei", 0))) / WEI
                                    for order in self.orders.get(market_id, [])
                                    if order["side"] == missing)
                    tail_room = max(0.0, abs(net) - committed)
            key = side, price_decimal
            if key in kept_keys:
                continue
            price, shares = float(price_decimal), wanted[key]
            if tail_room is not None and shares > tail_room + 1e-9:
                continue
            cost = price * shares
            if budget is not None and self.reserved_cost(market_id) + cost > budget + 1e-9:
                continue
            token = token_ids.get(side)
            record = {"side": side, "price": price, "shares": shares,
                      "remaining": shares, "tokenId": str(token), "orderId": None,
                      "orderHash": "", "uncertain": True}
            try:
                result = self.live.place_limit(
                    token_id=token, price=price, quantity=shares,
                    fee_rate_bps=fee_rate_bps,
                    is_neg_risk=is_neg_risk, is_yield_bearing=is_yield_bearing,
                )
            except RuntimeError as error:
                self.log(f"[LIVE] place failed {side} {price}: {error}")
                if getattr(error, "ambiguous", False) and getattr(error, "order_hash", None):
                    record["orderHash"] = error.order_hash
                    self.orders.setdefault(market_id, []).append(record)
                    self._remember(market_id, record)
                    self._invalidate()
                    break
                continue
            record.update(orderId=str(result["orderId"]), orderHash=str(result["orderHash"]),
                          removalLockedUntil=result.get("removalLockedUntil"), uncertain=False)
            self.orders.setdefault(market_id, []).append(record)
            self._remember(market_id, record)
            kept_keys.add(key)
            placed += 1
        return placed

    def drop(self, keep: set[str]) -> None:
        for market_id in list(self.orders):
            if market_id in keep:
                continue
            self._refresh(market_id)
            self._remove(market_id, list(self.orders.get(market_id, [])))
