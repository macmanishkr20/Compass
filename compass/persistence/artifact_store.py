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

from compass.config import get_settings

logger = logging.getLogger("compass.artifacts")

_blob_service = None
_container_ensured = False


def _get_blob_container():
    global _blob_service, _container_ensured
    from azure.storage.blob import BlobServiceClient

    cfg = get_settings().storage
    if _blob_service is None:
        _blob_service = BlobServiceClient.from_connection_string(
            cfg.blob_connection_string
        )
    container = _blob_service.get_container_client(cfg.blob_container)
    if not _container_ensured:
        try:
            container.create_container()
        except Exception:  # noqa: BLE001 — already exists
            pass
        _container_ensured = True
    return container


def _upload_sync(name: str, content: str) -> str:
    container = _get_blob_container()
    container.upload_blob(name, content.encode(), overwrite=True)
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
