"""
Token discovery — pulls candidate Solana pairs from Dexscreener.
No API key needed. Rate limit is ~300 req/min, we stay well under.
"""
import requests

BASE = "https://api.dexscreener.com"
HEADERS = {"User-Agent": "memecoin-screener/0.1"}


def _get(path: str, params: dict | None = None) -> dict:
    resp = requests.get(f"{BASE}{path}", params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_boosted_tokens() -> list[str]:
    """Mint addresses of tokens with active paid boosts (proxy for promo activity)."""
    try:
        data = _get("/token-boosts/latest/v1")
    except requests.RequestException:
        return []
    items = data if isinstance(data, list) else data.get("data", [])
    return [i["tokenAddress"] for i in items if i.get("chainId") == "solana"]


def get_pairs_for_tokens(mints: list[str]) -> list[dict]:
    """Batch lookup: up to 30 mint addresses per call."""
    pairs = []
    for i in range(0, len(mints), 30):
        chunk = ",".join(mints[i:i + 30])
        try:
            data = _get(f"/latest/dex/tokens/{chunk}")
        except requests.RequestException:
            continue
        pairs.extend(data.get("pairs") or [])
    return [p for p in pairs if p.get("chainId") == "solana"]


def search_pairs(query: str) -> list[dict]:
    """Free-text search, useful for trending terms."""
    try:
        data = _get("/latest/dex/search", params={"q": query})
    except requests.RequestException:
        return []
    return [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]


def dedupe_by_mint(pairs: list[dict]) -> list[dict]:
    """One pair per token — keep the most liquid."""
    best: dict[str, dict] = {}
    for p in pairs:
        mint = (p.get("baseToken") or {}).get("address")
        if not mint:
            continue
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if mint not in best or liq > ((best[mint].get("liquidity") or {}).get("usd") or 0):
            best[mint] = p
    return list(best.values())


def discover(extra_queries: list[str] | None = None) -> list[dict]:
    """Main entry point — returns deduped Solana pairs to screen."""
    pairs = []

    boosted = get_boosted_tokens()
    if boosted:
        pairs.extend(get_pairs_for_tokens(boosted))

    for q in (extra_queries or ["SOL", "pump", "bonk"]):
        pairs.extend(search_pairs(q))

    return dedupe_by_mint(pairs)


if __name__ == "__main__":
    found = discover()
    print(f"Found {len(found)} unique Solana pairs")
    for p in found[:10]:
        sym = (p.get("baseToken") or {}).get("symbol")
        liq = (p.get("liquidity") or {}).get("usd") or 0
        print(f"  {sym:<12} liq=${liq:>12,.0f}  {(p.get('baseToken') or {}).get('address')}")