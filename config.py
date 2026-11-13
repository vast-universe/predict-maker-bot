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
    starting_cash: float = 10_000.0
    max_order_cost: float = 100.0
    risk_fraction: float = 0.10
    max_exposure_fraction: float = 0.10
    min_edge_per_share: float = 0.03
    min_hedge_profit_per_share: float = 0.01
    minimum_order_cost: float = 1.0
    fixed_shares: float = 10.0  # base net shares; low prices may require more for $1 minimum
    min_pair_edge: float = 0.01  # arb comb ratio must clear 1.010
    rebalance_trigger: float = 0.60  # trim Up when Down ask trades above this, and vice versa
    min_rebalance_profit_per_share: float = 0.0
    # --- maker mode: replicate the tracked wallet's two-sided bid ladder ---
    # Measured from 3000 fills: 94.8% maker, 100% Bids, both sides, 1c grid,
    # top level 1c under the mid, ~6 levels, 100 shares per level, pair cost 0.97.
    maker_mode: bool = True
    quote_offset_cents: float = 1.0
    ladder_levels: int = 5
    ladder_step_cents: float = 1.0
    level_shares: float = 100.0
    requote_ticks: int = 1
    replenish_delay: float = 8.0  # wallet refills a taken tick after ~8s (median)
    queue_ramp: float = 8.0  # seconds for a fresh order to earn full queue share
    # Median depth resting at our own tick on the live book, so a nearly
    # drained tick does not hand us a 90% queue share.
    queue_depth_floor: float = 269.0
    # The wallet fills in bursts (~15 per round, ~9.8 fills each). One 3s
    # snapshot must not sweep the whole ladder, so cap levels per side.
    burst_levels: float = 2.0
    # Wallet bursts move a median of 79 shares; smaller depth wobbles are
    # noise and must not register as a hit on our level.
    burst_min_shares: float = 25.0
    # Deep levels fill at cheap prices when a print sweeps through them, which
    # is where the maker earns its spread. Capping how far below the touch a
    # level may fill removed exactly those fills and pushed the paired cost from
    # 0.9527 to 1.0071, so the cap is off by default.
    max_fill_distance: Optional[float] = None
    skew_cents_max: float = 0.0  # measured wallet quotes both sides symmetrically
    # None means a single market may use any amount, bounded only by free cash.
    maker_market_budget: Optional[float] = None
    # Measured: fills at 0.01/0.02 exist but are far rarer than mid prices, so the
    # ladder shrinks (not disappears) at extreme prices where the 2% taker fee and
    # the near-total loss risk dominate the tiny absolute edge.
    # The wallet rebalances by taking: 5% of its fills are aggressive buys on the
    # under-weighted side, which keeps its Up/Down ratio near 0.97.
    # Measured: the wallet acts once |net| passes ~196 shares and then takes only
    # ~10 shares, so exposure stays open instead of being flattened at once.
    rebalance_trigger_shares: float = 196.0
    rebalance_take_shares: float = 10.0
    rebalance_cooldown: float = 8.0
    low_price_shrink_below: float = 0.05
    low_price_shrink_factor: float = 0.3
    reward_spread_threshold: float = 0.06
    reward_share_threshold: float = 100.0
    # The scan is WebSocket driven: a pushed book rescans after a short debounce,
    # but never more often than min_scan_interval and never later than interval.
    min_scan_interval: float = 3.0
    scan_debounce: float = 0.25
    ws_warmup_seconds: float = 3.0
    data_dir: str = "paper_data"
    predict_api_key: Optional[str] = os.getenv("PREDICT_API_KEY") or None
    predict_base_url: str = os.getenv("PREDICT_BASE_URL", "https://api.predict.fun").rstrip("/")
    predict_account_address: Optional[str] = os.getenv("PREDICT_ACCOUNT_ADDRESS") or None
