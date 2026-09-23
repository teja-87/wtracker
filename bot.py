import os
import asyncio

import discord
from dotenv import load_dotenv
from fastapi import FastAPI, Request

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

intents = discord.Intents.default()

discord_client = discord.Client(intents=intents)

app = FastAPI()


@discord_client.event
async def on_ready():
    print(f"Discord bot online: {discord_client.user}")
    print(f"Discord channel ID: {CHANNEL_ID}")

    channel = discord_client.get_channel(CHANNEL_ID)

    if channel is None:
        print("❌ Could not find Discord channel!")
        return

    print(f"Found channel: {channel.name}")

    await channel.send("🟢 Wallet tracker is online!")

    print("✅ Startup message sent to Discord")


@app.get("/")
async def home():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    print("===== WEBHOOK RECEIVED =====")
    print(data)
    print("============================")

    return {"status": "received"}


@app.on_event("startup")
async def startup():
    asyncio.create_task(discord_client.start(DISCORD_TOKEN))