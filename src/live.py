"""
Real-time wallet watching over WebSocket.

Replaces polling for the wallets you care most about. Instead of asking Helius
every 30 minutes what happened, this holds an open connection and gets pushed
each transaction seconds after it confirms.

WHY WEBSOCKET AND NOT WEBHOOKS
  Webhooks need a publicly reachable URL. On a home machine that means ngrok
  or a deployed server. A WebSocket is an outbound connection, so it works
  from behind any router with no setup.

LATENCY, HONESTLY
  This fires seconds after a transaction confirms. That is a large improvement
  over 30-minute polling, but it is still after the fact: the wallet's fill is
  already done and the price has already moved by whatever their buy moved it.
  On a token with thin liquidity that can be a lot. Treat this as "know
  quickly", not "get in at the same price".

CREDITS
  Subscriptions are cheap compared to repeated history calls, but streamed
  messages still consume credits. Watch your Helius dashboard for the first
  day and drop wallets if it climbs faster than you expected.

Needs: pip install websockets
"""
import asyncio
import json
import os
import time
from pathlib import Path

import requests

WS_URL = "wss://mainnet.helius-rpc.com"
RPC_URL = "https://mainnet.helius-rpc.com"
HELIUS_API = "https://api.helius.xyz"
WALLETS_PATH = Path(__file__).resolve().parent.parent / "wallets.json"

QUOTE_MINTS = {
    "SOL",
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}

# Don't re-alert the same signature if the socket reconnects
_seen_signatures: set[str] = set()
_SEEN_CAP = 2000


def _key() -> str:
    return os.getenv("HELIUS_API_KEY", "")


def load_watch_list() -> list[dict]:
    """
    Wallets to watch live. Uses wallets.json, but only entries with
    "live": true — watching every wallet in real time burns credits for
    little benefit on the slow ones.
    """
    if not WALLETS_PATH.exists():
        return []
    with open(WALLETS_PATH) as f:
        wallets = json.load(f)
    return [w for w in wallets
            if w.get("live") and w.get("address")
            and w["address"] != "PASTE_WALLET_ADDRESS_HERE"]


def fetch_parsed_tx(signature: str) -> dict | None:
    """Pull the parsed transaction so we can see what was actually swapped."""
    try:
        resp = requests.post(
            f"{HELIUS_API}/v0/transactions",
            params={"api-key": _key()},
            json={"transactions": [signature]},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[live] parse failed for {signature[:8]}...: {e}")
        return None
    data = resp.json()
    return data[0] if isinstance(data, list) and data else None


def extract_buy(tx: dict, wallet_address: str) -> dict | None:
    """
    Did this wallet acquire a non-quote token? Returns the buy, or None.

    Uses tokenTransfers, which the parsed endpoint gives us directly, rather
    than diffing raw balances.
    """
    if not tx or tx.get("transactionError"):
        return None

    for tr in tx.get("tokenTransfers") or []:
        if tr.get("toUserAccount") != wallet_address:
            continue
        mint = tr.get("mint")
        if not mint or mint in QUOTE_MINTS:
            continue
        return {
            "mint": mint,
            "amount": tr.get("tokenAmount"),
            "signature": tx.get("signature"),
            "ts": tx.get("timestamp") or time.time(),
            "source": tx.get("source"),
        }
    return None


def format_live_alert(buy: dict, label: str, pair: dict | None) -> str:
    mint = buy["mint"]
    symbol = (pair.get("baseToken") or {}).get("symbol") if pair else None
    name = symbol or mint[:10]

    lines = [
        f"⚡ {label} just bought {name}",
        "",
    ]

    if pair:
        price = float(pair.get("priceUsd") or 0)
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
        chg = float((pair.get("priceChange") or {}).get("h24") or 0)
        lines += [
            f"Price ${price:.10f}".rstrip("0"),
            f"Liquidity ${liq:,.0f}   MCap ${mcap:,.0f}   24h {chg:+.0f}%",
        ]
        if liq < 10_000:
            lines.append("⚠ Below the $10k liquidity floor — hard to exit.")
        lines.append(f"Chart: https://dexscreener.com/solana/{mint}")
    else:
        lines.append("No Dexscreener pair yet — likely pre-graduation.")

    lines += [
        "",
        f"Tx: https://solscan.io/tx/{buy['signature']}",
        f"Mint: {mint}",
        "",
        "Live alert — no safety filter or sizing applied. Their fill is "
        "already done; yours would be worse.",
    ]
    return "\n".join(lines)


def handle_buy(buy: dict, label: str) -> None:
    from src import discovery, notifier

    pairs = discovery.dedupe_by_mint(discovery.get_pairs_for_tokens([buy["mint"]]))
    pair = pairs[0] if pairs else None
    notifier.send(format_live_alert(buy, label, pair))


async def watch(on_buy=None) -> None:
    """
    Hold a WebSocket open and alert on every buy by a watched wallet.
    Reconnects with backoff if the connection drops.
    """
    import websockets

    wallets = load_watch_list()
    if not wallets:
        print('[live] no wallets marked "live": true in wallets.json')
        return
    if not _key():
        print("[live] HELIUS_API_KEY not set")
        return

    by_address = {w["address"]: w.get("label", w["address"][:8]) for w in wallets}
    url = f"{WS_URL}/?api-key={_key()}"
    backoff = 2

    print(f"[live] watching {len(wallets)} wallet(s): {', '.join(by_address.values())}")

    while True:
        try:
            async with websockets.connect(url, ping_interval=25, ping_timeout=15) as ws:
                sub_to_address = {}
                for i, (addr, label) in enumerate(by_address.items(), start=1):
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": i,
                        "method": "logsSubscribe",
                        "params": [{"mentions": [addr]}, {"commitment": "confirmed"}],
                    }))
                    resp = json.loads(await ws.recv())
                    if "result" in resp:
                        sub_to_address[resp["result"]] = addr

                print(f"[live] connected, {len(sub_to_address)} subscription(s)")
                backoff = 2

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("method") != "logsNotification":
                        continue
                    params = msg.get("params") or {}
                    addr = sub_to_address.get(params.get("subscription"))
                    if not addr:
                        continue

                    value = (params.get("result") or {}).get("value") or {}
                    sig = value.get("signature")
                    if not sig or value.get("err") or sig in _seen_signatures:
                        continue

                    _seen_signatures.add(sig)
                    if len(_seen_signatures) > _SEEN_CAP:
                        _seen_signatures.clear()

                    # Give the indexer a moment to catch up before parsing
                    await asyncio.sleep(1.5)
                    tx = await asyncio.to_thread(fetch_parsed_tx, sig)
                    buy = extract_buy(tx, addr)
                    if not buy:
                        continue

                    label = by_address[addr]
                    print(f"[live] {label} bought {buy['mint'][:10]}")
                    if on_buy:
                        on_buy(buy, label)
                    else:
                        await asyncio.to_thread(handle_buy, buy, label)

        except Exception as e:
            print(f"[live] connection lost ({e}); reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def run(on_buy=None) -> None:
    """Blocking entry point, safe to call from a thread."""
    asyncio.run(watch(on_buy))


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    run()