import argparse
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from config import Config
from markets import PredictMarketFeed, valid_price
from paper_portfolio import PaperPortfolio
from strategy import (
    Opportunity,
    evaluate_arb,
    evaluate_hedge,
    evaluate_market,
    evaluate_rebalance,
    hedge_reserve,
    evaluate_rebalance_take,
    mid_up_price,
    quote_ladder,
)
from trader import PaperTrader


TITLE_WIDTH = 40


def _short_title(snapshot) -> str:
    title = snapshot.market_title or (
        f"{snapshot.asset} Up or Down - {snapshot.window_minutes}m round"
    )
    if len(title) <= TITLE_WIDTH + 3:
        return title
    return title[:TITLE_WIDTH].rstrip() + "..."


def _asset_line(snapshot, quoted: int, scanned: int, signals: int) -> str:
    seconds = int(max(0.0, snapshot.seconds_left or 0.0))
    return (
        f"[{snapshot.asset}] {_short_title(snapshot)} @{seconds}s "
        f"[{quoted}/{scanned}] - {signals} sig"
    )


def _execute_orders(trader: PaperTrader, orders: list, *, maker: bool = False) -> int:
    """Fill a scan batch in parallel, committing paper state in submission order."""
    if not orders:
        return 0
    turn = [0]
    condition = threading.Condition()

    def run(index, opportunity):
        with condition:
            while turn[0] != index:
                condition.wait()
            try:
                return trader.execute(opportunity, maker=maker)
            finally:
                turn[0] += 1
                condition.notify_all()

    with ThreadPoolExecutor(max_workers=min(8, len(orders))) as pool:
        futures = [pool.submit(run, index, order) for index, order in enumerate(orders)]
        results = [future.result() for future in futures]
    return sum(1 for filled in results if filled)


def _check_settlements(feed, portfolio: PaperPortfolio, log) -> None:
    """Apply settlement for markets Predict has already finalized."""
    if not portfolio.open_positions or not hasattr(feed, "get_resolution"):
        return
    settled = 0
    for market_id in list(portfolio.open_positions):
        try:
            resolution = feed.get_resolution(market_id)
        except RuntimeError as error:
            log(f"Could not check settlement for market {market_id}: {error}")
            continue
        if resolution:
            portfolio.settle_trade(market_id, str(resolution["winner"]))
            settled += 1
    if settled:
        log(f"Settled {settled} paper position(s)")


def _refresh_account_positions(feed, portfolio: PaperPortfolio, config: Config,
                               say, log) -> None:
    """Mirror the wallet's live Predict positions when an address is configured."""
    if not config.predict_account_address:
        return
    try:
        portfolio.update_account_positions(
            feed.get_positions_by_address(config.predict_account_address)
        )
    except RuntimeError as error:
        portfolio.set_account_positions_error(str(error))
        say(f"Live position query failed: {error}")


def _wait_for_next_scan(feed, interval: float, config: Config,
                        scan_started_at: float) -> bool:
    """Wait for the next scan, waking early when the WebSocket pushes a book.

    The wait is bounded by ``interval`` so REST-only operation keeps polling,
    and floored by ``min_scan_interval`` so a busy book cannot trigger a scan
    loop. Returns True when a streamed message ended the wait.
    """
    elapsed = time.monotonic() - scan_started_at
    floor = max(0.0, config.min_scan_interval - elapsed)
    if floor > 0:
        time.sleep(floor)
    budget = max(0.0, interval - elapsed - floor)
    waiter = getattr(feed, "wait_for_update", None)
    if budget > 0 and callable(waiter) and waiter(budget):
        if config.scan_debounce > 0:
            # Let a burst of book updates settle before pricing off it.
            time.sleep(config.scan_debounce)
        return True
    return False


def run(cycles: int, interval: float, config: Config) -> None:
    feed = PredictMarketFeed(
        config.assets,
        config.windows_minutes,
        api_key=config.predict_api_key,
        base_url=config.predict_base_url,
    )
    source = "predict"
    portfolio = PaperPortfolio(config.starting_cash, Path(config.data_dir), source)
    trader = PaperTrader(portfolio)

    def log(message: str) -> None:
        """Diagnostics stay off the terminal to match the compact console."""
        return None

    def say(message: str) -> None:
        print(message, flush=True)
        log(message)

    try:
        # Give the socket a bounded head start so the first scan prices off the
        # stream instead of falling back to REST on every cold start.
        warm_up = getattr(feed, "warm_up", None)
        if callable(warm_up):
            warm_up(config.ws_warmup_seconds)
        loop = _maker_loop if config.maker_mode else _scan_loop
        loop(feed, portfolio, trader, cycles, interval, config, say, log)
    finally:
        close = getattr(feed, "close", None)
        if callable(close):
            close()


