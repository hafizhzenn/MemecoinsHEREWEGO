"""
X (Twitter) monitoring — find tokens whose mention rate is accelerating.

Data source is twitterapi.io, not the official X API. X killed its free tier
in February 2026 and now bills ~$0.005 per post read, which works out to
hundreds of dollars a month for continuous polling. twitterapi.io serves the
same data at roughly $0.15 per 1,000 tweets.

COST BUDGET
  Each search returns ~20 tweets.
  every 10 min = 144 calls/day = ~2,880 tweets/day  ~= $13/month
  every  5 min = 288 calls/day = ~5,760 tweets/day  ~= $26/month
  Default below is 10 min. There is a small free credit to test with.

HOW THE SIGNAL IS USED — read this before wiring it into scoring
  Mention velocity is the single most manipulated signal in memecoins. Paid
  promotion, bot rings and coordinated shilling all look exactly like organic
  excitement from the outside, and by the time a token is loud on X it has had
  thousands of eyes on it already.

  So this module does two separate things, and they pull in opposite
  directions:

    1. ALERTS (social_alerts) — tells you what is spiking, with tweet links,
       so you can look. This is information, not a buy signal.

    2. RISK FLAG (social_penalty) — feeds a NEGATIVE adjustment into the
       scorer. A token your momentum scorer already likes AND that is
       screaming on X is more likely to be a top, not less.

  If you later decide to invert #2 into a bonus, do it because your paper
  trade log says so, not because a spike looked convincing.
"""
import os
import re
import sqlite3
import time
from pathlib import Path

import requests

BASE = "https://api.twitterapi.io"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "screener.db"

# Solana mint addresses are base58, 32-44 chars. Excludes 0, O, I, l by design.
MINT_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,14})\b")

# Words that are base58-valid but obviously not mints
MINT_BLACKLIST = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS social_mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mint TEXT NOT NULL,
    cashtag TEXT,
    tweet_id TEXT,
    author TEXT,
    followers INTEGER,
    likes INTEGER,
    retweets INTEGER,
    UNIQUE(mint, tweet_id)
);
CREATE INDEX IF NOT EXISTS idx_social_mint_ts ON social_mentions(mint, ts);

