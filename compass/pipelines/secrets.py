"""Where a connection's credential actually lives.

The indirection matters more than the backend, which is why this file exists
on day one rather than when someone gets round to Key Vault. A connection
stores a `secret_ref`; the value is fetched here at the moment it is used. If
credentials went inline into `Connection.config` now, moving them later would
mean migrating every stored connection — whereas with the reference in place,
`local` becomes `keyvault` by changing one setting.

Compass already loads Key Vault, in `_load_key_vault()` in common/config.py,
but that is a different job and cannot serve this one: it runs once at boot,
reads only, and flattens every secret into `os.environ`. A connection needs
writes (an OAuth token refreshing), per-connection scoping, and revocation.
The dependency and the credential are already there, though, which makes the
vault-backed store cheap when it is wanted.

The local store is deliberately modest about what it claims. It obscures a
value at rest with a key derived from the workspace; it is not a defence
against someone who can read the data directory, and it says so rather than
implying a protection it does not provide.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Protocol

from compass.common.config import get_settings

logger = logging.getLogger("compass.pipelines")


class SecretStore(Protocol):
    """Get, set and forget one credential by reference."""

    async def get(self, ref: str) -> str: ...
    async def set(self, ref: str, value: str) -> None: ...
    async def delete(self, ref: str) -> None: ...


def _obscure(value: bytes, key: bytes) -> bytes:
    """A keystream XOR, from HMAC-SHA256 in counter mode.

    Not encryption anyone should rely on, and not called encryption here. It
    keeps a token from sitting in plain sight in a file that might be copied
    into a backup or a screenshot, using only the standard library. The real
    answer is `keyvault`, and the reason this is acceptable as a default is
    that the reference indirection makes moving to it a config change.
    """
    out = bytearray()
    counter = 0
    while len(out) < len(value):
        block = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(a ^ b for a, b in zip(value, out[: len(value)]))


class LocalSecretStore:
    """One JSON file under the data directory, values obscured at rest."""

    def __init__(self) -> None:
        self._cache: dict[str, str] | None = None

    @property
    def _path(self) -> Path:
        settings = get_settings()
        folder = settings.workspace_root / settings.data_dir / "secrets"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / "connections.json"

    @property
    def _key(self) -> bytes:
        """Derived from an explicit key when there is one, the workspace when
        there is not. The explicit key is what makes the file portable between
        machines; without it, a copied file is inert elsewhere, which is a
        reasonable default for a local store."""
        material = os.environ.get("COMPASS_SECRET_KEY", "") or str(
            get_settings().workspace_root
        )
        return hashlib.sha256(material.encode("utf-8")).digest()

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        try:
            self._cache = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._cache = {}
        return self._cache

    def _flush(self) -> None:
        path = self._path
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._load(), indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # best effort; not every filesystem obeys
        except OSError:
            pass
        tmp.replace(path)

    async def get(self, ref: str) -> str:
        stored = self._load().get(ref)
        if not stored:
            return ""
        try:
            return _obscure(base64.b64decode(stored), self._key).decode("utf-8")
        except Exception:  # noqa: BLE001 — a key change makes old values junk
            logger.warning("secret %s could not be read; treat it as unset", ref)
            return ""

    async def set(self, ref: str, value: str) -> None:
        blob = _obscure(value.encode("utf-8"), self._key)
        self._load()[ref] = base64.b64encode(blob).decode("ascii")
        self._flush()

    async def delete(self, ref: str) -> None:
        self._load().pop(ref, None)
        self._flush()


class KeyVaultSecretStore:
    """Azure Key Vault, one secret per reference.

    Uses the same `DefaultAzureCredential` the rest of Compass already relies
    on. Vault names allow letters, digits and hyphens only, so a reference is
    normalised on the way in and the mapping stays one-to-one.
    """

    def __init__(self, vault_url: str) -> None:
        self._url = vault_url
        self._client = None

    def _name(self, ref: str) -> str:
        return "compass-conn-" + "".join(
            c if c.isalnum() else "-" for c in ref
        ).strip("-")

    def _get_client(self):
        if self._client is None:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            self._client = SecretClient(
                vault_url=self._url, credential=DefaultAzureCredential()
            )
        return self._client

    async def get(self, ref: str) -> str:
        import asyncio

        def _fetch() -> str:
            try:
                return self._get_client().get_secret(self._name(ref)).value or ""
            except Exception:  # noqa: BLE001 — a missing secret is "unset"
                return ""

        return await asyncio.to_thread(_fetch)

    async def set(self, ref: str, value: str) -> None:
        import asyncio

        await asyncio.to_thread(
            lambda: self._get_client().set_secret(self._name(ref), value)
        )

    async def delete(self, ref: str) -> None:
        import asyncio

        def _remove() -> None:
            try:
                self._get_client().begin_delete_secret(self._name(ref))
            except Exception:  # noqa: BLE001 — already gone is success
                pass

        await asyncio.to_thread(_remove)


_store: SecretStore | None = None


def get_secret_store() -> SecretStore:
    """The configured store, chosen the way the transcript store is chosen.

    Falls back to local, loudly, when `keyvault` is asked for without a vault
    URL — a silent downgrade to a weaker store is exactly the failure someone
    would not notice until it mattered.
    """
    global _store
    if _store is not None:
        return _store
    settings = get_settings()
    backend = (settings.pipelines.secrets_backend or "local").lower()
    if backend == "keyvault":
        url = settings.key_vault_url
        if url:
            logger.info("pipeline secrets: Azure Key Vault (%s)", url)
            _store = KeyVaultSecretStore(url)
            return _store
        logger.warning(
            "pipeline secrets: keyvault requested but no key_vault_url is "
            "set; falling back to the local store"
        )
    logger.info("pipeline secrets: local file")
    _store = LocalSecretStore()
    return _store


def reset_secret_store() -> None:
    """Forget the chosen store. For tests."""
    global _store
    _store = None
