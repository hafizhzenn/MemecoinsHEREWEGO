"""
Stage 2 momentum scoring — ranks tokens that passed the safety filter.
Every component returns 0..1, then gets weighted into a 0..100 score.
See docs/screening-criteria.md for rationale behind each weight.
"""
import time

# Weights must sum to 1.0
WEIGHTS = {
    "volume_acceleration": 0.25,
    "buy_pressure": 0.20,
    "trader_breadth": 0.15,
    "liquidity_health": 0.20,
    "age_sweet_spot": 0.10,
    "price_momentum": 0.10,
}

# A token this far past its pump is likely exit liquidity
LATE_ENTRY_H24_PCT = 400


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _safe(d: dict | None, *keys, default=0):
    cur = d or {}
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default if k == keys[-1] else {})
    return cur if cur not in (None, "") else default


def volume_acceleration(pair: dict) -> float:
    """Is the last hour hotter than the trailing 6h average?"""
    vol_h1 = float(_safe(pair, "volume", "h1"))
    vol_h6 = float(_safe(pair, "volume", "h6"))
    if vol_h6 <= 0:
        return 0.0
    baseline = vol_h6 / 6
    if baseline <= 0:
        return 0.0
    ratio = vol_h1 / baseline
    # ratio 1.0 = normal, 3.0+ = strongly accelerating
    return _clamp((ratio - 1.0) / 2.0)


def buy_pressure(pair: dict) -> float:
    """Net buying in the last hour. 0.5 = balanced, 1.0 = all buys."""
    buys = float(_safe(pair, "txns", "h1", "buys"))
    sells = float(_safe(pair, "txns", "h1", "sells"))
    total = buys + sells
    if total < 20:  # too few trades to be meaningful
        return 0.0
    share = buys / total
    # map 0.5..0.75 onto 0..1; above 75% buys is often wash trading
    if share > 0.85:
        return 0.3
    return _clamp((share - 0.5) / 0.25)


def trader_breadth(pair: dict) -> float:
    """Many distinct trades beats a few large ones (organic spread)."""
    txns_h1 = float(_safe(pair, "txns", "h1", "buys")) + float(_safe(pair, "txns", "h1", "sells"))
    # 50 txns/h = weak, 500+/h = strong
    return _clamp((txns_h1 - 50) / 450)


def liquidity_health(pair: dict) -> float:
    """Liquidity as a share of market cap. Thin liquidity = can't exit."""
    liq = float(_safe(pair, "liquidity", "usd"))
    mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
    if liq <= 0 or mcap <= 0:
        return 0.0
    ratio = liq / mcap
    # <2% is dangerous, 10%+ is healthy
    return _clamp((ratio - 0.02) / 0.08)


def age_sweet_spot(pair: dict) -> float:
    """Too new = rug risk, too old = momentum gone. Sweet spot ~2-48h."""
    created_ms = pair.get("pairCreatedAt")
    if not created_ms:
        return 0.0
    age_h = (time.time() * 1000 - created_ms) / 3_600_000
    if age_h < 1:
        return 0.0          # instant-rug window
    if age_h <= 6:
        return 1.0
    if age_h <= 48:
        return _clamp(1.0 - (age_h - 6) / 42 * 0.5)
    if age_h <= 168:
        return 0.3
    return 0.1


def price_momentum(pair: dict) -> float:
    """Rising but not already parabolic."""
    h1 = float(_safe(pair, "priceChange", "h1"))
    h24 = float(_safe(pair, "priceChange", "h24"))
    if h24 > LATE_ENTRY_H24_PCT:
        return 0.1          # probably late to the party
    if h1 <= 0:
        return 0.0
    return _clamp(h1 / 50)  # +50% in an hour maxes this out


COMPONENTS = {
    "volume_acceleration": volume_acceleration,
    "buy_pressure": buy_pressure,
    "trader_breadth": trader_breadth,
    "liquidity_health": liquidity_health,
    "age_sweet_spot": age_sweet_spot,
    "price_momentum": price_momentum,
}


def score_pair(pair: dict) -> dict:
    """Returns {'score': 0-100, 'breakdown': {component: (raw, weighted)}}."""
    breakdown = {}
    total = 0.0
    for name, fn in COMPONENTS.items():
        try:
            raw = fn(pair)
        except (TypeError, ValueError, ZeroDivisionError):
            raw = 0.0
        weighted = raw * WEIGHTS[name]
        breakdown[name] = (round(raw, 3), round(weighted * 100, 1))
        total += weighted
    return {"score": round(total * 100, 1), "breakdown": breakdown}


def explain(result: dict) -> str:
    """Human-readable score breakdown for alerts/logs."""
    lines = [f"Score: {result['score']}/100"]
    for name, (raw, weighted) in sorted(
        result["breakdown"].items(), key=lambda kv: -kv[1][1]
    ):
        lines.append(f"  {name:<22} raw={raw:<6} contributes {weighted}")
    return "\n".join(lines)