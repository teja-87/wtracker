import os
import asyncio
from collections import deque

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Header, HTTPException

import helius_api
from storage import wallet_store

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

# Optional: put your dev server's ID here for instant slash-command sync while
# testing. Global sync (leave unset) can take up to ~1 hour to show up.
GUILD_ID = os.getenv("GUILD_ID")
GUILD_ID = int(GUILD_ID) if GUILD_ID else None

# Optional: comma-separated role IDs allowed to add/remove wallets, in
# addition to server Administrators. If unset, only Administrators can.
ADMIN_ROLE_IDS = {
    int(r.strip()) for r in os.getenv("ADMIN_ROLE_IDS", "").split(",") if r.strip()
}

HELIUS_WEBHOOK_SECRET = os.getenv("HELIUS_WEBHOOK_SECRET")
MIN_SOL_THRESHOLD = float(os.getenv("MIN_SOL_THRESHOLD", "0.0"))

SOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

intents = discord.Intents.default()
discord_client = discord.Client(intents=intents)
tree = app_commands.CommandTree(discord_client)

app = FastAPI()

_seen_signatures: deque = deque(maxlen=2000)
_seen_set: set = set()

http_session: aiohttp.ClientSession | None = None


def mark_seen(sig: str) -> bool:
    if sig in _seen_set:
        return False
    _seen_set.add(sig)
    _seen_signatures.append(sig)
    if len(_seen_signatures) == _seen_signatures.maxlen and _seen_signatures[0] not in _seen_signatures:
        _seen_set.discard(_seen_signatures[0])
    return True


def short(addr: str, n: int = 4) -> str:
    return f"{addr[:n]}...{addr[-n:]}" if addr and len(addr) > 2 * n else addr


def is_valid_address(address: str) -> bool:
    # Solana base58 addresses are 32-44 chars. Not a full checksum, just a sanity check.
    return 32 <= len(address) <= 44 and address.isalnum()


def is_authorized(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or interaction.user is None:
        return False
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms and perms.administrator:
        return True
    if ADMIN_ROLE_IDS:
        user_role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
        if user_role_ids & ADMIN_ROLE_IDS:
            return True
    return False


# =========================
# CORE PARSING
# =========================

def parse_wallet_trades(tx: dict) -> list[dict]:
    if tx.get("type") != "SWAP":
        return []

    account_data = tx.get("accountData", [])
    signature = tx.get("signature")
    timestamp = tx.get("timestamp")
    source = tx.get("source")

    tracked = wallet_store.addresses()
    if not tracked:
        return []

    trades = []

    for wallet in tracked:
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
                "sol_change": sol_change,
                "signature": signature,
                "timestamp": timestamp,
                "source": source,
            })

    return trades


# =========================
# TOKEN METADATA
# =========================

async def fetch_token_info(mint: str) -> dict:
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
            pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
            return {
                "symbol": pair.get("baseToken", {}).get("symbol"),
                "name": pair.get("baseToken", {}).get("name"),
                "price_usd": pair.get("priceUsd"),
                "mcap": pair.get("marketCap") or pair.get("fdv"),
                "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
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

    embed = discord.Embed(title=f"{emoji} {trade['action']} — {name} ({symbol})", color=color)

    wallet_label = wallet_store.label(trade["wallet"])
    embed.add_field(name="Wallet", value=f"[{wallet_label}](https://solscan.io/account/{trade['wallet']})", inline=True)
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
        value=f"[Tx](https://solscan.io/tx/{trade['signature']}) | [DexScreener](https://dexscreener.com/solana/{trade['mint']})",
        inline=False,
    )
    embed.set_footer(text=trade.get("source") or "")
    return embed


# =========================
# SLASH COMMANDS
# =========================

