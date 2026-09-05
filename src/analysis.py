"""
Strategy evaluation — does the log say anything yet, and if so, what?

This is deliberately not a "how much did I make" report. With fat-tailed
payoffs and small samples, raw PnL is mostly noise. The questions worth
asking are:

  1. Can this winrate be told apart from a coin flip yet? (Wilson interval)
  2. What is the expectancy per trade, not the winrate? A 25% winrate with
     10x winners beats a 70% winrate with small ones.
  3. Which scorer components actually separate winners from losers?
  4. How many more trades until the answer means anything?

Component attribution (3) is the useful one. Every alert stores its score
breakdown, so once there are enough closed trades you can see whether e.g.
volume_acceleration was higher in the winners or the losers. If a component
scores HIGHER in losers, it is actively hurting and should be reweighted or
inverted — no amount of staring at charts tells you that.
"""
import json
import math
from collections import defaultdict

from src import tracker

MIN_FOR_ATTRIBUTION = 10
MIN_FOR_VERDICT = 20


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """
    95% confidence interval for a winrate. Handles small n properly, unlike
    the naive wins/n +- margin, which goes nonsense below ~30 samples.
    """
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def trades_needed(observed_rate: float, target_width: float = 0.20) -> int:
    """
    Roughly how many closed trades before the confidence interval narrows to
    target_width. Answers "how long until I know?" honestly.
    """
    p = min(max(observed_rate, 0.05), 0.95)
    z = 1.96
    n = (4 * z * z * p * (1 - p)) / (target_width * target_width)
    return int(math.ceil(n))


def expectancy(closed: list) -> dict:
    """
    Average dollars per trade, split into its parts. This matters more than
    winrate: the exit ladder is designed to lose small and win big, so a low
    winrate is expected and not by itself bad.
    """
    if not closed:
        return {"per_trade": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "win_loss_ratio": None, "profit_factor": None}

    wins = [r["realized_pnl_usd"] for r in closed if r["realized_pnl_usd"] > 0]
    losses = [r["realized_pnl_usd"] for r in closed if r["realized_pnl_usd"] <= 0]

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "per_trade": round(sum(r["realized_pnl_usd"] for r in closed) / len(closed), 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "win_loss_ratio": round(avg_win / abs(avg_loss), 2) if avg_loss else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
    }


def component_attribution(conn, source: str | None = None) -> list[dict]:
    """
    For each scorer component, its average raw value in winners vs losers.

    A component that scores higher in LOSERS is hurting you. That is the
    single most actionable thing this whole file produces.
    """
    q = """SELECT a.breakdown, p.realized_pnl_usd FROM positions p
           JOIN alerts a ON p.alert_id = a.id
           WHERE p.status = 'CLOSED'"""
    params = []
    if source:
        q += " AND COALESCE(a.source, 'momentum') = ?"
        params.append(source)

    winners = defaultdict(list)
    losers = defaultdict(list)

    for row in conn.execute(q, params):
        try:
            bd = json.loads(row["breakdown"] or "{}")
        except json.JSONDecodeError:
            continue
        bucket = winners if row["realized_pnl_usd"] > 0 else losers
        for name, val in bd.items():
            raw = val[0] if isinstance(val, (list, tuple)) and val else val
            if isinstance(raw, (int, float)):
                bucket[name].append(raw)

    names = set(winners) | set(losers)
    out = []
    for name in sorted(names):
        w = winners.get(name, [])
        l = losers.get(name, [])
        aw = sum(w) / len(w) if w else None
        al = sum(l) / len(l) if l else None
        gap = (aw - al) if (aw is not None and al is not None) else None
        out.append({
            "component": name,
            "avg_in_winners": round(aw, 3) if aw is not None else None,
            "avg_in_losers": round(al, 3) if al is not None else None,
            "gap": round(gap, 3) if gap is not None else None,
            "n_winners": len(w),
            "n_losers": len(l),
        })

    out.sort(key=lambda c: (c["gap"] is None, -(c["gap"] or 0)))
    return out


