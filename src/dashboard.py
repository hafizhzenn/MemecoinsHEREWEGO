"""
Web dashboard + background scheduler.

Runs both scans on their own timers and serves a page showing the two arms
side by side, so you can leave it running instead of firing commands by hand.

    python -m src.dashboard

Then open http://localhost:5000

INTERVALS AND API BUDGET
  Momentum scan uses Dexscreener (free, no key) plus a few Birdeye safety
  checks on tokens that pass the score threshold. Cheap.

  Smart money scan uses Helius at 100 credits per wallet per run. With 5
  wallets:
      every 30 min -> ~24k credits/day  (~720k/month, fits the 1M free tier)
      every 10 min -> ~72k credits/day  (over the free tier within a fortnight)
  The default below is 30 min. Raise SMARTMONEY_INTERVAL_SECONDS rather than
  lowering it if you add wallets.
"""
import os
import threading
import time
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string

load_dotenv()

from src import smartmoney, tracker  # noqa: E402
from src.main import manage_open_positions, scan_once, scan_smart_money  # noqa: E402

MOMENTUM_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
SMARTMONEY_INTERVAL = int(os.getenv("SMARTMONEY_INTERVAL_SECONDS", "1800"))
MIN_TRADES_FOR_VERDICT = 20

app = Flask(__name__)

# Scheduler state, read by the page
state = {
    "started_at": time.time(),
    "momentum_last_run": None,
    "momentum_next_run": None,
    "smartmoney_last_run": None,
    "smartmoney_next_run": None,
    "recent_buys": [],
    "recent_buys_at": None,
    "log": [],
    "errors": 0,
}
state_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
    with state_lock:
        state["log"].insert(0, line)
        del state["log"][60:]
    print(f"[dashboard] {line}", flush=True)


def momentum_loop() -> None:
    while True:
        try:
            conn = tracker.connect()
            manage_open_positions(conn, verbose=False)
            n = scan_once(conn, verbose=False)
            conn.close()
            log(f"momentum scan complete, {n} alert(s)")
        except Exception as e:
            with state_lock:
                state["errors"] += 1
            log(f"momentum scan failed: {e}")
        with state_lock:
            state["momentum_last_run"] = time.time()
            state["momentum_next_run"] = time.time() + MOMENTUM_INTERVAL
        time.sleep(MOMENTUM_INTERVAL)


def smartmoney_loop() -> None:
    while True:
        try:
            conn = tracker.connect()
            n = scan_smart_money(conn, verbose=False)
            conn.close()
            buys = smartmoney.recent_buys_report(within_hours=24)
            with state_lock:
                state["recent_buys"] = buys[:40]
                state["recent_buys_at"] = time.time()
            log(f"smart money scan complete, {n} alert(s), {len(buys)} recent buys")
        except Exception as e:
            with state_lock:
                state["errors"] += 1
            log(f"smart money scan failed: {e}")
        with state_lock:
            state["smartmoney_last_run"] = time.time()
            state["smartmoney_next_run"] = time.time() + SMARTMONEY_INTERVAL
        time.sleep(SMARTMONEY_INTERVAL)


def arm_stats(conn, source: str) -> dict:
    rows = conn.execute(
        """SELECT p.*, a.score, a.tier, a.mint FROM positions p
           JOIN alerts a ON p.alert_id = a.id
           WHERE COALESCE(a.source, 'momentum') = ?
           ORDER BY p.opened_ts DESC""",
        (source,),
    ).fetchall()
    closed = [r for r in rows if r["status"] == "CLOSED"]
    open_ = [r for r in rows if r["status"] == "OPEN"]
    wins = sum(1 for r in closed if r["realized_pnl_usd"] > 0)
    pnl = sum(r["realized_pnl_usd"] for r in closed)

    rejected = conn.execute(
        """SELECT COUNT(*) AS n FROM alerts
           WHERE COALESCE(source, 'momentum') = ? AND safety_passed = 0""",
        (source,),
    ).fetchone()["n"]

    return {
        "closed": len(closed),
        "wins": wins,
        "winrate": round(wins / len(closed) * 100, 1) if closed else None,
        "pnl": round(pnl, 2),
        "rejected": rejected,
        "open": [
            {
                "symbol": r["symbol"] or (r["mint"] or "")[:8],
                "score": r["score"],
                "size": r["size_usd"],
                "entry": r["entry_price"],
                "hours": round((time.time() - r["opened_ts"]) / 3600, 1),
            }
            for r in open_
        ],
        "recent_closed": [
            {
                "symbol": r["symbol"] or (r["mint"] or "")[:8],
                "pnl": round(r["realized_pnl_usd"], 2),
                "reason": r["close_reason"] or "",
            }
            for r in closed[:6]
        ],
    }


