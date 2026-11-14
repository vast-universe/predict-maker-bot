import argparse
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from config import Config
from markets import PredictMarketFeed, valid_price
from paper_portfolio import PaperPortfolio
from strategy import (
    Opportunity,
    fund_ladders,
    mid_up_price,
    quote_ladder,
    risk_adjusted_ladder,
)
from trader import LiveBook, LiveTrader, PaperTrader


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
    floor = min(max(0.0, config.min_scan_interval - elapsed),
                max(0.0, interval - elapsed))
    if floor > 0:
        time.sleep(floor)
    budget = max(0.0, interval - elapsed - floor)
    waiter = getattr(feed, "wait_for_update", None)
    wait_started_at = time.monotonic()
    if budget > 0 and callable(waiter) and waiter(budget):
        if config.scan_debounce > 0:
            # Let a burst of book updates settle before pricing off it.
            time.sleep(min(config.scan_debounce,
                           max(0.0, interval - (time.monotonic() - scan_started_at))))
        return True
    # A missing/disconnected WebSocket waiter can return immediately. Preserve
    # the REST scan interval instead of spinning at min_scan (or zero).
    unspent = budget - (time.monotonic() - wait_started_at)
    if unspent > 0:
        time.sleep(unspent)
    return False


def _cutoff_wait_interval(snapshots, config, interval, snapshots_at, scan_started_at):
    """Wake at risk cutoffs even when an otherwise quiet book never updates."""
    elapsed = time.monotonic() - snapshots_at
    delays = [snapshot.seconds_left - cutoff - elapsed
              for snapshot in snapshots if snapshot.seconds_left is not None
              for cutoff in (config.risk_stop_new_seconds, config.risk_stop_all_seconds)
              if snapshot.seconds_left > cutoff]
    if not delays:
        return interval
    deadline = time.monotonic() - scan_started_at + max(0.0, min(delays))
    return min(interval, deadline)


def run(cycles: int, interval: float, config: Config) -> None:
    feed = PredictMarketFeed(
        config.assets,
        config.windows_minutes,
        api_key=config.predict_api_key,
        base_url=config.predict_base_url,
    )
    live_book = None
    try:
        if config.live_trading:
            missing = [name for name, value in (
                ("PREDICT_API_KEY", config.predict_api_key),
                ("PREDICT_ACCOUNT_ADDRESS", config.predict_account_address),
                ("PRIVY_WALLET_PRIVATE_KEY", config.privy_wallet_private_key),
            ) if not value]
            if missing:
                raise RuntimeError(f"Live trading needs {', '.join(missing)}")
            live = LiveTrader(
                api_key=config.predict_api_key,
                account_address=config.predict_account_address,
                private_key=config.privy_wallet_private_key,
                base_url=config.predict_base_url,
            )
            if live.open_orders():
                raise RuntimeError(
                    "Live account already has OPEN orders. Resolve them before starting; "
                    "this process does not adopt or cancel pre-existing orders."
                )
            live_book = LiveBook(live, log=print)
            print("Live trading ENABLED: resting ladder will be sent as real orders")
        portfolio = PaperPortfolio(
            config.starting_cash, Path(config.data_dir), "predict",
            replenish_delay=config.replenish_delay,
        )
        trader = PaperTrader(portfolio)

        def log(message: str) -> None:
            """Diagnostics stay off the terminal to match the compact console."""
            return None

        def say(message: str) -> None:
            print(message, flush=True)
            log(message)

        # Give the socket a bounded head start so the first scan prices off the
        # stream instead of falling back to REST on every cold start.
        warm_up = getattr(feed, "warm_up", None)
        if callable(warm_up):
            warm_up(config.ws_warmup_seconds)
        _maker_loop(feed, portfolio, trader, cycles, interval, config, say, log,
                    live_book=live_book)
    finally:
        if live_book is not None:
            live_book.drop(set())
        close = getattr(feed, "close", None)
        if callable(close):
            close()