def _scan_loop(feed, portfolio, trader, cycles: int, interval: float, config: Config,
               say, log) -> None:
    source = "predict"
    cycle = 0
    last_rest_at = 0.0  # settlement and account reads are REST; keep them off the hot path
    last_summary_at = 0.0
    while cycles == 0 or cycle < cycles:
        cycle += 1
        scan_started_at = time.monotonic()
        if scan_started_at - last_rest_at >= interval:
            last_rest_at = scan_started_at
            _check_settlements(feed, portfolio, log)
            _refresh_account_positions(feed, portfolio, config, say, log)
        try:
            snapshots = feed.get_snapshots()
            for diagnostic in getattr(feed, "diagnostics", []):
                log(f"[FEED] {diagnostic}")
            portfolio.update_market_marks(snapshots)
        except RuntimeError as error:
            say(f"Market scan failed: {error}")
            if cycles == 0 or cycle < cycles:
                time.sleep(interval)
                continue
            break

        stats: dict[str, dict] = {}
        for snapshot in snapshots:
            stat = stats.setdefault(
                snapshot.asset, {"scanned": 0, "quoted": 0, "signals": 0, "snapshot": snapshot}
            )
            stat["scanned"] += 1
            if valid_price(snapshot.up_ask) and valid_price(snapshot.down_ask):
                stat["quoted"] += 1
            current = stat["snapshot"]
            if (snapshot.window_minutes, snapshot.seconds_left or float("inf")) < (
                current.window_minutes, current.seconds_left or float("inf")
            ):
                stat["snapshot"] = snapshot
            quote = (
                f"lead={((snapshot.spot_price / snapshot.price_to_beat - 1) * 10_000):+.2f}bp"
                if snapshot.spot_price is not None and snapshot.price_to_beat
                else "lead=unavailable"
            )
            up_text = f"{snapshot.up_ask:.3f}" if snapshot.up_ask is not None else "unavailable"
            down_text = f"{snapshot.down_ask:.3f}" if snapshot.down_ask is not None else "unavailable"
            log(f"[{source.upper()}] {snapshot.asset} {snapshot.window_minutes}m "
                f"market={snapshot.market_id} Up ask={up_text} "
                f"Down ask={down_text} {quote}")
        if not snapshots:
            log("No open markets with usable quotes")

        orders = []
        pending_proceeds = 0.0
        hedged_market_ids = set()

        def remaining_budget() -> float:
            return min(
                portfolio.cash + pending_proceeds,
                max(0.0, config.starting_cash * config.max_exposure_fraction
                    - portfolio.open_exposure),
            )

        def reserved_cash(exclude_market_id=None) -> float:
            return sum(
                hedge_reserve(position, config.min_hedge_profit_per_share)
                for market_id, position in portfolio.open_positions.items()
                if market_id != exclude_market_id
            )

        # Trim unmatched legs when the opposite side trades rich enough.
        for snapshot in snapshots:
            position = portfolio.open_positions.get(snapshot.market_id)
            if not position:
                continue
            opportunity, reason = evaluate_rebalance(
                snapshot, position=position,
                trigger=config.rebalance_trigger,
                min_profit_per_share=config.min_rebalance_profit_per_share,
            )
            if opportunity:
                orders.append(opportunity)
                pending_proceeds += opportunity.quantity * opportunity.ask
                stats[snapshot.asset]["signals"] += 1
                log(f"[REBAL] {snapshot.asset} {snapshot.window_minutes}m "
                    f"market={snapshot.market_id} sell {opportunity.side} "
                    f"{opportunity.quantity:g} @ {opportunity.ask:.3f}")
            else:
                log(f"[REBAL WAIT] {snapshot.asset} {snapshot.window_minutes}m "
                    f"market={snapshot.market_id} {reason}")

        # Handle profitable hedges across all markets before adding new risk.
        for snapshot in snapshots:
            position = portfolio.open_positions.get(snapshot.market_id)
            if not position:
                continue
            risk_budget = min(
                config.max_order_cost,
                config.starting_cash * config.risk_fraction,
                max(0.0, remaining_budget() - reserved_cash(snapshot.market_id)),
            )
            opportunity, reason = evaluate_hedge(
                snapshot, position=position, risk_budget=risk_budget,
                min_order_cost=config.minimum_order_cost,
                min_locked_profit_per_share=config.min_hedge_profit_per_share,
                fixed_shares=config.fixed_shares,
            )
            if opportunity:
                orders.append(opportunity)
                hedged_market_ids.add(snapshot.market_id)
                stats[snapshot.asset]["signals"] += 1
                log(f"[FILLED] market={snapshot.market_id} hedge {opportunity.side} "
                    f"{opportunity.quantity:g} net shares @ {opportunity.ask:.3f}")
            elif not opportunity:
                log(f"[HEDGE WAIT] {snapshot.asset} {snapshot.window_minutes}m "
                    f"market={snapshot.market_id} {reason}")

        for snapshot in snapshots:
            if snapshot.market_id in hedged_market_ids:
                continue  # At most one order per market on each scan.
            position = portfolio.open_positions.get(snapshot.market_id)
            if not position and snapshot.market_id in portfolio.traded_market_ids:
                log(f"[SKIP] market={snapshot.market_id} already settled or closed")
                continue
            risk_budget = min(
                config.max_order_cost,
                config.starting_cash * config.risk_fraction,
                max(0.0, remaining_budget() - reserved_cash()),
            )
            opportunity = None
            if not position:
                opportunity, _ = evaluate_arb(
                    snapshot, risk_budget=risk_budget,
                    min_order_cost=config.minimum_order_cost,
                    fixed_shares=config.fixed_shares,
                    min_pair_edge=config.min_pair_edge,
                )
            if opportunity is None:
                opportunity, reason = evaluate_market(
                    snapshot, risk_budget=risk_budget,
                    min_edge_per_share=config.min_edge_per_share,
                    min_order_cost=config.minimum_order_cost,
                    fixed_shares=config.fixed_shares,
                )
            if opportunity and position:
                net_up = float(position.get("up_quantity", 0.0)) - float(
                    position.get("down_quantity", 0.0)
                )
                if ((net_up > 1e-9 and opportunity.side == "DOWN")
                        or (net_up < -1e-9 and opportunity.side == "UP")):
                    opportunity = None
                    reason = "opposite-side orders must meet the profit-lock condition"
            if opportunity and opportunity.side != "PAIR":
                reserve = opportunity.quantity * max(
                    0.0, 1.0 - config.min_hedge_profit_per_share - opportunity.cost_per_share
                )
                required = opportunity.quantity * opportunity.cost_per_share + reserve
                available = max(0.0, remaining_budget() - reserved_cash())
                if reserve + 1e-9 < config.minimum_order_cost:
                    opportunity = None
                    reason = "profitable equal-size hedge would be below the order minimum"
                elif required > available + 1e-9:
                    opportunity = None
                    reason = (f"entry plus hedge reserve needs ${required:.2f}; "
                              f"unreserved budget is ${available:.2f}")
            if opportunity:
                orders.append(opportunity)
                stats[snapshot.asset]["signals"] += 1
                log(f"[FILLED] market={snapshot.market_id} "
                    f"{opportunity.strategy} {opportunity.side} "
                    f"{opportunity.quantity:g} net shares @ {opportunity.ask:.3f}")
            else:
                log(f"[SKIP] {snapshot.asset} {snapshot.window_minutes}m "
                    f"market={snapshot.market_id} {reason}")

        status = feed.ws_status() if hasattr(feed, "ws_status") else "ws=off"
        say(f"{'' if cycle == 1 else chr(10)}--- Cycle {cycle} ---  {status}")
        for asset, stat in stats.items():
            say(_asset_line(stat["snapshot"], stat["quoted"], stat["scanned"], stat["signals"]))
        if orders:
            say(f"* Executing {len(orders)} orders in parallel...")
            _execute_orders(trader, orders)
        else:
            log("No qualifying paper trades")
        log(f"Cash remaining: ${portfolio.cash:.2f}")
        if cycles == 0 or cycle < cycles:
            if time.monotonic() - last_summary_at >= interval:
                last_summary_at = time.monotonic()
                portfolio.write_summary()
            if not _wait_for_next_scan(feed, interval, config, scan_started_at):
                say(f"Next cycle in {interval:g}s...")
    portfolio.write_summary()
    say(f"Summary written to {portfolio.summary_path}")



