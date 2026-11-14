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
    windows_minutes: tuple[int, ...] = (5, 15)
    starting_cash: float = 300.0
    minimum_order_cost: float = 1.0
    # Public matches sampled on 2026-11-14: BTC 5m/15m, BUY on both outcomes,
    # cent-grid prices; completed-looking maker hashes commonly total 100/40
    # shares respectively. Fills do NOT reveal unfilled orders or cancel rules.
    reference_wallet: str = "0x5E498154448608C826E005c93533B827Cbb6377e"
    live_trading: bool = False
    maker_mode: bool = True
    # Execution assumptions, not recovered private strategy parameters.
    quote_offset_cents: float = 1.0
    ladder_levels: int = 5
    ladder_step_cents: float = 1.0
    level_shares: float = 100.0
    level_shares_15m: float = 40.0
    # Dynamic sizing: when > 0 each level costs this many USDT and the shares
    # scale as notional / price, so the ladder stays legal at every tick instead
    # of only above 0.10. Both legs are equalised to the same share count.
    level_notional: float = 0.0
    quote_min_price: float = 0.01
    quote_max_price: float = 0.99
    requote_ticks: int = 1
    # Optional refill throttle; it is not the exchange's speed-bump duration.
    replenish_delay: float = 0.0
    skew_cents_max: float = 0.0
    # Optional tighter legacy cap, combined with risk_max_market_cost.
    maker_market_budget: Optional[float] = None
    # Our buy-only, hold-to-settlement risk policy. These are explicit design
    # assumptions, not recovered rules of the reference wallet. Limit both
    # one-sided fill exposure and paid cost plus every outstanding bid.
    risk_max_net_shares: float = 200.0
    risk_pair_margin: float = 0.02
    risk_max_market_cost: float = 250.0
    # Inside the first cutoff only the missing side may be bought, at a price
    # that locks the configured margin against the unmatched inventory cost.
    risk_stop_new_seconds: float = 60.0
    risk_stop_all_seconds: float = 10.0
    # A midpoint jump cancels quotes; the caller owns the cooldown clock.
    risk_shock_cents: float = 12.0
    risk_shock_cooldown: float = 10.0
    # Retained for compatibility with older helpers. The active buy-only Maker
    # loop never calls the ask-taking overlay, and CLI requires its trigger=0.
    rebalance_trigger_shares: float = 0.0
    rebalance_take_shares: float = 10.0
    rebalance_cooldown: float = 8.0
    reward_spread_threshold: float = 0.06
    reward_share_threshold: float = 100.0
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
