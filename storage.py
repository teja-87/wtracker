import json
import os
import asyncio
from pathlib import Path
from typing import Optional

DATA_FILE = Path(os.getenv("WALLETS_FILE", "wallets.json"))


class WalletStore:
    """
    Simple JSON-file-backed store of tracked wallets:
        { "<address>": {"label": "whale1", "added_by": 123456, "guild_id": 999} }
    Good enough for a single-bot-instance setup. Swap for sqlite/Postgres
    later if you need multi-instance or per-guild isolation at scale.
    """

    def __init__(self, path: Path = DATA_FILE):
        self.path = path
        self._lock = asyncio.Lock()
        self._wallets: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self._wallets = json.loads(self.path.read_text())
            except Exception as e:
                print(f"[storage] failed to load {self.path}: {e}")
                self._wallets = {}
        else:
            self._wallets = {}

    def _save(self):
        self.path.write_text(json.dumps(self._wallets, indent=2))

    async def add(self, address: str, label: Optional[str] = None, added_by: Optional[int] = None,
                  guild_id: Optional[int] = None) -> bool:
        """Returns True if this was a new wallet, False if it already existed (label/meta still updated)."""
        async with self._lock:
            is_new = address not in self._wallets
            entry = self._wallets.get(address, {})
            if label:
                entry["label"] = label
            if added_by is not None:
                entry["added_by"] = added_by
            if guild_id is not None:
                entry["guild_id"] = guild_id
            self._wallets[address] = entry
            self._save()
            return is_new

    async def remove(self, address: str) -> bool:
        async with self._lock:
            existed = address in self._wallets
            self._wallets.pop(address, None)
            self._save()
            return existed

    def all(self) -> dict[str, dict]:
        return dict(self._wallets)

    def addresses(self) -> set[str]:
        return set(self._wallets.keys())

    def label(self, address: str) -> str:
        return self._wallets.get(address, {}).get("label") or address


wallet_store = WalletStore()