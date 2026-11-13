# Predict.fun Paper Trading Bot

This is a fresh Predict.fun implementation inspired by the public strategy descriptions on [Polymtradebot](https://polymtradebot.com/), not its private source code. It reads active crypto up/down markets and simulates fixed-size directional entries with a conditional opposite-side profit lock. It never sends orders.

The paper strategy targets 10 net shares per order on the direction whose estimated win probability clears the fee-inclusive ask by at least 3 cents per share. If that order would cost less than $1, it increases the net quantity to the next whole share meeting the minimum. There is no fixed 20-share cap: available cash, per-order cost limits, and the shared exposure budget determine whether an order is affordable. Predict deducts buy fees from shares received, so gross order quantity is adjusted to target the chosen net share count. The simulation assumes each order fills at the displayed ask; depth, latency, and partial-fill risk are not simulated.

UP and DOWN quotes are independent: a missing Yes bid makes the DOWN ask unavailable, while a missing Yes ask makes the UP ask unavailable. A market with just one quoted side still reaches strategy evaluation. The bot compares the estimated probability minus fee-inclusive cost for each available side and selects the largest edge; a cheap quote alone is not a buy signal. Missing, invalid, or zero-size quotes never create an executable price. A hedge waits if its required opposite-side ask is unavailable. Portfolio marks are shown as unavailable when the held side lacks a real bid.

Each scan first checks all open markets for profitable opposite-side hedges, then considers new directional orders. A market may receive repeated entries while the same direction qualifies and budget remains. Hedging targets 10 net shares, increases quantity when needed for the $1 minimum, and never exceeds the unmatched net position. A smaller remainder can be hedged if its order still meets the minimum; otherwise it waits. Hedges require the unmatched shares' average cost plus the opposite side's fee-inclusive cost to be below $1 by at least 1 cent per share. Historical paired inventory is excluded from the unmatched cost basis.

Each order's cost is capped at 10% of starting cash and the $100 configured order limit; all open positions together share a 10% exposure budget. Before accepting a directional entry, the bot reserves enough cash and exposure budget for an equal-size hedge at the profit-lock threshold, including reserves for other markets. Reserved cash cannot fund new entries. A new entry is skipped if its profitable equal-size hedge would itself fall below the $1 order minimum. Reservations do not guarantee that a profitable hedge quote will appear. Paired positions continue to consume their full cost budget until settlement.

Predict.fun resolves these up/down markets from the configured price-feed provider's start and end prices. The directional estimate falls back to the book-implied probability, so a scan still evaluates quotes instead of going silent; without an external reference price it rarely finds a directional edge. Predict's `feeRateBps` and the gross/net share conversion are included in entry and hedge costs. Partial fills, order-book depth, and execution latency are not modeled.

## Run

```bash
cd predict-maker-bot
python3.13 main.py
```

That tracks BTC's 5-minute round. To follow more series, name ones that exist (`GET /v1/series` lists `btc/eth/bnb-updown-{5m,15m,1h,1d}` plus stock and commodity 15m/1d slugs; there is no SOL or XRP series):

```bash
python3.13 main.py --assets BTC,ETH,BNB --windows 5,15
```

To start a separate $100 paper ledger with a base target of 10 net shares:

```bash
python3.13 main.py --shares 10 --starting-cash 100 --data-dir paper_data_small
```

With the default 10% exposure limit, a $100 paper account has $10 for open cost plus hedge reserves. Low-priced entries may require substantially more hedge reserve than entry cash and will be skipped if unaffordable. `--starting-cash` does not reset existing ledger balances; use a new `--data-dir` for a fresh simulation. The default starting cash remains $1,000.

Sizing uses `net_quantity = max(base_shares, ceil($1 / cost_per_net_share))`, followed by the gross/net fee conversion. For example, at a 200 bps fee rate, asks of $0.07, $0.06, $0.04, $0.03, and $0.02 target 14, 17, 25, 33, and 49 net shares respectively. Gross order sizes are larger; each order's cash value is at least $1. These are sizing examples, not automatic buy signals.

By default the bot tracks BTC on the 5-minute window. Override with `--assets BTC,ETH,BNB --windows 5,15`; the slug must exist as a Predict series (`GET /v1/series`), otherwise discovery reports no eligible round for it. Quotes arrive over the Predict WebSocket (`wss://ws.predict.fun/ws`): the bot subscribes to `predictOrderbook/{marketId}`, `predictTradingStatus/{marketId}`, `predictMarketStatus/{marketId}` and `predictMarketChanged/{marketId}` for every tracked round, and rediscovers active rounds every 10 seconds or immediately when a `predictMarketChanged` notice arrives. Scans are push driven: the loop blocks until the socket delivers a message, then rescans after a short debounce, so it never waits out the full interval while a fresh book sits unused. `--interval` is the maximum wait and `--min-scan` the minimum gap between pushed scans. If the socket is down, missing, or a market has no quote younger than 15 seconds, that market falls back to the REST `markets/quotes` endpoint for the scan. Press `Ctrl+C` to stop it. To run one scan only, use `--cycles 1`; use `--interval 10` to cap pushed rescans at 10 seconds. The bot reads live Predict.fun quotes with the configured `PREDICT_API_KEY`; there is no offline source.

### Live market data (WebSocket)

* Endpoint: `wss://ws.predict.fun/ws` (BNB mainnet only), API key sent as the `x-api-key` handshake header.
* Topics: `predictOrderbook/{marketId}` (whole Yes book per message), `predictTradingStatus/{marketId}`, `predictMarketStatus/{marketId}` and `predictMarketChanged/{marketId}`. `predictTopOfBook/{marketId}` is also supported by the client when only best prices are needed.
* The latest value of each topic is sent right after the subscribe response; every message is a full snapshot, so the newest one replaces the previous copy.
* `heartbeat` arrives every 15 seconds and must be echoed back with the same `data`, otherwise the server closes the connection. On `system` reconnect notices and any other close, the client reconnects with backoff, resubscribes, and the scan refetches quotes from REST until the stream is fresh again.
* The book frame carries no `decimalPrecision`, so the value discovered over REST is kept per market id and used to derive the Down side: Down ask = `1 - best Yes bid`, Down bid = `1 - best Yes ask`. Without that, a market with two decimals would be complemented at the wrong tick size.
* `predictMarketChanged` forces a rediscovery (throttled to one per 2 seconds) so a rolled or resolved round is replaced without waiting for the 10-second TTL.
* The scan loop waits on the socket: a pushed message ends the wait, then `scan_debounce` lets a burst settle before pricing. Settlement and account reads stay on the slower `--interval` cadence so the hot path is stream-only.
* `websockets` is required for streaming (`pip install -r requirements.txt`). Without it the bot logs the reason and keeps scanning on REST quotes.

### Console output

Each scan prints a cycle banner carrying the stream status, then one line per asset (`[BTC] Bitcoin Up or Down - ... @5s [1/1] - 6 sig`), then executes the whole batch of orders in parallel:

```
--- Cycle 11989 ---  ws=live msgs=2310 reconn=0 last=0.1s
[BTC] Bitcoin Up or Down - March 13, 10:35AM-10:40AM ET @5s [1/1] - 6 sig
* Executing 2 orders in parallel...
[PAPER] BUY Up @ 0.36 x 5.9 (~$2.13) - dir Up@0.362
[PAPER] SELL Up @ 0.36 x 13.0 (~$4.68) - rebal sell_up@0.36 down>0.64
```

The banner reports `ws=live msgs=<frames> reconn=<reconnects> last=<seconds since the last frame>`, or `ws=down rest-fallback` / `ws=off` when the stream is unavailable and quotes come from REST. The bracketed `[quoted/scanned]` counts markets with both sides quoted.

Order kinds in the `[PAPER]` line: `dir` (directional entry), `hedge` (profit-lock on the opposite side), `arb` (buy both legs when `comb=1.0xx` clears the configured pair edge, printed as one line per leg), and `rebal sell_up/sell_down` (trim the unmatched leg once the opposite quote crosses `rebalance_trigger`). Only cycle banners, asset lines, the parallel-execution banner, and fills go to the terminal; skips, hedge/rebalance waits, feed diagnostics, and settlement details are kept off the console.

The continuous scan loop reads market data and records paper fills. Its paper ledgers are stored directly in `<data-dir>/`: `pnl_log.csv` records every simulated fill and settlement result (`SELL_UP`/`SELL_DOWN` rows carry negative cost as proceeds), and `portfolio.json` aggregates open Up/Down inventory and unmatched cost by market. Existing ledgers are migrated from fill history when the unmatched cost field is missing. Once Predict marks a market `RESOLVED`, the loop reads outcome statuses, credits the simulated payout, records realized P&L, and releases simulated exposure. This does not redeem or trade any real positions.

`pnl_summary.txt` beside the ledger mirrors the console layout: a `# P&L Summary — ...` header, `Settled markets`, `Total cost`, `Total payout`, `Realized P&L`, then one `--- <N>-min markets ---` block per window with a `Count | Cost | Payout | P&L` line and the per-market `Title → winner  +$0.48` results. Unlike the earlier version it no longer appends paper-position and account sections; open inventory stays in `portfolio.json` and `pnl_log.csv`.

```bash
python3 main.py --cycles 1
```

Ledger files sit directly in `paper_data/`: `pnl_log.csv`, `pnl_summary.txt`, and `portfolio.json`. Market discovery skips expired or future rounds and continues past candidates without a start price to find an eligible current round. To keep the console short, only the cycle banner, per-asset lines, and `[PAPER]` fills are printed.

## Modules

- `main.py`: scan loop, WebSocket-driven wakeups, and paper-only orchestration.
- `markets.py`: Predict.fun market discovery, the real-time WebSocket feed (order book, top of book, trading status, market changed, heartbeats, reconnect, push wakeups), live quotes, and market snapshots.
- `strategy.py`: minimum-order sizing, directional entries, profit-lock hedging, Predict fees, and hedge reserves.
- `trader.py`: paper fill logging.
- `paper_portfolio.py`: simulated cash, exposure, deduplication, and ledger persistence.
- `config.py`: base share quantity, edge thresholds, cash limits, and exposure cap.

The strategy reference material is [BTC 5-minute strategy](https://polymtradebot.com/blog/btc-up-or-down-5-minutes-strategy), [BTC 15-minute strategy](https://polymtradebot.com/blog/btc-up-or-down-15-minutes-strategy), [Predict.fun resolution rules](https://dev.predict.fun/concepts/resolution), and [Predict.fun fee schedule](https://docs.predict.fun/the-basics/predict-fees-and-limits).
# predict-maker-bot
