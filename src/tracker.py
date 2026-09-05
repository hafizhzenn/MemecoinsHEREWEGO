"""
Paper trading tracker — logs every alert and simulated trade to SQLite
so we can measure winrate per filter, same as the BTC bot's dashboard.

The point: no filter goes into production until the log says it works.
"""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "screener.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    score REAL,
    tier TEXT,
    price REAL,
    liquidity REAL,
    market_cap REAL,
    breakdown TEXT,
    safety_passed INTEGER,
    safety_reasons TEXT,
    source TEXT DEFAULT 'momentum'
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER,
    mint TEXT NOT NULL,
    symbol TEXT,
    opened_ts REAL NOT NULL,
    closed_ts REAL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    peak_price REAL,
    size_usd REAL,
    remaining_fraction REAL DEFAULT 1.0,
    hit_multiples TEXT DEFAULT '[]',
    realized_pnl_usd REAL DEFAULT 0,
    status TEXT DEFAULT 'OPEN',
    close_reason TEXT,
    FOREIGN KEY (alert_id) REFERENCES alerts(id)
);

CREATE INDEX IF NOT EXISTS idx_alerts_mint ON alerts(mint);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN source TEXT DEFAULT 'momentum'")
        conn.commit()
    return conn


def log_alert(conn, pair: dict, score_result: dict, tier: str | None,
              safety_passed: bool, safety_reasons: list[str],
              source: str = "momentum") -> int:
    base = pair.get("baseToken") or {}
    cur = conn.execute(
        """INSERT INTO alerts
           (ts, mint, symbol, score, tier, price, liquidity, market_cap,
            breakdown, safety_passed, safety_reasons, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            time.time(),
            base.get("address"),
            base.get("symbol"),
            score_result.get("score"),
            tier,
            float(pair.get("priceUsd") or 0),
            float((pair.get("liquidity") or {}).get("usd") or 0),
            float(pair.get("marketCap") or pair.get("fdv") or 0),
            json.dumps(score_result.get("breakdown", {})),
            1 if safety_passed else 0,
            json.dumps(safety_reasons),
            source,
        ),
    )
    conn.commit()
    return cur.lastrowid


def open_position(conn, alert_id: int, pair: dict, entry_price: float, size_usd: float) -> int:
    base = pair.get("baseToken") or {}
    cur = conn.execute(
        """INSERT INTO positions
           (alert_id, mint, symbol, opened_ts, entry_price, peak_price, size_usd)
           VALUES (?,?,?,?,?,?,?)""",
        (alert_id, base.get("address"), base.get("symbol"),
         time.time(), entry_price, entry_price, size_usd),
    )
    conn.commit()
    return cur.lastrowid


def get_open_positions(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM positions WHERE status = 'OPEN'").fetchall()


def update_peak(conn, position_id: int, price: float) -> None:
    conn.execute(
        "UPDATE positions SET peak_price = MAX(peak_price, ?) WHERE id = ?",
        (price, position_id),
    )
    conn.commit()


def record_partial_exit(conn, position_id: int, multiple: float,
                        sell_fraction: float, exit_price: float) -> None:
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    if not row:
        return
    hit = json.loads(row["hit_multiples"])
    hit.append(multiple)
    sold_usd = row["size_usd"] * sell_fraction
    pnl = sold_usd * (exit_price / row["entry_price"] - 1)
    conn.execute(
        """UPDATE positions
           SET remaining_fraction = remaining_fraction - ?,
               hit_multiples = ?,
               realized_pnl_usd = realized_pnl_usd + ?
           WHERE id = ?""",
        (sell_fraction, json.dumps(hit), pnl, position_id),
    )
    conn.commit()


def close_position(conn, position_id: int, exit_price: float, reason: str) -> None:
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    if not row:
        return
    remaining_usd = row["size_usd"] * row["remaining_fraction"]
    pnl = remaining_usd * (exit_price / row["entry_price"] - 1)
    conn.execute(
        """UPDATE positions
           SET closed_ts = ?, exit_price = ?, status = 'CLOSED',
               close_reason = ?, remaining_fraction = 0,
               realized_pnl_usd = realized_pnl_usd + ?
           WHERE id = ?""",
        (time.time(), exit_price, reason, pnl, position_id),
    )
    conn.commit()


def stats(conn) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) AS n,
                  SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                  SUM(realized_pnl_usd) AS total_pnl,
                  AVG(realized_pnl_usd) AS avg_pnl
           FROM positions WHERE status = 'CLOSED'"""
    ).fetchone()
    n = row["n"] or 0
    return {
        "closed_trades": n,
        "wins": row["wins"] or 0,
        "winrate": round((row["wins"] or 0) / n * 100, 1) if n else 0.0,
        "total_pnl_usd": round(row["total_pnl"] or 0, 2),
        "avg_pnl_usd": round(row["avg_pnl"] or 0, 2),
    }


def stats_by_tier(conn) -> list[dict]:
    """Which score tiers actually make money — this is the whole point."""
    rows = conn.execute(
        """SELECT a.tier,
                  COUNT(*) AS n,
                  SUM(CASE WHEN p.realized_pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                  SUM(p.realized_pnl_usd) AS pnl
           FROM positions p JOIN alerts a ON p.alert_id = a.id
           WHERE p.status = 'CLOSED'
           GROUP BY a.tier"""
    ).fetchall()
    return [
        {
            "tier": r["tier"],
            "trades": r["n"],
            "winrate": round((r["wins"] or 0) / r["n"] * 100, 1) if r["n"] else 0.0,
            "pnl_usd": round(r["pnl"] or 0, 2),
        }
        for r in rows
    ]

def stats_by_source(conn) -> list[dict]:
    """Momentum vs smart money, head to head."""
    rows = conn.execute(
        """SELECT COALESCE(a.source, 'momentum') AS source,
                  COUNT(*) AS n,
                  SUM(CASE WHEN p.realized_pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                  SUM(p.realized_pnl_usd) AS pnl
           FROM positions p JOIN alerts a ON p.alert_id = a.id
           WHERE p.status = 'CLOSED'
           GROUP BY COALESCE(a.source, 'momentum')"""
    ).fetchall()
    return [
        {
            "source": r["source"],
            "trades": r["n"],
            "winrate": round((r["wins"] or 0) / r["n"] * 100, 1) if r["n"] else 0.0,
            "pnl_usd": round(r["pnl"] or 0, 2),
        }
        for r in rows
    ]