def _market_budget(portfolio: PaperPortfolio, config: Config, market_id: str) -> float:
    """Additional cash this market may spend, net of its already paid cost."""
    committed = float((portfolio.open_positions.get(market_id) or {}).get("cost", 0.0))
    return max(0.0, min(_market_cost_limit(config) - committed, portfolio.cash))


def _market_cost_limit(config: Config) -> float:
    return min(config.risk_max_market_cost, config.maker_market_budget
               if config.maker_market_budget is not None else float("inf"))


def _book_fresh(snapshot, max_age: float = 15.0) -> bool:
    """Never submit from an old or unparseable exchange observation."""
    try:
        observed = datetime.fromisoformat(snapshot.observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return False
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        return math.isfinite(age) and -5.0 <= age <= max_age
    except (ValueError, TypeError, AttributeError):
        return False


def _inventory_skew(position: dict, snapshot, config: Config) -> float:
    """Shift quotes against the current net position to mean-revert inventory."""
    if not position:
        return 0.0
    net = float(position.get("up_quantity", 0.0)) - float(position.get("down_quantity", 0.0))
    shares = config.level_shares_15m if snapshot.window_minutes == 15 else config.level_shares
    scale = max(1.0, shares * max(1, config.ladder_levels))
    ratio = max(-1.0, min(1.0, net / scale))
    return ratio * config.skew_cents_max


def _guard_ladder(snapshot, ladder, position, config, history, cooldowns, *,
                  now: float, live: bool = False):
    """Update shock state, then apply the same inventory policy in both modes."""
    market_id = snapshot.market_id
    previous = history.get(market_id)
    history[market_id] = mid_up_price(snapshot)
    adjusted, reason = risk_adjusted_ladder(
        snapshot, ladder, position=position,
        max_net_shares=config.risk_max_net_shares,
        pair_margin=config.risk_pair_margin,
        max_market_cost=_market_cost_limit(config),
        stop_new_seconds=config.risk_stop_new_seconds,
        stop_all_seconds=config.risk_stop_all_seconds,
        shock_cents=config.risk_shock_cents, history_mid=previous,
        min_order_cost=config.minimum_order_cost,
        fee_rate_bps=snapshot.fee_rate_bps if live else 0.0,
    )
    if "midpoint shock" in reason:
        cooldowns[market_id] = now + config.risk_shock_cooldown
    if now < cooldowns.get(market_id, 0.0):
        return {"UP": [], "DOWN": []}, "risk: shock cooldown; cancel all bids"
    return {side: [level for level in levels
                   if config.quote_min_price - 1e-9 <= level.price <= config.quote_max_price + 1e-9]
            for side, levels in adjusted.items()}, reason


def _inventory_line(position: dict) -> str:
    up = float(position.get("up_quantity", 0.0))
    down = float(position.get("down_quantity", 0.0))
    cost = float(position.get("cost", 0.0))
    # Outcome-independent settlement bounds, excluding rewards and gas.
    return (f"net={up-down:+.2f}sh "
            f"settlePnL=[{min(up, down)-cost:+.2f},{max(up, down)-cost:+.2f}]")


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
                spread_threshold: float = 0.06,
                share_threshold: float = 100.0) -> str:
    """One console line per market: mid, both ladders, reward-qualified size."""
    resting = state.unfilled() if state else []
    up_prices = [f"{level.price:.2f}" for level in resting if level.side == "UP"]
    down_prices = [f"{level.price:.2f}" for level in resting if level.side == "DOWN"]
    qualified = sum(
        level.shares for level in resting
        if level.shares + 1e-9 >= share_threshold
        if abs(level.price - (mid_up if level.side == "UP" else 1.0 - mid_up))
        <= spread_threshold + 1e-12
    )
    seconds = int(max(0.0, snapshot.seconds_left or 0.0))
    return (
        f"[{snapshot.asset} {snapshot.window_minutes}m] {_short_title(snapshot)} @{seconds}s "
        f"mid={mid_up:.2f} | Up {'/'.join(up_prices) or '-'} "
        f"| Down {'/'.join(down_prices) or '-'} | qual={qualified:.0f}sh fill={fills}"
    )



