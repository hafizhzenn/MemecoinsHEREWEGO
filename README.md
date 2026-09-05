# Memecoin Screener (Solana)

A semi-automated screener for Solana memecoins. It scores new/trending tokens
on liquidity, holder distribution, and momentum, then surfaces buy/sell
suggestions with position sizing. **It does not auto-trade** — you execute
manually via Phantom/Axiom/Photon.

## ⚠️ Disclaimer
Memecoins are extremely high risk. Most tokens go to zero, rug pulls are
common, and there is no proven statistical edge here — this is a risk
filter + momentum heuristic, not a guaranteed strategy. Paper trade first.
Never risk money you can't afford to lose.

## Status
🚧 Early development — paper trading only.

## Architecture
- `src/fetcher.py` — pulls new/trending tokens (Dexscreener/Birdeye APIs)
- `src/rugcheck.py` — contract & liquidity safety checks
- `src/scorer.py` — momentum + risk scoring
- `src/notifier.py` — sends buy/sell alerts (Telegram/Discord/console)
- `docs/screening-criteria.md` — filter logic and rationale
- `docs/changelog.md` — what changed and why (for tracking what works)

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env  # add API keys
python src/fetcher.py
```

## Roadmap
- [ ] Token discovery (new pairs + trending)
- [ ] Rug/safety filter (LP lock, holder concentration, mint authority)
- [ ] Momentum scoring
- [ ] Alert delivery
- [ ] Paper trading tracker + winrate log
- [ ] Backtest harness against historical alerts
