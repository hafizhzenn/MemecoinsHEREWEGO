"""
Smart money tracking — watch known wallets, surface what they're buying.

Data source is Helius, not Birdeye: Birdeye gates every wallet and trader
endpoint behind a paid plan, while Helius's free tier (1M credits/month, no
card) covers this.

CREDIT BUDGET — read before changing the poll interval:
  The wallet history endpoint costs 100 credits per call.
  5 wallets polled every 30 min  = 240 calls/day  =  24k credits/day  ~720k/mo  OK
  5 wallets polled every 5 min   = 1440 calls/day = 144k credits/day  ~4.3M/mo  over
  Tracked wallets here hold for 1-5 days on average, so 30-minute granularity
  costs you nothing real. Adding wallets scales this linearly — 10 wallets at
  30 min is already 1.4M/month and over the free tier.

Design notes:
- This is a DISCOVERY source, not a scoring tweak. Tokens found here go through
  the same safety filter and sizing as momentum candidates, but get tagged
  source='smartmoney' so the tracker can compare the two head to head.
- Wallet reputation is not evidence. Everything here is logged as paper trades
  so your own results decide whether these wallets are worth following.
"""
import json
import os
import time
from pathlib import Path

import requests

HELIUS_BASE = "https://api.helius.xyz"
WALLETS_PATH = Path(__file__).resolve().parent.parent / "wallets.json"

# Free tier is 2 req/s on enhanced APIs — stay under it
CALL_DELAY_SECONDS = 0.6

