import os
import asyncio
import time
from collections import deque

import aiohttp
import discord
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Header, HTTPException

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

# Comma-separated list of Solana wallet addresses you want to track
TRACKED_WALLETS = {
    w.strip() for w in os.getenv("TRACKED_WALLETS", "").split(",") if w.strip()
}

# Optional: set this in Helius webhook config -> "Auth Header" and here,
# so randoms can't POST fake trades to your bot.
HELIUS_WEBHOOK_SECRET = os.getenv("HELIUS_WEBHOOK_SECRET")

# Ignore trades smaller than this (in SOL) to cut down on noise/dust
MIN_SOL_THRESHOLD = float(os.getenv("MIN_SOL_THRESHOLD", "0.0"))

# Label wallets: "A6CP...=whale1,4giAj...=whale2" (optional, purely cosmetic)
WALLET_LABELS = {}
for pair in os.getenv("WALLET_LABELS", "").split(","):
    if "=" in pair:
        addr, label = pair.split("=", 1)
        WALLET_LABELS[addr.strip()] = label.strip()

SOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

intents = discord.Intents.default()
discord_client = discord.Client(intents=intents)

app = FastAPI()

# Simple in-memory dedup ring buffer (Helius can redeliver the same tx)
_seen_signatures: deque = deque(maxlen=2000)
_seen_set: set = set()

http_session: aiohttp.ClientSession | None = None


def mark_seen(sig: str) -> bool:
    """Returns True if this signature is new (not yet processed)."""
    if sig in _seen_set:
        return False
    _seen_set.add(sig)
    _seen_signatures.append(sig)
    if len(_seen_signatures) == _seen_signatures.maxlen:
        # evict oldest from the set to match the deque
        oldest = _seen_signatures[0]
        # deque auto-drops oldest on append when full; keep set in sync lazily
        if oldest not in _seen_signatures:
            _seen_set.discard(oldest)
    return True


def short(addr: str, n: int = 4) -> str:
    return f"{addr[:n]}...{addr[-n:]}" if addr and len(addr) > 2 * n else addr


def label_for(addr: str) -> str:
    return WALLET_LABELS.get(addr, short(addr))


# =========================
# CORE PARSING
# =========================

def parse_wallet_trades(tx: dict) -> list[dict]:
    """
    Given one Helius enhanced-transaction object, figure out, for each
    tracked wallet, whether it bought or sold a non-SOL/non-stable token
    (i.e. a "meme coin"), purely from accountData — this works whether
    or not Helius populated events.swap, and regardless of which DEX/
    router (Jupiter, OKX, Raydium, pump.fun AMM, etc.) was used.
    """
    if tx.get("type") != "SWAP":
        return []

    account_data = tx.get("accountData", [])
    signature = tx.get("signature")
    timestamp = tx.get("timestamp")
    source = tx.get("source")

    trades = []

    for wallet in TRACKED_WALLETS:
        sol_change_lamports = 0
        token_changes: dict[str, float] = {}
        wallet_touched = False

        for entry in account_data:
            if entry.get("account") == wallet:
                sol_change_lamports = entry.get("nativeBalanceChange", 0)
                wallet_touched = True

            for tbc in entry.get("tokenBalanceChanges", []):
                if tbc.get("userAccount") == wallet:
                    wallet_touched = True
                    mint = tbc["mint"]
                    raw = int(tbc["rawTokenAmount"]["tokenAmount"])
                    decimals = tbc["rawTokenAmount"]["decimals"]
                    amount = raw / (10 ** decimals)
                    token_changes[mint] = token_changes.get(mint, 0.0) + amount

        if not wallet_touched:
            continue

        meme_changes = {
            m: a for m, a in token_changes.items()
            if m != SOL_MINT and m not in STABLE_MINTS and abs(a) > 0
        }

        sol_change = sol_change_lamports / 1e9

        for mint, amount in meme_changes.items():
            action = "BUY" if amount > 0 else "SELL"
            trades.append({
                "wallet": wallet,
                "action": action,
                "mint": mint,
                "token_amount": abs(amount),
                "sol_change": sol_change,   # negative for buys, positive for sells (net of fees)
                "signature": signature,
                "timestamp": timestamp,
                "source": source,
            })

    return trades


# =========================
# TOKEN METADATA (DexScreener, no API key needed)
# =========================

