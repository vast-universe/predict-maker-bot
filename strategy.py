from dataclasses import dataclass
from math import ceil, erf, floor as math_floor, isfinite, log, sqrt
from typing import Optional

from markets import MarketSnapshot, valid_price


@dataclass(frozen=True)
class Opportunity:
    market: MarketSnapshot
    side: str
    quantity: float  # net shares received after Predict buy fees
    ask: float
    fair_probability: float
    fee_per_share: float
    cost_per_share: float  # USDT cost per net share received
    expected_edge_per_share: float
    lead_bps: float
    up_ask: Optional[float] = None
    down_ask: Optional[float] = None
    up_cost_per_share: float = 0.0
    down_cost_per_share: float = 0.0
    up_order_quantity: float = 0.0  # gross order size before share-denominated fees
    down_order_quantity: float = 0.0
    strategy: str = "dir"  # console label: dir | hedge | arb | rebal
    condition: str = ""  # optional console suffix such as "down>0.64"
    comb_ratio: float = 0.0  # arbitrage payout/cost ratio shown as comb=1.010
    sell: bool = False  # True for SELL orders (rebalance trims)


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


def fair_up_probability(market: MarketSnapshot) -> Optional[float]:
    """Estimate the chance Predict's end price is above its opening price."""
    if (
        market.price_to_beat is None or market.spot_price is None
        or market.sigma_1m is None or market.seconds_left is None
        or market.price_to_beat <= 0 or market.spot_price <= 0
        or market.sigma_1m <= 0 or market.seconds_left <= 0
    ):
        # No external price feed: fall back to the book-implied probability so
        # the scan still evaluates quotes instead of going silent.
        if valid_price(market.up_ask) and valid_price(market.down_ask):
            implied = (float(market.up_ask) + (1.0 - float(market.down_ask))) / 2.0
            return min(0.99, max(0.01, implied))
        return None
    horizon_minutes = market.seconds_left / 60.0
    z_score = log(market.spot_price / market.price_to_beat) / (
        market.sigma_1m * sqrt(horizon_minutes)
    )
    probability = normal_cdf(z_score)
    # The public 5-minute model caps early-window overconfidence at 95%.
    if market.window_minutes == 5 and market.seconds_left > 120:
        probability = min(max(probability, 0.05), 0.95)
    return probability


def predict_taker_fee_per_share(price: float, fee_rate_bps: float) -> float:
    """Predict fee value in USDT per gross matched share."""
    if not 0.0 < price < 1.0 or fee_rate_bps < 0:
        return float("inf")
    return fee_rate_bps / 10_000.0 * min(price, 1.0 - price)


def net_shares_per_gross_share(price: float, fee_rate_bps: float) -> float:
    """Predict deducts buy fees from the shares received by the taker."""
    fee = predict_taker_fee_per_share(price, fee_rate_bps)
    if not 0.0 < price < 1.0 or fee == float("inf"):
        return 0.0
    return max(0.0, 1.0 - fee / price)


def cost_per_net_share(price: float, fee_rate_bps: float) -> float:
    net_ratio = net_shares_per_gross_share(price, fee_rate_bps)
    return price / net_ratio if net_ratio > 0 else float("inf")


def gross_quantity_for_net(net_quantity: float, price: float, fee_rate_bps: float) -> float:
    net_ratio = net_shares_per_gross_share(price, fee_rate_bps)
    return net_quantity / net_ratio if net_ratio > 0 else 0.0


def order_net_quantity(base_shares: float, price: float, fee_rate_bps: float,
                       min_order_cost: float) -> float:
    """Whole net shares targeting the base size and the gross order minimum."""
    unit_cost = cost_per_net_share(price, fee_rate_bps)
    if base_shares <= 0 or not isfinite(unit_cost) or unit_cost <= 0:
        return 0.0
    return max(base_shares, float(ceil(min_order_cost / unit_cost - 1e-12)))


def unhedged_average_cost(position: dict) -> float:
    up = float(position.get("up_quantity", 0.0))
    down = float(position.get("down_quantity", 0.0))
    net_quantity = abs(up - down)
    if net_quantity <= 1e-9:
        return 0.0
    if "unhedged_cost" in position:
        return float(position["unhedged_cost"]) / net_quantity
    side = "up" if up > down else "down"
    side_quantity = up if up > down else down
    return float(position.get(f"{side}_cost", position.get("cost", 0.0))) / side_quantity


