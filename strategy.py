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


def hedge_reserve(position: dict, min_locked_profit_per_share: float) -> float:
    """Cash needed to hedge all unmatched shares at the lock threshold."""
    quantity = abs(float(position.get("up_quantity", 0.0))
                   - float(position.get("down_quantity", 0.0)))
    return quantity * max(0.0, 1.0 - min_locked_profit_per_share
                          - unhedged_average_cost(position))


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


def evaluate_market(
    market: MarketSnapshot,
    *,
    risk_budget: float,
    min_lead_bps: float = 0.0,
    min_edge_per_share: float = 0.03,
    min_order_cost: float = 1.0,
    fixed_shares: float = 10.0,
) -> tuple[Optional[Opportunity], str]:
    """Choose a directional entry, increasing base size for the $1 minimum."""
    if risk_budget <= 0:
        return None, "no available risk budget"
    if fixed_shares <= 0:
        return None, "fixed share size must be positive"
    probability_up = fair_up_probability(market)
    if probability_up is None:
        return None, "directional: missing reference price, volatility, or time remaining"

    lead_bps = _lead_bps(market)
    if abs(lead_bps) < min_lead_bps:
        return None, f"directional: lead {abs(lead_bps):.2f}bp < {min_lead_bps:.2f}bp"
    candidates = []
    for side, ask, probability in (
        ("UP", market.up_ask, probability_up),
        ("DOWN", market.down_ask, 1.0 - probability_up),
    ):
        if valid_price(ask):
            all_in_cost = cost_per_net_share(ask, market.fee_rate_bps)
            candidates.append((probability - all_in_cost, side, ask, probability, all_in_cost))
    if not candidates:
        return None, "directional: neither UP nor DOWN has a valid ask"
    edge, side, ask, fair_probability, all_in_cost = max(candidates, key=lambda item: item[0])
    if edge + 1e-12 < min_edge_per_share:
        return None, (
            f"directional: {side} fair={fair_probability:.1%} ask={ask:.3f} "
            f"all-in={all_in_cost:.4f}; net edge {edge * 100:.2f}c/share "
            f"< {min_edge_per_share * 100:.2f}c/share"
        )

    quantity = order_net_quantity(fixed_shares, ask, market.fee_rate_bps, min_order_cost)
    gross_quantity = gross_quantity_for_net(quantity, ask, market.fee_rate_bps)
    notional = gross_quantity * ask
    if notional + 1e-9 < min_order_cost:
        return None, f"Predict order must be at least ${min_order_cost:.2f} before fees"
    cost = quantity * all_in_cost
    if cost > risk_budget + 1e-9:
        return None, (
            f"{quantity:g}-share {side} entry costs ${cost:.2f}; "
            f"available budget is ${risk_budget:.2f}"
        )
    return Opportunity(
        market=market, side=side, quantity=quantity, ask=ask,
        fair_probability=fair_probability, fee_per_share=all_in_cost - ask,
        cost_per_share=all_in_cost, expected_edge_per_share=edge, lead_bps=lead_bps,
        up_ask=market.up_ask if side == "UP" else None,
        down_ask=market.down_ask if side == "DOWN" else None,
        up_cost_per_share=all_in_cost if side == "UP" else 0.0,
        down_cost_per_share=all_in_cost if side == "DOWN" else 0.0,
        up_order_quantity=gross_quantity if side == "UP" else 0.0,
        down_order_quantity=gross_quantity if side == "DOWN" else 0.0,
    ), f"qualifies: directional {side} {quantity:g} shares edge={edge * 100:.2f}c/share"


