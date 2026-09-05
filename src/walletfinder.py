"""
Wallet discovery — find candidate smart money wallets by measured performance,
not reputation.

Method: take tokens that recently ran, pull each one's top traders by realized
PnL, and keep the wallets that show up across MULTIPLE unrelated tokens. One
big win is luck. Repeat appearances across tokens that have nothing to do with
each other is the part that might generalize.

Caveats worth keeping in mind:
- This is backward-looking by construction. Wallets are selected because they
  already won, which is exactly the selection bias that makes past winners look
  better than they are going forward.
- Bots and MEV wallets appear constantly in top-trader lists and are not
  copyable. Filtered roughly by trade count below, imperfectly.
- Candidates are written with enabled=false. Review before turning any on.
- /defi/v2/tokens/top_traders needs a Starter plan or above. On the free tier
  it will 401 and this module will tell you so rather than failing silently.
"""
import json
import os
import time
from collections import defaultdict
from pathlib import Path

import requests

BIRDEYE_BASE = "https://public-api.birdeye.so"
WALLETS_PATH = Path(__file__).resolve().parent.parent / "wallets.json"

CALL_DELAY_SECONDS = 0.2

# A wallet must appear as a top trader on at least this many distinct tokens
MIN_DISTINCT_TOKENS = 2
# Above this trade count on a single token it's almost certainly a bot
MAX_TRADES_PER_TOKEN = 200


def _headers() -> dict:
    return {"X-API-KEY": os.getenv("BIRDEYE_API_KEY", ""), "x-chain": "solana"}


def _get(path: str, params: dict) -> dict | None:
    try:
        resp = requests.get(f"{BIRDEYE_BASE}{path}", params=params,
                            headers=_headers(), timeout=20)
        if resp.status_code in (401, 403):
            print(f"[walletfinder] {path} needs a higher Birdeye plan "
                  f"(got {resp.status_code}). Skipping.")
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"[walletfinder] {path} failed: {e}")
        return None


def get_leaderboard(timeframe: str = "1W", limit: int = 10) -> list[dict]:
    """Birdeye's own trader leaderboard, if the plan allows it."""
    data = _get("/trader/gainers-losers", {
        "type": timeframe, "sort_by": "PnL", "sort_type": "desc",
        "offset": 0, "limit": limit,
    })
    if not data:
        return []
    items = (data.get("data") or {}).get("items") or []
    return [
        {
            "address": i.get("address"),
            "pnl": i.get("pnl"),
            "volume": i.get("volume"),
            "trade_count": i.get("trade_count"),
            "source": "leaderboard",
        }
        for i in items if i.get("address")
    ]


def get_token_top_traders(mint: str, limit: int = 10) -> list[dict]:
    """Top traders on one token, sorted by realized PnL."""
    data = _get("/defi/v2/tokens/top_traders", {
        "address": mint, "sort_by": "realized_pnl", "sort_type": "desc",
        "time_frame": "7d", "offset": 0, "limit": limit,
    })
    if not data:
        return []
    items = (data.get("data") or {}).get("items") or []
    return [
        {
            "address": i.get("owner") or i.get("address"),
            "realized_pnl": i.get("realizedPnl") or 0,
            "volume_usd": i.get("volumeUsd") or 0,
            "trade_count": i.get("trade") or 0,
        }
        for i in items if (i.get("owner") or i.get("address"))
    ]