def _market_budget(portfolio: PaperPortfolio, config: Config) -> float:
    """Cash a single market may commit.

    ``maker_market_budget`` caps it explicitly; when unset the only limit is the
    free cash, so one round can use the whole bankroll.
    """
    if config.maker_market_budget is None:
        return max(0.0, portfolio.cash)
    return max(0.0, min(config.maker_market_budget, portfolio.cash))


_REBALANCE_LAST: dict[str, float] = {}


def _rebalance_ready(config: Config, snapshot) -> bool:
    """Rate-limit taker rebalances per market.

    The wallet lifts ~10 shares at a time and does not chain those lifts back to
    back; without a cooldown a 10-share take would fire every scan and flatten
    the book just as fast as the old 100-share behaviour.
    """
    now = time.monotonic()
    last = _REBALANCE_LAST.get(snapshot.market_id, 0.0)
    if last and now - last < config.rebalance_cooldown:
        return False
    return True


def _inventory_skew(portfolio: PaperPortfolio, snapshot, config: Config) -> float:
    """Shift quotes against the current net position to mean-revert inventory."""
    position = portfolio.open_positions.get(snapshot.market_id)
    if not position:
        return 0.0
    net = float(position.get("up_quantity", 0.0)) - float(position.get("down_quantity", 0.0))
    scale = max(1.0, config.level_shares * max(1, config.ladder_levels))
    ratio = max(-1.0, min(1.0, net / scale))
    return ratio * config.skew_cents_max


