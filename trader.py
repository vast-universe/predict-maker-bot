import json
import time
from typing import Optional
from urllib.error import HTTPError, URLError
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
# Without them everything stays paper. The order flow follows the Predict docs:
# GET /v1/auth/message -> personal_sign -> POST /v1/auth -> Bearer JWT (24h),
# then SDK-built EIP-712 orders posted to POST /v1/orders.
# ---------------------------------------------------------------------------

WEI = 10 ** 18


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
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Predict {method} /v1/{path} -> HTTP {error.code}: {detail}") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"Predict {method} /v1/{path} failed: {error}") from error

    def _jwt(self) -> str:
        if self.token and time.time() < self.token_deadline:
            return self.token
        message = self._request("GET", "auth/message", auth=False)["data"]["message"]
        signature = Account.sign_message(
            encode_defunct(text=message), private_key=self.private_key
        ).signature.to_0x_hex()
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
        """Build the signer, preferring the Predict-account mode when it applies.

        Passing predict_account makes the SDK verify on chain that the key owns
        the account (ecdsaValidatorStorage). That only holds for a Privy-managed
        account; a plain EOA has no such record, so fall back to signing directly
        instead of failing outright.
        """
        if self.builder is not None:
            return self.builder
        from predict_sdk import ChainId, OrderBuilder, OrderBuilderOptions
        if self.account_address:
            try:
                self.builder = OrderBuilder.make(
                    ChainId.BNB_MAINNET, self.private_key,
                    OrderBuilderOptions(predict_account=self.account_address),
                )
                return self.builder
            except Exception as error:  # noqa: BLE001 - ownership check is optional
                self.errors.append(
                    f"predict_account mode unavailable ({type(error).__name__}); "
                    f"signing as a plain EOA"
                )
        self.builder = OrderBuilder.make(ChainId.BNB_MAINNET, self.private_key)
        return self.builder

    # --------------------------------------------------------------- orders
    def place_limit(self, *, token_id: str, price: float, quantity: float,
                    fee_rate_bps: float, side: str = "BUY",
                    is_neg_risk: bool = False, is_yield_bearing: bool = False) -> dict:
        """Sign a BUY limit order and post it. Returns the API response data."""
        from predict_sdk import BuildOrderInput, LimitHelperInput, Side
        builder = self._order_builder()
        side_enum = Side.BUY if side.upper() == "BUY" else Side.SELL
        price_wei = int(round(price * WEI))
        quantity_wei = int(round(quantity * WEI))
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
        response = self._request("POST", "orders", {"data": {
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
        }})
        return response.get("data", {})

    def cancel(self, order_ids: list[str]) -> dict:
        return self._request("POST", "orders/remove", {
            "data": {"orderIds": [str(i) for i in order_ids]}
        })

    def open_orders(self) -> list[dict]:
        return self._request("GET", "orders", auth=True).get("data", [])

    def positions(self) -> list[dict]:
        return self._request(
            "GET", f"positions/{self.account_address}", auth=False
        ).get("data", [])


class LiveBook:
    """Mirrors the local ladder onto the real exchange, cancelling and replacing.

    Keeps the order ids the API returned so the next sync can remove exactly the
    orders that changed, the same reconcile-then-place loop the reference maker
    runs, and never touches orders it did not create.
    """

    def __init__(self, live: LiveTrader, log=None):
        self.live = live
        self.log = log or (lambda message: None)
        self.orders: dict[str, list[dict]] = {}  # market_id -> placed orders

    def sync(self, market_id: str, token_ids: dict, ladder: dict, *,
             fee_rate_bps: float, is_neg_risk: bool = False,
             is_yield_bearing: bool = False) -> int:
        """Replace the market's resting orders with `ladder`. Returns the count placed."""
        wanted = {}
        for side, levels in ladder.items():
            token = token_ids.get(side)
            if not token:
                continue
            for level in levels:
                wanted[(side, round(level.price, 4))] = level.shares
        existing = {
            (o.get("side"), round(float(o.get("price", 0)), 4)): o
            for o in self.orders.get(market_id, [])
        }
        if existing and set(existing) == set(wanted):
            return 0  # nothing changed, leave the book alone
        stale = [o["orderId"] for o in existing.values() if o.get("orderId")]
        stale += [o["orderId"] for o in self.orders.get(market_id, [])
                  if o.get("orderId") and (o.get("side"), round(float(o.get("price", 0)), 4)) not in wanted]
        stale = list(dict.fromkeys(stale))
        if stale:
            try:
                self.live.cancel(stale)
            except RuntimeError as error:
                self.log(f"[LIVE] cancel failed for market {market_id}: {error}")
        placed = []
        for (side, price), shares in wanted.items():
            token = token_ids.get(side)
            try:
                result = self.live.place_limit(
                    token_id=token, price=price, quantity=shares,
                    fee_rate_bps=fee_rate_bps,
                    is_neg_risk=is_neg_risk, is_yield_bearing=is_yield_bearing,
                )
            except RuntimeError as error:
                self.log(f"[LIVE] place failed {side} {price}: {error}")
                continue
            placed.append({"side": side, "price": price, "shares": shares,
                           "orderId": result.get("orderId"),
                           "orderHash": result.get("orderHash")})
        if placed:
            self.orders[market_id] = placed
        else:
            self.orders.pop(market_id, None)
        return len(placed)

    def drop(self, keep: set[str]) -> None:
        for market_id in list(self.orders):
            if market_id in keep:
                continue
            ids = [o["orderId"] for o in self.orders[market_id] if o.get("orderId")]
            if ids:
                try:
                    self.live.cancel(ids)
                except RuntimeError as error:
                    self.log(f"[LIVE] cancel failed on rollover {market_id}: {error}")
            self.orders.pop(market_id, None)