def unhedged_cost_after_buy(position: dict, side: str, quantity: float, cost: float) -> float:
    net_up = float(position.get("up_quantity", 0.0)) - float(position.get("down_quantity", 0.0))
    previous = abs(net_up) * unhedged_average_cost(position)
    if side == "PAIR":
        return previous
    signed_quantity = quantity if side == "UP" else -quantity
    if abs(net_up) <= 1e-9 or net_up * signed_quantity > 0:
        return previous + cost
    if quantity <= abs(net_up) + 1e-9:
        return max(0.0, previous * (1.0 - quantity / abs(net_up)))
    return cost * (quantity - abs(net_up)) / quantity


def _lead_bps(market: MarketSnapshot) -> float:
    if market.spot_price is None or market.price_to_beat is None or market.price_to_beat <= 0:
        return 0.0
    return (market.spot_price / market.price_to_beat - 1.0) * 10_000.0


# ---------------------------------------------------------------------------
# Maker approximation of the reference wallet's public BUY fills. Prices and
# completed fill quantities can be observed; original ladder depth, historical
# midpoint offsets and cancellation triggers cannot be recovered from matches.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteLevel:
    """One resting bid: price on the market's tick grid and its size in shares."""

    price: float
    shares: float


def mid_up_price(market: MarketSnapshot) -> Optional[float]:
    """Fair Up price from the book: midpoint of the two-sided Up quote."""
    if valid_price(market.up_bid) and valid_price(market.up_ask):
        return (float(market.up_bid) + float(market.up_ask)) / 2.0
    # One-sided book: fall back to the Down ask complement so quoting still runs.
    if valid_price(market.up_ask) and valid_price(market.down_ask):
        return (float(market.up_ask) + (1.0 - float(market.down_ask))) / 2.0
    if valid_price(market.up_bid) and valid_price(market.down_bid):
        return (float(market.up_bid) + (1.0 - float(market.down_bid))) / 2.0
    return None


def _snap_to_tick(price: float, precision: int) -> float:
    """Round a price onto the market's tick grid."""
    tick = 10 ** -precision
    steps = math_floor(price / tick + 0.5)
    return round(min(max(steps * tick, tick), 1.0 - tick), precision)


def quote_ladder(
    market: MarketSnapshot,
    *,
    offset_cents: float = 1.0,
    levels: int = 5,
    step_cents: float = 1.0,
    level_shares: float = 100.0,
    skew_cents: float = 0.0,
    min_price: float = 0.0,
    max_price: float = 1.0,
    level_notional: float = 0.0,
    min_order_cost: float = 1.0,
    anchor_mid_up: Optional[float] = None,
) -> dict[str, list[QuoteLevel]]:
    """Build the two-sided bid ladder around the book mid.

    Floor each outcome's midpoint independently to avoid rounding one bid up.
    Keep orders below the visible opposite quote (including mint matching).
    Offset/depth/refill settings are explicit approximations, not private rules.
    """
    mid_up = anchor_mid_up if anchor_mid_up is not None else mid_up_price(market)
    if (not valid_price(mid_up) or levels <= 0 or level_shares <= 0
            or not all(isfinite(value) for value in
                       (offset_cents, step_cents, level_shares, skew_cents,
                        min_price, max_price, level_notional, min_order_cost))):
        return {"UP": [], "DOWN": []}
    precision = max(0, int(market.decimal_precision))
    tick = 10 ** -precision
    total_ticks = 10 ** precision
    ticks_per_cent = max(1, int(round(total_ticks / 100)))
    offset_ticks = max(1, int(round(offset_cents * ticks_per_cent)))
    step_ticks = max(1, int(round(step_cents * ticks_per_cent)))
    # Work on the integer tick grid so every level is exactly step_ticks apart.
    mid_ticks = int(math_floor(mid_up / tick + 1e-9))
    down_mid_ticks = int(math_floor((1.0 - mid_up) / tick + 1e-9))
    ladder: dict[str, list[QuoteLevel]] = {"UP": [], "DOWN": []}
    # Inventory skew: when long Up, quote the Up bid deeper and the Down bid
    # shallower, so the next fills pull the position back toward flat. This is
    # the lever that fights the adverse selection of always buying the loser.
    skew_ticks = int(round(skew_cents * ticks_per_cent))
    for side, side_mid_ticks in (("UP", mid_ticks), ("DOWN", down_mid_ticks)):
        side_skew = skew_ticks if side == "UP" else -skew_ticks
        ask = market.up_ask if side == "UP" else market.down_ask
        passive_ceiling = (int(ceil(float(ask) / tick - 1e-9)) - 1
                           if valid_price(ask) else total_ticks - 1)
        for index in range(levels):
            ticks = min(side_mid_ticks - offset_ticks - side_skew,
                        passive_ceiling) - index * step_ticks
            if not 0 < ticks < total_ticks:
                continue
            price = round(ticks * tick, precision)
            if not valid_price(price) or (ladder[side] and price >= ladder[side][-1].price):
                # Clamping at the bottom tick would stack duplicate levels.
                continue
            if not (min_price - 1e-9 <= price <= max_price + 1e-9):
                continue
            # Dynamic sizing: hold each level at a fixed cash cost instead of a
            # fixed share count. With 10 fixed shares a level is only worth 1 USDT
            # at 0.10, so 44% of one measured round (mid below 0.11) could not be
            # quoted at all; scaling the shares as notional / price keeps every
            # tick legal at a constant 1 USDT of risk per level.
            shares = level_shares
            if level_notional > 0:
                shares = level_notional / price
            ladder[side].append(QuoteLevel(price=price, shares=shares))
    if level_notional > 0:
        ladder = _equalise_shares(ladder, budget=level_notional * levels * 4)
    return {side: [level for level in side_levels
                   if level.price * level.shares + 1e-9 >= min_order_cost]
            for side, side_levels in ladder.items()}


