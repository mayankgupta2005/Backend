"""NovaShields Backend — Device, Command Ack, Camera, and Firebase integration tests.

These tests exercise the NEW extension endpoints added to the backend.
They require a running backend (default: http://localhost:8000).

Tests that require physical hardware are marked and documented as
NOT TESTABLE without the physical ESP devices.
"""
import os
import uuid
import pytest
import requests


def get_base_url():
    if os.environ.get("REACT_APP_BACKEND_URL"):
        return os.environ.get("REACT_APP_BACKEND_URL")
    return "http://localhost:8000"


BASE_URL = get_base_url()
API = f"{BASE_URL.rstrip('/')}/api"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    try:
        s.post(f"{API}/auth/register", json={"name": "DeviceTest", "email": "devicetest@nova.com", "password": "password"})
        r = s.post(f"{API}/auth/login", json={"email": "devicetest@nova.com", "password": "password"})
        if r.status_code == 200:
            token = r.json().get("access_token")
            s.headers.update({"Authorization": f"Bearer {token}"})
    except Exception:
        pass
    return s


# =====================================================================
# DEVICE REGISTRATION
# =====================================================================

TEST_DEVICE_ID = f"test_blackbox_{uuid.uuid4().hex[:8]}"
TEST_ALERT_MODULE_ID = f"test_alert_{uuid.uuid4().hex[:8]}"
TEST_CAMERA_ID = f"test_cam_{uuid.uuid4().hex[:8]}"
TEST_VEHICLE_ID = f"vehicle_{uuid.uuid4().hex[:8]}"


def test_register_blackbox(session):
    payload = {
        "device_id": TEST_DEVICE_ID,
        "device_type": "BLACKBOX",
        "name": "Test Black Box",
        "vehicle_id": TEST_VEHICLE_ID,
    }
    r = session.post(f"{API}/devices", json=payload)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["device_id"] == TEST_DEVICE_ID
    assert d["device_type"] == "BLACKBOX"
    assert d["enabled"] is True


def test_register_alert_module(session):
    payload = {
        "device_id": TEST_ALERT_MODULE_ID,
        "device_type": "ALERT_MODULE",
        "name": "Test Alert Module",
        "vehicle_id": TEST_VEHICLE_ID,
    }
    r = session.post(f"{API}/devices", json=payload)
    assert r.status_code == 201, r.text


def test_register_camera(session):
    payload = {
        "device_id": TEST_CAMERA_ID,
        "device_type": "CAMERA",
        "name": "Test Camera",
        "vehicle_id": TEST_VEHICLE_ID,
    }
    r = session.post(f"{API}/devices", json=payload)
    assert r.status_code == 201, r.text


def test_register_duplicate_device_fails(session):
    payload = {
        "device_id": TEST_DEVICE_ID,
        "device_type": "BLACKBOX",
        "name": "Duplicate",
    }
    r = session.post(f"{API}/devices", json=payload)
    assert r.status_code == 400


def test_list_devices(session):
    r = session.get(f"{API}/devices")
    assert r.status_code == 200
    devices = r.json()
    ids = [d["device_id"] for d in devices]
    assert TEST_DEVICE_ID in ids


def test_list_devices_filter_by_type(session):
    r = session.get(f"{API}/devices", params={"device_type": "CAMERA"})
    assert r.status_code == 200
    for d in r.json():
        assert d["device_type"] == "CAMERA"


def test_get_device(session):
    r = session.get(f"{API}/devices/{TEST_DEVICE_ID}")
    assert r.status_code == 200
    d = r.json()
    assert d["device_id"] == TEST_DEVICE_ID
    assert d["vehicle_id"] == TEST_VEHICLE_ID
    assert d["is_online"] is False  # No telemetry sent yet


def test_get_device_not_found(session):
    r = session.get(f"{API}/devices/nonexistent_device_xyz")
    assert r.status_code == 404


def test_update_device(session):
    r = session.put(f"{API}/devices/{TEST_DEVICE_ID}", json={"name": "Updated BB"})
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "Updated BB"


def test_get_device_status(session):
    r = session.get(f"{API}/devices/{TEST_DEVICE_ID}/status")
    assert r.status_code == 200
    d = r.json()
    assert "is_online" in d
    assert "device_id" in d


# =====================================================================
# COMMAND LIFECYCLE & ACKNOWLEDGEMENT
# =====================================================================

def test_command_creates_pending(session):
    """POST /api/commands should create a command with PENDING status."""
    r = session.post(f"{API}/commands", json={
        "device_id": TEST_DEVICE_ID,
        "command": "buzzer_on",
        "payload": {"duration": 5},
    })
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["status"] == "PENDING"
    assert "command_id" in d
    return d["command_id"]


def test_command_status_endpoint(session):
    """Create a command and check its status via GET endpoint."""
    r = session.post(f"{API}/commands", json={
        "device_id": TEST_DEVICE_ID,
        "command": "led_red",
        "payload": {},
    })
    assert r.status_code == 201
    cid = r.json()["command_id"]

    r = session.get(f"{API}/commands/{cid}/status")
    assert r.status_code == 200
    d = r.json()
    assert d["command_id"] == cid
    assert d["status"] == "PENDING"


def test_command_ack_received(session):
    """Simulate device acknowledging RECEIVED."""
    r = session.post(f"{API}/commands", json={
        "device_id": TEST_DEVICE_ID,
        "command": "led_green",
    })
    cid = r.json()["command_id"]

    r = session.post(f"{API}/commands/{cid}/ack", json={"status": "RECEIVED"})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "RECEIVED"

    # Verify in status endpoint
    r = session.get(f"{API}/commands/{cid}/status")
    assert r.json()["status"] == "RECEIVED"
    assert r.json().get("received_at") is not None