def exit_breakdown(conn, source: str | None = None) -> list[dict]:
    """Which exit rules are firing, and what they cost or saved."""
    q = """SELECT p.close_reason, COUNT(*) AS n, SUM(p.realized_pnl_usd) AS pnl
           FROM positions p JOIN alerts a ON p.alert_id = a.id
           WHERE p.status = 'CLOSED'"""
    params = []
    if source:
        q += " AND COALESCE(a.source, 'momentum') = ?"
        params.append(source)
    q += " GROUP BY p.close_reason ORDER BY n DESC"

    rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        reason = (r["close_reason"] or "unknown").split("(")[0].strip()
        out.append({
            "reason": reason,
            "count": r["n"],
            "pnl": round(r["pnl"] or 0, 2),
        })
    return out


def score_buckets(conn, source: str | None = None) -> list[dict]:
    """
    Do higher scores actually do better? If the buckets don't separate,
    the score isn't measuring anything and the tiers are decoration.
    """
    q = """SELECT a.score, p.realized_pnl_usd FROM positions p
           JOIN alerts a ON p.alert_id = a.id
           WHERE p.status = 'CLOSED' AND a.score IS NOT NULL"""
    params = []
    if source:
        q += " AND COALESCE(a.source, 'momentum') = ?"
        params.append(source)

    buckets = {"65-74": [], "75-84": [], "85+": []}
    for r in conn.execute(q, params):
        s = r["score"]
        key = "85+" if s >= 85 else ("75-84" if s >= 75 else "65-74")
        buckets[key].append(r["realized_pnl_usd"])

    out = []
    for label, pnls in buckets.items():
        if not pnls:
            out.append({"band": label, "trades": 0, "winrate": None, "avg_pnl": None})
            continue
        wins = sum(1 for p in pnls if p > 0)
        out.append({
            "band": label,
            "trades": len(pnls),
            "winrate": round(wins / len(pnls) * 100, 1),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
        })
    return out


def top_trades(conn, source: str | None = None, limit: int = 5) -> dict:
    """
    Best and worst individual trades. Useful for eyeballing what a winner
    actually looked like versus a loser — patterns the aggregate stats hide.
    """
    q = """SELECT p.symbol, p.mint, p.entry_price, p.exit_price, p.size_usd,
                  p.realized_pnl_usd, p.close_reason, p.opened_ts, p.closed_ts,
                  a.score, a.tier, a.liquidity, a.market_cap,
                  COALESCE(a.source, 'momentum') AS source
           FROM positions p JOIN alerts a ON p.alert_id = a.id
           WHERE p.status = 'CLOSED'"""
    params = []
    if source:
        q += " AND COALESCE(a.source, 'momentum') = ?"
        params.append(source)

    rows = [dict(r) for r in conn.execute(q, params)]
    for r in rows:
        entry = r["entry_price"] or 0
        r["multiple"] = round((r["exit_price"] or 0) / entry, 2) if entry else None
        r["return_pct"] = round(((r["exit_price"] or 0) / entry - 1) * 100, 1) if entry else None
        if r["opened_ts"] and r["closed_ts"]:
            r["held_h"] = round((r["closed_ts"] - r["opened_ts"]) / 3600, 1)
        else:
            r["held_h"] = None
        r["reason"] = (r["close_reason"] or "").split("(")[0].strip()
        r["realized_pnl_usd"] = round(r["realized_pnl_usd"] or 0, 2)

    rows.sort(key=lambda r: -r["realized_pnl_usd"])
    return {
        "best": rows[:limit],
        "worst": list(reversed(rows[-limit:])) if len(rows) > limit else [],
    }


def evaluate(conn, source: str) -> dict:
    rows = conn.execute(
        """SELECT p.* FROM positions p JOIN alerts a ON p.alert_id = a.id
           WHERE p.status = 'CLOSED' AND COALESCE(a.source, 'momentum') = ?""",
        (source,),
    ).fetchall()

    n = len(rows)
    wins = sum(1 for r in rows if r["realized_pnl_usd"] > 0)
    lo, hi = wilson_interval(wins, n)
    exp = expectancy(rows)

    # Can we rule out a coin flip? If 50% sits inside the interval, no.
    conclusive = n >= MIN_FOR_VERDICT and not (lo <= 0.5 <= hi)

    return {
        "source": source,
        "trades": n,
        "wins": wins,
        "winrate": round(wins / n * 100, 1) if n else None,
        "ci_low": round(lo * 100, 1),
        "ci_high": round(hi * 100, 1),
        "conclusive": conclusive,
        "expectancy": exp,
        "more_needed": max(0, trades_needed(wins / n if n else 0.5) - n),
        "components": component_attribution(conn, source) if n >= MIN_FOR_ATTRIBUTION else [],
        "exits": exit_breakdown(conn, source),
        "score_bands": score_buckets(conn, source),
        "top": top_trades(conn, source),
    }


