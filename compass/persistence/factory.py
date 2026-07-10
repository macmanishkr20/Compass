"""Backend selection: COMPASS_STORAGE_BACKEND=local|cosmos.

Setting AZURE_COSMOS_ENDPOINT + AZURE_COSMOS_KEY implies cosmos unless the
backend is pinned explicitly. Misconfiguration fails loudly at startup, not
silently mid-session.
"""

from __future__ import annotations

import logging

from compass.config import get_settings
from compass.persistence.base import TranscriptStore

logger = logging.getLogger("compass.persistence")

_store: TranscriptStore | None = None


def get_transcript_store() -> TranscriptStore:
    global _store
    if _store is not None:
        return _store
    cfg = get_settings().storage
    if cfg.backend == "cosmos":
        if not cfg.cosmos_configured:
            raise RuntimeError(
                "COMPASS_STORAGE_BACKEND=cosmos but AZURE_COSMOS_ENDPOINT / "
                "AZURE_COSMOS_KEY are not set."
            )
        from compass.persistence.cosmos_store import CosmosTranscriptStore

        logger.info("transcript store: Cosmos DB (%s)", cfg.cosmos_database)
        _store = CosmosTranscriptStore()
    else:
        from compass.persistence.session_store import SessionStore

        logger.info("transcript store: local JSONL")
        _store = SessionStore()
    return _store