@tree.command(name="addwallet", description="Start tracking a Solana wallet's buys/sells")
@app_commands.describe(address="Solana wallet address", label="Optional friendly name shown in alerts")
async def addwallet(interaction: discord.Interaction, address: str, label: str = None):
    await interaction.response.defer(ephemeral=True)

    if not is_authorized(interaction):
        await interaction.followup.send("❌ You don't have permission to manage tracked wallets.", ephemeral=True)
        return

    if not is_valid_address(address):
        await interaction.followup.send("❌ That doesn't look like a valid Solana wallet address.", ephemeral=True)
        return

    is_new = await wallet_store.add(
        address, label=label, added_by=interaction.user.id,
        guild_id=interaction.guild_id,
    )

    synced = await helius_api.add_address(http_session, address)

    msg = f"{'✅ Now tracking' if is_new else 'ℹ️ Already tracking'} `{address}`"
    if label:
        msg += f" as **{label}**"
    if not helius_api.configured():
        msg += "\n⚠️ HELIUS_API_KEY / HELIUS_WEBHOOK_ID not set — add this address to your Helius webhook manually in the dashboard."
    elif not synced:
        msg += "\n⚠️ Saved locally, but failed to sync with the Helius webhook automatically. Try `/syncwallets` or check the logs."

    await interaction.followup.send(msg, ephemeral=True)


@tree.command(name="removewallet", description="Stop tracking a Solana wallet")
@app_commands.describe(address="Solana wallet address to remove")
async def removewallet(interaction: discord.Interaction, address: str):
    await interaction.response.defer(ephemeral=True)

    if not is_authorized(interaction):
        await interaction.followup.send("❌ You don't have permission to manage tracked wallets.", ephemeral=True)
        return

    existed = await wallet_store.remove(address)
    if not existed:
        await interaction.followup.send(f"ℹ️ `{address}` wasn't being tracked.", ephemeral=True)
        return

    synced = await helius_api.remove_address(http_session, address)

    msg = f"🗑️ Stopped tracking `{address}`"
    if not helius_api.configured():
        msg += "\n⚠️ Remember to remove it from your Helius webhook manually."
    elif not synced:
        msg += "\n⚠️ Removed locally, but failed to sync with the Helius webhook. Try `/syncwallets`."

    await interaction.followup.send(msg, ephemeral=True)


@tree.command(name="listwallets", description="List all wallets currently being tracked")
async def listwallets(interaction: discord.Interaction):
    wallets = wallet_store.all()
    if not wallets:
        await interaction.response.send_message("No wallets are being tracked yet. Use `/addwallet`.", ephemeral=True)
        return

    lines = []
    for addr, info in wallets.items():
        label = f" — **{info['label']}**" if info.get("label") else ""
        lines.append(f"`{addr}`{label}")

    await interaction.response.send_message("**Tracked wallets:**\n" + "\n".join(lines), ephemeral=True)


@tree.command(name="syncwallets", description="Force-push the local wallet list to the Helius webhook")
async def syncwallets(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not is_authorized(interaction):
        await interaction.followup.send("❌ You don't have permission to do that.", ephemeral=True)
        return

    if not helius_api.configured():
        await interaction.followup.send("⚠️ HELIUS_API_KEY / HELIUS_WEBHOOK_ID aren't set, nothing to sync.", ephemeral=True)
        return

    ok = await helius_api.sync_all(http_session, wallet_store.addresses())
    if ok:
        await interaction.followup.send(f"✅ Synced {len(wallet_store.addresses())} wallet(s) to the Helius webhook.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Sync failed — check the bot logs.", ephemeral=True)


# =========================
# DISCORD BOT
# =========================

@discord_client.event
async def on_ready():
    print(f"Discord bot online: {discord_client.user}")

    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        print(f"Slash commands synced to guild {GUILD_ID}")
    else:
        await tree.sync()
        print("Slash commands synced globally (can take up to ~1h to appear)")

    print(f"Tracking {len(wallet_store.addresses())} wallet(s) from {wallet_store.path}")

    channel = discord_client.get_channel(CHANNEL_ID)
    if channel is None:
        print("❌ Could not find Discord channel!")
        return

    await channel.send(f"🟢 Wallet tracker is online — tracking {len(wallet_store.addresses())} wallet(s). Use `/addwallet` to add more.")
    print("✅ Startup message sent to Discord")


# =========================
# HEALTH CHECK
# =========================

@app.get("/")
async def home():
    return {"status": "ok", "service": "Solana Wallet Tracker", "tracked_wallets": len(wallet_store.addresses())}


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
            continue

        for trade in parse_wallet_trades(tx):
            if abs(trade["sol_change"]) < MIN_SOL_THRESHOLD:
                continue

            info = await fetch_token_info(trade["mint"])
            embed = build_embed(trade, info)

            if channel is not None:
                await channel.send(embed=embed)
            else:
                print("⚠️ Channel not found, dropping alert:", trade)

    return {"status": "received"}


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