def evaluate_hedge(
    market: MarketSnapshot,
    *,
    position: dict,
    risk_budget: float,
    min_order_cost: float = 1.0,
    min_locked_profit_per_share: float = 0.0,
    fixed_shares: float = 10.0,
) -> tuple[Optional[Opportunity], str]:
    """Hedge unmatched shares, sizing for the order minimum without overbuying."""
    up_quantity = float(position.get("up_quantity", 0.0))
    down_quantity = float(position.get("down_quantity", 0.0))
    net_up_quantity = up_quantity - down_quantity
    if abs(net_up_quantity) <= 1e-9:
        if up_quantity > 1e-9 and down_quantity > 1e-9:
            return None, "hedge: sides are already balanced"
        return None, "hedge: no directional position"
    if fixed_shares <= 0:
        return None, "hedge: fixed share size must be positive"

    held_side = "UP" if net_up_quantity > 0 else "DOWN"
    hedge_side = "DOWN" if held_side == "UP" else "UP"
    held_average_cost = unhedged_average_cost(position)
    ask = market.down_ask if hedge_side == "DOWN" else market.up_ask
    if not valid_price(ask):
        return None, f"hedge: missing valid {hedge_side} ask; waiting for a real quote"
    hedge_all_in_cost = cost_per_net_share(ask, market.fee_rate_bps)
    target_quantity = order_net_quantity(
        fixed_shares, ask, market.fee_rate_bps, min_order_cost
    )
    held_quantity = min(abs(net_up_quantity), target_quantity)
    locked_profit = 1.0 - held_average_cost - hedge_all_in_cost
    if locked_profit + 1e-12 < min_locked_profit_per_share:
        return None, (
            f"hedge: held {held_side} avg=${held_average_cost:.4f}; opposite {hedge_side} "
            f"all-in=${hedge_all_in_cost:.4f}; locked edge {locked_profit * 100:.2f}c/share "
            f"< {min_locked_profit_per_share * 100:.2f}c/share"
        )

    gross_quantity = gross_quantity_for_net(held_quantity, ask, market.fee_rate_bps)
    if gross_quantity * ask + 1e-9 < min_order_cost:
        return None, f"Predict hedge order must be at least ${min_order_cost:.2f} before fees"
    hedge_cost = held_quantity * hedge_all_in_cost
    if hedge_cost > risk_budget + 1e-9:
        return None, f"hedge costs ${hedge_cost:.2f}; available budget is ${risk_budget:.2f}"
    return Opportunity(
        market=market, side=hedge_side, quantity=held_quantity, ask=ask,
        fair_probability=1.0, fee_per_share=hedge_all_in_cost - ask,
        cost_per_share=hedge_all_in_cost, expected_edge_per_share=locked_profit,
        lead_bps=_lead_bps(market),
        up_ask=market.up_ask if hedge_side == "UP" else None,
        down_ask=market.down_ask if hedge_side == "DOWN" else None,
        up_cost_per_share=hedge_all_in_cost if hedge_side == "UP" else 0.0,
        down_cost_per_share=hedge_all_in_cost if hedge_side == "DOWN" else 0.0,
        up_order_quantity=gross_quantity if hedge_side == "UP" else 0.0,
        down_order_quantity=gross_quantity if hedge_side == "DOWN" else 0.0,
        strategy="hedge",
    ), f"qualifies: {hedge_side} hedge locks ${locked_profit * held_quantity:.2f}"


def evaluate_arb(
    market: MarketSnapshot,
    *,
    risk_budget: float,
    min_order_cost: float = 1.0,
    fixed_shares: float = 10.0,
    min_pair_edge: float = 0.01,
) -> tuple[Optional[Opportunity], str]:
    """Buy both legs when the combined ask plus fees pays more than $1 per pair."""
    if not (valid_price(market.up_ask) and valid_price(market.down_ask)):
        return None, "arb: both Up and Down asks are required"
    if fixed_shares <= 0:
        return None, "arb: fixed share size must be positive"
    up_cost = cost_per_net_share(market.up_ask, market.fee_rate_bps)
    down_cost = cost_per_net_share(market.down_ask, market.fee_rate_bps)
    pair_cost = up_cost + down_cost
    if not isfinite(pair_cost) or pair_cost <= 0:
        return None, "arb: combined cost is not finite"
    comb_ratio = 1.0 / pair_cost
    if comb_ratio - 1.0 + 1e-12 < min_pair_edge:
        return None, (
            f"arb: combined cost ${pair_cost:.4f} leaves no edge "
            f"(comb={comb_ratio:.3f} < {1.0 + min_pair_edge:.3f})"
        )
    quantity = order_net_quantity(fixed_shares, pair_cost, 0.0, min_order_cost)
    cost = quantity * pair_cost
    if cost > risk_budget + 1e-9:
        return None, (
            f"arb: {quantity:g}-share pair costs ${cost:.2f}; "
            f"available budget is ${risk_budget:.2f}"
        )
    return Opportunity(
        market=market, side="PAIR", quantity=quantity, ask=pair_cost,
        fair_probability=1.0, fee_per_share=0.0, cost_per_share=pair_cost,
        expected_edge_per_share=1.0 - pair_cost, lead_bps=_lead_bps(market),
        up_ask=market.up_ask, down_ask=market.down_ask,
        up_cost_per_share=up_cost, down_cost_per_share=down_cost,
        up_order_quantity=gross_quantity_for_net(quantity, market.up_ask, market.fee_rate_bps),
        down_order_quantity=gross_quantity_for_net(quantity, market.down_ask, market.fee_rate_bps),
        strategy="arb", comb_ratio=comb_ratio,
    ), f"qualifies: arb comb={comb_ratio:.3f} {quantity:g} shares"


