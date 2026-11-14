import csv
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from strategy import Opportunity, unhedged_cost_after_buy
from markets import valid_price


LOG_FIELDS = [
    "timestamp", "market_id", "market_title", "asset", "window_minutes", "side", "quantity",
    "up_ask", "down_ask", "ask", "fair_probability", "fee_per_share", "cost",
    "up_cost_per_share", "down_cost_per_share", "up_order_quantity", "down_order_quantity",
    "expected_edge", "lead_bps", "settled", "winner", "payout", "realized_pnl",
]


def _money(value: float) -> str:
    """Format a USDT amount as the summary shows it, e.g. $388.10 / -$12.34."""
    if value < 0:
        return f"-${abs(value):.2f}"
    return f"${value:.2f}"


def _signed_money(value: float) -> str:
    """Format a P&L amount with an explicit sign, e.g. +$0.48 / -$1.06."""
    if value < 0:
        return f"-${abs(value):.2f}"
    return f"+${value:.2f}"


@dataclass
class RestingLevel:
    """One simulated resting bid: price, size and whether it already traded."""

    side: str
    price: float
    shares: float
    filled: bool = False
    placed_at: float = 0.0


@dataclass
class MakerQuoteState:
    """The live two-sided ladder for one market, as the bot would rest it."""

    market_id: str
    mid_up: float
    levels: list[RestingLevel] = field(default_factory=list)
    quoted_at: float = 0.0
    reward_seconds: float = 0.0        # seconds with a reward-qualified ladder
    reward_share_seconds: float = 0.0  # qualified shares x seconds, for pool share
    last_bids: dict = field(default_factory=dict)   # side -> {price: size}
    last_asks: dict = field(default_factory=dict)

    def unfilled(self, side: str | None = None) -> list[RestingLevel]:
        return [
            level for level in self.levels
            if not level.filled and (side is None or level.side == side)
        ]


