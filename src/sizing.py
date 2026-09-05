"""
Stage 3 — position sizing and exit rules.

Memecoins are fat-tailed: most positions go to zero, a few pay for everything.
So sizing is deliberately small and uniform-ish rather than conviction-weighted,
and exits are laddered so you bank something on the ones that do run.
"""

# Fraction of bankroll per position at each score tier
TIERS = [
    (85, 0.020, "A"),   # score >= 85 -> 2.0% of bankroll
    (75, 0.015, "B"),
    (65, 0.010, "C"),
]
MIN_SCORE = 65
MAX_CONCURRENT_POSITIONS = 5
MAX_TOTAL_EXPOSURE = 0.10   # never more than 10% of bankroll in memecoins at once

# Laddered exits: (gain multiple, fraction of the position to sell)
TAKE_PROFIT_LADDER = [
    (2.0, 0.50),   # at 2x, sell half — this recovers your cost basis
    (4.0, 0.25),
    (10.0, 0.15),
    # remaining 10% rides as a lottery ticket
]
STOP_LOSS_PCT = -0.50           # cut at -50%
TIME_STOP_HOURS = 24            # if it hasn't moved in 24h, momentum thesis failed
TRAILING_STOP_FROM_PEAK = -0.35  # after 2x, trail 35% off the peak


def tier_for_score(score: float) -> tuple[float, str] | None:
    for threshold, fraction, label in TIERS:
        if score >= threshold:
            return fraction, label
    return None


def size_position(
    score: float,
    bankroll: float,
    open_positions: int = 0,
    current_exposure: float = 0.0,
) -> dict:
    """Returns a sizing decision with a human-readable reason."""
    if score < MIN_SCORE:
        return {"take": False, "reason": f"score {score} below minimum {MIN_SCORE}"}

    if open_positions >= MAX_CONCURRENT_POSITIONS:
        return {"take": False, "reason": f"already at max {MAX_CONCURRENT_POSITIONS} positions"}

    tier = tier_for_score(score)
    if tier is None:
        return {"take": False, "reason": "no tier matched"}
    fraction, label = tier

    exposure_headroom = MAX_TOTAL_EXPOSURE - current_exposure
    if exposure_headroom <= 0:
        return {"take": False, "reason": "at max total exposure"}

    fraction = min(fraction, exposure_headroom)
    amount = round(bankroll * fraction, 2)

    return {
        "take": True,
        "tier": label,
        "fraction": round(fraction, 4),
        "amount_usd": amount,
        "reason": f"tier {label} at {fraction * 100:.2f}% of bankroll",
    }


def exit_plan(entry_price: float) -> dict:
    """Concrete price levels to act on, given an entry."""
    return {
        "entry": entry_price,
        "stop_loss": round(entry_price * (1 + STOP_LOSS_PCT), 12),
        "take_profits": [
            {
                "multiple": mult,
                "price": round(entry_price * mult, 12),
                "sell_fraction": frac,
            }
            for mult, frac in TAKE_PROFIT_LADDER
        ],
        "time_stop_hours": TIME_STOP_HOURS,
        "trailing_stop_after_2x": TRAILING_STOP_FROM_PEAK,
    }


def check_exit(position: dict, current_price: float, hours_held: float) -> dict | None:
    """
    position: {'entry_price', 'peak_price', 'remaining_fraction', 'hit_multiples': [...]}
    Returns an action dict, or None to hold.
    """
    entry = position["entry_price"]
    if entry <= 0:
        return None

    change = (current_price - entry) / entry
    multiple = current_price / entry

    if change <= STOP_LOSS_PCT:
        return {"action": "SELL_ALL", "reason": f"stop loss hit ({change * 100:.0f}%)"}

    peak = max(position.get("peak_price", entry), current_price)
    if multiple >= 2.0:
        drawdown = (current_price - peak) / peak
        if drawdown <= TRAILING_STOP_FROM_PEAK:
            return {
                "action": "SELL_ALL",
                "reason": f"trailing stop ({drawdown * 100:.0f}% off peak)",
            }

    hit = set(position.get("hit_multiples", []))
    for mult, frac in TAKE_PROFIT_LADDER:
        if multiple >= mult and mult not in hit:
            return {
                "action": "SELL_PARTIAL",
                "sell_fraction": frac,
                "multiple": mult,
                "reason": f"take profit at {mult}x",
            }

    if hours_held >= TIME_STOP_HOURS and multiple < 1.2:
        return {"action": "SELL_ALL", "reason": f"time stop ({hours_held:.0f}h, no move)"}

    return None