def evaluate_rebalance(
    market: MarketSnapshot,
    *,
    position: dict,
    trigger: float = 0.60,
    min_profit_per_share: float = 0.0,
) -> tuple[Optional[Opportunity], str]:
    """Trim the unmatched leg when the opposite side trades above the trigger."""
    up_quantity = float(position.get("up_quantity", 0.0))
    down_quantity = float(position.get("down_quantity", 0.0))
    net_up_quantity = up_quantity - down_quantity
    if abs(net_up_quantity) <= 1e-9:
        return None, "rebal: legs are already balanced"
    if net_up_quantity > 0:
        if down_quantity <= 1e-9:
            return None, "rebal: keep a Down leg so the market can still settle"
        if not valid_price(market.down_ask) or market.down_ask <= trigger:
            return None, f"rebal: Down ask {market.down_ask!r} not above {trigger:.2f}"
        sell_price = 1.0 - float(market.down_ask)
        side, label = "UP", "sell_up"
        held_quantity, held_cost = up_quantity, float(position.get("up_cost", 0.0))
        condition = f"down>{market.down_ask:.2f}"
    else:
        if up_quantity <= 1e-9:
            return None, "rebal: keep an Up leg so the market can still settle"
        if not valid_price(market.up_ask) or market.up_ask <= trigger:
            return None, f"rebal: Up ask {market.up_ask!r} not above {trigger:.2f}"
        sell_price = 1.0 - float(market.up_ask)
        side, label = "DOWN", "sell_down"
        held_quantity, held_cost = down_quantity, float(position.get("down_cost", 0.0))
        condition = f"up>{market.up_ask:.2f}"
    if not valid_price(sell_price):
        return None, f"rebal: no valid bid for {side}"
    average_cost = held_cost / held_quantity if held_quantity else 0.0
    if sell_price - average_cost + 1e-12 < min_profit_per_share:
        return None, (
            f"rebal: {label} at {sell_price:.3f} does not clear average cost "
            f"{average_cost:.3f}"
        )
    quantity = min(abs(net_up_quantity), held_quantity)
    return Opportunity(
        market=market, side=side, quantity=quantity, ask=sell_price,
        fair_probability=0.0, fee_per_share=0.0, cost_per_share=average_cost,
        expected_edge_per_share=sell_price - average_cost,
        lead_bps=_lead_bps(market),
        up_ask=market.up_ask, down_ask=market.down_ask,
        up_cost_per_share=average_cost if side == "UP" else 0.0,
        down_cost_per_share=average_cost if side == "DOWN" else 0.0,
        strategy="rebal", condition=condition, sell=True,
    ), f"qualifies: rebal {label} {quantity:g} shares at {sell_price:.3f}"


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
            # Respect the quote band. Below it a 10-share level would be worth
            # less than Predict's 1 USDT minimum order, so resting there would be
            # rejected; the band keeps every level legal.
            if not (min_price - 1e-9 <= price <= max_price + 1e-9):
                continue
            ladder[side].append(QuoteLevel(price=price, shares=level_shares))
    return ladder


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
