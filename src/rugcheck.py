"""
Stage 1 safety filter — hard reject tokens that fail these checks.
See docs/screening-criteria.md for rationale.
"""

MIN_LIQUIDITY_USD = 10_000
MAX_TOP10_HOLDER_PCT = 30


def passes_safety_filter(pair: dict) -> tuple[bool, list[str]]:
    """Returns (passed, reasons_if_failed)."""
    reasons = []

    liquidity = pair.get("liquidity", {}).get("usd", 0)
    if liquidity < MIN_LIQUIDITY_USD:
        reasons.append(f"liquidity ${liquidity:.0f} below ${MIN_LIQUIDITY_USD}")

    # TODO: LP lock check, mint authority check, holder concentration check
    # (needs on-chain data — Solscan/Birdeye/Helius API)

    return (len(reasons) == 0, reasons)