def build_payload() -> dict:
    conn = tracker.connect()
    momentum = arm_stats(conn, "momentum")
    smart = arm_stats(conn, "smartmoney")
    conn.close()

    total_closed = momentum["closed"] + smart["closed"]
    with state_lock:
        snapshot = {
            "momentum_next_run": state["momentum_next_run"],
            "smartmoney_next_run": state["smartmoney_next_run"],
            "recent_buys": list(state["recent_buys"]),
            "recent_buys_at": state["recent_buys_at"],
            "log": list(state["log"])[:20],
            "errors": state["errors"],
            "uptime_h": round((time.time() - state["started_at"]) / 3600, 1),
        }

    return {
        "momentum": momentum,
        "smartmoney": smart,
        "total_closed": total_closed,
        "needed": MIN_TRADES_FOR_VERDICT * 2,
        "verdict_ready": (momentum["closed"] >= MIN_TRADES_FOR_VERDICT
                          and smart["closed"] >= MIN_TRADES_FOR_VERDICT),
        "now": time.time(),
        **snapshot,
    }


@app.route("/api/state")
def api_state():
    return jsonify(build_payload())


@app.route("/")
def index():
    return render_template_string(PAGE, d=build_payload())


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Screener</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --ground:#EEF0F2; --ink:#16191C; --muted:#6B7378; --rule:#CDD3D7;
    --momentum:#2D5F8A; --copytrade:#8A5A2D;
    --loss:#A8342A; --gain:#2E6B4F; --panel:#F7F8F9;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:var(--ground); color:var(--ink);
    font-family:'IBM Plex Sans',system-ui,sans-serif;
    font-size:15px; line-height:1.55;
  }
  .num{font-family:'IBM Plex Mono',ui-monospace,monospace; font-variant-numeric:tabular-nums}
  .wrap{max-width:1080px; margin:0 auto; padding:48px 24px 72px}

  header{border-bottom:2px solid var(--ink); padding-bottom:20px; margin-bottom:36px}
  h1{font-size:20px; font-weight:600; margin:0 0 6px}
  .sub{color:var(--muted); font-size:14px; margin:0}

  .verdict{
    background:var(--panel); border:1px solid var(--rule);
    padding:24px 28px; margin-bottom:36px;
  }
  .verdict h2{margin:0 0 8px; font-size:17px; font-weight:600}
  .verdict p{margin:0; color:var(--muted); max-width:62ch}
  .progress{
    height:6px; background:var(--rule); margin-top:18px; position:relative;
  }
  .progress span{display:block; height:100%; background:var(--ink)}
  .progress-label{
    display:flex; justify-content:space-between; margin-top:8px;
    font-size:13px; color:var(--muted);
  }

  .arms{display:grid; grid-template-columns:1fr 1fr; gap:24px; margin-bottom:36px}
  .arm{background:var(--panel); border:1px solid var(--rule); border-top:3px solid var(--accent); padding:22px 24px}
  .arm.momentum{--accent:var(--momentum)}
  .arm.copytrade{--accent:var(--copytrade)}
  .arm h3{margin:0 0 2px; font-size:16px; font-weight:600; color:var(--accent)}
  .arm .role{margin:0 0 20px; font-size:13px; color:var(--muted)}

  .figures{display:flex; gap:28px; margin-bottom:20px}
  .fig .v{font-size:26px; font-weight:500; display:block; line-height:1.1}
  .fig .k{font-size:12px; color:var(--muted)}
  .neg{color:var(--loss)} .pos{color:var(--gain)}

  table{width:100%; border-collapse:collapse; font-size:14px}
  th{
    text-align:left; font-weight:500; color:var(--muted); font-size:12px;
    padding:6px 0; border-bottom:1px solid var(--rule);
  }
  td{padding:7px 0; border-bottom:1px solid var(--rule)}
  tr:last-child td{border-bottom:none}
  td.r,th.r{text-align:right}

  .empty{color:var(--muted); font-size:14px; padding:14px 0}
  .empty strong{display:block; color:var(--ink); font-weight:500; margin-bottom:3px}

  h4{font-size:13px; font-weight:600; margin:22px 0 6px; color:var(--muted)}

  .feed{background:var(--panel); border:1px solid var(--rule); padding:22px 24px; margin-bottom:36px}
  .feed h3{margin:0 0 2px; font-size:16px; font-weight:600}
  .feed .role{margin:0 0 18px; font-size:13px; color:var(--muted)}
  .thin{color:var(--muted)}

  footer{display:grid; grid-template-columns:1fr 1fr; gap:24px; border-top:1px solid var(--rule); padding-top:22px}
  .log{font-size:13px; max-height:190px; overflow:auto}
  .log div{padding:2px 0; color:var(--muted)}
  .timers{font-size:13px; color:var(--muted)}
  .timers div{padding:2px 0}

  @media (max-width:760px){
    .arms,footer{grid-template-columns:1fr}
    .wrap{padding:32px 18px 56px}
  }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>Memecoin screener</h1>
    <p class="sub">Two strategies running in paper mode. Scans run on their own
    timers; nothing here places a real trade.</p>
  </header>

  {% set pct = (d.total_closed / d.needed * 100) if d.needed else 0 %}
  <section class="verdict">
    {% if d.verdict_ready %}
      <h2>Both arms have enough closed trades to compare</h2>
      <p>Momentum: {{ d.momentum.winrate }}% win, ${{ '%.2f'|format(d.momentum.pnl) }}.
         Copy trading: {{ d.smartmoney.winrate }}% win, ${{ '%.2f'|format(d.smartmoney.pnl) }}.
         This is still your own log, not a guarantee about future trades.</p>
    {% else %}
      <h2>Not enough trades to tell whether either strategy works</h2>
      <p>A verdict needs {{ MIN }} closed trades per arm. Below that, a winrate is
         mostly noise — three losses in a row is an ordinary run even for a
         strategy that works, and three wins proves nothing either.</p>
    {% endif %}
    <div class="progress"><span style="width:{{ [pct,100]|min }}%"></span></div>
    <div class="progress-label">
      <span class="num">{{ d.total_closed }} closed</span>
      <span class="num">{{ d.needed }} needed</span>
    </div>
  </section>

  <div class="arms">
    {% for key, arm, title, role in [
        ('momentum', d.momentum, 'Momentum', 'Scores every new pair on volume, liquidity and buy pressure.'),
        ('copytrade', d.smartmoney, 'Copy trading', 'Follows tokens that several tracked wallets bought.')] %}
    <section class="arm {{ key }}">
      <h3>{{ title }}</h3>
      <p class="role">{{ role }}</p>

      <div class="figures">
        <div class="fig">
          <span class="v num">{{ arm.closed }}</span>
          <span class="k">closed</span>
        </div>
        <div class="fig">
          <span class="v num">{% if arm.winrate is not none %}{{ arm.winrate }}%{% else %}&mdash;{% endif %}</span>
          <span class="k">win rate</span>
        </div>
        <div class="fig">
          <span class="v num {% if arm.pnl < 0 %}neg{% elif arm.pnl > 0 %}pos{% endif %}">
            {{ '%.0f'|format(arm.pnl) }}
          </span>
          <span class="k">dollars</span>
        </div>
      </div>

      <h4>Open</h4>
      {% if arm.open %}
      <table>
        <tr><th>Token</th><th class="r">Score</th><th class="r">Size</th><th class="r">Held</th></tr>
        {% for p in arm.open %}
        <tr>
          <td>{{ p.symbol }}</td>
          <td class="r num">{{ p.score }}</td>
          <td class="r num">${{ '%.0f'|format(p.size) }}</td>
          <td class="r num">{{ p.hours }}h</td>
        </tr>
        {% endfor %}
      </table>
      {% else %}
        {% if key == 'copytrade' %}
        <div class="empty">
          <strong>Nothing bought yet</strong>
          The tracked wallets are trading tokens below the
          ${{ '{:,}'.format(10000) }} liquidity floor, so the safety filter
          rejects them. {{ arm.rejected }} rejected so far. Track wallets that
          hold longer and this arm will start firing.
        </div>
        {% else %}
        <div class="empty"><strong>No open positions</strong>Next scan will look again.</div>
        {% endif %}
      {% endif %}

      {% if arm.recent_closed %}
      <h4>Recently closed</h4>
      <table>
        {% for p in arm.recent_closed %}
        <tr>
          <td>{{ p.symbol }}</td>
          <td class="thin">{{ p.reason }}</td>
          <td class="r num {% if p.pnl < 0 %}neg{% elif p.pnl > 0 %}pos{% endif %}">
            {{ '%.2f'|format(p.pnl) }}
          </td>
        </tr>
        {% endfor %}
      </table>
      {% endif %}
    </section>
    {% endfor %}
  </div>

  <section class="feed">
    <h3>What the tracked wallets are buying</h3>
    <p class="role">Every buy in the last 24 hours, whether or not it passed the
    filters. Liquidity is the column that decides.</p>
    {% if d.recent_buys %}
    <table>
      <tr><th>Wallet</th><th>Token</th><th class="r">Liquidity</th><th class="r">24h</th><th class="r">Seen</th></tr>
      {% for b in d.recent_buys %}
      <tr>
        <td>{{ b.wallet }}</td>
        <td>{{ b.symbol or b.mint[:10] }}</td>
        <td class="r num">${{ '{:,.0f}'.format(b.get('liquidity', 0)) }}</td>
        <td class="r num {% if b.get('price_change_h24', 0) < 0 %}neg{% elif b.get('price_change_h24', 0) > 0 %}pos{% endif %}">
          {{ '%+.0f'|format(b.get('price_change_h24', 0)) }}%
        </td>
        <td class="r num thin">{{ '%.1f'|format((d.now - b.ts) / 3600) }}h</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">
      <strong>No buys recorded yet</strong>
      The first smart money scan runs shortly after startup.
    </div>
    {% endif %}
  </section>

  <footer>
    <div class="timers">
      <div>Running for <span class="num">{{ d.uptime_h }}h</span></div>
      <div>Momentum scan every <span class="num">{{ MOM_MIN }}</span> min</div>
      <div>Wallet scan every <span class="num">{{ SM_MIN }}</span> min</div>
      {% if d.errors %}<div class="neg">{{ d.errors }} scan error(s) — see log</div>{% endif %}
    </div>
    <div class="log">
      {% for line in d.log %}<div class="num">{{ line }}</div>{% endfor %}
      {% if not d.log %}<div>Waiting for the first scan.</div>{% endif %}
    </div>
  </footer>