# Quote tokens: receiving these is not "buying a memecoin"
QUOTE_MINTS = {
    "SOL",
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def _api_key() -> str:
    return os.getenv("HELIUS_API_KEY", "")


def load_wallets() -> list[dict]:
    if not WALLETS_PATH.exists():
        return []
    with open(WALLETS_PATH) as f:
        wallets = json.load(f)
    return [w for w in wallets
            if w.get("enabled", True)
            and w.get("address")
            and w["address"] != "PASTE_WALLET_ADDRESS_HERE"]


def get_wallet_txs(address: str, limit: int = 100, swaps_only: bool = True) -> list[dict]:
    """
    Recent parsed transactions for one wallet.
    swaps_only filters server-side, which keeps the response small without
    costing extra — the call is 100 credits either way.
    """
    key = _api_key()
    if not key:
        print("[smartmoney] HELIUS_API_KEY not set in .env")
        return []

    params = {"api-key": key, "limit": limit, "tokenAccounts": "balanceChanged"}
    if swaps_only:
        params["type"] = "SWAP"

    try:
        resp = requests.get(
            f"{HELIUS_BASE}/v1/wallet/{address}/history",
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[smartmoney] history failed for {address[:8]}...: {e}")
        return []

    return resp.json().get("data") or []


def extract_buys(txs: list[dict], within_hours: float = 6) -> list[dict]:
    """
    Token acquisitions only. A positive balanceChange amount means the wallet
    received that token; quote assets and failed txs are excluded.
    """
    cutoff = time.time() - within_hours * 3600
    buys = []

    for tx in txs:
        if tx.get("error"):
            continue
        ts = tx.get("timestamp")
        if not ts or ts < cutoff:
            continue

        for change in tx.get("balanceChanges") or []:
            amount = change.get("amount") or 0
            mint = change.get("mint")

            if amount <= 0 or not mint:
                continue
            if mint in QUOTE_MINTS:
                continue

            buys.append({
                "mint": mint,
                "symbol": "",          # filled in later from Dexscreener
                "amount": amount,
                "ts": ts,
                "tx_hash": tx.get("signature"),
            })

    return buys


def find_consensus_buys(within_hours: float = 6, min_wallets: int = 2) -> list[dict]:
    """
    Tokens bought by multiple tracked wallets recently.

    min_wallets=2 is the main quality lever: one wallet buying is noise,
    several independently buying the same thing in a short window is the
    signal. With a small wallet list this will often return nothing, which
    is the filter working rather than failing.
    """
    wallets = load_wallets()
    if not wallets:
        print("[smartmoney] no wallets enabled in wallets.json")
        return []

    by_mint: dict[str, dict] = {}

    for w in wallets:
        txs = get_wallet_txs(w["address"])
        for buy in extract_buys(txs, within_hours):
            mint = buy["mint"]
            entry = by_mint.setdefault(mint, {
                "mint": mint,
                "symbol": "",
                "buyers": [],
                "first_seen": buy["ts"],
            })
            if w["label"] not in entry["buyers"]:
                entry["buyers"].append(w["label"])
            entry["first_seen"] = min(entry["first_seen"], buy["ts"])
        time.sleep(CALL_DELAY_SECONDS)

    results = [e for e in by_mint.values() if len(e["buyers"]) >= min_wallets]
    results.sort(key=lambda e: (-len(e["buyers"]), -e["first_seen"]))
    return results


def recent_buys_report(within_hours: float = 24) -> list[dict]:
    """
    Everything the tracked wallets bought recently, newest first.
    Symbols get resolved via Dexscreener so the output is readable.
    """
    from src import discovery

    wallets = load_wallets()
    out = []
    for w in wallets:
        txs = get_wallet_txs(w["address"])
        for buy in extract_buys(txs, within_hours):
            out.append({**buy, "wallet": w["label"]})
        time.sleep(CALL_DELAY_SECONDS)

    if out:
        mints = list({b["mint"] for b in out})
        pairs = {
            (p.get("baseToken") or {}).get("address"): p
            for p in discovery.dedupe_by_mint(discovery.get_pairs_for_tokens(mints))
        }
        for b in out:
            pair = pairs.get(b["mint"])
            if pair:
                b["symbol"] = (pair.get("baseToken") or {}).get("symbol", "")
                b["price_usd"] = float(pair.get("priceUsd") or 0)
                b["liquidity"] = float((pair.get("liquidity") or {}).get("usd") or 0)
                b["price_change_h24"] = float((pair.get("priceChange") or {}).get("h24") or 0)

    out.sort(key=lambda b: -b["ts"])
    return out


def audit_tracked_wallets() -> None:
    """Sanity report on enabled wallets. Run before trusting any of them."""
    wallets = load_wallets()
    if not wallets:
        print("[smartmoney] no wallets enabled")
        return

    print(f"\n=== Tracked wallets ({len(wallets)}) ===")
    for w in wallets:
        txs = get_wallet_txs(w["address"])
        buys_7d = extract_buys(txs, within_hours=24 * 7)
        buys_24h = extract_buys(txs, within_hours=24)
        distinct = len({b["mint"] for b in buys_7d})
        last = max((b["ts"] for b in buys_7d), default=None)
        last_str = f"{(time.time() - last) / 3600:.1f}h ago" if last else "none in 7d"
        print(f"  {w['label']:<12} {distinct:>3} tokens (7d)  "
              f"{len({b['mint'] for b in buys_24h}):>2} (24h)  "
              f"last buy: {last_str}")
        time.sleep(CALL_DELAY_SECONDS)
    print()


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    cmd = sys.argv[1] if len(sys.argv) > 1 else "consensus"

    if cmd == "audit":
        audit_tracked_wallets()
    elif cmd == "recent":
        for b in recent_buys_report():
            age_h = (time.time() - b["ts"]) / 3600
            sym = b.get("symbol") or b["mint"][:8]
            print(f"  {b['wallet']:<12} {sym:<12} {age_h:>5.1f}h ago  "
                  f"liq ${b.get('liquidity', 0):,.0f}  "
                  f"24h {b.get('price_change_h24', 0):+.0f}%")
    else:
        found = find_consensus_buys()
        print(f"Found {len(found)} tokens bought by 2+ tracked wallets")
        for e in found:
            age_m = (time.time() - e["first_seen"]) / 60
            print(f"  {e['mint']}  {len(e['buyers'])} buyers "
                  f"({', '.join(e['buyers'])}) — {age_m:.0f}m ago")