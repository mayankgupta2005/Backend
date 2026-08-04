"""NovaShields backend API tests - iteration 2 (ML + Trips added)."""
import os
import io
import csv
import pytest
import requests

def get_base_url():
    if os.environ.get("REACT_APP_BACKEND_URL"):
        return os.environ.get("REACT_APP_BACKEND_URL")
    for path in ["../Nova_APP-main/frontend/.env", "/app/frontend/.env"]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    content = f.read()
                    if "REACT_APP_BACKEND_URL=" in content:
                        return content.split("REACT_APP_BACKEND_URL=")[1].split("\n")[0].strip()
            except Exception:
                pass
    return "http://localhost:8000"

BASE_URL = get_base_url()
API = f"{BASE_URL.rstrip('/')}/api"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    try:
        s.post(f"{API}/auth/register", json={"name": "Test", "email": "test@nova.com", "password": "password"})
        r = s.post(f"{API}/auth/login", json={"email": "test@nova.com", "password": "password"})
        if r.status_code == 200:
            token = r.json().get("access_token")
            s.headers.update({"Authorization": f"Bearer {token}"})
    except Exception:
        pass
    return s


# ---- Health ----
def test_health(session):
    r = session.get(f"{API}/")
    assert r.status_code == 200
    assert r.json().get("status") == "online"


# ---- Contacts CRUD (regression) ----
def test_contacts_crud(session):
    payload = {"name": "TEST_Contact", "phone": "+15551234567", "relation": "Friend", "priority": 2}
    r = session.post(f"{API}/contacts", json=payload)
    assert r.status_code == 201, r.text
    cid = r.json()["contact_id"]
    r = session.get(f"{API}/contacts")
    assert any(x["contact_id"] == cid for x in r.json())
    r = session.delete(f"{API}/contacts/{cid}")
    assert r.status_code == 200


# ---- Alerts (regression) ----
def test_alerts_create_and_list(session):
    payload = {"device_id": "device_test", "event_type": "manual_sos", "severity": "critical",
               "confidence": 0.9, "message": "TEST_alert", "telemetry_snapshot": {"speed_kmh": 40}}
    r = session.post(f"{API}/alerts", json=payload)
    assert r.status_code == 201
    aid = r.json()["alert_id"]
    r = session.get(f"{API}/alerts")
    assert r.json()[0]["alert_id"] == aid


# ---- Commands (regression) ----
def test_commands_create_and_list(session):
    r = session.post(f"{API}/commands", json={"device_id": "device_test", "command": "buzzer_on", "payload": {"duration": 3}})
    assert r.status_code == 201
    cid = r.json()["command_id"]
    r = session.get(f"{API}/commands")
    assert any(x["command_id"] == cid for x in r.json())


# ---- Simulate scenarios ----
@pytest.mark.parametrize("scenario,expected_sev", [
    ("cruise", "info"),
    ("hard_brake", "warning"),
    ("crash", "critical"),
    ("freefall", "critical"),
])
def test_simulate(session, scenario, expected_sev):
    r = session.post(f"{API}/simulate", json={"scenario": scenario})
    assert r.status_code == 200
    assert r.json()["rule"]["severity"] == expected_sev


# ---- ML: Dataset Reset (do first so we have known state) ----
def test_ml_dataset_reset(session):
    r = session.post(f"{API}/ml/dataset/reset")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert 200 <= rows <= 260  # seed is 6 classes * 40 = 240


# ---- ML: Status ----
def test_ml_status(session):
    r = session.get(f"{API}/ml/status")
    assert r.status_code == 200
    data = r.json()
    assert "trained" in data
    assert "label_distribution" in data
    assert isinstance(data["label_distribution"], dict)
    assert len(data["label_distribution"]) >= 2


# ---- ML: Train ----
def test_ml_train(session):
    r = session.post(f"{API}/ml/train")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["accuracy"] > 0.7
    assert "per_class" in data
    assert len(data["per_class"]) >= 2
    assert "classes" in data