async def fetch_token_info(mint: str) -> dict:
    """Best-effort token metadata lookup. Returns {} on any failure."""
    global http_session
    if http_session is None:
        return {}
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
    try:
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json()
            pairs = data.get("pairs") or []
            if not pairs:
                return {}
            # pick the highest-liquidity pair
            pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
            return {
                "symbol": pair.get("baseToken", {}).get("symbol"),
                "name": pair.get("baseToken", {}).get("name"),
                "price_usd": pair.get("priceUsd"),
                "mcap": pair.get("marketCap") or pair.get("fdv"),
                "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
                "url": pair.get("url"),
            }
    except Exception as e:
        print(f"[dexscreener] lookup failed for {mint}: {e}")
        return {}


# =========================
# DISCORD EMBED
# =========================

def build_embed(trade: dict, info: dict) -> discord.Embed:
    is_buy = trade["action"] == "BUY"
    color = discord.Color.green() if is_buy else discord.Color.red()
    emoji = "🟢" if is_buy else "🔴"

    symbol = info.get("symbol") or short(trade["mint"], 6)
    name = info.get("name") or symbol
    sol_amount = abs(trade["sol_change"])
    token_amount = trade["token_amount"]
    price_per_token_sol = (sol_amount / token_amount) if token_amount else 0

    title = f"{emoji} {trade['action']} — {name} ({symbol})"
    embed = discord.Embed(title=title, color=color, timestamp=None)

    embed.add_field(name="Wallet", value=f"[{label_for(trade['wallet'])}](https://solscan.io/account/{trade['wallet']})", inline=True)
    embed.add_field(name="Amount", value=f"{token_amount:,.4f} {symbol}", inline=True)
    embed.add_field(name="SOL", value=f"{sol_amount:.4f} SOL", inline=True)

    if price_per_token_sol:
        embed.add_field(name="Price/token", value=f"{price_per_token_sol:.8f} SOL", inline=True)
    if info.get("price_usd"):
        embed.add_field(name="Price (USD)", value=f"${float(info['price_usd']):.6f}", inline=True)
    if info.get("mcap"):
        embed.add_field(name="Market Cap", value=f"${float(info['mcap']):,.0f}", inline=True)
    if info.get("liquidity_usd"):
        embed.add_field(name="Liquidity", value=f"${float(info['liquidity_usd']):,.0f}", inline=True)

    embed.add_field(name="Mint", value=f"`{trade['mint']}`", inline=False)
    embed.add_field(
        name="Links",
        value=(
            f"[Tx](https://solscan.io/tx/{trade['signature']}) | "
            f"[DexScreener](https://dexscreener.com/solana/{trade['mint']})"
        ),
        inline=False,
    )
    embed.set_footer(text=trade.get("source") or "")
    return embed


# =========================
# DISCORD BOT
# =========================

@discord_client.event
async def on_ready():
    print(f"Discord bot online: {discord_client.user}")
    print(f"Discord channel ID: {CHANNEL_ID}")
    print(f"Tracking {len(TRACKED_WALLETS)} wallet(s): {TRACKED_WALLETS}")

    channel = discord_client.get_channel(CHANNEL_ID)
    if channel is None:
        print("❌ Could not find Discord channel!")
        return

    print(f"Found channel: {channel.name}")
    await channel.send(f"🟢 Wallet tracker is online — tracking {len(TRACKED_WALLETS)} wallet(s).")
    print("✅ Startup message sent to Discord")


# =========================
# HEALTH CHECK
# =========================

@app.get("/")
async def home():
    return {"status": "ok", "service": "Solana Wallet Tracker", "tracked_wallets": len(TRACKED_WALLETS)}


# =========================
# HELIUS WEBHOOK
# =========================

@app.post("/webhook")
async def webhook(request: Request, authorization: str | None = Header(default=None)):
    if HELIUS_WEBHOOK_SECRET and authorization != HELIUS_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    if isinstance(data, dict):
        data = [data]

    channel = discord_client.get_channel(CHANNEL_ID)

    for tx in data:
        sig = tx.get("signature")
        if sig and not mark_seen(sig):
            continue  # already processed this tx

        trades = parse_wallet_trades(tx)

        for trade in trades:
            if abs(trade["sol_change"]) < MIN_SOL_THRESHOLD:
                continue

            info = await fetch_token_info(trade["mint"])
            embed = build_embed(trade, info)

            if channel is not None:
                await channel.send(embed=embed)
            else:
                print("⚠️ Channel not found, dropping alert:", trade)

    return {"status": "received", "trades_found": True}


# =========================
# STARTUP / SHUTDOWN
# =========================

@app.on_event("startup")
async def startup():
    global http_session
    http_session = aiohttp.ClientSession()
    asyncio.create_task(discord_client.start(DISCORD_TOKEN))


@app.on_event("shutdown")
async def shutdown():
    global http_session
    if http_session:
        await http_session.close()
    await discord_client.close()