@dataclass
class PaperPortfolio:
    cash: float
    data_dir: Path
    source: str = "predict"
    total_cost: float = 0.0
    filled_trades: int = 0
    traded_market_ids: set[str] = field(default_factory=set)
    open_positions: dict[str, dict] = field(default_factory=dict)
    realized_pnl: float = 0.0
    market_marks: dict[str, dict[str, float | None]] = field(default_factory=dict)
    account_positions: list[dict] | None = None
    account_positions_error: str | None = None
    maker_quotes: dict[str, MakerQuoteState] = field(default_factory=dict)
    maker_fills: list[dict] = field(default_factory=list)
    replenish_delay: float = 8.0  # wallet refills a taken tick after ~8s (median)
    queue_ramp: float = 8.0  # seconds for a fresh order to reach full queue share
    queue_depth_floor: float = 269.0  # median depth resting at our tick (measured)
    burst_levels: float = 2.0  # levels one snapshot may take per side
    burst_min_shares: float = 25.0  # a print smaller than this is noise, not a hit
    burst_max_shares: float = 79.0  # shares one snapshot may take in total (measured burst)

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.data_dir / "pnl_log.csv"
        self.summary_path = self.data_dir / "pnl_summary.txt"
        self.state_path = self.data_dir / "portfolio.json"
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.cash = float(state["cash"])
            self.total_cost = float(state.get("total_cost", 0.0))
            self.filled_trades = int(state.get("filled_trades", state.get("filled_pairs", 0)))
            self.traded_market_ids.update(state.get("traded_market_ids", []))
            self.open_positions.update(state.get("open_positions", {}))
            self.realized_pnl = float(state.get("realized_pnl", 0.0))
        self._ensure_log_schema()
        with self.log_path.open("r", newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
            for row in rows:
                market_id = row.get("market_id")
                if market_id:
                    self.traded_market_ids.add(market_id)
        self._normalize_open_positions(rows)
        # Persist the opening state right away: otherwise a run with no fills
        # leaves no portfolio.json, and the ledger looks like it lost its state.
        if not self.state_path.exists():
            self._save_state()

    def _ensure_log_schema(self) -> None:
        if not self.log_path.exists():
            with self.log_path.open("w", newline="", encoding="utf-8") as file:
                csv.writer(file).writerow(LOG_FIELDS)
            return

        with self.log_path.open("r", newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            old_fields = reader.fieldnames or []
            if old_fields == LOG_FIELDS:
                return
            old_rows = list(reader)

        migrated_rows = []
        for row in old_rows:
            migrated = {field: row.get(field, "") for field in LOG_FIELDS}
            if not migrated.get("side") and row.get("up_ask") and row.get("down_ask"):
                migrated["side"] = "PAIR"
            migrated_rows.append(migrated)

        with self.log_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=LOG_FIELDS)
            writer.writeheader()
            writer.writerows(migrated_rows)

    def record_trade(self, opportunity: Opportunity, *, maker: bool = False) -> float:
        market = opportunity.market
        position = self.open_positions.get(market.market_id)
        if market.market_id in self.traded_market_ids and not maker:
            # Open markets may receive repeated fixed-size orders. Same-side
            # additions are allowed; opposite-side orders may only reduce the
            # current net position and cannot flip it. Maker fills are exempt:
            # a two-sided ladder deliberately buys whichever side trades.
            if not position or opportunity.side == "PAIR":
                return 0.0
            net_up_quantity = (
                float(position.get("up_quantity", 0.0))
                - float(position.get("down_quantity", 0.0))
            )
            if abs(net_up_quantity) > 1e-9:
                held_side = "UP" if net_up_quantity > 0 else "DOWN"
                if opportunity.side != held_side and opportunity.quantity > abs(net_up_quantity) + 1e-6:
                    return 0.0
        cost = opportunity.quantity * opportunity.cost_per_share
        if cost > self.cash + 1e-9:
            return 0.0
        self.cash -= cost
        self.total_cost += cost
        self.filled_trades += 1
        self.traded_market_ids.add(market.market_id)
        position = self.open_positions.setdefault(
            market.market_id,
            {"up_quantity": 0.0, "down_quantity": 0.0, "cost": 0.0,
             "up_cost": 0.0, "down_cost": 0.0,
             "asset": market.asset, "window_minutes": market.window_minutes},
        )
        position["unhedged_cost"] = unhedged_cost_after_buy(
            position, opportunity.side, opportunity.quantity, cost
        )
        if opportunity.side in ("UP", "PAIR"):
            position["up_quantity"] = float(position.get("up_quantity", 0.0)) + opportunity.quantity
            position["up_cost"] = float(position.get("up_cost", 0.0)) + (
                opportunity.quantity * (
                    opportunity.up_cost_per_share or opportunity.cost_per_share
                )
            )
        if opportunity.side in ("DOWN", "PAIR"):
            position["down_quantity"] = float(position.get("down_quantity", 0.0)) + opportunity.quantity
            position["down_cost"] = float(position.get("down_cost", 0.0)) + (
                opportunity.quantity * (
                    opportunity.down_cost_per_share or opportunity.cost_per_share
                )
            )
        position["cost"] = float(position.get("cost", 0.0)) + cost
        if maker:
            self.maker_fills.append({
                "market_id": market.market_id, "side": opportunity.side,
                "price": opportunity.ask, "quantity": opportunity.quantity,
                "at": time.monotonic(),
            })
        with self.log_path.open("a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(
                [market.observed_at, market.market_id, market.market_title or "",
                 market.asset, market.window_minutes,
                 opportunity.side, opportunity.quantity,
                 opportunity.up_ask if opportunity.up_ask is not None else "",
                 opportunity.down_ask if opportunity.down_ask is not None else "",
                 opportunity.ask, round(opportunity.fair_probability, 6),
                 round(opportunity.fee_per_share, 6), round(cost, 4),
                 round(opportunity.up_cost_per_share, 8),
                 round(opportunity.down_cost_per_share, 8),
                 round(opportunity.up_order_quantity, 4),
                 round(opportunity.down_order_quantity, 4),
                 round(opportunity.expected_edge_per_share * opportunity.quantity, 4),
                 round(opportunity.lead_bps, 3), "", "", "", ""]
            )
        self.state_path.write_text(
            json.dumps(
                {"cash": round(self.cash, 4), "total_cost": round(self.total_cost, 4),
                 "filled_trades": self.filled_trades,
                 "traded_market_ids": sorted(self.traded_market_ids),
                 "open_positions": self.open_positions,
                 "realized_pnl": round(self.realized_pnl, 4)},
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        self.write_summary()
        return cost

    def record_sell(self, opportunity: Opportunity) -> float:
        """Trim the unmatched leg of a hedged position and return the proceeds."""
        market = opportunity.market
        position = self.open_positions.get(market.market_id)
        if position is None or not opportunity.sell:
            return 0.0
        side = opportunity.side
        quantity_key = "up_quantity" if side == "UP" else "down_quantity"
        cost_key = "up_cost" if side == "UP" else "down_cost"
        held_quantity = float(position.get(quantity_key, 0.0))
        quantity = min(float(opportunity.quantity), held_quantity)
        if quantity <= 1e-9:
            return 0.0
        held_cost = float(position.get(cost_key, 0.0))
        average_cost = held_cost / held_quantity if held_quantity else 0.0
        proceeds = quantity * opportunity.ask
        self.cash += proceeds
        self.realized_pnl += quantity * (opportunity.ask - average_cost)
        self.filled_trades += 1
        position[quantity_key] = held_quantity - quantity
        position[cost_key] = max(0.0, held_cost - quantity * average_cost)
        position["cost"] = max(0.0, float(position.get("cost", 0.0)) - quantity * average_cost)
        # Trimming exactly the unmatched leg leaves the two sides balanced.
        position["unhedged_cost"] = 0.0
        with self.log_path.open("a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(
                [market.observed_at, market.market_id, market.market_title or "",
                 market.asset, market.window_minutes, f"SELL_{side}", quantity,
                 market.up_ask if market.up_ask is not None else "",
                 market.down_ask if market.down_ask is not None else "",
                 opportunity.ask, 0.0, 0.0, round(-proceeds, 6),
                 0.0, 0.0, quantity, quantity,
                 round(opportunity.expected_edge_per_share * quantity, 4),
                 round(opportunity.lead_bps, 3), "", "", "", ""]
            )
        self._save_state()
        self.write_summary()
        return proceeds

    # ------------------------------------------------------------ maker book
    def sync_maker_quotes(
        self,
        snapshot,
        ladder: dict[str, list],
        *,
        mid_up: float,
        requote_ticks: int = 1,
        replenish_delay: float = 8.0,
        now: float | None = None,
    ) -> bool:
        """Rest (or re-quote) the two-sided ladder for one market.

        Returns True when the ladder was replaced. The ladder is replaced only
        after the mid moves at least ``requote_ticks`` ticks, mirroring the
        wallet's measured behaviour (it re-quotes on drift, not on every tick).
        """
        now = time.monotonic() if now is None else now
        precision = max(0, int(getattr(snapshot, "decimal_precision", 3)))
        tick = 10 ** -precision
        state = self.maker_quotes.get(snapshot.market_id)
        fresh = {
            (side, round(level.price, precision)): level.shares
            for side, side_levels in ladder.items()
            for level in side_levels
        }
        if state is not None:
            drifted = abs(mid_up - state.mid_up) / tick
            if drifted + 1e-9 < max(1, requote_ticks):
                # The mid has not moved, so the ladder shape still holds: refill
                # any level that was taken, keeping the rest untouched. Waiting
                # for the whole ladder to empty would leave a visible hole in the
                # quote after a single fill, which the wallet never shows (it
                # refills a taken tick within seconds).
                replenished = False
                for level in state.levels:
                    key = (level.side, round(level.price, precision))
                    if key not in fresh:
                        continue
                    if not (level.filled or level.shares + 1e-9 < fresh[key]):
                        continue
                    # A re-placed order is not instantly live: the venue's speed
                    # bump delays removal and re-entry. Without a cooldown one
                    # tick could be eaten and refilled several times a second,
                    # which pushed the fill rate to 213/min against the wallet's
                    # 33/min.
                    if level.placed_at and now - level.placed_at < replenish_delay:
                        continue
                    level.filled = False
                    level.shares = fresh[key]
                    level.placed_at = now
                    replenished = True
                if replenished or state.unfilled():
                    return replenished
        # placed_at stays 0 on a fresh ladder: a newly rested order is live at
        # once, and only a *replacement* has to wait out the venue's delay.
        levels = [
            RestingLevel(side=side, price=level.price, shares=level.shares)
            for side, side_levels in ladder.items()
            for level in side_levels
        ]
        carried = state or MakerQuoteState(market_id=snapshot.market_id, mid_up=mid_up)
        self.maker_quotes[snapshot.market_id] = MakerQuoteState(
            market_id=snapshot.market_id, mid_up=mid_up, levels=levels, quoted_at=now,
            reward_seconds=carried.reward_seconds,
            reward_share_seconds=carried.reward_share_seconds,
            last_bids=carried.last_bids, last_asks=carried.last_asks,
        )
        return True

    def collect_maker_fills(self, snapshot, *, budget: float | None = None,
                            queue_ramp: float | None = None,
                            max_fill_distance: float | None = None) -> list[RestingLevel]:
        """Fill resting bids from book depth, sharing each print by queue order.

        Our simulated order is not in the real book, so a depth change at a tick
        is the whole market trading there rather than our share of it. The share
        we take is our size against the depth normally resting at that tick, then
        limited by the per-burst caps. Booking the full print instead let a thin
        tick hand most of its volume to us (280 fills/min against the wallet's
        33), and waiting for the tick to clear made every fill land after the
        price had already moved (pair cost 1.05).
        """
        state = self.maker_quotes.get(snapshot.market_id)
        if state is None:
            return []
        traded, previous_depth = self._traded_shares(state, snapshot)
        precision = max(0, int(getattr(snapshot, "decimal_precision", 3)))
        committed = float((self.open_positions.get(snapshot.market_id) or {}).get("cost", 0.0))
        now_mono = time.monotonic()
        queue_ramp = self.queue_ramp if queue_ramp is None else queue_ramp
        fills = []
        # A single 3-second snapshot merges several takers into one print, which
        # made one scan sweep six levels a side and produce 20 fills per burst
        # against the wallet's 9.8. Cap how many levels one snapshot may take,
        # closest to the mid first (that is where a print consumes first).
        per_side_cap = max(1, int(round(self.burst_levels))) if self.burst_levels else None
        burst_cap = self.burst_max_shares if self.burst_max_shares else None
        burst_taken = 0.0
        taken_per_side = {"UP": 0, "DOWN": 0}
        # Interleave the sides by distance to their own mid. Sorting by side
        # first made "DOWN" (alphabetically before "UP") consume the whole
        # burst budget, which pushed the Up/Down ratio to 0.33.
        ordered = sorted(
            state.unfilled(),
            key=lambda lv: abs(
                lv.price - (state.mid_up if lv.side == "UP" else 1.0 - state.mid_up)
            ),
        )
        for level in ordered:
            tick = round(level.price, precision)
            if per_side_cap is not None and taken_per_side.get(level.side, 0) >= per_side_cap:
                continue
            if previous_depth.get(level.side, {}).get(tick, 0.0) <= 1e-9:
                # Nothing was resting ahead of us at this tick to be cleared.
                continue
            if level.placed_at and now_mono - level.placed_at < self.replenish_delay:
                continue
            # Queue share, not sweep-through. We rest at the same tick as other
            # makers, so a print there is split by queue order rather than only
            # reaching us once the tick is emptied. Waiting for the tick to clear
            # makes every fill land after the price has already moved past it,
            # which turned the pair cost from 0.9658 into 1.0489 and made the
            # strategy lose money the more it traded.
            traded_at_level = traded.get(level.side, {}).get(tick, 0.0)
            if traded_at_level <= 1e-9:
                continue
            # A taker that actually reaches our tick moved a meaningful block.
            # Counting every one-share depth wobble as a print made each 3s scan
            # fill something, so the round never had the wallet's silent gaps
            # (15 bursts per round, ~9.8 fills each, ~79 shares per burst).
            if traded_at_level + 1e-9 < self.burst_min_shares:
                continue
            # Queue depth must be the depth that normally stands behind us, not
            # the depth left at the instant of the print. Measured depth at our
            # own tick is a median 269 shares, giving us a 27% queue share; using
            # the instant depth (often nearly drained, so ~10 shares) implied a
            # 91% share and over-filled by 3.6x, which is exactly the gap between
            # 1/0.27 and the observed volume ratio.
            resting_depth = previous_depth.get(level.side, {}).get(tick, 0.0)
            resting_depth = max(resting_depth, self.queue_depth_floor)
            queue_total = max(resting_depth, 0.0) + level.shares
            if queue_total <= 1e-9:
                continue
            # Time priority: a fresh order sits behind everything already resting
            # at the tick, so it barely trades until that queue drains. Ramping
            # the queue share in over queue_ramp seconds is what brings the fill
            # rate down to the wallet's 128 fills / 2,799 shares per round;
            # crediting the full proportional share from the first tick instead
            # produced 480-660 fills and 15k-35k shares.
            age = now_mono - level.placed_at if level.placed_at else queue_ramp
            ramp = 0.0 if queue_ramp <= 0 else min(1.0, max(0.0, age / queue_ramp))
            if ramp <= 0.0:
                continue
            quantity = min(
                level.shares,
                traded_at_level * level.shares / queue_total * ramp,
            )
            # One snapshot can span several seconds of takers, so its total must
            # not exceed what a single burst clears in the real book (median 79
            # shares against the 143 we were booking).
            if burst_cap is not None:
                remaining = burst_cap - burst_taken
                if remaining <= 1e-9:
                    # Skip this level but keep scanning: a plain break would end
                    # the loop and could starve the other side entirely.
                    continue
                quantity = min(quantity, remaining)
            # One snapshot can span several seconds of price movement, during
            # which every tick in between shows less depth and would all be
            # counted as our fill (the median simultaneous fill spanned 28
            # cents, up to 96). A single sweep only clears the ticks the print
            # actually reached, so cap how far from the touch we can fill.
            if max_fill_distance is not None:
                touch = snapshot.up_bid if level.side == "UP" else snapshot.down_bid
                if touch is not None and float(touch) - level.price > max_fill_distance + 1e-12:
                    continue
            # Budget check uses the worst case (our limit), so a fill can never
            # be affordable at the limit and then overspend at the better price.
            cost = quantity * level.price
            if cost > self.cash + 1e-9:
                continue
            if budget is not None and committed + cost > budget + 1e-9:
                continue
            if quantity + 1e-9 >= level.shares:
                level.filled = True
            else:
                level.shares -= quantity
            # Book the fill at the better of our resting price and the price the
            # book is actually showing. A sweep can drag the market far below our
            # bid before we sample it, and charging our stale limit booked fills
            # like 0.67 for something offered at 0.51, which drove the pair cost
            # to 1.03 while the wallet sat at 0.84 on the same rounds.
            fill_price = level.price
            book_price = snapshot.up_ask if level.side == "UP" else snapshot.down_ask
            if book_price is not None and valid_price(book_price):
                fill_price = min(level.price, float(book_price))
            fill_cost = quantity * fill_price
            taken_per_side[level.side] = taken_per_side.get(level.side, 0) + 1
            burst_taken += quantity
            committed += fill_cost
            fills.append(RestingLevel(side=level.side, price=fill_price, shares=quantity))
        return fills

    def _traded_shares(self, state: MakerQuoteState, snapshot) -> tuple[dict, dict]:
        """Shares that traded per price level since the previous book snapshot.

        Down-side levels are derived from the Yes book: a Down bid at p is a Yes
        ask at 1-p, so Down depth is read off the mirrored Yes side. Returns the
        traded amounts and the depth that was standing before this snapshot.
        """
        precision = max(0, int(getattr(snapshot, "decimal_precision", 3)))
        total_ticks = 10 ** precision
        books = {"UP": snapshot.up_bids, "DOWN": snapshot.up_asks}
        traded: dict[str, dict[float, float]] = {}
        previous_depth: dict[str, dict[float, float]] = {}
        for side, levels in books.items():
            current: dict[float, float] = {}
            for price, size in levels or ():
                ticks = round(price * total_ticks)
                if side == "DOWN":
                    ticks = total_ticks - ticks
                current[round(ticks / total_ticks, precision)] = size
            previous = state.last_bids if side == "UP" else state.last_asks
            previous_depth[side] = dict(previous)
            if previous:
                for price, before in previous.items():
                    after = current.get(price, 0.0)
                    if before - after > 1e-9:
                        traded.setdefault(side, {})[price] = before - after
            if side == "UP":
                state.last_bids = current
            else:
                state.last_asks = current
        return traded, previous_depth

    def accrue_maker_rewards(
        self,
        snapshot,
        *,
        spread_threshold: float = 0.06,
        share_threshold: float = 100.0,
        now: float | None = None,
    ) -> None:
        """Track how long a reward-qualified ladder rested on the book."""
        state = self.maker_quotes.get(snapshot.market_id)
        if state is None:
            return
        now = time.monotonic() if now is None else now
        elapsed = max(0.0, now - state.quoted_at) if state.quoted_at else 0.0
        # Advance the clock here, not in sync_maker_quotes: the ladder is
        # re-quoted almost every cycle, so resetting it there would leave the
        # reward interval at zero forever.
        state.quoted_at = now
        if elapsed <= 0:
            return
        qualified = sum(
            level.shares for level in state.unfilled()
            if abs(level.price - (state.mid_up if level.side == "UP" else 1.0 - state.mid_up))
            <= spread_threshold + 1e-12 and level.shares + 1e-9 >= share_threshold
        )
        if qualified > 0:
            state.reward_seconds += elapsed
            state.reward_share_seconds += qualified * elapsed

    def drop_maker_quotes(self, keep_market_ids: set[str]) -> None:
        """Forget ladders for rounds that rolled over or resolved."""
        for market_id in list(self.maker_quotes):
            if market_id not in keep_market_ids:
                del self.maker_quotes[market_id]

    def maker_pair_stats(self, window_seconds: float = 2.0) -> dict:
        """Pair opposite-side maker fills by time and price the real pair cost.

        The wallet's own measured figure is computed the same way: an Up fill and
        a Down fill within a couple of seconds of each other in the same market
        are one matched pair, and the pair costs ``up_price + down_price``.
        Aggregate averages are meaningless when the two sides fill at different
        mid levels, which is why this pairing is the comparable metric.
        """
        by_market: dict[str, dict[str, list]] = {}
        for fill in self.maker_fills:
            by_market.setdefault(fill["market_id"], {"UP": [], "DOWN": []})[
                fill["side"]].append(fill)
        pairs, unmatched = [], 0
        for sides in by_market.values():
            used: set[int] = set()
            for up in sides["UP"]:
                best, best_gap = None, None
                for index, down in enumerate(sides["DOWN"]):
                    if index in used:
                        continue
                    gap = abs(down["at"] - up["at"])
                    if gap <= window_seconds and (best_gap is None or gap < best_gap):
                        best, best_gap = index, gap
                if best is None:
                    unmatched += 1
                    continue
                used.add(best)
                down = sides["DOWN"][best]
                quantity = min(up["quantity"], down["quantity"])
                pairs.append((quantity, up["price"] + down["price"]))
            unmatched += len(sides["DOWN"]) - len(used)
        if not pairs:
            return {"pairs": 0, "quantity": 0.0, "pair_cost": None, "unmatched": unmatched}
        total_quantity = sum(quantity for quantity, _ in pairs)
        weighted = sum(quantity * cost for quantity, cost in pairs)
        return {
            "pairs": len(pairs),
            "quantity": total_quantity,
            "pair_cost": weighted / total_quantity if total_quantity else None,
            "unmatched": unmatched,
        }

    @property
    def maker_qualified_seconds(self) -> float:
        return sum(state.reward_seconds for state in self.maker_quotes.values())

    @property
    def open_exposure(self) -> float:
        return sum(float(position["cost"]) for position in self.open_positions.values())

    def update_market_marks(self, snapshots) -> None:
        for snapshot in snapshots:
            self.market_marks[snapshot.market_id] = {
                "up_bid": 1.0 - snapshot.down_ask if valid_price(snapshot.down_ask) else None,
                "down_bid": 1.0 - snapshot.up_ask if valid_price(snapshot.up_ask) else None,
                "fee_rate_bps": snapshot.fee_rate_bps,
            }

    def update_account_positions(self, positions: list[dict]) -> None:
        self.account_positions = positions
        self.account_positions_error = None

    def set_account_positions_error(self, error: str) -> None:
        self.account_positions_error = error

    def settle_trade(self, market_id: str, winner: str) -> float:
        position = self.open_positions.get(market_id)
        if position is None:
            return 0.0
        up_quantity = float(position.get("up_quantity", 0.0))
        down_quantity = float(position.get("down_quantity", 0.0))
        payout = (
            up_quantity if winner == "UP" else down_quantity if winner == "DOWN"
            else (up_quantity + down_quantity) * 0.5 if winner == "TIE" else 0.0
        )
        cost = float(position["cost"])
        pnl = payout - cost
        self.cash += payout
        self.total_cost = max(0.0, self.total_cost - cost)
        self.realized_pnl += pnl
        del self.open_positions[market_id]
        self._update_ledger_settlement(market_id, winner, payout, pnl)
        self._save_state()
        self.write_summary()
        return pnl

    def _update_ledger_settlement(self, market_id: str, winner: str, payout: float, pnl: float) -> None:
        with self.log_path.open("r", newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            rows = list(reader)
        for row in rows:
            if row.get("market_id") == market_id:
                row["settled"] = "true"
                row["winner"] = winner
                quantity = float(row.get("quantity") or 0.0)
                side = row.get("side")
                if side in ("SELL_UP", "SELL_DOWN"):
                    # Proceeds were credited when the trim filled; no payout now.
                    row_payout = 0.0
                elif side == "PAIR":
                    row_payout = quantity
                elif winner == side:
                    row_payout = quantity
                elif winner == "TIE":
                    row_payout = quantity * 0.5
                else:
                    row_payout = 0.0
                row_cost = float(row.get("cost") or 0.0)
                row["payout"] = f"{row_payout:.4f}"
                row["realized_pnl"] = f"{row_payout - row_cost:.4f}"
        with self.log_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=LOG_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def _save_state(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {"cash": round(self.cash, 4), "total_cost": round(self.total_cost, 4),
                 "filled_trades": self.filled_trades,
                 "traded_market_ids": sorted(self.traded_market_ids),
                 "open_positions": self.open_positions,
                 "realized_pnl": round(self.realized_pnl, 4)},
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    def _normalize_open_positions(self, rows: list[dict] | None = None) -> None:
        logged_side_costs: dict[str, dict[str, float]] = {}
        replayed_positions: dict[str, dict] = {}
        for row in rows or []:
            market_id = row.get("market_id")
            if not market_id:
                continue
            side_costs = logged_side_costs.setdefault(market_id, {"up_cost": 0.0, "down_cost": 0.0})
            quantity = float(row.get("quantity") or 0.0)
            side = row.get("side")
            if side in ("UP", "DOWN"):
                replayed = replayed_positions.setdefault(
                    market_id, {"up_quantity": 0.0, "down_quantity": 0.0, "unhedged_cost": 0.0}
                )
                replayed["unhedged_cost"] = unhedged_cost_after_buy(
                    replayed, side, quantity, float(row.get("cost") or 0.0)
                )
                key = "up_quantity" if side == "UP" else "down_quantity"
                replayed[key] += quantity
            if side == "UP":
                side_costs["up_cost"] += float(row.get("cost") or 0.0)
            elif side == "DOWN":
                side_costs["down_cost"] += float(row.get("cost") or 0.0)
            elif side == "PAIR":
                up_cost_per_share = float(row.get("up_cost_per_share") or 0.0)
                down_cost_per_share = float(row.get("down_cost_per_share") or 0.0)
                if up_cost_per_share > 0 and down_cost_per_share > 0:
                    side_costs["up_cost"] += quantity * up_cost_per_share
                    side_costs["down_cost"] += quantity * down_cost_per_share
                else:
                    total = float(row.get("cost") or 0.0)
                    side_costs["up_cost"] += total / 2.0
                    side_costs["down_cost"] += total / 2.0

        for market_id, position in list(self.open_positions.items()):
            if "up_quantity" in position or "down_quantity" in position:
                normalized = position
            else:
                side = position.get("side")
                quantity = float(position.get("quantity", 0.0))
                normalized = {
                    "up_quantity": quantity if side in ("UP", "PAIR") else 0.0,
                    "down_quantity": quantity if side in ("DOWN", "PAIR") else 0.0,
                    "cost": float(position.get("cost", 0.0)),
                    "asset": position.get("asset", ""),
                    "window_minutes": position.get("window_minutes", 0),
                }
                self.open_positions[market_id] = normalized

            quantities = {
                "up_quantity": float(normalized.get("up_quantity", 0.0)),
                "down_quantity": float(normalized.get("down_quantity", 0.0)),
            }
            side_costs = logged_side_costs.get(market_id, {})
            total_quantity = quantities["up_quantity"] + quantities["down_quantity"]
            total_cost = float(normalized.get("cost", 0.0))
            for side in ("up", "down"):
                key = f"{side}_cost"
                if key in normalized:
                    continue
                logged_cost = float(side_costs.get(key, 0.0))
                if logged_cost > 0:
                    normalized[key] = logged_cost
                elif total_quantity > 0:
                    normalized[key] = total_cost * quantities[f"{side}_quantity"] / total_quantity
                else:
                    normalized[key] = 0.0
            if "unhedged_cost" not in normalized:
                replayed = replayed_positions.get(market_id)
                net_up = quantities["up_quantity"] - quantities["down_quantity"]
                if replayed and abs(
                    replayed["up_quantity"] - replayed["down_quantity"] - net_up
                ) <= 1e-6:
                    normalized["unhedged_cost"] = replayed["unhedged_cost"]
                else:
                    side = "up" if net_up > 0 else "down"
                    side_quantity = quantities[f"{side}_quantity"]
                    normalized["unhedged_cost"] = (
                        abs(net_up) * normalized[f"{side}_cost"] / side_quantity
                        if side_quantity > 0 else 0.0
                    )


    def position_valuation(self) -> list[dict]:
        """Per-market inventory with side averages and the unrealised P&L.

        A mark is only computed for a side whose bid is quoted: without a real
        bid the position is reported as unmatched rather than marked at a price
        nobody is showing.
        """
        out = []
        for market_id, position in self.open_positions.items():
            up = float(position.get("up_quantity", 0.0))
            down = float(position.get("down_quantity", 0.0))
            up_cost = float(position.get("up_cost", 0.0))
            down_cost = float(position.get("down_cost", 0.0))
            cost = float(position.get("cost", 0.0))
            mark = self.market_marks.get(market_id) or {}
            up_bid, down_bid = mark.get("up_bid"), mark.get("down_bid")
            value = None
            if (up <= 1e-9 or valid_price(up_bid)) and (down <= 1e-9 or valid_price(down_bid)):
                value = (up * float(up_bid or 0.0)) + (down * float(down_bid or 0.0))
            out.append({
                "market_id": market_id,
                "asset": position.get("asset", ""),
                "window_minutes": int(position.get("window_minutes") or 0),
                "up_quantity": up,
                "up_average": up_cost / up if up > 1e-9 else None,
                "up_bid": up_bid,
                "down_quantity": down,
                "down_average": down_cost / down if down > 1e-9 else None,
                "down_bid": down_bid,
                "cost": cost,
                "value": value,
                "unrealized": (value - cost) if value is not None else None,
                "net": up - down,
            })
        return out

    def _summary_position_lines(self) -> list[str]:
        """The open-inventory block: size, average price, mark and unrealised P&L."""
        positions = self.position_valuation()
        if not positions:
            return []
        lines = ["", "--- positions ---"]
        total_cost = total_value = 0.0
        marked_all = True
        for row in positions:
            lines.append(
                f"{row['asset']} {row['window_minutes']}m market={row['market_id']}"
            )
            for side in ("up", "down"):
                quantity = row[f"{side}_quantity"]
                if quantity <= 1e-9:
                    continue
                average = row[f"{side}_average"]
                bid = row[f"{side}_bid"]
                cost = quantity * average
                if valid_price(bid):
                    value = quantity * float(bid)
                    lines.append(
                        f"  {side.title():4s} {quantity:9.1f} shares @ {average:.4f} "
                        f"| bid {float(bid):.3f} | cost ${cost:.2f} | value ${value:.2f} "
                        f"| {_signed_money(value - cost)}"
                    )
                else:
                    lines.append(
                        f"  {side.title():4s} {quantity:9.1f} shares @ {average:.4f} "
                        f"| bid n/a | cost ${cost:.2f} | value n/a"
                    )
            unrealized = row["unrealized"]
            if unrealized is None:
                marked_all = False
                lines.append("  Mark: unavailable (missing bid)")
            else:
                lines.append(
                    f"  Value ${row['value']:.2f} | Unrealized {_signed_money(unrealized)}"
                )
            lines.append(f"  Net {row['net']:+.1f} shares")
            total_cost += row["cost"]
            if row["value"] is not None:
                total_value += row["value"]
        if marked_all and positions:
            lines.append(
                f"Open total: cost ${total_cost:.2f} | value ${total_value:.2f} "
                f"| unrealized {_signed_money(total_value - total_cost)}"
            )
        return lines

    def write_summary(self) -> None:
        with self.log_path.open("r", newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        settled = [row for row in rows if (row.get("settled") or "").lower() == "true"]
        total_cost = sum(float(row.get("cost") or 0.0) for row in settled)
        total_payout = sum(float(row.get("payout") or 0.0) for row in settled)

        results_by_market: dict[str, dict] = {}
        for row in settled:
            market_id = row.get("market_id", "")
            result = results_by_market.setdefault(
                market_id,
                {"row": row, "pnl": 0.0, "window": int(row.get("window_minutes") or 0)},
            )
            result["pnl"] += float(row.get("realized_pnl") or 0.0)

        # Show a block per window that actually has settled or open inventory,
        # instead of a fixed 5/15 pair that a single-window run never fills.
        window_ids = {result["window"] for result in results_by_market.values()}
        window_ids |= {
            int(position.get("window_minutes") or 0)
            for position in self.open_positions.values()
        }
        position_lines = self._summary_position_lines()
        pair_stats = self.maker_pair_stats()
        maker_lines = []
        if pair_stats["pairs"] or pair_stats["unmatched"]:
            pair_cost = pair_stats["pair_cost"]
            maker_lines = [
                "",
                "--- maker ---",
                f"Matched pairs:  {pair_stats['pairs']} ({pair_stats['quantity']:.0f} shares)",
                f"Pair cost:      {pair_cost:.4f}" if pair_cost is not None else "Pair cost:      n/a",
                f"Unmatched:      {pair_stats['unmatched']} fills",
                f"Reward time:    {self.maker_qualified_seconds:.0f}s qualified",
            ]
            up = sum(f["quantity"] for f in self.maker_fills if f["side"] == "UP")
            down = sum(f["quantity"] for f in self.maker_fills if f["side"] == "DOWN")
            if down > 1e-9:
                maker_lines.append(f"Up/Down ratio:  {up / down:.3f}")
        windows = sorted(window for window in window_ids if window > 0)
        lines = [
            f"# P&L Summary — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
            f"Settled markets: {len(results_by_market)}",
            f"Total cost:     ${total_cost:.2f}",
            f"Total payout: ${total_payout:.2f}",
            f"Realized P&L: {_money(total_payout - total_cost)}",
        ] + position_lines + maker_lines + [""]
        for window in windows:
            group = [row for row in settled if int(row.get("window_minutes") or 0) == window]
            group_cost = sum(float(row.get("cost") or 0.0) for row in group)
            group_payout = sum(float(row.get("payout") or 0.0) for row in group)
            group_pnl = group_payout - group_cost
            lines.append(f"--- {window}-min markets ---")
            lines.append(
                f"Count: {len({row.get('market_id') for row in group})} | "
                f"Cost: ${group_cost:.2f} | Payout: ${group_payout:.2f} | "
                f"P&L: ${group_pnl:+.2f}"
            )
            window_results = [
                result for result in results_by_market.values() if result["window"] == window
            ]
            for result in window_results:
                row = result["row"]
                title = row.get("market_title") or (
                    f"{row.get('asset', 'Market')} Up or Down - "
                    f"{row.get('window_minutes', '?')}m ({row.get('market_id', '')})"
                )
                winner = row.get("winner") or "Unknown"
                lines.append(f"{title} → {winner}  {_signed_money(result['pnl'])}")
            lines.append("")
        while lines and lines[-1] == "":
            lines.pop()
        self.summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