</div>
<script>
  setTimeout(function(){ location.reload(); }, 60000);
</script>
</body>
</html>
"""


@app.context_processor
def inject_constants():
    return {
        "MIN": MIN_TRADES_FOR_VERDICT,
        "MOM_MIN": MOMENTUM_INTERVAL // 60,
        "SM_MIN": SMARTMONEY_INTERVAL // 60,
    }


def main() -> None:
    wallets = len(smartmoney.load_wallets())
    daily_credits = (86400 / SMARTMONEY_INTERVAL) * wallets * 100
    print(f"[dashboard] momentum scan every {MOMENTUM_INTERVAL // 60} min")
    print(f"[dashboard] wallet scan every {SMARTMONEY_INTERVAL // 60} min "
          f"({wallets} wallets, ~{daily_credits:,.0f} Helius credits/day, "
          f"~{daily_credits * 30 / 1_000_000:.1f}M/month)")
    if daily_credits * 30 > 1_000_000:
        print("[dashboard] WARNING: that is over the 1M free tier. "
              "Raise SMARTMONEY_INTERVAL_SECONDS or track fewer wallets.")

    threading.Thread(target=momentum_loop, daemon=True).start()
    threading.Thread(target=smartmoney_loop, daemon=True).start()

    print("[dashboard] open http://localhost:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()