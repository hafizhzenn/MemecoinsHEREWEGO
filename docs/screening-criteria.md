# Screening Criteria

This doc tracks *why* each filter exists so we can tell what's actually
working vs. superstition, same way we tracked the BTC bot's filters.

## Stage 1 — Safety filter (hard reject if failed)
| Check | Why |
|---|---|
| LP liquidity locked or burned | Prevents dev pulling liquidity (rug) |
| Mint authority renounced | Prevents dev minting infinite supply |
| Top 10 holders < 30% of supply (excl. LP) | Reduces dump risk from whales |
| Min liquidity (e.g. $10k+) | Avoids unsellable illiquid tokens |
| Contract age > X minutes | Avoids instant-rug sniper bait |

## Stage 2 — Momentum score (0–100)
| Signal | Weight | Why |
|---|---|---|
| Volume growth (5m/1h) | TBD | Early momentum indicator |
| Unique buyer growth | TBD | Distinguishes real interest from wash trading |
| Buy/sell ratio | TBD | Net buying pressure |
| Holder count growth | TBD | Organic spread vs. bot activity |

## Stage 3 — Sizing & exit rules
- Position size: TBD (likely % of bankroll, smaller than the BTC bot given
  higher variance)
- Take-profit / stop-loss: TBD
- Max concurrent positions: TBD

## Open questions
- How do we distinguish organic hype from coordinated pump groups?
- What's a realistic winrate target given no historical edge is proven yet?

## Change log
- 2026-09-06: Initial criteria drafted, no live data yet.
