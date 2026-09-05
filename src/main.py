"""
Main loop — discover, screen, score, size, alert, track.

Usage:
    python -m src.main scan            # one pass
    python -m src.main watch           # loop every SCAN_INTERVAL seconds
    python -m src.main stats           # paper trading results so far
"""
import json
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from src import discovery, notifier, scorer, sizing, smartmoney, tracker  # noqa: E402
from src.rugcheck import passes_safety_filter  # noqa: E402

BANKROLL = float(os.getenv("BANKROLL_USD", "1000"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
ALERT_COOLDOWN_HOURS = 6


def _recently_alerted(conn, mint: str) -> bool:
    cutoff = time.time() - ALERT_COOLDOWN_HOURS * 3600
    row = conn.execute(
        "SELECT 1 FROM alerts WHERE mint = ? AND ts > ? LIMIT 1", (mint, cutoff)
    ).fetchone()
    return row is not None


def scan_once(conn, verbose: bool = True) -> int:
    pairs = discovery.discover()
    if verbose:
        print(f"[scan] {len(pairs)} candidate pairs")

    open_positions = tracker.get_open_positions(conn)
    position_count = len(open_positions)
    current_exposure = sum(p["size_usd"] for p in open_positions) / BANKROLL if BANKROLL else 0

    # Score everything first, so we only spend Birdeye credits on the best ones
    scored = []
    for pair in pairs:
        result = scorer.score_pair(pair)
        if result["score"] >= sizing.MIN_SCORE:
            scored.append((result["score"], pair, result))
    scored.sort(key=lambda t: -t[0])

    if verbose:
        print(f"[scan] {len(scored)} passed momentum threshold ({sizing.MIN_SCORE})")

    alerts_sent = 0
    for _, pair, result in scored[:10]:   # cap API spend per scan
        mint = (pair.get("baseToken") or {}).get("address")
        symbol = (pair.get("baseToken") or {}).get("symbol", "?")
        if not mint or _recently_alerted(conn, mint):
            continue

        safe, reasons = passes_safety_filter(pair)
        decision = sizing.size_position(
            result["score"], BANKROLL, position_count, current_exposure
        )

        tracker.log_alert(conn, pair, result, decision.get("tier"), safe, reasons)

        if not safe:
            if verbose:
                print(f"  ✗ {symbol:<12} score {result['score']:<5} REJECTED: {'; '.join(reasons)}")
            continue

        if not decision["take"]:
            if verbose:
                print(f"  – {symbol:<12} score {result['score']:<5} skipped: {decision['reason']}")
            continue

        entry_price = float(pair.get("priceUsd") or 0)
        if entry_price <= 0:
            continue

        exits = sizing.exit_plan(entry_price)
        notifier.send(notifier.format_alert(pair, result, decision, exits))
        alerts_sent += 1

        # Paper trade: record it so we can measure whether this actually works
        alert_id = conn.execute(
            "SELECT id FROM alerts ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        tracker.open_position(conn, alert_id, pair, entry_price, decision["amount_usd"])
        current_exposure += decision["fraction"]
        position_count += 1

    return alerts_sent


def manage_open_positions(conn, verbose: bool = True) -> None:
    """Re-check open paper positions against exit rules."""
    positions = tracker.get_open_positions(conn)
    if not positions:
        return

    mints = [p["mint"] for p in positions]
    pairs = {
        (p.get("baseToken") or {}).get("address"): p
        for p in discovery.dedupe_by_mint(discovery.get_pairs_for_tokens(mints))
    }

    for pos in positions:
        pair = pairs.get(pos["mint"])
        if not pair:
            continue
        price = float(pair.get("priceUsd") or 0)
        if price <= 0:
            continue

        tracker.update_peak(conn, pos["id"], price)
        hours_held = (time.time() - pos["opened_ts"]) / 3600

        action = sizing.check_exit(
            {
                "entry_price": pos["entry_price"],
                "peak_price": max(pos["peak_price"], price),
                "remaining_fraction": pos["remaining_fraction"],
                "hit_multiples": json.loads(pos["hit_multiples"]),
            },
            price,
            hours_held,
        )
        if not action:
            continue

        if action["action"] == "SELL_ALL":
            tracker.close_position(conn, pos["id"], price, action["reason"])
            notifier.send(f"🔴 SELL ALL {pos['symbol']} @ ${price:.10f}".rstrip("0")
                          + f"\nReason: {action['reason']}")
        else:
            tracker.record_partial_exit(
                conn, pos["id"], action["multiple"], action["sell_fraction"], price
            )
            notifier.send(
                f"🟡 TAKE PROFIT {pos['symbol']} — sell "
                f"{int(action['sell_fraction'] * 100)}% at {action['multiple']}x"
                f"\nPrice: ${price:.10f}".rstrip("0")
            )

def scan_smart_money(conn, verbose: bool = True) -> int:
    """Discovery via tracked wallets instead of momentum. Same gates after that."""
    consensus = smartmoney.find_consensus_buys(within_hours=6, min_wallets=2)
    if verbose:
        print(f"[smartmoney] {len(consensus)} tokens bought by 2+ tracked wallets")
    if not consensus:
        return 0

    pairs = {
        (p.get("baseToken") or {}).get("address"): p
        for p in discovery.dedupe_by_mint(
            discovery.get_pairs_for_tokens([c["mint"] for c in consensus])
        )
    }

    open_positions = tracker.get_open_positions(conn)
    position_count = len(open_positions)
    current_exposure = sum(p["size_usd"] for p in open_positions) / BANKROLL if BANKROLL else 0

    alerts_sent = 0
    for entry in consensus:
        pair = pairs.get(entry["mint"])
        if not pair or _recently_alerted(conn, entry["mint"]):
            continue

        result = scorer.score_pair(pair)
        safe, reasons = passes_safety_filter(pair)
        decision = sizing.size_position(
            result["score"], BANKROLL, position_count, current_exposure
        )

        tracker.log_alert(conn, pair, result, decision.get("tier"),
                          safe, reasons, source="smartmoney")

        symbol = (pair.get("baseToken") or {}).get("symbol", entry["mint"][:8])
        if not safe:
            if verbose:
                print(f"  ✗ {symbol:<12} REJECTED: {'; '.join(reasons)}")
            continue
        if not decision["take"]:
            if verbose:
                print(f"  – {symbol:<12} skipped: {decision['reason']}")
            continue

        entry_price = float(pair.get("priceUsd") or 0)
        if entry_price <= 0:
            continue

        exits = sizing.exit_plan(entry_price)
        msg = notifier.format_alert(pair, result, decision, exits)
        msg = msg.replace("🎯", "🐋", 1) + f"\n\nBought by: {', '.join(entry['buyers'])}"
        notifier.send(msg)
        alerts_sent += 1

        alert_id = conn.execute("SELECT id FROM alerts ORDER BY id DESC LIMIT 1").fetchone()["id"]
        tracker.open_position(conn, alert_id, pair, entry_price, decision["amount_usd"])
        current_exposure += decision["fraction"]
        position_count += 1

    return alerts_sent

def _section(conn, source: str, title: str) -> None:
    rows = conn.execute(
        """SELECT p.*, a.score, a.tier FROM positions p
           JOIN alerts a ON p.alert_id = a.id
           WHERE COALESCE(a.source, 'momentum') = ?""",
        (source,),
    ).fetchall()
    closed = [r for r in rows if r["status"] == "CLOSED"]
    open_ = [r for r in rows if r["status"] == "OPEN"]

    print(f"\n{'=' * 50}")
    print(f"  {title}")
    print("=" * 50)

    if not closed and not open_:
        print("  no trades yet")
        return

    if closed:
        wins = sum(1 for r in closed if r["realized_pnl_usd"] > 0)
        pnl = sum(r["realized_pnl_usd"] for r in closed)
        print(f"  Closed:  {len(closed)}   Winrate: {wins / len(closed) * 100:.1f}%   "
              f"PnL: ${pnl:,.2f}")
        if len(closed) < 20:
            print(f"  ⚠ {len(closed)} trades is too few to conclude anything.")
    else:
        print("  Closed:  0")

    if open_:
        print(f"  Open:    {len(open_)}")
        for r in open_:
            print(f"    {r['symbol']:<12} score {r['score']:<6} size ${r['size_usd']}")


def compare_sections(conn) -> None:
    """Head to head. Deliberately refuses to pick a winner on thin data."""
    by_source = {s["source"]: s for s in tracker.stats_by_source(conn)}
    mom = by_source.get("momentum")
    smart = by_source.get("smartmoney")

    print(f"\n{'=' * 50}")
    print("  MOMENTUM vs SMART MONEY")
    print("=" * 50)

    if not mom or not smart:
        print("  Need closed trades from both sources to compare.")
        return

    MIN_FOR_VERDICT = 20
    for label, s in (("momentum", mom), ("smartmoney", smart)):
        print(f"  {label:<12} {s['trades']:>3} trades  "
              f"{s['winrate']:>5.1f}% win  ${s['pnl_usd']:>10,.2f}")

    if mom["trades"] < MIN_FOR_VERDICT or smart["trades"] < MIN_FOR_VERDICT:
        print(f"\n  No verdict: both need {MIN_FOR_VERDICT}+ closed trades.")
        print("  Picking a winner on fewer than that is reading noise.")
        return

    better = max((mom, smart), key=lambda s: s["pnl_usd"])
    other = mom if better is smart else smart
    gap = better["pnl_usd"] - other["pnl_usd"]
    print(f"\n  Ahead: {better['source']} by ${gap:,.2f}")
    print("  Still only a backtest of your own log, not a guarantee.")


def print_stats(conn) -> None:
    _section(conn, "momentum", "SECTION 1 — MOMENTUM SCREENER")
    _section(conn, "smartmoney", "SECTION 2 — COPY TRADING")
    compare_sections(conn)

    overall = tracker.stats(conn)
    print(f"\n  Overall: {overall['closed_trades']} closed, "
          f"${overall['total_pnl_usd']:,.2f} total PnL\n")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    conn = tracker.connect()

    if cmd == "stats":
        print_stats(conn)
    elif cmd == "scan":
        manage_open_positions(conn)
        n = scan_once(conn)
        print(f"[scan] {n} alerts sent")
    elif cmd == "smartmoney":
        manage_open_positions(conn)
        n = scan_smart_money(conn)
        print(f"[smartmoney] {n} alerts sent")
    elif cmd == "audit":
        smartmoney.audit_tracked_wallets()
    elif cmd == "findwallets":
        from src import walletfinder
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 2
        walletfinder.discover_wallets(min_tokens=n)
    elif cmd == "watch":
        print(f"[watch] scanning every {SCAN_INTERVAL}s. Ctrl-C to stop.")
        try:
            while True:
                manage_open_positions(conn)
                scan_once(conn)
                time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            print("\n[watch] stopped")
            print_stats(conn)
    else:
        print(__doc__)

    conn.close()


if __name__ == "__main__":
    main()