def risk_adjusted_ladder(
    market: MarketSnapshot,
    ladder: dict[str, list[QuoteLevel]],
    *,
    position: Optional[dict],
    max_net_shares: float,
    pair_margin: float,
    max_market_cost: float,
    stop_new_seconds: float,
    stop_all_seconds: float,
    shock_cents: float,
    history_mid: Optional[float] = None,
    min_order_cost: float = 1.0,
    fee_rate_bps: float = 0.0,
) -> tuple[dict[str, list[QuoteLevel]], str]:
    """Apply our buy-only inventory policy without mutating quotes or state.

    This is a self-designed policy, not a recovered wallet rule. A missing-side
    bid must profitably pair with the existing unmatched inventory; it cannot
    buy more shares than that inventory. Other bids respect the worst-case net
    position if just one outcome fills. All bids together fit the remaining
    paid-cost budget. Prices conservatively reserve share-denominated taker
    fees for any price-improved execution up to the bid limit, supporting
    postOnly=False even though no bid crosses the visible ask. Sizes remain gross,
    making the one-sided net-share limits conservative. The caller reconciles
    this entire target against its
    resting orders before placing any increase, and owns the shock cooldown.
    """
    empty: dict[str, list[QuoteLevel]] = {"UP": [], "DOWN": []}
    parameters = (max_net_shares, pair_margin, max_market_cost,
                  stop_new_seconds, stop_all_seconds, shock_cents, min_order_cost,
                  fee_rate_bps)
    if (not all(isfinite(value) for value in parameters)
            or max_net_shares <= 0 or not 0 <= pair_margin < 1
            or max_market_cost < 0 or min_order_cost <= 0 or shock_cents < 0
            or not 0 <= fee_rate_bps < 10_000
            or not 0 <= stop_all_seconds <= stop_new_seconds):
        return empty, "risk: invalid policy parameters"
    seconds_left = market.seconds_left
    if seconds_left is None or not isfinite(seconds_left):
        return empty, "risk: missing time remaining"
    if seconds_left <= stop_all_seconds:
        return empty, "risk: final cutoff; cancel all bids"
    mid = mid_up_price(market)
    if not valid_price(mid):
        return empty, "risk: missing midpoint"
    if history_mid is not None:
        if not valid_price(history_mid):
            return empty, "risk: invalid previous midpoint"
        if shock_cents > 0 and abs(mid - history_mid) + 1e-12 >= shock_cents / 100.0:
            return empty, "risk: midpoint shock; cancel all bids"

    position = position or {}
    try:
        up = float(position.get("up_quantity", 0.0))
        down = float(position.get("down_quantity", 0.0))
        if (up + down > 1e-9 and "cost" not in position
                and ((up > 1e-9 and "up_cost" not in position)
                     or (down > 1e-9 and "down_cost" not in position))):
            return empty, "risk: missing inventory cost basis"
        paid_cost = float(position.get("cost", float(position.get("up_cost", 0.0))
                                       + float(position.get("down_cost", 0.0))))
        unmatched_average = unhedged_average_cost(position)
    except (TypeError, ValueError, ZeroDivisionError):
        return empty, "risk: invalid inventory"
    if (not all(isfinite(value) and value >= 0
                for value in (up, down, paid_cost, unmatched_average))):
        return empty, "risk: invalid inventory"
    cash_budget = max(0.0, max_market_cost - paid_cost)
    if cash_budget + 1e-9 < min_order_cost:
        return empty, "risk: market paid-cost limit"

    net = up - down
    missing = "DOWN" if net > 1e-9 else "UP" if net < -1e-9 else None
    close_only = seconds_left <= stop_new_seconds
    if close_only and missing is None:
        return empty, "risk: close-only cutoff; inventory balanced"
    capacities = {"UP": max(0.0, max_net_shares - net),
                  "DOWN": max(0.0, max_net_shares + net)}
    if missing is not None:
        capacities[missing] = abs(net)
    if close_only:
        capacities["UP" if missing == "DOWN" else "DOWN"] = 0.0

    precision = max(0, int(market.decimal_precision))
    tick = 10 ** -precision

    def floor_price(price: float) -> float:
        return round(math_floor(price / tick + 1e-9) * tick, precision)

    def effective_cost(price: float) -> float:
        cost = cost_per_net_share(price, fee_rate_bps)
        # The raw fee-adjusted curve can fall above 0.5 at very high rates.
        # Its maximum over all executable prices <= this bid is at the bid or
        # at 0.5. Reserving that interval maximum is monotone in the bid limit.
        return max(cost, cost_per_net_share(0.5, fee_rate_bps)) if price >= 0.5 else cost

    def price_for_cost(ceiling: float, limit: float) -> float:
        """Highest legal tick under a net-cost cap, in logarithmic time.

        Keep the fee curve's partition for compatibility; the execution-price
        interval maximum makes the effective-cost cap monotone on each piece.
        """
        highest = max(0, int(math_floor(ceiling / tick + 1e-9)))
        half = int(math_floor(0.5 / tick + 1e-9))
        rate = fee_rate_bps / 10_000.0
        turning = int(math_floor((2 * rate / (1 + rate)) / tick + 1e-9))
        candidates = [0]

        def increasing_search(low: int, high: int) -> None:
            found = 0
            while low <= high:
                middle = (low + high) // 2
                if effective_cost(round(middle * tick, precision)) <= limit + 1e-12:
                    found = middle
                    low = middle + 1
                else:
                    high = middle - 1
            candidates.append(found)

        increasing_search(1, min(highest, half))
        decreasing_end = min(highest, turning)
        if decreasing_end > half and effective_cost(
                round(decreasing_end * tick, precision)) <= limit + 1e-12:
            candidates.append(decreasing_end)
        increasing_search(max(half + 1, turning + 1), highest)
        return round(max(candidates) * tick, precision)

    prepared: dict[str, list[QuoteLevel]] = {"UP": [], "DOWN": []}
    for side in prepared:
        if capacities[side] <= 1e-9:
            continue
        levels = sorted((level for level in ladder.get(side, [])
                         if isfinite(level.price) and valid_price(level.price)
                         and isfinite(level.shares) and level.shares > 0),
                        key=lambda level: -level.price)
        if not levels:
            continue
        ceiling = 1.0 - tick
        ask = market.up_ask if side == "UP" else market.down_ask
        if valid_price(ask):
            ceiling = min(ceiling, (ceil(float(ask) / tick - 1e-9) - 1) * tick)
        # Every individual bid must retain settlement value after a possible
        # taker fee, even if its opposite quote is later removed or never fills.
        cost_limit = 1.0 - pair_margin
        if side == missing:
            cost_limit = min(cost_limit, 1.0 - pair_margin - unmatched_average)
        ceiling = price_for_cost(ceiling, cost_limit)
        # Shift the whole side down together, preserving its level spacing.
        shift = max(0.0, levels[0].price - floor_price(ceiling))
        last_price = 1.0
        for level in levels:
            price = floor_price(level.price - shift)
            if (not valid_price(price) or price >= last_price - 1e-12
                    or effective_cost(price) > cost_limit + 1e-12):
                continue
            prepared[side].append(QuoteLevel(price, level.shares))
            last_price = price

    # Bound the highest effective costs, including share-denominated fees, so
    # every cross-level net-share pairing retains the configured margin.
    if prepared["UP"] and prepared["DOWN"]:
        effective = {side: max(effective_cost(level.price) for level in levels)
                     for side, levels in prepared.items()}
        excess = max(0.0, sum(effective.values()) - (1.0 - pair_margin))
        if excess > 1e-12:
            cost_limits = {side: value - excess / 2.0
                           for side, value in effective.items()}
            if any(value <= 0 for value in cost_limits.values()):
                cost_limits = {side: (1.0 - pair_margin) / 2.0
                               for side in prepared}
            for side, levels in prepared.items():
                cost_limit = cost_limits[side]
                ceiling = price_for_cost(levels[0].price, cost_limit)
                shift = max(0.0, levels[0].price - ceiling)
                prepared[side] = [QuoteLevel(price, level.shares)
                                  for level in levels
                                  if valid_price(price := floor_price(level.price - shift))
                                  and effective_cost(price) <= cost_limit + 1e-12]

    sized: dict[str, list[QuoteLevel]] = {"UP": [], "DOWN": []}
    for side, levels in prepared.items():
        remaining = capacities[side]
        for level in levels:
            quantity = min(level.shares, remaining)
            if quantity * level.price + 1e-9 < min_order_cost:
                continue
            sized[side].append(QuoteLevel(level.price, quantity))
            remaining = max(0.0, remaining - quantity)
    quoted_cost = sum(level.price * level.shares
                      for levels in sized.values() for level in levels)
    scale = min(1.0, cash_budget / quoted_cost) if quoted_cost > 0 else 0.0
    adjusted = {
        side: [QuoteLevel(level.price, level.shares * scale) for level in levels
               if level.price * level.shares * scale + 1e-9 >= min_order_cost]
        for side, levels in sized.items()
    }
    final_cost = sum(level.price * level.shares
                     for levels in adjusted.values() for level in levels)
    mode = "close-only" if close_only else "two-sided"
    return adjusted, (f"risk: {mode}; net={net:+.4f}; "
                      f"paid={paid_cost:.2f}; bids={final_cost:.2f}")