def _maker_opportunity(snapshot, level, mid_up: float):
    """Turn one simulated maker fill into the ledger's Opportunity shape."""
    probability = mid_up if level.side == "UP" else 1.0 - mid_up
    return Opportunity(
        market=snapshot, side=level.side, quantity=level.shares, ask=level.price,
        fair_probability=probability, fee_per_share=0.0, cost_per_share=level.price,
        expected_edge_per_share=0.0, lead_bps=0.0,
        up_ask=snapshot.up_ask if level.side == "UP" else None,
        down_ask=snapshot.down_ask if level.side == "DOWN" else None,
        up_cost_per_share=level.price if level.side == "UP" else 0.0,
        down_cost_per_share=level.price if level.side == "DOWN" else 0.0,
        up_order_quantity=level.shares if level.side == "UP" else 0.0,
        down_order_quantity=level.shares if level.side == "DOWN" else 0.0,
        strategy="maker",
    )


def _maker_line(snapshot, mid_up: float, state, fills: int,
                spread_threshold: float = 0.06) -> str:
    """One console line per market: mid, both ladders, reward-qualified size."""
    resting = state.unfilled() if state else []
    up_prices = [f"{level.price:.2f}" for level in resting if level.side == "UP"]
    down_prices = [f"{level.price:.2f}" for level in resting if level.side == "DOWN"]
    qualified = sum(
        level.shares for level in resting
        if abs(level.price - (mid_up if level.side == "UP" else 1.0 - mid_up))
        <= spread_threshold + 1e-12
    )
    seconds = int(max(0.0, snapshot.seconds_left or 0.0))
    return (
        f"[{snapshot.asset}] {_short_title(snapshot)} @{seconds}s "
        f"mid={mid_up:.2f} | Up {'/'.join(up_prices) or '-'} "
        f"| Down {'/'.join(down_prices) or '-'} | qual={qualified:.0f}sh fill={fills}"
    )


