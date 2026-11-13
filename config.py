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
    starting_cash: float = 1_000.0
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
    # The scan is WebSocket driven: a pushed book rescans after a short debounce,
    # but never more often than min_scan_interval and never later than interval.
    min_scan_interval: float = 1.0
    scan_debounce: float = 0.25
    ws_warmup_seconds: float = 3.0
    data_dir: str = "paper_data"
    predict_api_key: Optional[str] = os.getenv("PREDICT_API_KEY") or None
    predict_base_url: str = os.getenv("PREDICT_BASE_URL", "https://api.predict.fun").rstrip("/")
    predict_account_address: Optional[str] = os.getenv("PREDICT_ACCOUNT_ADDRESS") or None
    privy_wallet_private_key: Optional[str] = os.getenv("PRIVY_WALLET_PRIVATE_KEY") or None
