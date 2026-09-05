"""
Pulls new/trending Solana token pairs from Dexscreener (no API key needed).
"""
import requests

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"


def get_trending_solana_pairs(query: str = "solana") -> list[dict]:
    """Fetch pairs matching a query, filtered to Solana chain."""
    resp = requests.get(DEXSCREENER_SEARCH_URL, params={"q": query}, timeout=10)
    resp.raise_for_status()
    pairs = resp.json().get("pairs", [])
    return [p for p in pairs if p.get("chainId") == "solana"]


if __name__ == "__main__":
    pairs = get_trending_solana_pairs()
    for p in pairs[:10]:
        print(p.get("baseToken", {}).get("symbol"), p.get("priceUsd"), p.get("liquidity", {}).get("usd"))