# ---- ML: Predict normal ----
def test_ml_predict_normal(session):
    body = {"ax": 0.02, "ay": 0.01, "az": 1.0, "gx": 1, "gy": 1, "gz": 1,
            "pitch": 0, "roll": 0, "lean_angle": 3, "speed_kmh": 40}
    r = session.post(f"{API}/ml/predict", json=body)
    assert r.status_code == 200
    d = r.json()
    assert d.get("available") is True
    assert d.get("verdict") == "normal"


# ---- ML: Predict crash-like ----
def test_ml_predict_crash(session):
    body = {"ax": 2.9, "ay": 1.8, "az": 0.5, "gx": 180, "gy": 90, "gz": 45,
            "pitch": -30, "roll": 40, "lean_angle": 68, "speed_kmh": 52}
    r = session.post(f"{API}/ml/predict", json=body)
    assert r.status_code == 200
    d = r.json()
    assert d.get("available") is True
    assert d.get("verdict") in {"collision", "bike_fall"}


# ---- ML: Dataset Preview ----
def test_ml_dataset_preview(session):
    r = session.get(f"{API}/ml/dataset/preview")
    assert r.status_code == 200
    d = r.json()
    assert "total_rows" in d
    assert isinstance(d["sample"], list)
    assert len(d["sample"]) <= 20
    assert isinstance(d["labels"], dict)


# ---- ML: Dataset Append (adds 3 rows and count increases by 3) ----
def test_ml_dataset_append(session):
    before = session.get(f"{API}/ml/dataset/preview").json()["total_rows"]
    rows = {
        "rows": [
            {"ax": 0.1, "ay": 0.0, "az": 1.0, "gx": 1, "gy": 1, "gz": 1,
             "pitch": 0, "roll": 0, "lean_angle": 2, "speed_kmh": 40, "label": "normal"},
            {"ax": -1.9, "ay": 0.1, "az": 0.9, "gx": -8, "gy": 2, "gz": 0,
             "pitch": -10, "roll": 0, "lean_angle": 5, "speed_kmh": 45, "label": "hard_brake"},
            {"ax": 2.7, "ay": 1.5, "az": 0.5, "gx": 150, "gy": 80, "gz": 40,
             "pitch": -30, "roll": 40, "lean_angle": 60, "speed_kmh": 55, "label": "collision"},
        ]
    }
    r = session.post(f"{API}/ml/dataset/append", json=rows)
    assert r.status_code == 200
    d = r.json()
    assert d["added"] == 3
    assert d["total"] == before + 3


# ---- Analyze includes ml field after training ----
def test_analyze_includes_ml(session):
    body = {"telemetry": {"ax": 0.02, "ay": 0.01, "az": 1.0, "lean_angle": 3, "speed_kmh": 40}}
    r = session.post(f"{API}/analyze", json=body)
    assert r.status_code == 200
    d = r.json()
    assert "ml" in d
    assert d["ml"].get("available") is True
    assert "verdict" in d["ml"]


# ---- Analyze normal (regression) ----
def test_analyze_normal(session):
    body = {"telemetry": {"ax": 0.02, "ay": 0.01, "az": 1.0, "lean_angle": 3, "speed_kmh": 40}}
    r = session.post(f"{API}/analyze", json=body)
    assert r.status_code == 200
    assert r.json()["rule"]["event"] == "normal_ride"


# ---- Analyze crash (regression) ----
def test_analyze_crash(session):
    body = {"telemetry": {"ax": 2.9, "ay": 1.8, "az": 0.5, "lean_angle": 68, "gx": 180}}
    r = session.post(f"{API}/analyze", json=body)
    assert r.status_code == 200
    d = r.json()
    assert d["rule"]["severity"] == "critical"
    assert d["final_verdict"] in {"collision", "bike_fall"}


