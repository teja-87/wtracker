import os
from typing import Optional

import aiohttp

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")
HELIUS_WEBHOOK_ID = os.getenv("HELIUS_WEBHOOK_ID")
BASE_URL = "https://api.helius.xyz/v0/webhooks"

# True once we've confirmed HELIUS_API_KEY / HELIUS_WEBHOOK_ID are usable
_configured = bool(HELIUS_API_KEY and HELIUS_WEBHOOK_ID)


def configured() -> bool:
    return _configured


async def _get_webhook(session: aiohttp.ClientSession) -> dict:
    url = f"{BASE_URL}/{HELIUS_WEBHOOK_ID}?api-key={HELIUS_API_KEY}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def _update_webhook(session: aiohttp.ClientSession, webhook: dict) -> dict:
    url = f"{BASE_URL}/{HELIUS_WEBHOOK_ID}?api-key={HELIUS_API_KEY}"
    # Helius expects the full object back on PUT, not a partial patch
    payload = {
        "webhookURL": webhook["webhookURL"],
        "transactionTypes": webhook.get("transactionTypes", ["SWAP"]),
        "accountAddresses": webhook.get("accountAddresses", []),
        "webhookType": webhook.get("webhookType", "enhanced"),
    }
    if webhook.get("authHeader"):
        payload["authHeader"] = webhook["authHeader"]

    async with session.put(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def add_address(session: aiohttp.ClientSession, address: str) -> bool:
    """Returns True on success (or if Helius isn't configured, in which case
    it's a no-op and the caller should warn the user to add it manually)."""
    if not _configured:
        return False
    try:
        webhook = await _get_webhook(session)
        addresses = set(webhook.get("accountAddresses", []))
        if address in addresses:
            return True
        addresses.add(address)
        webhook["accountAddresses"] = sorted(addresses)
        await _update_webhook(session, webhook)
        return True
    except Exception as e:
        print(f"[helius_api] add_address failed for {address}: {e}")
        return False


async def remove_address(session: aiohttp.ClientSession, address: str) -> bool:
    if not _configured:
        return False
    try:
        webhook = await _get_webhook(session)
        addresses = set(webhook.get("accountAddresses", []))
        if address not in addresses:
            return True
        addresses.discard(address)
        webhook["accountAddresses"] = sorted(addresses)
        await _update_webhook(session, webhook)
        return True
    except Exception as e:
        print(f"[helius_api] remove_address failed for {address}: {e}")
        return False


async def sync_all(session: aiohttp.ClientSession, addresses: set[str]) -> bool:
    """Force the Helius webhook's address list to exactly match `addresses`."""
    if not _configured:
        return False
    try:
        webhook = await _get_webhook(session)
        webhook["accountAddresses"] = sorted(addresses)
        await _update_webhook(session, webhook)
        return True
    except Exception as e:
        print(f"[helius_api] sync_all failed: {e}")
        return False


async def get_current_addresses(session: aiohttp.ClientSession) -> Optional[set]:
    if not _configured:
        return None
    try:
        webhook = await _get_webhook(session)
        return set(webhook.get("accountAddresses", []))
    except Exception as e:
        print(f"[helius_api] get_current_addresses failed: {e}")
        return None