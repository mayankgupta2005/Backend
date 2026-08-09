"""
NovaShields Firebase Realtime Database Service Layer.

Provides both synchronous (legacy) and async-safe wrappers for all
Firebase operations.  The async variants use ``asyncio.to_thread`` so
they never block the FastAPI event loop.

Responsibilities
────────────────
WRITE:
  • device status, live telemetry, accident status, alert status
  • device commands (structured with lifecycle)
  • emergency commands (legacy flat format)
  • camera metadata, camera capture, alert state

READ:
  • device telemetry, status, command acknowledgement, camera status
  • accident status

Security:
  • Firebase Admin SDK credentials are never exposed to clients.
  • All credential references stay server-side only.
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import firebase_admin
from firebase_admin import credentials, db

logger = logging.getLogger(__name__)

_firebase_initialized = False


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_firebase():
    """Initialise the Firebase Admin SDK (idempotent)."""
    global _firebase_initialized
    if _firebase_initialized:
        return

    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    sa_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH")
    db_url = os.environ.get("FIREBASE_DATABASE_URL")

    if not db_url:
        logger.warning("Firebase not initialized: FIREBASE_DATABASE_URL not set.")
        return

    try:
        if not firebase_admin._apps:
            if sa_json:
                cred_dict = json.loads(sa_json)
                cred = credentials.Certificate(cred_dict)
            elif sa_path and os.path.exists(sa_path):
                cred = credentials.Certificate(sa_path)
            else:
                logger.warning(
                    "Firebase not initialized: Neither FIREBASE_SERVICE_ACCOUNT_JSON "
                    "nor valid FIREBASE_SERVICE_ACCOUNT_PATH provided."
                )
                return

            firebase_admin.initialize_app(cred, {"databaseURL": db_url})
        _firebase_initialized = True
        logger.info("Firebase Admin SDK initialized successfully for RTDB.")
    except Exception:
        logger.error(
            "Failed to initialize Firebase Admin SDK. "
            "Check credentials and configuration."
        )


def is_initialized() -> bool:
    """Return whether Firebase has been successfully initialised."""
    return _firebase_initialized


# ---------------------------------------------------------------------------
# Low-level helpers  (synchronous – run inside to_thread for async callers)
# ---------------------------------------------------------------------------

def _set_node(path: str, data: dict):
    """Merge *data* into the Firebase node at *path* (update semantics)."""
    init_firebase()
    if not _firebase_initialized:
        return
    try:
        ref = db.reference(path)
        ref.update(data)
    except Exception as e:
        logger.error(f"Failed to update Firebase node {path}: {e}")


def _overwrite_node(path: str, data: Any):
    """Completely overwrite the Firebase node at *path*."""
    init_firebase()
    if not _firebase_initialized:
        return
    try:
        ref = db.reference(path)
        ref.set(data)
    except Exception as e:
        logger.error(f"Failed to set Firebase node {path}: {e}")


def _read_node(path: str) -> Optional[Any]:
    """Read and return the value at *path*, or ``None`` on failure."""
    init_firebase()
    if not _firebase_initialized:
        return None
    try:
        ref = db.reference(path)
        return ref.get()
    except Exception as e:
        logger.error(f"Failed to read Firebase node {path}: {e}")
        return None


def _delete_node(path: str):
    """Delete the Firebase node at *path*."""
    init_firebase()
    if not _firebase_initialized:
        return
    try:
        ref = db.reference(path)
        ref.delete()
    except Exception as e:
        logger.error(f"Failed to delete Firebase node {path}: {e}")


# ---------------------------------------------------------------------------
# SYNCHRONOUS write helpers  (retained for backward-compatibility)
# ---------------------------------------------------------------------------

def update_system_status(status_dict: dict):
    _set_node("system_status", status_dict)


def update_device_status(device_id: str, status_dict: dict):
    _set_node(f"devices/{device_id}/status", status_dict)


def update_live_telemetry(device_id: str, telemetry_dict: dict):
    # For telemetry, we use set() to completely overwrite instead of merge
    init_firebase()
    if not _firebase_initialized:
        return
    try:
        ref = db.reference(f"devices/{device_id}/live_telemetry")
        ref.set(telemetry_dict)
    except Exception as e:
        logger.error(f"Failed to update live_telemetry for {device_id}: {e}")


def update_accident_status(device_id: str, accident_dict: dict):
    _set_node(f"devices/{device_id}/accident_status", accident_dict)


def update_alert_status(device_id: str, alert_dict: dict):
    _set_node(f"devices/{device_id}/alert_status", alert_dict)


def push_emergency_command(device_id: str, command_dict: dict):
    _set_node(f"devices/{device_id}/emergency_commands", command_dict)


def update_camera_capture(device_id: str, capture_dict: dict):
    _set_node(f"devices/{device_id}/camera_capture", capture_dict)


# ---------------------------------------------------------------------------
# ASYNC wrappers  (non-blocking for use inside FastAPI handlers)
# ---------------------------------------------------------------------------

async def async_update_device_status(device_id: str, status_dict: dict):
    await asyncio.to_thread(update_device_status, device_id, status_dict)


async def async_update_live_telemetry(device_id: str, telemetry_dict: dict):
    await asyncio.to_thread(update_live_telemetry, device_id, telemetry_dict)


async def async_update_accident_status(device_id: str, accident_dict: dict):
    await asyncio.to_thread(update_accident_status, device_id, accident_dict)


async def async_update_alert_status(device_id: str, alert_dict: dict):
    await asyncio.to_thread(update_alert_status, device_id, alert_dict)


async def async_push_emergency_command(device_id: str, command_dict: dict):
    await asyncio.to_thread(push_emergency_command, device_id, command_dict)


async def async_update_camera_capture(device_id: str, capture_dict: dict):
    await asyncio.to_thread(update_camera_capture, device_id, capture_dict)


async def async_update_system_status(status_dict: dict):
    await asyncio.to_thread(update_system_status, status_dict)


# ---------------------------------------------------------------------------
# NEW: Structured command write  (Firebase device commands)
# ---------------------------------------------------------------------------

def write_device_command(device_id: str, command_data: dict):
    """Write a structured command to ``devices/{device_id}/commands/current``.

    *command_data* should contain at minimum:
        command, command_id, status, created_at
    """
    _overwrite_node(f"devices/{device_id}/commands/current", command_data)


async def async_write_device_command(device_id: str, command_data: dict):
    await asyncio.to_thread(write_device_command, device_id, command_data)


def update_command_ack(device_id: str, ack_data: dict):
    """Merge acknowledgement fields into ``devices/{device_id}/commands/current``.

    Typically called when a device reports RECEIVED / EXECUTED / FAILED.
    """
    _set_node(f"devices/{device_id}/commands/current", ack_data)


async def async_update_command_ack(device_id: str, ack_data: dict):
    await asyncio.to_thread(update_command_ack, device_id, ack_data)


# ---------------------------------------------------------------------------
# NEW: Alert state  (for alert-module to read)
# ---------------------------------------------------------------------------

def update_alert_state(device_id: str, alert_data: dict):
    """Write/update the ``devices/{device_id}/alert`` node.

    The alert module reads this to know whether to activate buzzer/LEDs/GSM.
    """
    _overwrite_node(f"devices/{device_id}/alert", alert_data)


async def async_update_alert_state(device_id: str, alert_data: dict):
    await asyncio.to_thread(update_alert_state, device_id, alert_data)


# ---------------------------------------------------------------------------
# NEW: Camera metadata
# ---------------------------------------------------------------------------

def update_camera_metadata(device_id: str, camera_data: dict):
    """Write/update the ``devices/{device_id}/camera`` node."""
    _set_node(f"devices/{device_id}/camera", camera_data)


async def async_update_camera_metadata(device_id: str, camera_data: dict):
    await asyncio.to_thread(update_camera_metadata, device_id, camera_data)


# ---------------------------------------------------------------------------
# NEW: READ helpers  (async-only – always use from FastAPI handlers)
# ---------------------------------------------------------------------------

def read_device_telemetry(device_id: str) -> Optional[dict]:
    """Read ``devices/{device_id}/live_telemetry``."""
    return _read_node(f"devices/{device_id}/live_telemetry")


async def async_read_device_telemetry(device_id: str) -> Optional[dict]:
    return await asyncio.to_thread(read_device_telemetry, device_id)


def read_device_status(device_id: str) -> Optional[dict]:
    """Read ``devices/{device_id}/status``."""
    return _read_node(f"devices/{device_id}/status")


async def async_read_device_status(device_id: str) -> Optional[dict]:
    return await asyncio.to_thread(read_device_status, device_id)


def read_command_status(device_id: str) -> Optional[dict]:
    """Read ``devices/{device_id}/commands/current``."""
    return _read_node(f"devices/{device_id}/commands/current")


async def async_read_command_status(device_id: str) -> Optional[dict]:
    return await asyncio.to_thread(read_command_status, device_id)


def read_camera_status(device_id: str) -> Optional[dict]:
    """Read ``devices/{device_id}/camera``."""
    return _read_node(f"devices/{device_id}/camera")


async def async_read_camera_status(device_id: str) -> Optional[dict]:
    return await asyncio.to_thread(read_camera_status, device_id)


def read_accident_status(device_id: str) -> Optional[dict]:
    """Read ``devices/{device_id}/accident_status``."""
    return _read_node(f"devices/{device_id}/accident_status")


async def async_read_accident_status(device_id: str) -> Optional[dict]:
    return await asyncio.to_thread(read_accident_status, device_id)


def read_alert_state(device_id: str) -> Optional[dict]:
    """Read ``devices/{device_id}/alert``."""
    return _read_node(f"devices/{device_id}/alert")


async def async_read_alert_state(device_id: str) -> Optional[dict]:
    return await asyncio.to_thread(read_alert_state, device_id)


# ---------------------------------------------------------------------------
# Utility: now_iso (duplicate kept local to avoid circular import)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