def find_repeat_winners(mints: list[str], min_tokens: int = MIN_DISTINCT_TOKENS) -> list[dict]:
    """
    Given a set of tokens that ran, find wallets that profited on several of them.

    The min_tokens threshold is the whole filter. At 2 you get a lot of noise;
    at 3+ you get very few wallets but they're far more interesting.
    """
    appearances: dict[str, dict] = defaultdict(
        lambda: {"tokens": [], "total_pnl": 0.0, "total_trades": 0}
    )

    for mint in mints:
        traders = get_token_top_traders(mint)
        for t in traders:
            if t["trade_count"] > MAX_TRADES_PER_TOKEN:
                continue  # bot-like
            if t["realized_pnl"] <= 0:
                continue
            rec = appearances[t["address"]]
            if mint not in rec["tokens"]:
                rec["tokens"].append(mint)
            rec["total_pnl"] += t["realized_pnl"]
            rec["total_trades"] += t["trade_count"]
        time.sleep(CALL_DELAY_SECONDS)

    winners = [
        {
            "address": addr,
            "distinct_tokens": len(rec["tokens"]),
            "total_realized_pnl": round(rec["total_pnl"], 2),
            "total_trades": rec["total_trades"],
            "tokens": rec["tokens"],
        }
        for addr, rec in appearances.items()
        if len(rec["tokens"]) >= min_tokens
    ]
    winners.sort(key=lambda w: (-w["distinct_tokens"], -w["total_realized_pnl"]))
    return winners


def source_recent_winners(min_gain_pct: float = 100, limit: int = 15) -> list[str]:
    """Tokens that ran in the last 24h — the pool we mine for repeat winners."""
    from src import discovery

    pairs = discovery.discover()
    runners = [
        p for p in pairs
        if float((p.get("priceChange") or {}).get("h24") or 0) >= min_gain_pct
        and float((p.get("liquidity") or {}).get("usd") or 0) >= 20_000
    ]
    runners.sort(key=lambda p: -float((p.get("priceChange") or {}).get("h24") or 0))
    return [(p.get("baseToken") or {}).get("address")
            for p in runners[:limit]
            if (p.get("baseToken") or {}).get("address")]


def load_existing() -> list[dict]:
    if not WALLETS_PATH.exists():
        return []
    with open(WALLETS_PATH) as f:
        return json.load(f)


def save_candidates(candidates: list[dict]) -> int:
    """
    Merge new candidates into wallets.json as enabled=false.
    Never overwrites or re-disables a wallet you've already reviewed.
    """
    existing = load_existing()
    known = {w["address"] for w in existing if w.get("address")}

    added = 0
    for c in candidates:
        if c["address"] in known:
            continue
        existing.append({
            "label": f"auto-{c['address'][:6]}",
            "address": c["address"],
            "enabled": False,
            "discovered_ts": time.time(),
            "distinct_tokens": c["distinct_tokens"],
            "total_realized_pnl": c["total_realized_pnl"],
        })
        added += 1

    with open(WALLETS_PATH, "w") as f:
        json.dump(existing, f, indent=2)
    return added


def discover_wallets(min_tokens: int = MIN_DISTINCT_TOKENS) -> None:
    """Full discovery run. Writes candidates to wallets.json, disabled."""
    print("[walletfinder] sourcing tokens that ran in the last 24h...")
    mints = source_recent_winners()
    print(f"[walletfinder] mining {len(mints)} tokens for repeat winners")

    if not mints:
        print("[walletfinder] no runners found — try again later")
        return

    winners = find_repeat_winners(mints, min_tokens)
    print(f"\n[walletfinder] {len(winners)} wallets profited on {min_tokens}+ tokens\n")

    for w in winners[:20]:
        print(f"  {w['address']}")
        print(f"    {w['distinct_tokens']} tokens, "
              f"${w['total_realized_pnl']:,.0f} realized PnL, "
              f"{w['total_trades']} trades")

    board = get_leaderboard()
    if board:
        print(f"\n[walletfinder] +{len(board)} from Birdeye leaderboard")
        winners.extend([
            {"address": b["address"], "distinct_tokens": 1,
             "total_realized_pnl": b.get("pnl") or 0, "total_trades": 0, "tokens": []}
            for b in board
        ])

    added = save_candidates(winners)
    print(f"\n[walletfinder] added {added} new candidates to wallets.json "
          f"(all disabled — review and set enabled:true on the ones you want)")
    print("[walletfinder] these are past winners by construction. That is not "
          "the same as future performance.\n")


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else MIN_DISTINCT_TOKENS
    discover_wallets(min_tokens=n)