def report() -> None:
    conn = tracker.connect()

    for source, title in [("momentum", "MOMENTUM SCREENER"),
                          ("smartmoney", "COPY TRADING")]:
        e = evaluate(conn, source)
        print(f"\n{'=' * 58}")
        print(f"  {title}")
        print("=" * 58)

        if not e["trades"]:
            print("  No closed trades yet.")
            continue

        print(f"  {e['trades']} closed, {e['wins']} winners "
              f"({e['winrate']}%)")
        print(f"  True winrate is somewhere in {e['ci_low']}-{e['ci_high']}% "
              f"(95% confidence)")

        if e["conclusive"]:
            print("  -> This is distinguishable from a coin flip.")
        else:
            print(f"  -> Not distinguishable from chance yet. "
                  f"~{e['more_needed']} more trades needed.")

        x = e["expectancy"]
        print(f"\n  Per trade:   ${x['per_trade']}")
        print(f"  Avg winner:  ${x['avg_win']}      Avg loser: ${x['avg_loss']}")
        if x["win_loss_ratio"] is not None:
            print(f"  Win/loss size ratio: {x['win_loss_ratio']}  "
                  f"(needs to beat {round((100 - (e['winrate'] or 0)) / max(e['winrate'] or 1, 1), 2)} "
                  f"to break even at this winrate)")
        if x["profit_factor"] is not None:
            print(f"  Profit factor: {x['profit_factor']}  (above 1.0 is profitable)")

        if e["exits"]:
            print("\n  Exits:")
            for ex in e["exits"]:
                print(f"    {ex['reason']:<22} {ex['count']:>3}x   ${ex['pnl']:>9,.2f}")

        bands = [b for b in e["score_bands"] if b["trades"]]
        if len(bands) > 1:
            print("\n  Does score predict outcome?")
            for b in bands:
                print(f"    {b['band']:<8} {b['trades']:>3} trades   "
                      f"{b['winrate']:>5}% win   avg ${b['avg_pnl']}")

        if e["top"]["best"]:
            print("\n  Best trades:")
            for t in e["top"]["best"]:
                print(f"    {(t['symbol'] or t['mint'][:8]):<12} "
                      f"score {t['score']:<5} "
                      f"{t['multiple']}x  ${t['realized_pnl_usd']:>8,.2f}  "
                      f"{t['held_h']}h  {t['reason']}")
        if e["top"]["worst"]:
            print("\n  Worst trades:")
            for t in e["top"]["worst"]:
                print(f"    {(t['symbol'] or t['mint'][:8]):<12} "
                      f"score {t['score']:<5} "
                      f"{t['multiple']}x  ${t['realized_pnl_usd']:>8,.2f}  "
                      f"{t['held_h']}h  {t['reason']}")

        if e["components"]:
            print("\n  Component attribution (raw value in winners vs losers):")
            for c in e["components"]:
                flag = ""
                if c["gap"] is not None and c["gap"] < -0.1:
                    flag = "  <-- HIGHER IN LOSERS"
                print(f"    {c['component']:<22} "
                      f"win {c['avg_in_winners']}  lose {c['avg_in_losers']}"
                      f"{flag}")
            print("\n  A component that scores higher in losers is working")
            print("  against you. Reweight it before touching anything else.")
        elif e["trades"] < MIN_FOR_ATTRIBUTION:
            print(f"\n  Component attribution needs {MIN_FOR_ATTRIBUTION}+ trades "
                  f"({MIN_FOR_ATTRIBUTION - e['trades']} to go).")

    print()
    conn.close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    report()