def _maker_loop(feed, portfolio, trader, cycles: int, interval: float, config: Config,
                say, log, live_book=None) -> None:
    """Buy passive inventory under cost/exposure limits; hold it to settlement."""
    cycle = 0
    last_rest_at = 0.0
    last_summary_at = 0.0
    history, cooldowns = {}, {}
    seen_markets, resolution_checked_at = set(), {}
    last_seen = {}
    while cycles == 0 or cycle < cycles:
        cycle += 1
        scan_started_at = time.monotonic()
        if scan_started_at - last_rest_at >= interval:
            last_rest_at = scan_started_at
            if live_book is None:
                _check_settlements(feed, portfolio, log)
                _refresh_account_positions(feed, portfolio, config, say, log)
        try:
            snapshots = feed.get_snapshots()
            snapshots_at = time.monotonic()
            for diagnostic in getattr(feed, "diagnostics", []):
                log(f"[FEED] {diagnostic}")
            portfolio.update_market_marks(snapshots)
        except RuntimeError as error:
            portfolio.drop_maker_quotes(set())
            if live_book is not None:
                live_book.drop(set())
            for market_id in history:
                cooldowns[market_id] = time.monotonic() + config.risk_shock_cooldown
            say(f"Market scan failed; withdrawing quotes: {error}")
            if cycles == 0 or cycle < cycles:
                _wait_for_next_scan(feed, interval, config, scan_started_at)
            continue

        live_ids = {snapshot.market_id for snapshot in snapshots}
        # A missing book is not proof a round ended. Preserve its midpoint and
        # re-enter a full cooldown on data return; bound old history by round age.
        for market_id in set(history) - live_ids:
            cooldowns[market_id] = scan_started_at + config.risk_shock_cooldown
        last_seen.update({market_id: scan_started_at for market_id in live_ids})
        retention = max(config.windows_minutes) * 60 + config.risk_shock_cooldown + 60
        keep_history = {key for key, seen_at in last_seen.items()
                        if scan_started_at - seen_at <= retention}
        history = {key: value for key, value in history.items() if key in keep_history}
        cooldowns = {key: value for key, value in cooldowns.items() if key in keep_history}
        last_seen = {key: value for key, value in last_seen.items() if key in keep_history}
        portfolio.drop_maker_quotes(live_ids)
        if live_book is not None:
            try:
                for snapshot in snapshots:
                    if (snapshot.settlements_pending is not None
                            and _book_fresh(snapshot, getattr(feed, "quote_max_age", 15.0))):
                        live_book.observe_pending(
                            snapshot.market_id, clear=not snapshot.settlements_pending,
                            timestamp_ms=snapshot.book_timestamp_ms)
                live_book.drop(live_ids)
                if not live_book.refresh():
                    raise RuntimeError("tracked order statuses are not confirmed")
                seen_markets.update(live_ids)
                for market_id in sorted(seen_markets - live_ids):
                    if scan_started_at - resolution_checked_at.get(market_id, 0.0) < interval:
                        continue
                    resolution_checked_at[market_id] = scan_started_at
                    if feed.get_resolution(market_id) and live_book.resolve_market(market_id):
                        seen_markets.discard(market_id)
                rows = feed.get_positions_by_address(config.predict_account_address)
                tokens = {item.market_id: {"UP": item.up_token_id, "DOWN": item.down_token_id}
                          for item in snapshots}
                if not live_book.reconcile_positions(rows, token_ids_by_market=tokens):
                    raise RuntimeError("positions have not caught up with confirmed fills")
                portfolio.update_account_positions(rows)
                collateral = live_book.live.collateral_balance()
                # Filled inventory remains deployed until official resolution.
                # Never reset this cap to the full cash budget every scan.
                global_room = max(0.0, config.starting_cash - live_book.total_position_cost())
                quote_cash = min(global_room, collateral)
            except RuntimeError as error:
                live_book.drop(set())
                portfolio.set_account_positions_error(str(error))
                say(f"[LIVE] Inventory/balance not reconciled; withdrawing quotes: {error}")
                if cycles == 0 or cycle < cycles:
                    _wait_for_next_scan(feed, interval, config, scan_started_at)
                continue
        else:
            paid = sum(float(position.get("cost", 0.0))
                       for position in portfolio.open_positions.values())
            quote_cash = min(portfolio.cash, max(0.0, config.starting_cash - paid))

        status = feed.ws_status() if hasattr(feed, "ws_status") else "ws=off"
        say(f"{'' if cycle == 1 else chr(10)}--- Cycle {cycle} ---  {status}")
        candidates, mids, fills_by_market, reasons, positions = {}, {}, {}, {}, {}
        for snapshot in snapshots:
            market_id = snapshot.market_id
            mid_up = mid_up_price(snapshot)
            candidates[market_id] = {"UP": [], "DOWN": []}
            fills_by_market[market_id] = 0
            if mid_up is None or not _book_fresh(snapshot, getattr(feed, "quote_max_age", 15.0)):
                cooldowns[market_id] = time.monotonic() + config.risk_shock_cooldown
                reasons[market_id] = "risk: stale or missing book; cancel all bids"
                continue
            mids[market_id] = mid_up
            spread = (snapshot.reward_spread_threshold
                      if snapshot.reward_spread_threshold is not None
                      else config.reward_spread_threshold)
            threshold = (snapshot.reward_share_threshold
                         if snapshot.reward_share_threshold is not None
                         else config.reward_share_threshold)
            if live_book is None:
                # Old quotes can trade before we observe a jump and cancel them.
                # Book fills first; the shock guard must not erase adverse fills.
                portfolio.accrue_maker_rewards(
                    snapshot, spread_threshold=spread, share_threshold=threshold)
                fills = portfolio.collect_maker_fills(
                    snapshot, budget=_market_budget(portfolio, config, market_id))
                fills_by_market[market_id] = _execute_orders(
                    trader, [_maker_opportunity(snapshot, level, mid_up) for level in fills],
                    maker=True)
                position = portfolio.open_positions.get(market_id) or {}
            else:
                position = live_book.position_state(market_id)
            positions[market_id] = position
            anchor_mid = mid_up
            state = portfolio.maker_quotes.get(market_id) if live_book is None else None
            if state and abs(mid_up - state.mid_up) * 10 ** snapshot.decimal_precision + 1e-9 < max(1, config.requote_ticks):
                anchor_mid = state.mid_up
            raw_ladder = quote_ladder(
                snapshot,
                offset_cents=config.quote_offset_cents,
                levels=config.ladder_levels,
                step_cents=config.ladder_step_cents,
                level_shares=(config.level_shares_15m if snapshot.window_minutes == 15
                              else config.level_shares),
                level_notional=config.level_notional,
                min_price=config.quote_min_price,
                max_price=config.quote_max_price,
                skew_cents=_inventory_skew(position, snapshot, config),
                min_order_cost=config.minimum_order_cost,
                anchor_mid_up=anchor_mid,
            )
            timed_snapshot = replace(snapshot, seconds_left=(
                max(0.0, snapshot.seconds_left - (time.monotonic() - snapshots_at))
                if snapshot.seconds_left is not None else None))
            candidates[market_id], reasons[market_id] = _guard_ladder(
                timed_snapshot, raw_ladder, position, config, history, cooldowns,
                now=time.monotonic(), live=live_book is not None)

        if live_book is not None:
            # Orders outside this scan may still be locked or pending removal.
            inactive = sum(live_book.reserved_cost(market_id)
                           for market_id in live_book.orders if market_id not in candidates)
            quote_cash = max(0.0, quote_cash - inactive)
            caps = {market_id: max(0.0, _market_cost_limit(config)
                                  - live_book.inventory_cost(market_id))
                    for market_id in candidates}
            remaining = {
                market_id: {(order["side"], order["price"]): order.get("remaining", order["shares"])
                            for order in live_book.orders.get(market_id, [])
                            if order.get("remaining", order["shares"]) <= next(
                                (level.shares for level in candidates[market_id].get(order["side"], [])
                                 if level.price == order["price"]), -1) + 1e-9}
                for market_id in candidates}
        else:
            paid = sum(float(position.get("cost", 0.0))
                       for position in portfolio.open_positions.values())
            quote_cash = min(portfolio.cash, max(0.0, config.starting_cash - paid))
            caps = {market_id: _market_budget(portfolio, config, market_id)
                    for market_id in candidates}
            remaining = {
                market_id: {(level.side, level.price): level.shares
                            for level in state.unfilled()
                            if abs((level.target_shares or level.shares)
                                   - next((target.shares for target in candidates.get(market_id, {}).get(level.side, [])
                                           if target.price == level.price), -1)) <= 1e-9}
                for market_id, state in portfolio.maker_quotes.items()}
        ladders = fund_ladders(candidates, cash=quote_cash, market_budgets=caps,
                               remaining_shares=remaining)
        for snapshot in snapshots:
            market_id = snapshot.market_id
            ladder = ladders[market_id]
            mid_up = mids.get(market_id)
            seconds_left = (snapshot.seconds_left - (time.monotonic() - snapshots_at)
                            if snapshot.seconds_left is not None else 0.0)
            if seconds_left <= config.risk_stop_new_seconds:
                # Earlier markets' REST work may cross a cutoff after initial
                # funding. Reapply the stricter tail policy immediately.
                timed_snapshot = replace(snapshot, seconds_left=max(0.0, seconds_left))
                ladder, _ = _guard_ladder(
                    timed_snapshot, ladder, positions.get(market_id, {}), config, {}, {},
                    now=time.monotonic(), live=live_book is not None)
            if live_book is not None:
                token_ids = {"UP": snapshot.up_token_id, "DOWN": snapshot.down_token_id}
                if (not all(token_ids.values()) or mid_up is None
                        or not _book_fresh(snapshot, getattr(feed, "quote_max_age", 15.0))
                        or seconds_left <= config.risk_stop_all_seconds):
                    ladder = {"UP": [], "DOWN": []}
                other_reserved = live_book.reserved_cost() - live_book.reserved_cost(market_id)
                live_book.sync(
                    market_id, token_ids, ladder,
                    fee_rate_bps=snapshot.fee_rate_bps,
                    is_neg_risk=snapshot.is_neg_risk,
                    is_yield_bearing=snapshot.is_yield_bearing,
                    budget=max(0.0, min(caps[market_id],
                                        min(global_room, collateral) - other_reserved)),
                    expires_at=time.time() + max(0.0, seconds_left),
                    stop_new_seconds=config.risk_stop_new_seconds,
                    stop_all_seconds=config.risk_stop_all_seconds,
                )
                say(f"[{snapshot.asset} {snapshot.window_minutes}m] LIVE market={market_id} "
                    f"reserved=${live_book.reserved_cost(market_id):.2f} "
                    f"{_inventory_line(positions.get(market_id, {}))} | {reasons[market_id]}")
                continue
            if mid_up is None:
                portfolio.maker_quotes.pop(market_id, None)
                say(f"[{snapshot.asset} {snapshot.window_minutes}m] {reasons[market_id]}")
                continue
            portfolio.sync_maker_quotes(
                snapshot, ladder, mid_up=mid_up, requote_ticks=config.requote_ticks,
                replenish_delay=config.replenish_delay,
            )
            spread = (snapshot.reward_spread_threshold
                      if snapshot.reward_spread_threshold is not None
                      else config.reward_spread_threshold)
            threshold = (snapshot.reward_share_threshold
                         if snapshot.reward_share_threshold is not None
                         else config.reward_share_threshold)
            say(_maker_line(snapshot, mid_up, portfolio.maker_quotes.get(market_id),
                            fills_by_market[market_id], spread, threshold)
                + f" | {_inventory_line(positions.get(market_id, {}))} | {reasons[market_id]}")
        if cycles == 0 or cycle < cycles:
            if live_book is None and time.monotonic() - last_summary_at >= interval:
                last_summary_at = time.monotonic()
                portfolio.write_summary()
            wait_interval = _cutoff_wait_interval(
                snapshots, config, interval, snapshots_at, scan_started_at)
            if not _wait_for_next_scan(feed, wait_interval, config, scan_started_at):
                say(f"Next cycle in {interval:g}s...")
    if live_book is not None:
        return
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
    parser.add_argument("--live", action="store_true",
                        help="place real orders (needs PREDICT_API_KEY, "
                             "PREDICT_ACCOUNT_ADDRESS, PRIVY_WALLET_PRIVATE_KEY)")
    parser.add_argument("--offset", type=float, default=Config.quote_offset_cents,
                        help="maker top level distance below the mid, in cents")
    parser.add_argument("--levels", type=int, default=Config.ladder_levels,
                        help="maker ladder depth per side")
    parser.add_argument("--level-shares", type=float, default=Config.level_shares,
                        help="shares per level except 15-minute rounds (default: 100)")
    parser.add_argument("--15m-level-shares", dest="level_shares_15m", type=float,
                        default=Config.level_shares_15m,
                        help="shares per level for 15-minute rounds (default: 40)")
    parser.add_argument("--level-notional", type=float, default=Config.level_notional,
                        help="USDT per level (shares = notional / price); 0 uses --level-shares")
    parser.add_argument("--quote-min", type=float, default=Config.quote_min_price,
                        help="lowest price the ladder may rest at")
    parser.add_argument("--quote-max", type=float, default=Config.quote_max_price,
                        help="highest price the ladder may rest at")
    parser.add_argument("--replenish-delay", type=float, default=Config.replenish_delay,
                        help="seconds before a taken maker tick may re-quote")
    parser.add_argument("--skew-cents", type=float, default=Config.skew_cents_max,
                        help="optional inventory price skew in cents (both modes)")
    parser.add_argument("--rebalance-trigger", type=float,
                        default=Config.rebalance_trigger_shares,
                        help="legacy option; must stay 0 in the buy-only Maker strategy")
    parser.add_argument("--max-net-shares", type=float, default=Config.risk_max_net_shares,
                        help="maximum worst-case one-sided inventory in shares")
    parser.add_argument("--pair-margin", type=float, default=Config.risk_pair_margin,
                        help="minimum planned margin per paired net share, in USDT")
    parser.add_argument("--max-market-cost", type=float, default=Config.risk_max_market_cost,
                        help="paid inventory plus all bids per market, in USDT")
    parser.add_argument("--stop-new-seconds", type=float, default=Config.risk_stop_new_seconds,
                        help="only buy the deficient side within this expiry cutoff")
    parser.add_argument("--stop-all-seconds", type=float, default=Config.risk_stop_all_seconds,
                        help="cancel all bids within this expiry cutoff")
    parser.add_argument("--shock-cents", type=float, default=Config.risk_shock_cents,
                        help="midpoint move between scans that withdraws quotes; 0 disables")
    parser.add_argument("--shock-cooldown", type=float, default=Config.risk_shock_cooldown,
                        help="seconds to wait after a shock or stale book")
    parser.add_argument("--assets", default=",".join(Config.assets),
                        help="comma-separated assets, e.g. BTC,ETH,BNB")
    parser.add_argument("--windows", default=",".join(str(w) for w in Config.windows_minutes),
                        help="comma-separated window minutes, e.g. 5,15")
    parser.add_argument("--starting-cash", type=float, default=Config.starting_cash,
                        help="initial paper cash and global deployed-cost cap in either mode")
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
               for value in (args.starting_cash, args.interval)):
        parser.error("--starting-cash and --interval must be finite and positive")
    if not math.isfinite(args.min_scan) or args.min_scan < 0:
        parser.error("--min-scan must be finite and >= 0 (0 scans on every update)")
    if not math.isfinite(args.offset) or args.offset < 0:
        parser.error("--offset must be finite and >= 0")
    if args.levels < 1:
        parser.error("--levels must be >= 1")
    if not all(math.isfinite(value) and value > 0
               for value in (args.level_shares, args.level_shares_15m)):
        parser.error("share quantities must be finite and positive")
    if not math.isfinite(args.level_notional) or args.level_notional < 0:
        parser.error("--level-notional must be finite and >= 0")
    if not (math.isfinite(args.quote_min) and math.isfinite(args.quote_max)
            and 0 < args.quote_min < args.quote_max < 1):
        parser.error("quote limits must satisfy 0 < --quote-min < --quote-max < 1")
    if not math.isfinite(args.skew_cents) or args.skew_cents < 0:
        parser.error("--skew-cents must be finite and >= 0")
    if not math.isfinite(args.rebalance_trigger) or args.rebalance_trigger < 0:
        parser.error("--rebalance-trigger must be finite and >= 0")
    if args.rebalance_trigger != 0:
        parser.error("--rebalance-trigger must be 0; active strategy never lifts an ask deliberately")
    if not all(math.isfinite(value) and value > 0
               for value in (args.max_net_shares, args.max_market_cost)):
        parser.error("--max-net-shares and --max-market-cost must be finite and positive")
    if not math.isfinite(args.pair_margin) or not 0 <= args.pair_margin < 1:
        parser.error("--pair-margin must be finite and in [0,1)")
    if (not all(math.isfinite(value) and value >= 0 for value in (
            args.stop_new_seconds, args.stop_all_seconds, args.shock_cents, args.shock_cooldown))
            or args.stop_all_seconds > args.stop_new_seconds):
        parser.error("risk cutoffs must be finite, nonnegative and stop-all <= stop-new")
    if not math.isfinite(args.replenish_delay) or args.replenish_delay < 0:
        parser.error("--replenish-delay must be finite and >= 0")
    if args.min_scan > args.interval:
        parser.error("--min-scan must not exceed --interval")
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        config = replace(
            Config(), starting_cash=args.starting_cash,
            data_dir=args.data_dir, assets=args.assets, windows_minutes=args.windows,
            min_scan_interval=args.min_scan,
            quote_offset_cents=args.offset, ladder_levels=args.levels,
            level_shares=args.level_shares, level_shares_15m=args.level_shares_15m,
            level_notional=args.level_notional,
            quote_min_price=args.quote_min, quote_max_price=args.quote_max,
            replenish_delay=args.replenish_delay,
            live_trading=args.live, skew_cents_max=args.skew_cents,
            rebalance_trigger_shares=args.rebalance_trigger,
            risk_max_net_shares=args.max_net_shares,
            risk_pair_margin=args.pair_margin,
            risk_max_market_cost=args.max_market_cost,
            risk_stop_new_seconds=args.stop_new_seconds,
            risk_stop_all_seconds=args.stop_all_seconds,
            risk_shock_cents=args.shock_cents,
            risk_shock_cooldown=args.shock_cooldown,
        )
        run(args.cycles, args.interval, config)
    except KeyboardInterrupt:
        print("\nStopped by user")