def fund_ladders(ladders: dict[str, dict[str, list[QuoteLevel]]], *,
                 cash: float, market_budgets: Optional[dict[str, float]] = None,
                 remaining_shares: Optional[dict[str, dict[tuple, float]]] = None,
                 ) -> dict[str, dict[str, list[QuoteLevel]]]:
    """Reserve cash across markets, nearest paired levels first, without resizing.

    A 300-USDT account cannot fund the reference-sized full ladders. Omit deeper
    levels rather than silently changing the observed share sizes or submitting
    orders whose combined liability exceeds available collateral.
    """
    funded = {market_id: {"UP": [], "DOWN": []} for market_id in ladders}
    remaining = max(0.0, cash) if isfinite(cash) else 0.0
    spent = {market_id: 0.0 for market_id in ladders}
    depth = max((len(levels) for ladder in ladders.values()
                 for levels in ladder.values()), default=0)
    for index in range(depth):
        for market_id, ladder in ladders.items():
            group = [(side, levels[index]) for side, levels in ladder.items()
                     if index < len(levels)]
            pending = (remaining_shares or {}).get(market_id, {})
            cost = sum(level.price * pending.get((side, level.price), level.shares)
                       for side, level in group)
            cap = (market_budgets or {}).get(market_id, cash)
            if (not isfinite(cost) or cost <= 0 or cost > remaining + 1e-9
                    or spent[market_id] + cost > cap + 1e-9):
                continue
            for side, level in group:
                funded[market_id][side].append(level)
            remaining -= cost
            spent[market_id] += cost
    return funded


