from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional


def _load_env_file() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("\"'")
        if name:
            os.environ.setdefault(name, value)


_load_env_file()


@dataclass(frozen=True)
class Config:
    assets: tuple[str, ...] = ("BTC",)
    windows_minutes: tuple[int, ...] = (5,)
    starting_cash: float = 300.0
    minimum_order_cost: float = 1.0
    # --- maker mode: replicate the tracked wallet's two-sided bid ladder ---
    # Measured from 3000 fills: 94.8% maker, 100% Bids, both sides, 1c grid,
    # top level 1c under the mid, ~6 levels, 100 shares per level, pair cost 0.97.
    # Live orders need all three credentials; anything missing keeps it paper.
    live_trading: bool = False
    maker_mode: bool = True
    quote_offset_cents: float = 1.0
    ladder_levels: int = 5
    ladder_step_cents: float = 1.0
    # 10 shares a level is the smallest fixed size that still clears Predict's
    # 1 USDT minimum order at every tick the ladder may quote: 10 x 0.10 = 1.00.
    # A fixed share count keeps both sides equal, so a fill on one side is always
    # pairable by the other (fixed notional sizing drifts them apart as the price
    # moves).
    level_shares: float = 10.0
    # Dynamic sizing: when > 0 each level costs this many USDT and the shares
    # scale as notional / price, so the ladder stays legal at every tick instead
    # of only above 0.10. Both legs are equalised to the same share count.
    level_notional: float = 0.0
    quote_min_price: float = 0.10
    quote_max_price: float = 0.90
    requote_ticks: int = 1
    # 0 = a replaced order is immediately live. The previous 8s came from watching
    # the wallet's refill cadence, which describes its behaviour, not a rule of
    # ours; a venue fills a resting order as soon as it is on the book.
    replenish_delay: float = 0.0
    # Median depth resting at our own tick on the live book, so a nearly
    # drained tick does not hand us a 90% queue share.
    # The wallet fills in bursts (~15 per round, ~9.8 fills each). One 3s
    # snapshot must not sweep the whole ladder, so cap levels per side.
    # Wallet bursts move a median of 79 shares; smaller depth wobbles are
    # noise and must not register as a hit on our level.
    skew_cents_max: float = 0.0  # measured wallet quotes both sides symmetrically
    # None means a single market may use any amount, bounded only by free cash.
    maker_market_budget: Optional[float] = None
    # The wallet rebalances by taking: 5% of its fills are aggressive buys on the
    # under-weighted side, which keeps its Up/Down ratio near 0.97.
    # The wallet never takes to flatten: every taker-flagged fill of its 4,200
    # measured fills is passive (473 complementary matches price at exactly
    # 1.0000, 103 same-outcome matches at the counterparty's price), so exposure
    # accumulates and is carried to settlement. A trigger of 0 disables taking;
    # set it above 0 only if you deliberately want a flatter book than the
    # reference wallet keeps.
    rebalance_trigger_shares: float = 0.0
    reward_spread_threshold: float = 0.06
    # The scan is WebSocket driven: a pushed book rescans after a short debounce,
    # but never more often than min_scan_interval and never later than interval.
    # Purely an execution throttle, not a market rule: it stops the loop from
    # spinning on every socket frame. It no longer tries to shape the fill count
    # to look like the wallet's.
    min_scan_interval: float = 1.0
    scan_debounce: float = 0.25
    ws_warmup_seconds: float = 3.0
    data_dir: str = "paper_data"
    predict_api_key: Optional[str] = os.getenv("PREDICT_API_KEY") or None
    predict_base_url: str = os.getenv("PREDICT_BASE_URL", "https://api.predict.fun").rstrip("/")
    predict_account_address: Optional[str] = os.getenv("PREDICT_ACCOUNT_ADDRESS") or None
    privy_wallet_private_key: Optional[str] = os.getenv("PRIVY_WALLET_PRIVATE_KEY") or None
