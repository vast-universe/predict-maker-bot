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
            if drifted + 1e-9 < max(1, requote_ticks) and state.unfilled():
                # The mid has not moved and nothing was taken, so the ladder is
                # still valid as-is.
                return False
            if drifted + 1e-9 < max(1, requote_ticks):
                # Nothing left resting but the mid is unchanged: replenish the
                # levels that are still inside the band, like the wallet does
                # after a level is fully taken (refills land on the same tick).
                replenished = False
                for level in state.levels:
                    key = (level.side, round(level.price, precision))
                    if level.filled and key in fresh:
                        level.filled = False
                        level.shares = fresh[key]
                        replenished = True
                if replenished:
                    return True
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

    def collect_maker_fills(self, snapshot, *, budget: float | None = None) -> list[RestingLevel]:
        """Fill resting bids whose price the book's ask has traded down to.

        A level is only marked filled when the paper ledger can actually pay for
        it, so the resting book never claims a fill the cash could not fund.
        """
        state = self.maker_quotes.get(snapshot.market_id)
        if state is None:
            return []
        # Fill detection from book depth, not from price movement. The book is
        # the whole Yes side, so a level's size shrinking means shares traded at
        # that price while the mid stood still -- exactly how the tracked wallet
        # gets both sides filled at the same mid and lands a 0.963 pair cost.
        # Price-crossing rules cannot express that: they only fill once the price
        # has already moved past the level, which always prices a pair at 1.00.
        traded = self._traded_shares(state, snapshot)
        committed = float((self.open_positions.get(snapshot.market_id) or {}).get("cost", 0.0))
        fills = []
        for level in state.unfilled():
            side_traded = traded.get(level.side, {})
            if not side_traded:
                continue
            # Any trade at or above our bid can consume it.
            if not any(price >= level.price - 1e-12 for price in side_traded):
                continue
            cost = level.shares * level.price
            if cost > self.cash + 1e-9:
                continue
            if budget is not None and committed + cost > budget + 1e-9:
                continue
            level.filled = True
            committed += cost
            fills.append(level)
        return fills

    def _traded_shares(self, state: MakerQuoteState, snapshot) -> dict[str, dict[float, float]]:
        """Shares that traded per price level since the previous book snapshot.

        Down-side levels are derived from the Yes book: a Down bid at p is a Yes
        ask at 1-p, so Down depth is read off the mirrored Yes side.
        """
        precision = max(0, int(getattr(snapshot, "decimal_precision", 3)))
        total_ticks = 10 ** precision
        books = {"UP": snapshot.up_bids, "DOWN": snapshot.up_asks}
        traded: dict[str, dict[float, float]] = {}
        for side, levels in books.items():
            current: dict[float, float] = {}
            for price, size in levels or ():
                ticks = round(price * total_ticks)
                if side == "DOWN":
                    ticks = total_ticks - ticks
                current[round(ticks / total_ticks, precision)] = size
            previous = state.last_bids if side == "UP" else state.last_asks
            if previous:
                for price, before in previous.items():
                    after = current.get(price, 0.0)
                    if before - after > 1e-9:
                        traded.setdefault(side, {})[price] = before - after
            if side == "UP":
                state.last_bids = current
            else:
                state.last_asks = current
        return traded

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