# ---- Trips lifecycle ----
@pytest.fixture(scope="module")
def trip_id(session):
    r = session.post(f"{API}/trips/start", json={"device_id": "device_test", "trigger": "manual"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "active"
    return data["trip_id"]


def test_trip_start_creates_active(session, trip_id):
    r = session.get(f"{API}/trips/{trip_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_trip_add_points_haversine(session, trip_id):
    # ~100m spacing points: 0.001 deg lat ~ 111m
    base_lat, base_lon = 12.9716, 77.5946
    pts = [
        {"trip_id": trip_id, "latitude": base_lat, "longitude": base_lon, "speed_kmh": 20, "lean_angle": 3, "g_force": 1.0},
        {"trip_id": trip_id, "latitude": base_lat + 0.0009, "longitude": base_lon, "speed_kmh": 25, "lean_angle": 5, "g_force": 1.05},
        {"trip_id": trip_id, "latitude": base_lat + 0.0018, "longitude": base_lon, "speed_kmh": 30, "lean_angle": 6, "g_force": 1.1},
    ]
    for p in pts:
        r = session.post(f"{API}/trips/point", json=p)
        assert r.status_code == 200
    # After 3 points spaced ~100m apart, total distance ~0.2 km (2 segments)
    r = session.get(f"{API}/trips/{trip_id}")
    trip = r.json()
    assert 0.15 < trip["distance_km"] < 0.30, f"distance was {trip['distance_km']}"
    assert trip["top_speed"] >= 30
    assert len(trip["points"]) == 3


def test_trip_add_event(session, trip_id):
    ev = {"trip_id": trip_id, "event_type": "hard_brake", "severity": "warning", "message": "TEST_event"}
    r = session.post(f"{API}/trips/event", json=ev)
    assert r.status_code == 200
    r = session.get(f"{API}/trips/{trip_id}")
    assert any(e["message"] == "TEST_event" for e in r.json()["events"])


def test_trips_list_excludes_points(session, trip_id):
    r = session.get(f"{API}/trips")
    assert r.status_code == 200
    trips = r.json()
    match = [t for t in trips if t["trip_id"] == trip_id]
    assert match, "trip not in list"
    t = match[0]
    assert "point_count" in t and t["point_count"] == 3
    assert "event_count" in t and t["event_count"] >= 1
    assert "points" not in t  # not full points array


def test_trip_end(session, trip_id):
    r = session.post(f"{API}/trips/{trip_id}/end")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "ended"
    assert d["ended_at"] is not None


def test_trip_delete(session, trip_id):
    r = session.delete(f"{API}/trips/{trip_id}")
    assert r.status_code == 200
    r = session.get(f"{API}/trips/{trip_id}")
    assert r.status_code == 404


# ---- Dataset CSV upload happy path ----
def test_ml_dataset_upload_csv(session):
    # Build small CSV with proper columns
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ax", "ay", "az", "gx", "gy", "gz", "pitch", "roll", "lean_angle", "speed_kmh", "label"])
    # Provide at least 2 classes so subsequent training would work; use dataset just for upload contract
    for i in range(5):
        w.writerow([0.0, 0.0, 1.0, 0, 0, 0, 0, 0, 3, 40, "normal"])
        w.writerow([2.8, 1.5, 0.5, 180, 80, 40, -30, 40, 65, 55, "collision"])
    content = buf.getvalue().encode()
    files = {"file": ("test.csv", content, "text/csv")}
    # Requests: must NOT send content-type json header
    r = requests.post(f"{API}/ml/dataset/upload", files=files)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["rows"] == 10
    assert "normal" in d["labels"] and "collision" in d["labels"]
    # Restore seed so other tests remain stable
    session.post(f"{API}/ml/dataset/reset")
    session.post(f"{API}/ml/train")


# =====================================================================
# ITERATION 3 - Medical + Notifications + PDF + Legacy Compat
# =====================================================================

# ---- Medical profile ----
def test_medical_default_empty(session):
    # Cleanup first
    r = session.get(f"{API}/medical")
    assert r.status_code == 200
    d = r.json()
    assert "full_name" in d
    assert d.get("user_id") == "default"


def test_medical_save_and_get(session):
    payload = {
        "user_id": "default",
        "full_name": "TEST Rider One",
        "dob": "1995-05-10",
        "blood_group": "O+",
        "allergies": "Penicillin",
        "emergency_contact_name": "Test EC",
        "emergency_contact_phone": "+15559990000",
    }
    r = session.post(f"{API}/medical", json=payload)
    assert r.status_code == 200
    r = session.get(f"{API}/medical")
    d = r.json()
    assert d["full_name"] == "TEST Rider One"
    assert d["blood_group"] == "O+"
    assert d["allergies"] == "Penicillin"
    assert d["emergency_contact_name"] == "Test EC"
    assert d["emergency_contact_phone"] == "+15559990000"


def test_public_medical_returns_saved(session):
    r = session.get(f"{API}/public/medical/default")
    assert r.status_code == 200
    d = r.json()
    assert d["full_name"] == "TEST Rider One"
    assert d["blood_group"] == "O+"


# ---- FCM notification tokens ----
def test_notifications_register_list_delete(session):
    fake_token = "TEST_fcm_token_abc123"
    # Register
    r = session.post(f"{API}/notifications/register", json={
        "user_id": "default", "token": fake_token, "device_label": "TEST_device"
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # List
    r = session.get(f"{API}/notifications/tokens")
    assert r.status_code == 200
    toks = r.json()
    assert any(t["token"] == fake_token for t in toks)

    # Delete
    r = session.delete(f"{API}/notifications/token", params={"token": fake_token})
    assert r.status_code == 200

    r = session.get(f"{API}/notifications/tokens")
    assert not any(t["token"] == fake_token for t in r.json())


def test_notifications_broadcast_no_service_account(session):
    r = session.post(f"{API}/notifications/broadcast", json={
        "user_id": "default", "title": "TEST", "body": "hi", "url": "/alerts"
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["backend_delivery"] == "requires_service_account"
    assert "target_count" in d


# ---- Insurance PDF report ----
def test_alert_pdf_report(session):
    # Create fresh alert
    payload = {"device_id": "device_test", "event_type": "collision",
               "severity": "critical", "confidence": 0.9,
               "latitude": 12.9716, "longitude": 77.5946, "speed": 45,
               "message": "TEST_pdf_alert",
               "telemetry_snapshot": {"ax": 2.9, "ay": 1.5, "az": 0.5, "speed_kmh": 45}}
    r = session.post(f"{API}/alerts", json=payload)
    assert r.status_code == 201
    aid = r.json()["alert_id"]

    r = session.get(f"{API}/alerts/{aid}/report.pdf")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/pdf")
    assert len(r.content) > 2048
    assert r.content[:4] == b"%PDF"


# ---- Legacy compat endpoints for website ----
def test_legacy_add_sample(session):
    before = session.get(f"{BASE_URL}/api/dataset-stats").json()["total_samples"]
    body = {
        "label": "normal",
        "accX": 0.02, "accY": 0.01, "accZ": 1.0,
        "gyroX": 1, "gyroY": 0, "gyroZ": 1,
        "pitch": 0, "roll": 0, "heading": 90,
        "timestamp": 1700000000
    }
    r = requests.post(f"{BASE_URL}/api/add-sample", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    after = session.get(f"{BASE_URL}/api/dataset-stats").json()["total_samples"]
    assert after == before + 1


def test_legacy_dataset_stats(session):
    r = requests.get(f"{BASE_URL}/api/dataset-stats")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "success"
    assert "total_samples" in d
    assert "label_distribution" in d
    assert isinstance(d["label_distribution"], dict)


def test_legacy_train_model(session):
    r = requests.post(f"{BASE_URL}/api/train-model")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    assert "metrics" in d
    assert "accuracy" in d["metrics"]
    assert "f1" in d["metrics"]


def test_legacy_cancel_sos(session):
    r = requests.post(f"{BASE_URL}/api/cancel-sos", json={"device_id": "device_001"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
