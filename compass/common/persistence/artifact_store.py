"""Artifact store for large payloads (tool-result spills).

Blob Storage when AZURE_STORAGE_CONNECTION_STRING is set, local disk
otherwise. Returns a human-readable locator that goes into the truncation
stub, so the model (and the user) can find the full content later.

The blob SDK's sync client is used via asyncio.to_thread — one small upload
per spill does not justify managing a second async client lifecycle.
"""

from __future__ import annotations

import asyncio
import logging

from compass.common.config import get_settings
from compass.common.persistence import blob

logger = logging.getLogger("compass.artifacts")


def _upload_sync(name: str, content: str) -> str:
    # Through the shared helper, which is where this module's own
    # create-the-container-once logic ended up: it had it right and the three
    # written after it did not, each paying for an extra round trip per call.
    container = blob.container(get_settings().storage.blob_container)
    container.upload_blob(
        name, content.encode(), overwrite=True, max_concurrency=blob.CONCURRENCY
    )
    return f"blob://{get_settings().storage.blob_container}/{name}"


async def save_artifact(name: str, content: str) -> str:
    """Persist `content` under `name`; returns a locator for the stub."""
    settings = get_settings()
    if settings.storage.blob_configured:
        try:
            return await asyncio.to_thread(_upload_sync, name, content)
        except Exception as err:  # noqa: BLE001 — degrade to local disk
            logger.error("blob upload failed, spilling locally: %s", err)
    path = settings.tool_results_dir / name
    try:
        await asyncio.to_thread(path.write_text, content)
        return str(path)
    except OSError as err:
        logger.error("local artifact write failed: %s", err)
        return "(artifact could not be saved)"
