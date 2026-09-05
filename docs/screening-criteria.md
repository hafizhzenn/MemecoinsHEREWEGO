## Weights as implemented (v0.1)
| Component | Weight | Rationale |
|---|---|---|
| volume_acceleration | 25% | Earliest real signal of interest; hard to fake cheaply at scale |
| liquidity_health | 20% | Liq/mcap ratio — the single best predictor of "can I actually exit" |
| buy_pressure | 20% | Net buying, but capped: >85% buys usually means wash trading, not demand |
| trader_breadth | 15% | Many distinct txns = organic spread, few large = one whale |
| age_sweet_spot | 10% | <1h is the instant-rug window; >48h and the move already happened |
| price_momentum | 10% | Deliberately low weight — price is the *result* of the others, not a cause |

## Why scoring runs before the safety check
Birdeye calls cost compute units. Scoring is free (Dexscreener data already
in hand), so we score everything and only spend API budget on the top 10.

## Things this deliberately does NOT do
- No auto-trading. Execution stays manual.
- No conviction-weighted sizing. Fat-tailed payoffs mean position count
  matters more than picking the "best" one.
- No detection of coordinated pumps or dev intent. Structurally impossible
  from public market data alone.

- 2026-09-06: Full pipeline — discovery, weighted momentum scorer, tiered sizing,
  laddered exits, Telegram/console alerts, SQLite paper trade tracker with
  winrate-by-tier reporting. All weights are first guesses; do NOT tune them
  by intuition, tune them against logged paper trade results.