def _equalise_shares(ladder: dict[str, list[QuoteLevel]],
                     budget: float = 0.0) -> dict[str, list[QuoteLevel]]:
    """Give both legs of each level the same share count, within a cash budget.

    A pair only earns the spread when both legs hold the same number of shares.
    With notional sizing the two sides sit at different prices, so their share
    counts differ (at mid 0.30 the Up leg is 3.45 shares and the Down leg 1.45),
    and every matched pair would leave the excess as one-sided exposure. Taking
    the larger of the two keeps both legs above the 1 USDT minimum, since the
    larger count comes from the cheaper side.

    The equalised ladder is then scaled down as a whole so its total cash cost
    fits ``budget``: at mid 0.05 the equalised sizes commit 196 USDT, two thirds
    of a 300 USDT balance, which would leave nothing to refill with.
    """
    up = list(ladder.get("UP") or [])
    down = list(ladder.get("DOWN") or [])
    if not up or not down:
        return ladder

    paired = min(len(up), len(down))
    merged: dict[str, list[QuoteLevel]] = {"UP": [], "DOWN": []}
    for index in range(paired):
        shares = max(up[index].shares, down[index].shares)
        merged["UP"].append(QuoteLevel(price=up[index].price, shares=shares))
        merged["DOWN"].append(QuoteLevel(price=down[index].price, shares=shares))
    # Levels one side does not have stay at their own size.
    merged["UP"].extend(up[paired:])
    merged["DOWN"].extend(down[paired:])

    if budget <= 0:
        return merged
    total = sum(level.price * level.shares
                for side in ("UP", "DOWN") for level in merged[side])
    if total <= budget + 1e-9:
        return merged
    scale = budget / total
    return {
        side: [QuoteLevel(price=l.price, shares=l.shares * scale) for l in levels]
        for side, levels in merged.items()
    }