def test_command_ack_executed(session):
    """Simulate device acknowledging EXECUTED."""
    r = session.post(f"{API}/commands", json={
        "device_id": TEST_DEVICE_ID,
        "command": "buzzer_off",
    })
    cid = r.json()["command_id"]

    r = session.post(f"{API}/commands/{cid}/ack", json={"status": "EXECUTED"})
    assert r.status_code == 200

    r = session.get(f"{API}/commands/{cid}/status")
    assert r.json()["status"] == "EXECUTED"
    assert r.json().get("executed_at") is not None


def test_command_ack_failed(session):
    """Simulate device reporting FAILED."""
    r = session.post(f"{API}/commands", json={
        "device_id": TEST_DEVICE_ID,
        "command": "activate_gsm",
    })
    cid = r.json()["command_id"]

    r = session.post(f"{API}/commands/{cid}/ack", json={
        "status": "FAILED", "error": "GSM module not responding"
    })
    assert r.status_code == 200

    r = session.get(f"{API}/commands/{cid}/status")
    d = r.json()
    assert d["status"] == "FAILED"
    assert d.get("error") == "GSM module not responding"


def test_command_ack_not_found(session):
    r = session.post(f"{API}/commands/nonexistent_cmd/ack", json={"status": "RECEIVED"})
    assert r.status_code == 404


# =====================================================================
# CAMERA METADATA
# =====================================================================

def test_camera_metadata_post(session):
    r = session.post(f"{API}/camera/{TEST_CAMERA_ID}/metadata", json={
        "online": True,
        "stream_url": "http://192.168.1.100:81/stream",
        "snapshot_url": "http://192.168.1.100/capture",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["online"] is True
    assert d["stream_url"] == "http://192.168.1.100:81/stream"
    assert d["snapshot_url"] == "http://192.168.1.100/capture"
    assert "updated_at" in d


def test_camera_metadata_get(session):
    r = session.get(f"{API}/camera/{TEST_CAMERA_ID}/metadata")
    assert r.status_code == 200
    d = r.json()
    assert d["device_id"] == TEST_CAMERA_ID
    assert d["stream_url"] == "http://192.168.1.100:81/stream"


def test_camera_status(session):
    r = session.get(f"{API}/camera/{TEST_CAMERA_ID}/status")
    assert r.status_code == 200
    d = r.json()
    assert "online" in d
    assert "device_id" in d


def test_camera_metadata_not_found(session):
    r = session.get(f"{API}/camera/nonexistent_cam/metadata")
    assert r.status_code == 404


# =====================================================================
# DEVICE TELEMETRY READ (Firebase-based)
# =====================================================================

def test_device_telemetry_read(session):
    """Reading telemetry from Firebase. May return 404 if no data in Firebase."""
    r = session.get(f"{API}/devices/{TEST_DEVICE_ID}/telemetry")
    # 404 is expected if no telemetry has been pushed to Firebase for this test device
    assert r.status_code in (200, 404)


def test_device_accident_status(session):
    r = session.get(f"{API}/devices/{TEST_DEVICE_ID}/accident")
    assert r.status_code == 200
    d = r.json()
    assert "status" in d


# =====================================================================
# BACKWARD COMPATIBILITY — Existing Endpoints
# =====================================================================

def test_health_still_works(session):
    r = session.get(f"{API}/")
    assert r.status_code == 200
    assert r.json()["status"] == "online"


def test_alerts_still_works(session):
    r = session.get(f"{API}/alerts")
    assert r.status_code == 200


def test_commands_list_still_works(session):
    r = session.get(f"{API}/commands")
    assert r.status_code == 200


def test_cancel_sos_still_works(session):
    r = requests.post(f"{BASE_URL}/api/cancel-sos", json={"device_id": "device_001"})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "success"


def test_contacts_still_works(session):
    r = session.get(f"{API}/contacts")
    assert r.status_code == 200


def test_medical_still_works(session):
    r = session.get(f"{API}/medical")
    assert r.status_code == 200


# =====================================================================
# CLEANUP — Remove test devices
# =====================================================================

def test_cleanup_devices(session):
    for did in [TEST_DEVICE_ID, TEST_ALERT_MODULE_ID, TEST_CAMERA_ID]:
        r = session.delete(f"{API}/devices/{did}")
        assert r.status_code == 200, f"Failed to delete {did}: {r.text}"


# =====================================================================
# HARDWARE-DEPENDENT TESTS
# NOT TESTABLE without physical ESP8266 / ESP32-CAM devices
# =====================================================================

@pytest.mark.skip(reason="Requires physical ESP8266 Black Box sending telemetry via WebSocket")
def test_ws_telemetry_with_real_device():
    """ESP8266 Black Box sends telemetry via /ws/telemetry/{device_id}.
    Backend should update Firebase live_telemetry and broadcast to dashboard."""
    pass


@pytest.mark.skip(reason="Requires physical ESP8266 Alert Module reading Firebase commands")
def test_alert_module_reads_firebase_command():
    """Alert Module reads commands/current from Firebase and reports ack."""
    pass


@pytest.mark.skip(reason="Requires physical ESP32-CAM providing MJPEG stream")
def test_esp32cam_live_stream():
    """ESP32-CAM provides live MJPEG stream; backend stores stream_url metadata."""
    pass


@pytest.mark.skip(reason="Requires physical ESP8266 to trigger real accident event")
def test_real_accident_to_alert_module_flow():
    """Full pipeline: Black Box detects accident → Backend → Firebase → Alert Module."""
    pass


@pytest.mark.skip(reason="Requires GSM module on Alert Module")
def test_gsm_activation():
    """Backend sends activate_gsm command → Alert Module triggers GSM call."""
    pass
