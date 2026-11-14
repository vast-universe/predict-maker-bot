# Architecture

1. `main.py` drives one scan loop. `--mode maker` (default) runs `_maker_loop`,
   `--mode taker` runs the directional `_scan_loop`.
1a. `markets.py`'s `PredictWebSocketFeed` runs one background WebSocket connection
   to `wss://ws.predict.fun/ws`. It echoes heartbeats, reconnects with backoff,
   resubscribes on every new connection, and keeps the latest order book per
   market in a thread-safe store. The feed subscribes `predictOrderbook`,
   `predictTradingStatus`, `predictMarketStatus` and `predictMarketChanged`, and
   only calls REST `markets/quotes` for markets without a fresh streamed quote.
   The streamed book frame carries no `decimalPrecision`, so discovery's
   per-market value is threaded into the quote builder.
1b. The scan loop is push driven. `wait_for_update` blocks on the socket; a
   pushed message ends the wait, `scan_debounce` lets a burst settle, and
   `min_scan_interval` floors the scan rate while `interval` caps it. Settlement
   and account reads stay on the slower `interval` cadence.
2. `markets.py` discovers active rounds via `/v1/series/{slug}/rounds`, accepting
   only current Chainlink crypto up/down markets with a start price. Snapshots
   carry best bid/ask on both outcomes plus the whole Yes book, which the maker
   diffs to detect fills.
3. `strategy.py`'s `quote_ladder` builds the two-sided bid ladder: five levels
   one cent apart from one cent under each side's own mid, 100 shares a level,
   symmetric, with optional inventory skew and a size cut below 0.05.
4. `paper_portfolio.py` keeps the simulated resting book. `collect_maker_fills`
   diffs book depth, credits our queue share (floored at the measured 269-share
   resting depth), ignores prints below `burst_min_shares`, and takes at most
   `burst_levels` levels a side per snapshot. `sync_maker_quotes` refills a taken
   level on the same tick when the mid has not drifted and replaces the whole
   ladder when it has, honouring `replenish_delay`.
5. Inventory is balanced by `evaluate_rebalance_take`, which lifts the ask on the
   under-weighted side once |net| passes 196 shares, taking about 10 shares at a
   time with a cooldown, paying the 2% taker fee.
6. Settlement polls `GET /v1/markets/{id}` until Predict reports `RESOLVED`, then
   credits `resolution.indexSet` (falling back to `outcomes[].status`) and records
   realised P&L. `TIE` splits the payout.
7. `paper_portfolio.py` writes `pnl_log.csv` per fill and settlement,
   `portfolio.json` for cash and open inventory, and `pnl_summary.txt` with
   settled results, an open-position block (size, average, mark, unrealised P&L,
   net) and a maker block (pairs, pair cost, unmatched, reward time, Up/Down
   ratio). `maker_pair_stats` pairs opposite-side fills inside a window so the
   pair cost is comparable to the wallet's own measurement.
8. `trader.py` prints `[PAPER] BUY/SELL` lines and records them through the
   portfolio. Execution assumes the resting price fills; depth beyond the queue
   share, latency and partial-fill risk are not simulated.

This is a behavioural replication of one wallet's observed market making, not a
reproduction of its source and not a claim of profitability. The paper fill model
is the main approximation: a real order sits inside the book and is matched by
time priority, which a simulated order cannot observe, so per-round volume still
runs about 1.5x high even though the pair cost and fill count match.