def _maker_loop(feed, portfolio, trader, cycles: int, interval: float, config: Config,
                say, log) -> None:
    """Two-sided bid ladder: re-quote on mid drift, fill when the ask trades down."""
    cycle = 0
    last_rest_at = 0.0
    last_summary_at = 0.0
    while cycles == 0 or cycle < cycles:
        cycle += 1
        scan_started_at = time.monotonic()
        if scan_started_at - last_rest_at >= interval:
            last_rest_at = scan_started_at
            _check_settlements(feed, portfolio, log)
            _refresh_account_positions(feed, portfolio, config, say, log)
        try:
            snapshots = feed.get_snapshots()
            for diagnostic in getattr(feed, "diagnostics", []):
                log(f"[FEED] {diagnostic}")
            portfolio.update_market_marks(snapshots)
        except RuntimeError as error:
            say(f"Market scan failed: {error}")
            if cycles == 0 or cycle < cycles:
                time.sleep(interval)
                continue
            break

        portfolio.drop_maker_quotes({snapshot.market_id for snapshot in snapshots})
        orders, lines, fills_total = [], [], 0
        for snapshot in snapshots:
            mid_up = mid_up_price(snapshot)
            if mid_up is None:
                log(f"[MAKER SKIP] market={snapshot.market_id} no two-sided mid")
                continue
            ladder = quote_ladder(
                snapshot,
                offset_cents=config.quote_offset_cents,
                levels=config.ladder_levels,
                step_cents=config.ladder_step_cents,
                level_shares=config.level_shares,
                skew_cents=_inventory_skew(portfolio, snapshot, config),
                low_price_shrink_below=config.low_price_shrink_below,
                low_price_shrink_factor=config.low_price_shrink_factor,
            )
            # Fill the ladder that was resting through this book first: the mid
            # has already moved, so those bids are the ones a taker just hit.
            fills = portfolio.collect_maker_fills(
                snapshot, budget=_market_budget(portfolio, config),
                queue_ramp=config.queue_ramp,
                max_fill_distance=config.max_fill_distance)
            for level in fills:
                orders.append(_maker_opportunity(snapshot, level, mid_up))
            # Settle the reward clock for the ladder that just rested before it
            # is replaced, otherwise a re-quote would reset the interval to zero.
            portfolio.accrue_maker_rewards(
                snapshot,
                spread_threshold=config.reward_spread_threshold,
                share_threshold=config.reward_share_threshold,
            )
            requoted = portfolio.sync_maker_quotes(
                snapshot, ladder, mid_up=mid_up, requote_ticks=config.requote_ticks,
                replenish_delay=config.replenish_delay,
            )
            # Balance inventory by taking the side we are short of, like the
            # wallet's aggressive fills, so the Up/Down ratio stays near 0.97.
            position = portfolio.open_positions.get(snapshot.market_id)
            if position:
                take, reason = evaluate_rebalance_take(
                    snapshot, position=position,
                    risk_budget=_market_budget(portfolio, config),
                    trigger_shares=config.rebalance_trigger_shares,
                    take_shares=config.rebalance_take_shares,
                    min_order_cost=config.minimum_order_cost,
                ) if _rebalance_ready(config, snapshot) else (None, "rebalance: cooling down")
                if take:
                    orders.append(take)
                    _REBALANCE_LAST[snapshot.market_id] = time.monotonic()
                    log(f"[REBAL TAKE] market={snapshot.market_id} {reason}")
                else:
                    log(f"[REBAL WAIT] market={snapshot.market_id} {reason}")
            fills_total += len(fills)
            if fills:
                log(f"[MAKER FILL] market={snapshot.market_id} {len(fills)} level(s) "
                    f"{'requoted' if requoted else 'held'}")
            lines.append(_maker_line(snapshot, mid_up,
                                     portfolio.maker_quotes.get(snapshot.market_id), len(fills),
                                     config.reward_spread_threshold))

        status = feed.ws_status() if hasattr(feed, "ws_status") else "ws=off"
        say(f"{'' if cycle == 1 else chr(10)}--- Cycle {cycle} ---  {status}")
        for line in lines:
            say(line)
        if orders:
            say(f"* Executing {len(orders)} maker fills in parallel...")
            _execute_orders(trader, orders, maker=True)
        else:
            log("No maker fills this cycle")
        log(f"Cash remaining: ${portfolio.cash:.2f} | qualified {portfolio.maker_qualified_seconds:.0f}s")
        if cycles == 0 or cycle < cycles:
            if time.monotonic() - last_summary_at >= interval:
                last_summary_at = time.monotonic()
                portfolio.write_summary()
            if not _wait_for_next_scan(feed, interval, config, scan_started_at):
                say(f"Next cycle in {interval:g}s...")
    stats = portfolio.maker_pair_stats()
    if stats["pair_cost"] is not None:
        say(f"Maker pairs: {stats['pairs']} ({stats['quantity']:.0f} shares) "
            f"pair cost {stats['pair_cost']:.4f} | unmatched {stats['unmatched']} fills")
    else:
        say(f"Maker pairs: 0 matched | {stats['unmatched']} unmatched fills")
    portfolio.write_summary()
    say(f"Summary written to {portfolio.summary_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Predict.fun paper trading bot")
    parser.add_argument("--cycles", type=int, default=0, help="number of scans; 0 runs continuously")
    parser.add_argument("--interval", type=float, default=12.0,
                        help="max seconds between scans; a pushed book rescans sooner")
    parser.add_argument("--min-scan", type=float, default=Config.min_scan_interval,
                        help="minimum seconds between WebSocket-driven scans")
    parser.add_argument("--mode", choices=("maker", "taker"), default="maker",
                        help="maker: two-sided bid ladder; taker: directional entries")
    parser.add_argument("--offset", type=float, default=Config.quote_offset_cents,
                        help="maker top level distance below the mid, in cents")
    parser.add_argument("--levels", type=int, default=Config.ladder_levels,
                        help="maker ladder depth per side")
    parser.add_argument("--level-shares", type=float, default=Config.level_shares,
                        help="shares rested on each maker level")
    parser.add_argument("--queue-ramp", type=float, default=Config.queue_ramp,
                        help="seconds for a fresh maker order to earn full queue share")
    parser.add_argument("--replenish-delay", type=float, default=Config.replenish_delay,
                        help="seconds before a taken maker tick may re-quote")
    parser.add_argument("--burst-levels", type=float, default=Config.burst_levels,
                        help="levels one snapshot may take per side")
    parser.add_argument("--burst-min-shares", type=float, default=Config.burst_min_shares,
                        help="minimum print size that counts as a hit on our level")
    parser.add_argument("--assets", default=",".join(Config.assets),
                        help="comma-separated assets, e.g. BTC,ETH,BNB")
    parser.add_argument("--windows", default=",".join(str(w) for w in Config.windows_minutes),
                        help="comma-separated window minutes, e.g. 5,15")
    parser.add_argument("--shares", type=float, default=Config.fixed_shares,
                        help="base net shares per order; increased when needed for the $1 minimum")
    parser.add_argument("--starting-cash", type=float, default=Config.starting_cash,
                        help="starting paper cash (does not reset an existing ledger)")
    parser.add_argument("--data-dir", default=Config.data_dir, help="paper ledger directory")
    args = parser.parse_args()
    args.assets = tuple(asset.strip().upper() for asset in args.assets.split(",") if asset.strip())
    try:
        args.windows = tuple(int(window) for window in args.windows.split(",") if window.strip())
    except ValueError:
        parser.error("--windows must be a comma-separated list of integers")
    if not args.assets or not args.windows:
        parser.error("--assets and --windows must not be empty")
    if args.cycles < 0 or args.interval <= 0:
        parser.error("--cycles must be >= 0 and --interval must be > 0")
    if not all(math.isfinite(value) and value > 0
               for value in (args.shares, args.starting_cash, args.interval)):
        parser.error("--shares, --starting-cash and --interval must be finite and positive")
    if not math.isfinite(args.min_scan) or args.min_scan <= 0:
        parser.error("--min-scan must be finite and positive")
    if not math.isfinite(args.offset) or args.offset < 0:
        parser.error("--offset must be finite and >= 0")
    if args.levels < 1:
        parser.error("--levels must be >= 1")
    if not math.isfinite(args.level_shares) or args.level_shares <= 0:
        parser.error("--level-shares must be finite and positive")
    if not math.isfinite(args.queue_ramp) or args.queue_ramp < 0:
        parser.error("--queue-ramp must be finite and >= 0")
    if not math.isfinite(args.replenish_delay) or args.replenish_delay < 0:
        parser.error("--replenish-delay must be finite and >= 0")
    if args.min_scan > args.interval:
        parser.error("--min-scan must not exceed --interval")
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        config = replace(
            Config(), fixed_shares=args.shares, starting_cash=args.starting_cash,
            data_dir=args.data_dir, assets=args.assets, windows_minutes=args.windows,
            min_scan_interval=args.min_scan, maker_mode=args.mode == "maker",
            quote_offset_cents=args.offset, ladder_levels=args.levels,
            level_shares=args.level_shares, queue_ramp=args.queue_ramp,
            replenish_delay=args.replenish_delay, burst_levels=args.burst_levels,
            burst_min_shares=args.burst_min_shares,
        )
        run(args.cycles, args.interval, config)
    except KeyboardInterrupt:
        print("\nStopped by user")
