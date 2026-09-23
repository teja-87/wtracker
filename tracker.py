import os

import discord
from dotenv import load_dotenv
from fastapi import FastAPI, Request
import uvicorn

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

# -------------------------
# Discord
# -------------------------

intents = discord.Intents.default()

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Discord bot online: {client.user}")

    channel = client.get_channel(CHANNEL_ID)

    if channel:
        await channel.send("🟢 Wallet tracker is online!")


# -------------------------
# FastAPI
# -------------------------

app = FastAPI()


@app.get("/")
async def home():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()

    print("\n===== WEBHOOK RECEIVED =====")
    print(data)
    print("============================\n")

    return {"status": "received"}


# -------------------------
# Start everything
# -------------------------

if __name__ == "__main__":
    import asyncio

    async def start():
        await client.start(DISCORD_TOKEN)

    loop = asyncio.get_event_loop()

    loop.create_task(start())

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
    )

    server = uvicorn.Server(config)

    loop.run_until_complete(server.serve())

    # main bot