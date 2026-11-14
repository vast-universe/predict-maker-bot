# Predict.fun Paper Trading Bot

A paper market maker for Predict.fun's crypto up/down markets, modelled on the
behaviour of the wallet `0x5E498154448608C826E005c93533B827Cbb6377e`
(`quantbarbie`, second on the season-2 points board, about $488k realised). It
reads live quotes over Predict's WebSocket, rests a two-sided bid ladder, books
simulated fills, and settles from Predict's own resolution endpoint. It never
sends orders.

## Maker strategy (default)

The behaviour below was measured from 4,200 of that wallet's fills across 27
rounds, not guessed:

| Observed in the wallet | Value |
| --- | --- |
| Maker share of fills | 94.8% |
| Quote side | 100% Bids, never Asks |
| Two-sided markets | 26 of 26 |
| Levels resting per side | 5 (deepest observed sweep took 5 ticks) |
| Shares per level | 100 (lifetime volume per level is a multiple of 100) |
| Tick spacing / top offset | 1 cent / 1 cent below the mid |
| Bid offset from mid | 1.50 cents per side (both sides equal, spread 0.00) |
| Refill of a taken level | median 8s, 30% within 2s |
| Fills per round | 128 over 279s, 22.8 shares each, in ~15 bursts of ~9.8 |
| Inventory | balanced by taking the short side: 196-share trigger, 10 shares a time |
| Reward band | `spreadThreshold` 0.06, `shareThreshold` 100, `makerFeeBps` 0 |

Each side rests five bids one cent apart starting one cent under its own mid, so
a matched pair costs `1 - 2 * 1c`. A level is refilled once the mid stops moving
and abandoned once it drifts. Inventory is balanced by lifting the offer on the
under-weighted side and paying the 2% taker fee, at the wallet's own thresholds.

Fills are modelled from book depth, not from price crossing: our simulated order
is not in the real book, so a print at our tick is split by queue share, using
the measured median resting depth (269 shares) rather than the often-drained
instant depth. Price-crossing rules were tried and gave every pair exactly 1.00.

Current fidelity against the same measurements:

| Metric | Bot | Wallet |
| --- | --- | --- |
| Shares per burst | 79 | 79 |
| Shares per side per round | 1,185 | 1,457 |
| Median fill | 17.7 shares | 22.8 |
| Pair cost (2s window) | 0.9709 | 0.9630 |
| Up/Down ratio per round | 0.79 | 0.997 median (0.17-1.62 across rounds) |

The Up/Down ratio looks unbalanced taken alone, but the wallet's own per-round
ratio spans 0.17 to 1.62 with a p25 of 0.909, so a single round at 0.79 sits well
inside its distribution: the wallet lets exposure run (median 196 shares before
it acts, 200 after) rather than keeping the book flat.

What still differs is fill granularity: one 6s snapshot books fewer, smaller
fills than the wallet's continuous stream. That is a limit of paper fill
simulation rather than of the strategy, since a real order sits inside the book
and is matched by time priority, which a simulated order cannot observe.

Known gaps: maker rewards (`hourlyRate` 3000 per market) are not credited, only
the time a reward-qualified ladder rests is tracked; slippage, order latency and
partial-fill risk are not modelled.

## Directional strategy (optional, `--mode taker`)

`--mode taker` keeps the earlier directional entry strategy: it estimates the
chance the round closes up, compares that against the fee-inclusive ask, and
requires a 3-cent edge per share. It targets 10 net shares, reserves cash for an
equal-size hedge, and can trim the unmatched leg once the opposite quote is rich.
Missing quotes never create an executable price.

## Run

```bash
cd predict-maker-bot
.venv/bin/python main.py
```

That rests the maker ladder on BTC's 5-minute round with a $10,000 paper balance.
A fresh `--data-dir` is needed to pick up a new starting balance, since an
existing ledger keeps its own cash:
 To follow more series, name ones that exist (`GET /v1/series` lists `btc/eth/bnb-updown-{5m,15m,1h,1d}` plus stock and commodity 15m/1d slugs; there is no SOL or XRP series):

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
