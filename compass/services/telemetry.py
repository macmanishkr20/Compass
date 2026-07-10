"""Telemetry — Azure Application Insights via OpenTelemetry.

The logEvent analog. If APPLICATIONINSIGHTS_CONNECTION_STRING is set, the
azure-monitor-opentelemetry distro is configured once at startup and every
log_event() lands in App Insights (customDimensions carry the properties,
prefixed `compass.`). Without the key, events go nowhere at zero cost —
instrumentation call sites never need to know.
"""

from __future__ import annotations

import logging

from compass.config import get_settings

_logger = logging.getLogger("compass.events")
_logger.propagate = False  # never spam the console; App Insights or nothing
_configured = False
_enabled = False


def setup_telemetry() -> bool:
    """Idempotent. Returns True when App Insights export is active."""
    global _configured, _enabled
    if _configured:
        return _enabled
    _configured = True
    telemetry = get_settings().telemetry
    if not telemetry.enabled:
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            connection_string=telemetry.connection_string,
            logger_name="compass.events",
        )
        _logger.setLevel(logging.INFO)
        _enabled = True
        logging.getLogger("compass.telemetry").info(
            "telemetry: exporting to Application Insights as role %r",
            telemetry.role_name,
        )
    except Exception as err:  # noqa: BLE001 — telemetry must never block startup
        logging.getLogger("compass.telemetry").error(
            "telemetry setup failed (continuing without): %s", err
        )
    return _enabled


def log_event(name: str, **properties) -> None:
    """Fire-and-forget structured event (tengu_* analog).

    Values must be scalars — never message content or file contents; the
    same PII discipline the original enforces with its VERIFIED type brand.
    """
    if not _enabled:
        return
    _logger.info(name, extra={f"compass.{k}": v for k, v in properties.items()})