CREATE TABLE IF NOT EXISTS social_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mint TEXT NOT NULL,
    cashtag TEXT,
    mentions_1h INTEGER,
    mentions_prev INTEGER,
    velocity REAL,
    top_tweet_url TEXT
);
"""

# Search queries. min_faves filters out the worst of the bot floor.
DEFAULT_QUERIES = [
    "solana memecoin min_faves:15 -filter:replies",
    "pump.fun min_faves:15 -filter:replies",
    "solana CA min_faves:20 -filter:replies",
    "solana gem min_faves:15 -filter:replies",
    "(sol OR solana) ape min_faves:20 -filter:replies",
    "bonk OR pumpfun OR letsbonk min_faves:15 -filter:replies",
]

SPIKE_MIN_MENTIONS = 5      # below this it's noise regardless of ratio
SPIKE_VELOCITY = 3.0        # this hour vs the previous, as a multiple
ALERT_COOLDOWN_HOURS = 6


def _key() -> str:
    return os.getenv("TWITTERAPI_IO_KEY", "")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def search(query: str, query_type: str = "Latest") -> list[dict]:
    """One page of advanced search results (~20 tweets)."""
    key = _key()
    if not key:
        print("[social] TWITTERAPI_IO_KEY not set in .env")
        return []
    try:
        resp = requests.get(
            f"{BASE}/twitter/tweet/advanced_search",
            headers={"X-API-Key": key},
            params={"query": query, "queryType": query_type},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[social] search failed: {e}")
        return []
    return resp.json().get("tweets") or []


def extract_tokens(text: str) -> tuple[list[str], list[str]]:
    """Pull candidate mint addresses and cashtags out of tweet text."""
    mints = [m for m in MINT_RE.findall(text or "") if m not in MINT_BLACKLIST]
    tags = [t.upper() for t in CASHTAG_RE.findall(text or "")]
    return mints, tags


def _tweet_field(tweet: dict, *names, default=0):
    for n in names:
        if n in tweet and tweet[n] is not None:
            return tweet[n]
    return default


def record_mentions(conn, tweets: list[dict]) -> int:
    """Store any tweet that names a mint address. Returns rows added."""
    added = 0
    for t in tweets:
        text = t.get("text") or t.get("full_text") or ""
        mints, tags = extract_tokens(text)
        if not mints:
            continue

        author = (t.get("author") or {})
        tweet_id = str(t.get("id") or t.get("id_str") or "")
        cashtag = tags[0] if tags else None

        for mint in mints:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO social_mentions
                       (ts, mint, cashtag, tweet_id, author, followers, likes, retweets)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        time.time(), mint, cashtag, tweet_id,
                        author.get("userName") or author.get("screen_name"),
                        _tweet_field(author, "followers", "followers_count"),
                        _tweet_field(t, "likeCount", "favorite_count"),
                        _tweet_field(t, "retweetCount", "retweet_count"),
                    ),
                )
                added += conn.total_changes and 1 or 0
            except sqlite3.Error:
                continue
    conn.commit()
    return added


def mention_velocity(conn, mint: str) -> dict:
    """Mentions in the last hour vs the hour before it."""
    now = time.time()
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM social_mentions WHERE mint = ? AND ts > ?",
        (mint, now - 3600),
    ).fetchone()["n"]
    prev = conn.execute(
        "SELECT COUNT(*) AS n FROM social_mentions WHERE mint = ? AND ts BETWEEN ? AND ?",
        (mint, now - 7200, now - 3600),
    ).fetchone()["n"]

    # prev=0 with a real current count is a cold start, not infinite velocity
    velocity = (cur / prev) if prev else (cur / 2 if cur else 0)
    return {"mint": mint, "current": cur, "previous": prev, "velocity": round(velocity, 2)}


def _recently_alerted(conn, mint: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM social_alerts WHERE mint = ? AND ts > ? LIMIT 1",
        (mint, time.time() - ALERT_COOLDOWN_HOURS * 3600),
    ).fetchone()
    return row is not None


def top_tweet(conn, mint: str) -> dict | None:
    row = conn.execute(
        """SELECT tweet_id, author, likes, cashtag FROM social_mentions
           WHERE mint = ? AND ts > ?
           ORDER BY likes DESC LIMIT 1""",
        (mint, time.time() - 3600),
    ).fetchone()
    if not row or not row["tweet_id"]:
        return None
    author = row["author"] or "i"
    return {
        "url": f"https://x.com/{author}/status/{row['tweet_id']}",
        "author": row["author"],
        "likes": row["likes"],
        "cashtag": row["cashtag"],
    }


def find_spikes(conn) -> list[dict]:
    """Tokens whose mention rate jumped in the last hour."""
    rows = conn.execute(
        """SELECT mint, COUNT(*) AS n FROM social_mentions
           WHERE ts > ? GROUP BY mint HAVING n >= ?""",
        (time.time() - 3600, SPIKE_MIN_MENTIONS),
    ).fetchall()

    spikes = []
    for r in rows:
        if _recently_alerted(conn, r["mint"]):
            continue
        v = mention_velocity(conn, r["mint"])
        if v["velocity"] < SPIKE_VELOCITY:
            continue
        v["top_tweet"] = top_tweet(conn, r["mint"])
        spikes.append(v)

    spikes.sort(key=lambda s: -s["velocity"])
    return spikes


def log_alert(conn, spike: dict) -> None:
    tt = spike.get("top_tweet") or {}
    conn.execute(
        """INSERT INTO social_alerts
           (ts, mint, cashtag, mentions_1h, mentions_prev, velocity, top_tweet_url)
           VALUES (?,?,?,?,?,?,?)""",
        (time.time(), spike["mint"], tt.get("cashtag"), spike["current"],
         spike["previous"], spike["velocity"], tt.get("url")),
    )
    conn.commit()


def social_penalty(conn, mint: str) -> tuple[float, str]:
    """
    Risk adjustment for the scorer, as (points_to_subtract, reason).

    Deliberately a penalty. A token that is already loud on X has had its
    move; buying into that is buying from the people who were early.
    """
    v = mention_velocity(conn, mint)
    if v["current"] < SPIKE_MIN_MENTIONS:
        return 0.0, ""
    if v["velocity"] >= 5:
        return 15.0, f"very loud on X ({v['current']} mentions/h, {v['velocity']}x)"
    if v["velocity"] >= SPIKE_VELOCITY:
        return 8.0, f"loud on X ({v['current']} mentions/h, {v['velocity']}x)"
    return 0.0, ""


def format_social_alert(spike: dict, pair: dict | None) -> str:
    """Notification body. Says what it is and, plainly, what it isn't."""
    tt = spike.get("top_tweet") or {}
    name = tt.get("cashtag") or (
        (pair.get("baseToken") or {}).get("symbol") if pair else None
    ) or spike["mint"][:8]

    lines = [
        f"📣 {name} is spiking on X",
        "",
        f"{spike['current']} mentions this hour (was {spike['previous']}) — "
        f"{spike['velocity']}x",
    ]

    if pair:
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        chg = float((pair.get("priceChange") or {}).get("h24") or 0)
        lines += [
            f"Liquidity ${liq:,.0f}   24h {chg:+.0f}%",
            f"Chart: https://dexscreener.com/solana/{spike['mint']}",
        ]
    else:
        lines.append("No Dexscreener pair found — may be pre-launch or fake.")

    if tt.get("url"):
        lines += ["", f"Loudest post ({tt.get('likes', 0)} likes):", tt["url"]]

    lines += [
        "",
        f"Mint: {spike['mint']}",
        "",
        "This is attention, not an edge. Hype is the easiest signal to fake, "
        "and if it's loud here you're already late. Not a buy alert.",
    ]
    return "\n".join(lines)


def scan(verbose: bool = True) -> int:
    """One full pass: search, record, detect spikes, notify."""
    from src import discovery, notifier

    conn = connect()

    tweets = []
    for q in DEFAULT_QUERIES:
        tweets.extend(search(q))
    if verbose:
        print(f"[social] {len(tweets)} tweets fetched")

    record_mentions(conn, tweets)
    spikes = find_spikes(conn)
    if verbose:
        print(f"[social] {len(spikes)} token(s) spiking")

    sent = 0
    if spikes:
        mints = [s["mint"] for s in spikes]
        pairs = {
            (p.get("baseToken") or {}).get("address"): p
            for p in discovery.dedupe_by_mint(discovery.get_pairs_for_tokens(mints))
        }
        for spike in spikes[:5]:
            pair = pairs.get(spike["mint"])
            notifier.send(format_social_alert(spike, pair))
            log_alert(conn, spike)
            sent += 1

    conn.close()
    return sent


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    n = scan()
    print(f"[social] {n} alert(s) sent")