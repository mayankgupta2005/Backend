import os
import json
import firebase_admin
from firebase_admin import credentials, db
import logging

logger = logging.getLogger(__name__)

_firebase_initialized = False

def init_firebase():
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
                logger.warning("Firebase not initialized: Neither FIREBASE_SERVICE_ACCOUNT_JSON nor valid FIREBASE_SERVICE_ACCOUNT_PATH provided.")
                return

            firebase_admin.initialize_app(cred, {
                'databaseURL': db_url
            })
        _firebase_initialized = True
        logger.info("Firebase Admin SDK initialized successfully for RTDB.")
    except Exception as e:
        logger.error("Failed to initialize Firebase Admin SDK. Check credentials and configuration.")

def _set_node(path: str, data: dict):
    init_firebase()
    if not _firebase_initialized:
        return
    try:
        ref = db.reference(path)
        ref.update(data)  # Use update to merge instead of overwrite, if preferred, or set
    except Exception as e:
        logger.error(f"Failed to update Firebase node {path}: {e}")

def update_system_status(status_dict: dict):
    _set_node("system_status", status_dict)

def update_device_status(device_id: str, status_dict: dict):
    _set_node(f"devices/{device_id}/status", status_dict)

def update_live_telemetry(device_id: str, telemetry_dict: dict):
    # For telemetry, we can use set() to completely overwrite instead of merge
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