def evaluate_rebalance_take(
    market: MarketSnapshot,
    *,
    position: dict,
    risk_budget: float,
    trigger_shares: float = 0.0,
    take_shares: float = 10.0,
    min_order_cost: float = 1.0,
) -> tuple[Optional[Opportunity], str]:
    """Optional paper-only inventory overlay; its trigger is user-defined."""
    if trigger_shares <= 0:
        return None, "rebalance: disabled"
    up = float(position.get("up_quantity", 0.0))
    down = float(position.get("down_quantity", 0.0))
    net = up - down
    gross = up + down
    if gross <= 1e-9:
        return None, "rebalance: no inventory"
    if abs(net) <= max(trigger_shares, 1e-9):
        return None, f"rebalance: net {net:+.0f} inside the {trigger_shares:g}-share band"
    # Buy the side we are short of, at its ask, paying the taker fee.
    side = "DOWN" if net > 0 else "UP"
    ask = market.down_ask if side == "DOWN" else market.up_ask
    if not valid_price(ask):
        return None, f"rebalance: no {side} ask to lift"
    all_in = cost_per_net_share(ask, market.fee_rate_bps)
    quantity = min(abs(net), order_net_quantity(take_shares, ask, market.fee_rate_bps,
                                                min_order_cost))
    if quantity <= 1e-9:
        return None, "rebalance: quantity rounds to zero"
    cost = quantity * all_in
    if cost > risk_budget + 1e-9:
        return None, f"rebalance: ${cost:.2f} exceeds budget ${risk_budget:.2f}"
    return Opportunity(
        market=market, side=side, quantity=quantity, ask=ask,
        fair_probability=0.5, fee_per_share=all_in - ask, cost_per_share=all_in,
        expected_edge_per_share=0.0, lead_bps=_lead_bps(market),
        up_ask=market.up_ask if side == "UP" else None,
        down_ask=market.down_ask if side == "DOWN" else None,
        up_cost_per_share=all_in if side == "UP" else 0.0,
        down_cost_per_share=all_in if side == "DOWN" else 0.0,
        up_order_quantity=gross_quantity_for_net(quantity, ask, market.fee_rate_bps)
        if side == "UP" else 0.0,
        down_order_quantity=gross_quantity_for_net(quantity, ask, market.fee_rate_bps)
        if side == "DOWN" else 0.0,
        strategy="rebal",
    ), f"qualifies: take {side} {quantity:g} to flatten net {net:+.0f}"
