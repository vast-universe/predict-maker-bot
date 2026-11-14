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
# Maker quoting: replicate the observed two-sided bid ladder
#
# The tracked wallet quotes Bids on both outcomes (never Asks), one cent apart,
# several levels deep, and holds to redemption. Measured from 3000 fills:
#   * pair cost at the same instant: median 0.970  -> 1.5c per side below mid
#   * level spacing: 1c (97% of fills sit on the 1c grid)
#   * ladder depth: median 3 levels live, up to 8
#   * level size: p99 = 100 shares, matching the market's shareThreshold
#   * rewards: hourlyRate per market, spreadThreshold 0.06, shareThreshold 100
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
    levels: int = 6,
    step_cents: float = 1.0,
    level_shares: float = 100.0,
    skew_cents: float = 0.0,
    min_price: float = 0.0,
    max_price: float = 1.0,
    level_notional: float = 0.0,
) -> dict[str, list[QuoteLevel]]:
    """Build the two-sided bid ladder around the book mid.

    Both sides sit the same distance below their own mid, so a matched pair
    costs ``1 - 2 * offset`` before fees. The observed wallet quotes its top
    level one tick under the mid and steps one tick down per extra level, with
    fills concentrated in the top few levels (measured mean offset 1.5c).
    """
    mid_up = mid_up_price(market)
    if mid_up is None or levels <= 0 or level_shares <= 0:
        return {"UP": [], "DOWN": []}
    precision = max(0, int(market.decimal_precision))
    tick = 10 ** -precision
    total_ticks = 10 ** precision
    ticks_per_cent = max(1, int(round(total_ticks / 100)))
    offset_ticks = max(1, int(round(offset_cents * ticks_per_cent)))
    step_ticks = max(1, int(round(step_cents * ticks_per_cent)))
    # Work on the integer tick grid so every level is exactly step_ticks apart.
    mid_ticks = int(math_floor(mid_up / tick + 0.5))
    ladder: dict[str, list[QuoteLevel]] = {"UP": [], "DOWN": []}
    # Inventory skew: when long Up, quote the Up bid deeper and the Down bid
    # shallower, so the next fills pull the position back toward flat. This is
    # the lever that fights the adverse selection of always buying the loser.
    skew_ticks = int(round(skew_cents * ticks_per_cent))
    for side, side_mid_ticks in (("UP", mid_ticks), ("DOWN", total_ticks - mid_ticks)):
        side_skew = skew_ticks if side == "UP" else -skew_ticks
        for index in range(levels):
            ticks = side_mid_ticks - (offset_ticks + side_skew + index * step_ticks)
            price = _snap_to_tick(ticks * tick, precision)
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
    return ladder


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
    trigger_shares: float = 196.0,
    take_shares: float = 10.0,
    min_order_cost: float = 1.0,
) -> tuple[Optional[Opportunity], str]:
    """Take the under-weighted side once inventory drifts past the wallet's band.

    Measured from the tracked wallet: it lets the net position run to a median of
    196 shares before acting, then lifts only ~10 shares at a time. Balancing
    sooner (a 20-share trigger taking 100 at once) flattened the book within
    seconds, which the wallet never does and which pays the 2% taker fee far too
    often.
    """
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
