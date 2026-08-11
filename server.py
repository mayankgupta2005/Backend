"""
NovaShields Smart Black Box - Mobile SOS App Backend
Provides:
- Emergency contact management (MongoDB)
- Alert history & command logging
- AI crash analysis using Claude Sonnet 4.6 via EMERGENT_LLM_KEY
- Rule-based crash detection endpoint
- Simulator endpoints for testing without hardware
- Multi-device IoT support (Black Box, Alert Module, Camera)
- Firebase RTDB synchronisation for device commands & state
- Camera metadata management
"""
import os
import uuid
import json
import asyncio
import collections
import logging
from enum import Enum
from datetime import datetime, timezone, timedelta
from typing import Annotated, Any, Optional

from bson import ObjectId
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
from fastapi import FastAPI, HTTPException, APIRouter, status, UploadFile, File, Depends, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
import bcrypt
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from ml import (
    append_dataset,
    load_dataset,
    model_status,
    predict_frame,
    replace_dataset_from_csv,
    reset_dataset,
    train_model,
)

load_dotenv()
import firebase_service

# ---------------------------------------------------------------------------
# Configuration / Database Mock Client
# ---------------------------------------------------------------------------
MONGO_URL = os.environ.get("MONGO_URL")
if not MONGO_URL or MONGO_URL == "mock":
    raise ValueError("Real-time DB required! Set MONGO_URL environment variable.")
    
DB_NAME = os.environ.get("DB_NAME", "novashields")
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")
EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    raise ValueError("JWT_SECRET environment variable is required!")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 1 week
DEVICE_ONLINE_TIMEOUT_SECONDS = int(os.environ.get("DEVICE_ONLINE_TIMEOUT_SECONDS", "60"))

CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

logger = logging.getLogger(__name__)

if CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True
    )



security = HTTPBearer()

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

# ---------------------------------------------------------------------------
# Mongo helpers (ObjectId-safe)
# ---------------------------------------------------------------------------
def _oid_to_str(v: Any) -> str:
    if isinstance(v, ObjectId):
        return str(v)
    return str(v)

PyObjectId = Annotated[str, BeforeValidator(_oid_to_str)]


class BaseDoc(BaseModel):
    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: Optional[PyObjectId] = Field(default=None, alias="_id")

    @classmethod
    def from_mongo(cls, doc: dict):
        if not doc:
            return None
        doc["_id"] = str(doc.get("_id"))
        return cls(**doc)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class UserRole(str, Enum):
    RIDER = "RIDER"
    FAMILY = "FAMILY"
    POLICE = "POLICE"
    ADMIN = "ADMIN"

class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    REJECTED = "REJECTED"

class User(BaseDoc):
    user_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: str
    hashed_password: str
    name: str
    role: UserRole = UserRole.RIDER
    status: UserStatus = UserStatus.ACTIVE
    created_at: str = Field(default_factory=now_iso)

class UserRegister(BaseModel):
    email: str
    password: str
    name: str
    role: UserRole = UserRole.RIDER

class UserLogin(BaseModel):
    email: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    user_id: str
    name: str
    role: str
    status: str

# ---- Security Utilities ----
def verify_password(plain_password: str, hashed_password: str):
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

def get_password_hash(password: str):
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        role: str = payload.get("role")
        status: str = payload.get("status")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return {"user_id": user_id, "role": role, "status": status}
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def require_role(*allowed_roles: str):
    async def role_checker(current_user: dict = Depends(get_current_user)):
        if current_user["status"] != UserStatus.ACTIVE.value:
            raise HTTPException(status_code=403, detail="Account is not active (pending or rejected).")
        if current_user["role"] not in allowed_roles:
            raise HTTPException(status_code=403, detail=f"Operation not permitted for role {current_user['role']}")
        return current_user
    return role_checker

class CameraCapture(BaseDoc):
    capture_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    device_id: str = "device_001"
    image_url: str
    created_at: str = Field(default_factory=now_iso)


# ---------------------------------------------------------------------------
# Device Types & Registration  (NEW — multi-device IoT support)
# ---------------------------------------------------------------------------
class DeviceType(str, Enum):
    BLACKBOX = "BLACKBOX"
    ALERT_MODULE = "ALERT_MODULE"
    CAMERA = "CAMERA"


class Device(BaseDoc):
    device_id: str
    device_type: DeviceType
    name: str = ""
    vehicle_id: Optional[str] = None
    user_id: Optional[str] = None
    enabled: bool = True
    is_online: bool = False
    last_seen: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class DeviceRegister(BaseModel):
    device_id: str
    device_type: DeviceType
    name: str = ""
    vehicle_id: Optional[str] = None
    user_id: Optional[str] = None


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    vehicle_id: Optional[str] = None
    user_id: Optional[str] = None
    enabled: Optional[bool] = None


# ---------------------------------------------------------------------------
# Camera Metadata  (NEW — ESP32-CAM stream/snapshot URL management)
# ---------------------------------------------------------------------------
class CameraMetadataModel(BaseModel):
    device_id: str
    online: bool = False
    stream_url: Optional[str] = None
    snapshot_url: Optional[str] = None
    last_frame_at: Optional[str] = None
    updated_at: str = Field(default_factory=now_iso)


class CameraMetadataIn(BaseModel):
    online: Optional[bool] = None
    stream_url: Optional[str] = None
    snapshot_url: Optional[str] = None
    last_frame_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Command Acknowledgement  (NEW — device reports execution status)
# ---------------------------------------------------------------------------
class CommandAck(BaseModel):
    status: str  # RECEIVED | EXECUTED | FAILED
    error: Optional[str] = None


class EmergencyContact(BaseDoc):

    contact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = "default"
    name: str
    phone: str
    relation: Optional[str] = "Contact"
    priority: int = 1
    created_at: str = Field(default_factory=now_iso)


class EmergencyContactIn(BaseModel):
    name: str
    phone: str
    relation: Optional[str] = "Contact"
    priority: int = 1


class AlertRecord(BaseDoc):
    alert_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    device_id: str
    event_type: str  # "crash_confirmed", "warning", "manual_sos", "false_alarm"
    severity: str  # "info" | "warning" | "danger" | "critical"
    confidence: float = 0.0
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed: Optional[float] = None
    message: str
    telemetry_snapshot: dict = {}
    ai_analysis: Optional[str] = None
    created_at: str = Field(default_factory=now_iso)


class AlertIn(BaseModel):
    device_id: str
    event_type: str
    severity: str = "warning"
    confidence: float = 0.0
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed: Optional[float] = None
    message: str
    telemetry_snapshot: dict = {}
    ai_analysis: Optional[str] = None


class CommandLog(BaseDoc):
    command_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    device_id: str
    command: str  # buzzer_on, send_sms, make_call, upload_snapshot, reboot, led_on
    payload: dict = {}
    status: str = "PENDING"  # PENDING | RECEIVED | EXECUTED | FAILED  (was: sent | ack | timeout)
    created_at: str = Field(default_factory=now_iso)
    received_at: Optional[str] = None
    executed_at: Optional[str] = None
    error: Optional[str] = None


class CommandIn(BaseModel):
    device_id: str
    command: str
    payload: dict = {}


class TelemetryFrame(BaseModel):
    device_id: str = "device_001"
    ax: float = 0.0
    ay: float = 0.0
    az: float = 1.0
    gx: float = 0.0
    gy: float = 0.0
    gz: float = 0.0
    speed_kmh: float = 0.0
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    battery: float = 100.0
    signal: int = 4
    pitch: float = 0.0
    roll: float = 0.0
    lean_angle: float = 0.0
    timestamp: Optional[str] = None


class RuleResult(BaseModel):
    event: str
    severity: str
    confidence: float
    triggers: list
    g_force: float
    lean_angle: float
    jerk: float


# ---------------------------------------------------------------------------
# Rule Engine
# ---------------------------------------------------------------------------
G_FORCE_CRASH = 2.5
G_FORCE_WARN = 1.6
LEAN_LIMIT = 55.0
LEAN_WARN = 40.0
FREEFALL = 0.25
JERK_CRASH = 25.0


def evaluate_rules(t: TelemetryFrame, prev: Optional[TelemetryFrame] = None) -> RuleResult:
    g_force = (t.ax * t.ax + t.ay * t.ay + t.az * t.az) ** 0.5
    triggers: list[str] = []
    severity = "info"
    event = "normal_ride"
    confidence = 0.05

    jerk = 0.0
    if prev is not None:
        jerk = ((t.ax - prev.ax) ** 2 + (t.ay - prev.ay) ** 2 + (t.az - prev.az) ** 2) ** 0.5

    if g_force >= G_FORCE_CRASH:
        triggers.append(f"HIGH_G:{g_force:.2f}g")
        severity = "critical"
        event = "collision"
        confidence = max(confidence, min(1.0, g_force / 4.0))
    elif g_force >= G_FORCE_WARN:
        triggers.append(f"WARN_G:{g_force:.2f}g")
        severity = "warning"
        event = "hard_brake"
        confidence = max(confidence, 0.45)

    if g_force <= FREEFALL:
        triggers.append("FREEFALL")
        severity = "critical"
        event = "bike_fall"
        confidence = max(confidence, 0.85)

    if abs(t.lean_angle) >= LEAN_LIMIT:
        triggers.append(f"LEAN:{t.lean_angle:.1f}deg")
        severity = "critical" if severity != "critical" else severity
        event = "bike_fall" if event == "normal_ride" else event
        confidence = max(confidence, 0.75)
    elif abs(t.lean_angle) >= LEAN_WARN:
        triggers.append(f"HARD_LEAN:{t.lean_angle:.1f}deg")
        if severity == "info":
            severity = "warning"

    if jerk >= JERK_CRASH:
        triggers.append(f"JERK:{jerk:.1f}")
        severity = "critical"
        event = "collision"
        confidence = max(confidence, 0.9)

    return RuleResult(
        event=event,
        severity=severity,
        confidence=round(confidence, 3),
        triggers=triggers,
        g_force=round(g_force, 3),
        lean_angle=round(t.lean_angle, 2),
        jerk=round(jerk, 2),
    )


# ---------------------------------------------------------------------------
# AI Engine - Claude Sonnet 4.6 via Emergent LLM Key
# ---------------------------------------------------------------------------
async def ai_analyze(t: TelemetryFrame, rule: RuleResult) -> dict:
    """Deep crash analysis using Claude Sonnet 4.6."""
    if not EMERGENT_LLM_KEY:
        return {
            "available": False,
            "reason": "EMERGENT_LLM_KEY not configured",
            "verdict": rule.event,
            "confidence": rule.confidence,
            "explanation": "AI layer disabled; rule engine verdict used.",
        }

    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
    except Exception as e:
        return {
            "available": False,
            "reason": f"integration lib missing: {e}",
            "verdict": rule.event,
            "confidence": rule.confidence,
            "explanation": "AI layer unavailable; rule engine verdict used.",
        }

    system_prompt = (
        "You are the AI crash-analysis brain for NovaShields, a smart black box for "
        "two-wheelers. You receive one telemetry frame + rule-engine output and must "
        "classify the event. Reply ONLY with compact JSON matching:\n"
        '{"verdict":"normal_ride|hard_brake|pothole|hard_lean|bike_fall|collision|false_alarm",'
        '"confidence":0.0-1.0,"severity":"info|warning|danger|critical",'
        '"explanation":"one short sentence","suggested_action":"none|monitor|alert|sos"}'
    )
    payload = {
        "telemetry": t.model_dump(),
        "rule_engine": rule.model_dump(),
    }

    chat = LlmChat(
        api_key=EMERGENT_LLM_KEY,
        session_id=f"crash-{uuid.uuid4()}",
        system_message=system_prompt,
    ).with_model("anthropic", "claude-sonnet-4-6")

    try:
        response = await chat.send_message(UserMessage(text=json.dumps(payload)))
        text = str(response).strip()
        # Try to isolate JSON
        start = text.find("{")
        end = text.rfind("}")
        parsed = json.loads(text[start : end + 1]) if start != -1 and end != -1 else {}
        parsed["available"] = True
        return parsed
    except Exception as e:
        return {
            "available": False,
            "reason": f"llm error: {e}",
            "verdict": rule.event,
            "confidence": rule.confidence,
            "explanation": "AI call failed; rule verdict used.",
        }


# ---------------------------------------------------------------------------
# FastAPI app & WebSocket Manager
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await client.admin.command('ping')
    except Exception:
        raise RuntimeError("Could not connect to database")
    
    # Start Firebase RTDB background listener
    loop = asyncio.get_running_loop()
    firebase_service.start_firebase_listener(loop, iot_manager.broadcast_firebase_update)
    
    yield
    
    firebase_service.stop_firebase_listener()

app = FastAPI(title="NovaShields Mobile SOS Backend", version="1.0.0", lifespan=lifespan)
api = APIRouter(prefix="/api")

class IoTConnectionManager:
    def __init__(self):
        self.active_devices: dict[str, WebSocket] = {}
        self.camera_viewers: dict[str, list[WebSocket]] = {}
        self.camera_sources: dict[str, WebSocket] = {}
        self.dashboard_viewers: dict[str, list[WebSocket]] = {}  # NEW: dashboard viewers per device
        self.firebase_viewers: list[WebSocket] = []  # NEW: Firebase real-time viewers
        self.telemetry_buffer: dict[str, collections.deque] = {}
        self.video_buffer: dict[str, collections.deque] = {}

    def init_buffers(self, device_id: str):
        if device_id not in self.telemetry_buffer:
            self.telemetry_buffer[device_id] = collections.deque(maxlen=300) # 30s @ 10Hz
        if device_id not in self.video_buffer:
            self.video_buffer[device_id] = collections.deque(maxlen=300)

    def add_telemetry(self, device_id: str, data: str):
        self.init_buffers(device_id)
        self.telemetry_buffer[device_id].append({"ts": now_iso(), "data": data})

    def add_video_frame(self, device_id: str, frame: bytes):
        self.init_buffers(device_id)
        self.video_buffer[device_id].append({"ts": now_iso(), "frame": frame})

    async def save_snapshot(self, device_id: str):
        self.init_buffers(device_id)
        telemetry = list(self.telemetry_buffer[device_id])
        snapshot = {
            "device_id": device_id,
            "timestamp": now_iso(),
            "telemetry_history": telemetry,
            "video_frames_count": len(self.video_buffer[device_id])
        }
        await db.blackbox_snapshots.insert_one(snapshot)

    async def connect_device(self, websocket: WebSocket, device_id: str):
        await websocket.accept()
        self.active_devices[device_id] = websocket
        firebase_service.update_device_status(device_id, {"is_online": True, "last_seen": now_iso()})

    def disconnect_device(self, device_id: str):
        if device_id in self.active_devices:
            del self.active_devices[device_id]
            firebase_service.update_device_status(device_id, {"is_online": False, "last_seen": now_iso()})

    async def send_command(self, device_id: str, command: dict):
        if device_id in self.active_devices:
            await self.active_devices[device_id].send_json(command)
        firebase_service.push_emergency_command(device_id, {"command": command, "command_timestamp": now_iso()})

    async def connect_camera_source(self, websocket: WebSocket, device_id: str):
        await websocket.accept()
        self.camera_sources[device_id] = websocket

    def disconnect_camera_source(self, device_id: str):
        if device_id in self.camera_sources:
            del self.camera_sources[device_id]

    async def connect_viewer(self, websocket: WebSocket, device_id: str):
        await websocket.accept()
        if device_id not in self.camera_viewers:
            self.camera_viewers[device_id] = []
        self.camera_viewers[device_id].append(websocket)

    def disconnect_viewer(self, websocket: WebSocket, device_id: str):
        if device_id in self.camera_viewers:
            if websocket in self.camera_viewers[device_id]:
                self.camera_viewers[device_id].remove(websocket)

    async def broadcast_frame(self, device_id: str, frame_bytes: bytes):
        if device_id in self.camera_viewers:
            dead_sockets = []
            for viewer in self.camera_viewers[device_id]:
                try:
                    await viewer.send_bytes(frame_bytes)
                except Exception:
                    dead_sockets.append(viewer)
            for dead in dead_sockets:
                self.disconnect_viewer(dead, device_id)

    # ---- NEW: Dashboard viewer management ----
    async def connect_dashboard_viewer(self, websocket: WebSocket, device_id: str):
        await websocket.accept()
        if device_id not in self.dashboard_viewers:
            self.dashboard_viewers[device_id] = []
        self.dashboard_viewers[device_id].append(websocket)

    def disconnect_dashboard_viewer(self, websocket: WebSocket, device_id: str):
        if device_id in self.dashboard_viewers:
            if websocket in self.dashboard_viewers[device_id]:
                self.dashboard_viewers[device_id].remove(websocket)

    async def broadcast_to_dashboard(self, device_id: str, event_type: str, data: dict):
        """Broadcast a typed JSON event to all dashboard viewers for a device."""
        message = {
            "type": event_type,
            "device_id": device_id,
            "data": data,
            "timestamp": now_iso(),
        }
        if device_id not in self.dashboard_viewers:
            return
        dead_sockets = []
        for viewer in self.dashboard_viewers[device_id]:
            try:
                await viewer.send_json(message)
            except Exception:
                dead_sockets.append(viewer)
        for dead in dead_sockets:
            self.disconnect_dashboard_viewer(dead, device_id)

    async def broadcast_to_all_dashboards(self, event_type: str, data: dict):
        """Broadcast an event to ALL dashboard viewers across all devices."""
        all_device_ids = list(self.dashboard_viewers.keys())
        for did in all_device_ids:
            await self.broadcast_to_dashboard(did, event_type, data)

    # ---- NEW: Firebase Realtime Stream Viewers ----
    async def connect_firebase_viewer(self, websocket: WebSocket):
        await websocket.accept()
        self.firebase_viewers.append(websocket)
        client_ip = websocket.client.host if websocket.client else "unknown"
        logger.info(f"🌐 [WEBSOCKET CONNECTED] Frontend client connected to /ws/firebase/live from {client_ip}. Active viewers: {len(self.firebase_viewers)}")

    def disconnect_firebase_viewer(self, websocket: WebSocket):
        if websocket in self.firebase_viewers:
            self.firebase_viewers.remove(websocket)
            logger.info(f"🔌 [WEBSOCKET DISCONNECTED] Frontend client disconnected. Active viewers remaining: {len(self.firebase_viewers)}")

    async def broadcast_firebase_update(self, path: str, data: Any, full_cache: dict):
        """Broadcast real-time Firebase RTDB change to all connected clients."""
        message = {
            "type": "firebase_update",
            "path": path,
            "data": data,
            "full_cache": full_cache,
            "timestamp": now_iso(),
        }
        num_viewers = len(self.firebase_viewers)
        logger.info(f"⚡ [BROADCASTING TO FRONTEND] Pushing Firebase update on path '{path}' to {num_viewers} frontend client(s)")
        dead_sockets = []
        for viewer in list(self.firebase_viewers):
            try:
                await viewer.send_json(message)
            except Exception as e:
                logger.error(f"Failed to send WS message to viewer: {e}")
                dead_sockets.append(viewer)
        for dead in dead_sockets:
            self.disconnect_firebase_viewer(dead)




async def _find_related_devices(device_id: str, target_type: str) -> list[str]:
    """Find related devices (e.g. alert module for a given blackbox) by vehicle_id."""
    source_device = await db.devices.find_one({"device_id": device_id})
    if not source_device or not source_device.get("vehicle_id"):
        return []
    cursor = db.devices.find({
        "vehicle_id": source_device["vehicle_id"],
        "device_type": target_type,
        "enabled": True,
    })
    docs = await cursor.to_list(length=10)
    return [d["device_id"] for d in docs]


iot_manager = IoTConnectionManager()

@app.websocket("/ws/telemetry/{device_id}")
async def ws_telemetry(websocket: WebSocket, device_id: str):
    await iot_manager.connect_device(websocket, device_id)
    try:
        while True:
            data = await websocket.receive_text()
            if data.strip() == "FALSE_ALARM":
                await db.alerts.insert_one({"device_id": device_id, "status": "false_alarm", "timestamp": now_iso()})
                firebase_service.update_accident_status(device_id, {"status": "normal", "timestamp": now_iso()})
                # NEW: Clear alert state on related alert modules
                try:
                    alert_modules = await _find_related_devices(device_id, DeviceType.ALERT_MODULE)
                    for am_id in alert_modules:
                        cancel_cmd = {
                            "command": "sos_off",
                            "command_id": str(uuid.uuid4()),
                            "status": "PENDING",
                            "created_at": now_iso(),
                            "received_at": None,
                            "executed_at": None,
                            "error": None,
                        }
                        await firebase_service.async_write_device_command(am_id, cancel_cmd)
                    await firebase_service.async_update_alert_state(device_id, {
                        "active": False, "alert_type": None, "message": "False alarm — cancelled",
                        "timestamp": now_iso(), "alert_id": None,
                    })
                except Exception as e:
                    logger.error(f"Error during FALSE_ALARM alert-module update: {e}")
                # Broadcast to dashboard viewers
                await iot_manager.broadcast_to_dashboard(device_id, "accident_status", {
                    "status": "false_alarm", "device_id": device_id,
                })
            elif data.strip() == "CONFIRMED_ACCIDENT":
                alert_id = str(uuid.uuid4())
                await db.alerts.insert_one({
                    "device_id": device_id, "status": "confirmed_accident",
                    "alert_id": alert_id, "timestamp": now_iso(),
                })
                firebase_service.update_accident_status(device_id, {"status": "confirmed_accident", "timestamp": now_iso()})
                await iot_manager.save_snapshot(device_id)
                if device_id in iot_manager.camera_sources:
                    await iot_manager.camera_sources[device_id].send_text("WAKE_UP")
                # NEW: Send SOS command to related alert modules via Firebase
                try:
                    alert_modules = await _find_related_devices(device_id, DeviceType.ALERT_MODULE)
                    for am_id in alert_modules:
                        sos_cmd = {
                            "command": "sos_on",
                            "command_id": str(uuid.uuid4()),
                            "status": "PENDING",
                            "created_at": now_iso(),
                            "received_at": None,
                            "executed_at": None,
                            "error": None,
                        }
                        await firebase_service.async_write_device_command(am_id, sos_cmd)
                    # Write alert state for alert module consumption
                    await firebase_service.async_update_alert_state(device_id, {
                        "active": True, "alert_type": "accident",
                        "message": "Confirmed accident detected",
                        "timestamp": now_iso(), "alert_id": alert_id,
                    })
                    # Send snapshot command to related cameras
                    cameras = await _find_related_devices(device_id, DeviceType.CAMERA)
                    for cam_id in cameras:
                        snap_cmd = {
                            "command": "upload_snapshot",
                            "command_id": str(uuid.uuid4()),
                            "status": "PENDING",
                            "created_at": now_iso(),
                            "received_at": None,
                            "executed_at": None,
                            "error": None,
                        }
                        await firebase_service.async_write_device_command(cam_id, snap_cmd)
                except Exception as e:
                    logger.error(f"Error during CONFIRMED_ACCIDENT device updates: {e}")
                # Broadcast to dashboard viewers
                await iot_manager.broadcast_to_dashboard(device_id, "accident_status", {
                    "status": "confirmed_accident", "device_id": device_id,
                    "alert_id": alert_id,
                })
            else:
                # Buffer the normal telemetry data
                iot_manager.add_telemetry(device_id, data)
                try:
                    telemetry_dict = json.loads(data)
                    # Calculate g_force for dashboard consumption
                    ax = telemetry_dict.get("ax", 0.0)
                    ay = telemetry_dict.get("ay", 0.0)
                    az = telemetry_dict.get("az", 1.0)
                    g_force = (ax * ax + ay * ay + az * az) ** 0.5
                    telemetry_dict["g_force"] = g_force
                    
                    firebase_service.update_live_telemetry(device_id, telemetry_dict)
                    # NEW: Update last_seen for online detection
                    firebase_service.update_device_status(device_id, {"last_seen": now_iso(), "is_online": True})
                    # Broadcast telemetry to dashboard viewers
                    await iot_manager.broadcast_to_dashboard(device_id, "telemetry", telemetry_dict)
                except Exception:
                    pass
    except WebSocketDisconnect:
        iot_manager.disconnect_device(device_id)
        # Broadcast offline status to dashboard viewers
        await iot_manager.broadcast_to_dashboard(device_id, "device_status", {
            "is_online": False, "device_id": device_id, "last_seen": now_iso(),
        })

@app.websocket("/ws/camera/stream/{device_id}")
async def ws_camera_stream(websocket: WebSocket, device_id: str):
    await iot_manager.connect_camera_source(websocket, device_id)
    try:
        while True:
            frame_bytes = await websocket.receive_bytes()
            iot_manager.add_video_frame(device_id, frame_bytes)
            await iot_manager.broadcast_frame(device_id, frame_bytes)
    except WebSocketDisconnect:
        iot_manager.disconnect_camera_source(device_id)

@app.websocket("/ws/camera/view/{device_id}")
async def ws_camera_view(websocket: WebSocket, device_id: str):
    await iot_manager.connect_viewer(websocket, device_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        iot_manager.disconnect_viewer(websocket, device_id)


# ---- NEW: Dashboard WebSocket Endpoint ----
@app.websocket("/ws/dashboard/{device_id}")
async def ws_dashboard(websocket: WebSocket, device_id: str):
    """Live WebSocket stream of telemetry, commands, and alerts specifically for the frontend dashboard."""
    await iot_manager.connect_dashboard_viewer(websocket, device_id)
    try:
        # Send an initial ping or status
        await iot_manager.broadcast_to_dashboard(device_id, "device_status", {
            "is_online": device_id in iot_manager.active_devices,
            "device_id": device_id,
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        iot_manager.disconnect_dashboard_viewer(websocket, device_id)


# ---- NEW: Firebase Realtime Database Stream WebSocket ----
@app.websocket("/ws/firebase/live")
async def ws_firebase_live(websocket: WebSocket):
    """Live WebSocket stream of Firebase RTDB changes across all devices."""
    await iot_manager.connect_firebase_viewer(websocket)
    try:
        # Send initial cache snapshot
        await websocket.send_json({
            "type": "firebase_snapshot",
            "full_cache": firebase_service.get_firebase_cache(),
            "timestamp": now_iso(),
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        iot_manager.disconnect_firebase_viewer(websocket)


app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@api.get("/")
async def health():
    return {"status": "online", "service": "NovaShields Mobile SOS", "time": now_iso()}


# ---- REALTIME FIREBASE ENDPOINTS ----
@api.get("/firebase/live")
async def get_firebase_live_data():
    """Get snapshot of current live Firebase cache."""
    cache = firebase_service.get_firebase_cache()
    return {"status": "success", "cache": cache, "timestamp": now_iso()}


@api.get("/firebase/live/{device_id}")
async def get_firebase_device_data(device_id: str):
    """Get live Firebase data for specific device_id."""
    cache = firebase_service.get_firebase_cache()
    device_data = cache.get(device_id)
    if not device_data:
        device_data = await firebase_service.async_read_camera_status(device_id)
    return {"status": "success", "device_id": device_id, "data": device_data, "timestamp": now_iso()}


@api.post("/simulator/firebase_camera")
async def simulate_firebase_camera_update(payload: dict):
    """Simulator endpoint to test writing camera metadata to Firebase RTDB."""
    device_id = payload.get("device_id", "6a784b5a2d5c723280f9a163")
    camera_data = {
        "device_id": device_id,
        "online": payload.get("online", True),
        "snapshot_url": payload.get("snapshot_url", "https://res.cloudinary.com/p0pwhnui/image/upload/v1786267512/cspc.jpg"),
        "stream_url": payload.get("stream_url", ""),
        "last_frame_at": payload.get("last_frame_at", now_iso()),
        "updated_at": now_iso(),
    }
    await firebase_service.async_update_camera_metadata(device_id, camera_data)
    return {"message": "Camera metadata updated in Firebase RTDB", "data": camera_data}



# ---- Auth Endpoints -------------------------------------------------------
@api.post("/auth/register")
async def register(user_in: UserRegister):
    existing = await db.users.find_one({"email": user_in.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    hashed_password = get_password_hash(user_in.password)
    
    # Auto-approve RIDER/FAMILY, set POLICE/ADMIN to PENDING_APPROVAL
    status = UserStatus.ACTIVE
    if user_in.role in [UserRole.POLICE, UserRole.ADMIN]:
        status = UserStatus.PENDING_APPROVAL
        
    # Bootstrap: if this is an ADMIN, and there are NO users in the DB yet, auto-approve
    if user_in.role == UserRole.ADMIN:
        count = await db.users.count_documents({})
        if count == 0:
            status = UserStatus.ACTIVE
            
    new_user = User(
        email=user_in.email,
        hashed_password=hashed_password,
        name=user_in.name,
        role=user_in.role,
        status=status
    )
    result = await db.users.insert_one(new_user.model_dump(exclude={"id"}))
    return {"message": "User registered successfully", "user_id": str(result.inserted_id), "status": status.value}

@api.post("/auth/login")
async def login(user_in: UserLogin):
    user = await db.users.find_one({"email": user_in.email})
    if not user or not verify_password(user_in.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    
    # Check approval status
    if user.get("status") == UserStatus.PENDING_APPROVAL.value:
        raise HTTPException(status_code=403, detail="Account pending admin approval.")
    if user.get("status") == UserStatus.REJECTED.value:
        raise HTTPException(status_code=403, detail="Account rejected by admin.")
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={
            "sub": user["user_id"],
            "role": user.get("role", UserRole.RIDER.value),
            "status": user.get("status", UserStatus.ACTIVE.value)
        }, 
        expires_delta=access_token_expires
    )
    return {
        "access_token": access_token, 
        "token_type": "bearer", 
        "user_id": user["user_id"], 
        "name": user["name"],
        "role": user.get("role", UserRole.RIDER.value),
        "status": user.get("status", UserStatus.ACTIVE.value)
    }


# ---- Emergency Contacts CRUD ----------------------------------------------
@api.get("/contacts")
async def list_contacts(user_id: str = Depends(get_current_user)):
    cursor = db.contacts.find({"user_id": user_id}).sort("priority", 1)
    docs = await cursor.to_list(length=100)
    return [EmergencyContact.from_mongo(d).model_dump() for d in docs]


@api.post("/contacts", status_code=status.HTTP_201_CREATED)
async def add_contact(body: EmergencyContactIn, user_id: str = Depends(get_current_user)):
    doc = EmergencyContact(user_id=user_id, **body.model_dump()).model_dump(exclude={"id"})
    result = await db.contacts.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return EmergencyContact.from_mongo(doc).model_dump()


@api.delete("/contacts/{contact_id}")
async def delete_contact(contact_id: str):
    res = await db.contacts.delete_one({"contact_id": contact_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Contact not found")
    return {"deleted": contact_id}


# ---- Alert history --------------------------------------------------------
@api.get("/alerts")
async def list_alerts(device_id: Optional[str] = None, limit: int = 50):
    q = {"device_id": device_id} if device_id else {}
    cursor = db.alerts.find(q).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    results = []
    for d in docs:
        try:
            results.append(AlertRecord.from_mongo(d).model_dump())
        except Exception:
            # Handle simplified alert docs from WebSocket handler
            d["_id"] = str(d.get("_id", ""))
            results.append(d)
    return results


@api.post("/alerts", status_code=status.HTTP_201_CREATED)
async def create_alert(body: AlertIn):
    doc = AlertRecord(**body.model_dump()).model_dump(exclude={"id"})
    result = await db.alerts.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return AlertRecord.from_mongo(doc).model_dump()


# ---- ESP32 Camera Endpoints -----------------------------------------------
@api.post("/camera/upload", status_code=status.HTTP_201_CREATED)
async def upload_camera_image(device_id: str = "device_001", file: UploadFile = File(...)):
    if not CLOUDINARY_CLOUD_NAME or not CLOUDINARY_API_KEY or not CLOUDINARY_API_SECRET:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cloudinary credentials are not configured on the server."
        )
    
    try:
        # Upload the file directly using Cloudinary
        upload_result = cloudinary.uploader.upload(
            file.file,
            folder="novashields_esp32"
        )
        image_url = upload_result.get("secure_url")
        if not image_url:
            raise Exception("Secure URL not returned by Cloudinary.")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Cloudinary upload error: {str(e)}"
        )
    
    doc = CameraCapture(device_id=device_id, image_url=image_url).model_dump(exclude={"id"})
    result = await db.camera_captures.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return CameraCapture.from_mongo(doc).model_dump()


@api.get("/camera/latest/{device_id}")
async def get_latest_image(device_id: str, _ = Depends(require_role("POLICE", "ADMIN"))):
    capture = await db.camera_captures.find_one(
        {"device_id": device_id},
        sort=[("created_at", -1)]
    )
    if not capture:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No image captures found for device {device_id}."
        )
    return CameraCapture.from_mongo(capture).model_dump()



# ---- Command Log ----------------------------------------------------------
@api.get("/commands")
async def list_commands(device_id: Optional[str] = None, limit: int = 50, _ = Depends(require_role("ADMIN", "POLICE"))):
    q = {"device_id": device_id} if device_id else {}
    cursor = db.commands.find(q).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [CommandLog.from_mongo(d).model_dump() for d in docs]


@api.post("/commands", status_code=status.HTTP_201_CREATED)
async def log_command(body: CommandIn, _ = Depends(require_role("RIDER", "FAMILY", "POLICE", "ADMIN"))):
    doc = CommandLog(**body.model_dump()).model_dump(exclude={"id"})
    result = await db.commands.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    # Send via existing WebSocket + legacy Firebase emergency_commands
    await iot_manager.send_command(body.device_id, body.model_dump())
    # NEW: Write structured command to Firebase for device consumption
    firebase_cmd = {
        "command": body.command,
        "command_id": doc["command_id"],
        "status": "PENDING",
        "created_at": doc["created_at"],
        "received_at": None,
        "executed_at": None,
        "error": None,
    }
    try:
        await firebase_service.async_write_device_command(body.device_id, firebase_cmd)
    except Exception as e:
        logger.error(f"Failed to write command to Firebase: {e}")
    # Broadcast command status to dashboard
    await iot_manager.broadcast_to_dashboard(body.device_id, "command_status", {
        "command": body.command, "command_id": doc["command_id"], "status": "PENDING",
    })
    return CommandLog.from_mongo(doc).model_dump()


# ---- AI / Rule analysis ---------------------------------------------------
class AnalyzeIn(BaseModel):
    telemetry: TelemetryFrame
    previous: Optional[TelemetryFrame] = None
    run_ai: bool = False


@api.post("/analyze")
async def analyze(body: AnalyzeIn):
    rule = evaluate_rules(body.telemetry, body.previous)
    ai_result: Optional[dict] = None
    if body.run_ai:
        ai_result = await ai_analyze(body.telemetry, rule)
    ml_result = predict_frame(body.telemetry.model_dump())
    # Final verdict priority: AI > ML > Rules (whichever available with higher confidence)
    verdict = rule.event
    severity = rule.severity
    confidence = rule.confidence
    if ml_result.get("available") and ml_result["confidence"] > confidence:
        verdict = ml_result["verdict"]
        confidence = ml_result["confidence"]
        # infer severity from ML class
        crit = {"collision", "bike_fall"}
        warn = {"hard_brake", "hard_lean", "pothole"}
        severity = "critical" if verdict in crit else "warning" if verdict in warn else "info"
    if ai_result and ai_result.get("available"):
        verdict = ai_result.get("verdict", verdict)
        severity = ai_result.get("severity", severity)
        confidence = ai_result.get("confidence", confidence)
    return {
        "rule": rule.model_dump(),
        "ml": ml_result,
        "ai": ai_result,
        "final_verdict": verdict,
        "final_severity": severity,
        "final_confidence": confidence,
    }





# ---- Trips ---------------------------------------------------------------
class TripStart(BaseModel):
    device_id: str = "device_001"
    user_id: str = "default"
    trigger: str = "manual"


class TripPoint(BaseModel):
    trip_id: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed_kmh: float = 0
    lean_angle: float = 0
    g_force: float = 0
    timestamp: Optional[str] = None


class TripEvent(BaseModel):
    trip_id: str
    event_type: str
    severity: str = "warning"
    message: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timestamp: Optional[str] = None


def _haversine(a, b):
    from math import radians, sin, cos, asin, sqrt
    lat1, lon1 = a
    lat2, lon2 = b
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(h))


@api.post("/trips/start")
async def start_trip(body: TripStart):
    trip = {
        "trip_id": str(uuid.uuid4()),
        "device_id": body.device_id,
        "user_id": body.user_id,
        "trigger": body.trigger,
        "started_at": now_iso(),
        "ended_at": None,
        "distance_km": 0.0,
        "top_speed": 0.0,
        "max_lean": 0.0,
        "max_g": 0.0,
        "points": [],
        "events": [],
        "status": "active",
    }
    await db.trips.insert_one(trip)
    trip.pop("_id", None)
    return trip


@api.post("/trips/point")
async def add_point(body: TripPoint):
    trip = await db.trips.find_one({"trip_id": body.trip_id})
    if not trip:
        raise HTTPException(404, "trip not found")
    pt = body.model_dump()
    pt["timestamp"] = pt.get("timestamp") or now_iso()
    inc_distance = 0.0
    if trip.get("points") and pt.get("latitude") and pt.get("longitude"):
        last = trip["points"][-1]
        if last.get("latitude") and last.get("longitude"):
            inc_distance = _haversine(
                (last["latitude"], last["longitude"]),
                (pt["latitude"], pt["longitude"]),
            )
    await db.trips.update_one(
        {"trip_id": body.trip_id},
        {
            "$push": {"points": pt},
            "$inc": {"distance_km": inc_distance},
            "$max": {
                "top_speed": pt.get("speed_kmh", 0),
                "max_lean": abs(pt.get("lean_angle", 0)),
                "max_g": pt.get("g_force", 0),
            },
        },
    )
    return {"ok": True, "added_km": round(inc_distance, 4)}


@api.post("/trips/event")
async def add_event(body: TripEvent):
    ev = body.model_dump()
    ev["timestamp"] = ev.get("timestamp") or now_iso()
    res = await db.trips.update_one({"trip_id": body.trip_id}, {"$push": {"events": ev}})
    if res.matched_count == 0:
        raise HTTPException(404, "trip not found")
    return {"ok": True}


@api.post("/trips/{trip_id}/end")
async def end_trip(trip_id: str):
    res = await db.trips.update_one(
        {"trip_id": trip_id, "status": "active"},
        {"$set": {"status": "ended", "ended_at": now_iso()}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "trip not found or already ended")
    trip = await db.trips.find_one({"trip_id": trip_id})
    trip["_id"] = str(trip["_id"])
    return trip


@api.get("/trips")
async def list_trips(user_id: str = Depends(get_current_user), limit: int = 30):
    cur = db.trips.find({"user_id": user_id}).sort("started_at", -1).limit(limit)
    docs = await cur.to_list(length=limit)
    out = []
    for d in docs:
        d["_id"] = str(d["_id"])
        d["point_count"] = len(d.get("points", []))
        d["event_count"] = len(d.get("events", []))
        d.pop("points", None)
        out.append(d)
    return out


@api.get("/trips/{trip_id}")
async def get_trip(trip_id: str):
    doc = await db.trips.find_one({"trip_id": trip_id})
    if not doc:
        raise HTTPException(404, "trip not found")
    doc["_id"] = str(doc["_id"])
    return doc


@api.delete("/trips/{trip_id}")
async def delete_trip(trip_id: str):
    res = await db.trips.delete_one({"trip_id": trip_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "trip not found")
    return {"deleted": trip_id}


# ---- ML training + inference ---------------------------------------------
class LabelRow(BaseModel):
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float
    pitch: float
    roll: float
    lean_angle: float
    speed_kmh: float
    label: str


class LabelBatch(BaseModel):
    rows: list[LabelRow]


@api.get("/ml/status")
async def ml_status():
    st = model_status()
    df = load_dataset()
    st["label_distribution"] = df["label"].value_counts().to_dict()
    return st


@api.post("/ml/train")
async def ml_train():
    try:
        return train_model()
    except Exception as e:
        raise HTTPException(400, str(e))


@api.post("/ml/predict")
async def ml_predict(body: TelemetryFrame):
    return predict_frame(body.model_dump())


@api.post("/ml/dataset/append")
async def dataset_append(body: LabelBatch):
    n = append_dataset([r.model_dump() for r in body.rows])
    return {"added": n, "total": int(len(load_dataset()))}


@api.post("/ml/dataset/upload")
async def dataset_upload(file: UploadFile = File(...)):
    try:
        content = await file.read()
        return replace_dataset_from_csv(content)
    except Exception as e:
        raise HTTPException(400, str(e))


@api.post("/ml/dataset/reset")
async def dataset_reset():
    return reset_dataset()


@api.get("/ml/dataset/preview")
async def dataset_preview(limit: int = 20):
    df = load_dataset()
    return {
        "total_rows": int(len(df)),
        "sample": df.head(limit).to_dict(orient="records"),
        "labels": df["label"].value_counts().to_dict(),
    }


# ---- Medical profile (for QR badge) --------------------------------------
class MedicalProfile(BaseModel):
    user_id: str = "default"
    full_name: str = ""
    dob: str = ""
    blood_group: str = ""
    allergies: str = ""
    medications: str = ""
    conditions: str = ""
    emergency_contact_name: str = ""
    emergency_contact_phone: str = ""
    insurance_provider: str = ""
    insurance_policy: str = ""
    organ_donor: bool = False
    doctor_phone: str = ""
    notes: str = ""


@api.get("/medical")
async def get_medical(user_id: str = "default"):
    doc = await db.medical.find_one({"user_id": user_id})
    if not doc:
        return MedicalProfile(user_id=user_id).model_dump()
    doc.pop("_id", None)
    return doc


@api.post("/medical")
async def save_medical(body: MedicalProfile):
    data = body.model_dump()
    await db.medical.update_one(
        {"user_id": body.user_id}, {"$set": data}, upsert=True
    )
    return data


# Public read-only endpoint used by first-responders scanning the QR
@api.get("/public/medical/{user_id}")
async def public_medical(user_id: str):
    doc = await db.medical.find_one({"user_id": user_id})
    if not doc:
        raise HTTPException(404, "not found")
    doc.pop("_id", None)
    doc.pop("insurance_provider", None)
    doc.pop("insurance_policy", None)
    return doc


# ---- FCM push notification tokens ----------------------------------------
class FcmTokenIn(BaseModel):
    user_id: str = "default"
    token: str
    device_label: Optional[str] = None


@api.post("/notifications/register")
async def register_token(body: FcmTokenIn):
    await db.fcm_tokens.update_one(
        {"token": body.token},
        {"$set": {
            "user_id": body.user_id,
            "token": body.token,
            "device_label": body.device_label,
            "updated_at": now_iso(),
        }},
        upsert=True,
    )
    return {"ok": True}


@api.get("/notifications/tokens")
async def list_tokens(user_id: str = "default"):
    cur = db.fcm_tokens.find({"user_id": user_id}, {"_id": 0})
    return await cur.to_list(length=50)


@api.delete("/notifications/token")
async def unregister_token(token: str):
    await db.fcm_tokens.delete_one({"token": token})
    return {"ok": True}


class PushRequest(BaseModel):
    user_id: str = "default"
    title: str
    body: str
    url: Optional[str] = "/alerts"


@api.post("/notifications/broadcast")
async def broadcast(body: PushRequest):
    """Send push notification to all registered devices for the user.
    NOTE: requires FIREBASE_SERVICE_ACCOUNT env or google-auth creds; without
    them we still record the broadcast intent so the app can fall back to
    a local browser Notification when open.
    """
    sa = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH")
    tokens = await db.fcm_tokens.find({"user_id": body.user_id}, {"token": 1, "_id": 0}).to_list(length=100)
    intent = {
        "user_id": body.user_id,
        "title": body.title,
        "body": body.body,
        "url": body.url,
        "created_at": now_iso(),
        "delivered_count": 0,
        "target_count": len(tokens),
        "backend_delivery": "requires_service_account",
    }
    if sa and os.path.exists(sa):
        try:
            import firebase_admin
            from firebase_admin import credentials, messaging
            if not firebase_admin._apps:
                firebase_admin.initialize_app(credentials.Certificate(sa))
            for t in tokens:
                try:
                    messaging.send(messaging.Message(
                        notification=messaging.Notification(title=body.title, body=body.body),
                        token=t["token"],
                        webpush=messaging.WebpushConfig(
                            fcm_options=messaging.WebpushFCMOptions(link=body.url)
                        ),
                    ))
                    intent["delivered_count"] += 1
                except Exception as e:  # noqa
                    intent.setdefault("errors", []).append(str(e)[:120])
            intent["backend_delivery"] = "firebase_admin"
        except Exception as e:
            intent["backend_delivery"] = f"fallback ({e})"
    await db.push_intents.insert_one(intent)
    intent.pop("_id", None)
    return intent


# ---- Insurance PDF report ------------------------------------------------
@api.get("/alerts/{alert_id}/report.pdf")
async def alert_report(alert_id: str):
    alert = await db.alerts.find_one({"alert_id": alert_id})
    if not alert:
        raise HTTPException(404, "alert not found")
    med = await db.medical.find_one({"user_id": "default"}) or {}
    contacts = await db.contacts.find({"user_id": "default"}, {"_id": 0}).to_list(length=20)

    # Best-effort surrounding trip context: most recent active/ended trip
    trip = await db.trips.find_one(
        {"device_id": alert.get("device_id")},
        sort=[("started_at", -1)],
    )

    pdf_bytes = _render_incident_pdf(alert, med, contacts, trip)
    filename = f"NovaShields_Incident_{alert_id[:8]}.pdf"
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


def _render_incident_pdf(alert, medical, contacts, trip):
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title="NovaShields Incident Report",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontName="Helvetica-Bold",
                        fontSize=22, leading=26, textColor=colors.HexColor("#0A0A0A"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName="Helvetica-Bold",
                        fontSize=12, leading=16, textColor=colors.HexColor("#B45309"),
                        spaceBefore=14, spaceAfter=6)
    body_s = ParagraphStyle("body", parent=styles["BodyText"], fontName="Helvetica",
                            fontSize=10, leading=13, textColor=colors.HexColor("#1F2937"))
    sub = ParagraphStyle("sub", parent=styles["BodyText"], fontName="Helvetica",
                         fontSize=8, leading=11, textColor=colors.HexColor("#6B7280"))
    mono = ParagraphStyle("mono", parent=body_s, fontName="Courier", fontSize=9, leading=12)

    story = []

    # Header
    story.append(Paragraph("NOVASHIELDS · INCIDENT REPORT", h1))
    story.append(Paragraph(
        f"Automatically generated · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"Alert ID {alert.get('alert_id','')}",
        sub,
    ))
    story.append(Spacer(1, 10))

    # Incident block
    story.append(Paragraph("INCIDENT SUMMARY", h2))
    lat, lon = alert.get("latitude"), alert.get("longitude")
    maps = f"https://maps.google.com/?q={lat},{lon}" if (lat and lon) else "—"
    inc = [
        ["Event", (alert.get("event_type") or "").replace("_", " ").title()],
        ["Severity", (alert.get("severity") or "—").upper()],
        ["Confidence", f"{(alert.get('confidence') or 0):.2f}"],
        ["Occurred", alert.get("created_at", "—")],
        ["Speed at event", f"{round(alert.get('speed') or 0)} km/h"],
        ["Location", f"{lat}, {lon}" if lat else "—"],
        ["Google Maps", maps],
        ["Device ID", alert.get("device_id", "—")],
        ["Rider message", alert.get("message", "—")],
    ]
    _kv_style = TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9),
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#6B7280")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ])
    t = Table(inc, colWidths=[42 * mm, 120 * mm])
    t.setStyle(_kv_style)
    story.append(t)

    if alert.get("ai_analysis"):
        story.append(Paragraph("AI ANALYSIS", h2))
        story.append(Paragraph(alert["ai_analysis"], body_s))

    # Rider medical
    story.append(Paragraph("RIDER PROFILE", h2))
    med_rows = [
        ["Full name", medical.get("full_name", "—")],
        ["DOB", medical.get("dob", "—")],
        ["Blood group", medical.get("blood_group", "—")],
        ["Allergies", medical.get("allergies", "—")],
        ["Medications", medical.get("medications", "—")],
        ["Conditions", medical.get("conditions", "—")],
        ["Organ donor", "YES" if medical.get("organ_donor") else "no"],
        ["Doctor", medical.get("doctor_phone", "—")],
        ["Insurance", f"{medical.get('insurance_provider','—')} · policy {medical.get('insurance_policy','—')}"],
    ]
    t2 = Table(med_rows, colWidths=[42 * mm, 120 * mm])
    t2.setStyle(_kv_style)
    story.append(t2)

    # Telemetry snapshot
    tel = alert.get("telemetry_snapshot") or {}
    if tel:
        story.append(Paragraph("TELEMETRY SNAPSHOT", h2))
        rows = [["Field", "Value"]]
        for k in ["ax","ay","az","gx","gy","gz","pitch","roll","lean_angle","speed_kmh","battery"]:
            if k in tel:
                rows.append([k, f"{tel[k]}"])
        t3 = Table(rows, colWidths=[42 * mm, 120 * mm])
        t3.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
            ("FONT", (0, 1), (-1, -1), "Courier", 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#B45309")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t3)

    # Trip context
    if trip:
        story.append(Paragraph("SURROUNDING TRIP", h2))
        trip_rows = [
            ["Trip ID", trip.get("trip_id", "—")],
            ["Started", trip.get("started_at", "—")],
            ["Ended", trip.get("ended_at", "—")],
            ["Distance", f"{(trip.get('distance_km') or 0):.2f} km"],
            ["Top speed", f"{round(trip.get('top_speed') or 0)} km/h"],
            ["Max lean", f"{round(trip.get('max_lean') or 0)}°"],
            ["Max G", f"{(trip.get('max_g') or 0):.2f}"],
            ["Events", str(len(trip.get("events") or []))],
        ]
        t4 = Table(trip_rows, colWidths=[42 * mm, 120 * mm])
        t4.setStyle(_kv_style)
        story.append(t4)

    # Emergency contacts
    story.append(Paragraph("EMERGENCY CONTACTS NOTIFIED", h2))
    if not contacts:
        story.append(Paragraph("No emergency contacts registered.", body_s))
    else:
        rows = [["Name", "Phone", "Relation", "Priority"]]
        for c in contacts:
            rows.append([c.get("name","—"), c.get("phone","—"), c.get("relation","—"), str(c.get("priority",1))])
        t5 = Table(rows, colWidths=[45 * mm, 45 * mm, 40 * mm, 22 * mm])
        t5.setStyle(TableStyle([
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
            ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0A0A0A")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E7EB")),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(t5)

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "This report was generated automatically by the NovaShields Smart Black Box. "
        "It is intended for insurance claims and first-responder reference. "
        "Data sourced from on-device IMU + GPS + cloud AI analysis.",
        sub,
    ))

    doc.build(story)
    return buf.getvalue()



# ---------------------------------------------------------------------------
# Device Registration & Management  (NEW)
# ---------------------------------------------------------------------------

@api.post("/devices", status_code=status.HTTP_201_CREATED)
async def register_device(body: DeviceRegister):
    """Register a new physical device (Black Box, Alert Module, or Camera)."""
    existing = await db.devices.find_one({"device_id": body.device_id})
    if existing:
        raise HTTPException(400, f"Device {body.device_id} already registered")
    device = Device(**body.model_dump()).model_dump(exclude={"id"})
    result = await db.devices.insert_one(device)
    device["_id"] = str(result.inserted_id)
    # Initialise Firebase status node for this device
    try:
        await firebase_service.async_update_device_status(body.device_id, {
            "is_online": False,
            "last_seen": None,
            "device_id": body.device_id,
            "device_type": body.device_type.value,
        })
    except Exception as e:
        logger.error(f"Failed to init Firebase status for {body.device_id}: {e}")
    return Device.from_mongo(device).model_dump()


@api.get("/devices")
async def list_devices(
    device_type: Optional[str] = None,
    vehicle_id: Optional[str] = None,
    user_id: str = Depends(get_current_user),
):
    """List registered devices, optionally filtered by type or vehicle."""
    q: dict = {}
    if device_type:
        q["device_type"] = device_type
    if vehicle_id:
        q["vehicle_id"] = vehicle_id
    cursor = db.devices.find(q).sort("created_at", -1)
    docs = await cursor.to_list(length=100)
    return [Device.from_mongo(d).model_dump() for d in docs]


@api.get("/devices/{device_id}")
async def get_device(device_id: str):
    """Get device details including computed online status."""
    doc = await db.devices.find_one({"device_id": device_id})
    if not doc:
        raise HTTPException(404, f"Device {device_id} not found")
    device = Device.from_mongo(doc).model_dump()
    # Compute online status from last_seen
    if device.get("last_seen"):
        try:
            last_seen_dt = datetime.fromisoformat(device["last_seen"])
            delta = (datetime.now(timezone.utc) - last_seen_dt).total_seconds()
            device["is_online"] = delta < DEVICE_ONLINE_TIMEOUT_SECONDS
        except (ValueError, TypeError):
            device["is_online"] = False
    else:
        device["is_online"] = False
    return device


@api.put("/devices/{device_id}")
async def update_device(device_id: str, body: DeviceUpdate):
    """Update device configuration (name, vehicle_id, enabled, etc.)."""
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(400, "No fields to update")
    res = await db.devices.update_one({"device_id": device_id}, {"$set": update_data})
    if res.matched_count == 0:
        raise HTTPException(404, f"Device {device_id} not found")
    doc = await db.devices.find_one({"device_id": device_id})
    return Device.from_mongo(doc).model_dump()


@api.delete("/devices/{device_id}")
async def delete_device(device_id: str):
    """Deregister a device."""
    res = await db.devices.delete_one({"device_id": device_id})
    if res.deleted_count == 0:
        raise HTTPException(404, f"Device {device_id} not found")
    return {"deleted": device_id}


@api.get("/devices/{device_id}/status")
async def get_device_status(device_id: str):
    """Get real-time device status from Firebase + computed online state."""
    fb_status = await firebase_service.async_read_device_status(device_id)
    result = fb_status or {"is_online": False, "device_id": device_id}
    # Compute online state from last_seen
    last_seen = result.get("last_seen")
    if last_seen:
        try:
            last_seen_dt = datetime.fromisoformat(last_seen)
            delta = (datetime.now(timezone.utc) - last_seen_dt).total_seconds()
            result["is_online"] = delta < DEVICE_ONLINE_TIMEOUT_SECONDS
        except (ValueError, TypeError):
            result["is_online"] = False
    else:
        result["is_online"] = False
    return result


# ---------------------------------------------------------------------------
# Command Acknowledgement  (NEW)
# ---------------------------------------------------------------------------

@api.get("/commands/{command_id}/status")
async def get_command_status(command_id: str):
    """Get the current status of a command (from MongoDB + Firebase)."""
    doc = await db.commands.find_one({"command_id": command_id})
    if not doc:
        raise HTTPException(404, f"Command {command_id} not found")
    doc["_id"] = str(doc["_id"])
    # Also check Firebase for latest ack if device_id is available
    device_id = doc.get("device_id")
    if device_id:
        fb_cmd = await firebase_service.async_read_command_status(device_id)
        if fb_cmd and fb_cmd.get("command_id") == command_id:
            # Merge Firebase state into response
            doc["firebase_status"] = fb_cmd.get("status")
            doc["firebase_received_at"] = fb_cmd.get("received_at")
            doc["firebase_executed_at"] = fb_cmd.get("executed_at")
            doc["firebase_error"] = fb_cmd.get("error")
    return doc


@api.post("/commands/{command_id}/ack")
async def ack_command(command_id: str, body: CommandAck):
    """Device reports command acknowledgement (RECEIVED / EXECUTED / FAILED)."""
    doc = await db.commands.find_one({"command_id": command_id})
    if not doc:
        raise HTTPException(404, f"Command {command_id} not found")

    update_fields: dict = {"status": body.status}
    if body.status == "RECEIVED":
        update_fields["received_at"] = now_iso()
    elif body.status == "EXECUTED":
        update_fields["executed_at"] = now_iso()
    elif body.status == "FAILED":
        update_fields["error"] = body.error or "Unknown error"

    await db.commands.update_one({"command_id": command_id}, {"$set": update_fields})

    # Update Firebase command node
    device_id = doc.get("device_id")
    if device_id:
        try:
            await firebase_service.async_update_command_ack(device_id, update_fields)
        except Exception as e:
            logger.error(f"Failed to update Firebase command ack: {e}")
        # Broadcast to dashboard
        await iot_manager.broadcast_to_dashboard(device_id, "command_status", {
            "command_id": command_id,
            "command": doc.get("command"),
            "status": body.status,
            "error": body.error,
        })

    return {"command_id": command_id, "status": body.status}


# ---------------------------------------------------------------------------
# Camera Metadata  (NEW)
# ---------------------------------------------------------------------------

@api.post("/camera/{device_id}/metadata")
async def update_camera_metadata(device_id: str, body: CameraMetadataIn):
    """ESP32-CAM or admin reports camera metadata (stream URL, snapshot URL, online status)."""
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    update_data["device_id"] = device_id
    update_data["updated_at"] = now_iso()

    await db.camera_metadata.update_one(
        {"device_id": device_id},
        {"$set": update_data},
        upsert=True,
    )
    # Sync to Firebase
    try:
        await firebase_service.async_update_camera_metadata(device_id, update_data)
    except Exception as e:
        logger.error(f"Failed to sync camera metadata to Firebase: {e}")
    # Broadcast to dashboard
    await iot_manager.broadcast_to_dashboard(device_id, "camera_status", update_data)
    return update_data


@api.get("/camera/{device_id}/metadata")
async def get_camera_metadata(device_id: str):
    """Get camera metadata (stream URL, snapshot URL, online status)."""
    # Try MongoDB first
    doc = await db.camera_metadata.find_one({"device_id": device_id})
    if doc:
        doc.pop("_id", None)
        return doc
    # Fallback to Firebase
    fb_cam = await firebase_service.async_read_camera_status(device_id)
    if fb_cam:
        return fb_cam
    raise HTTPException(404, f"No camera metadata for device {device_id}")


@api.get("/camera/{device_id}/status")
async def get_camera_status_endpoint(device_id: str):
    """Get camera online/offline status."""
    fb_cam = await firebase_service.async_read_camera_status(device_id)
    if fb_cam:
        return {
            "device_id": device_id,
            "online": fb_cam.get("online", False),
            "stream_url": fb_cam.get("stream_url"),
            "snapshot_url": fb_cam.get("snapshot_url"),
            "last_frame_at": fb_cam.get("last_frame_at"),
            "updated_at": fb_cam.get("updated_at"),
        }
    # Fallback to MongoDB
    doc = await db.camera_metadata.find_one({"device_id": device_id})
    if doc:
        doc.pop("_id", None)
        return doc
    return {"device_id": device_id, "online": False}


# ---------------------------------------------------------------------------
# Firebase telemetry read  (NEW — for when telemetry comes via Firebase, not WS)
# ---------------------------------------------------------------------------

@api.get("/devices/{device_id}/telemetry")
async def get_device_telemetry(device_id: str):
    """Read the latest telemetry for a device from Firebase.
    Includes staleness detection based on timestamp.
    """
    telemetry = await firebase_service.async_read_device_telemetry(device_id)
    if not telemetry:
        raise HTTPException(404, f"No telemetry available for device {device_id}")
    # Staleness detection
    ts = telemetry.get("timestamp")
    stale = True
    if ts:
        try:
            ts_dt = datetime.fromisoformat(ts)
            delta = (datetime.now(timezone.utc) - ts_dt).total_seconds()
            stale = delta > 30
        except (ValueError, TypeError):
            stale = True
    telemetry["stale"] = stale
    return telemetry


@api.get("/devices/{device_id}/accident")
async def get_device_accident_status(device_id: str):
    """Read the current accident status from Firebase."""
    accident = await firebase_service.async_read_accident_status(device_id)
    if not accident:
        return {"device_id": device_id, "status": "normal"}
    return accident


app.include_router(api)


# ---- Website merge (legacy NovaShields site) -----------------------------
# Serve the original static website (index.html, dashboard.html, blackbox.html,
# login.html, register.html) at /site/*.
WEBSITE_DIR = os.path.join(os.path.dirname(__file__), "website")
if os.path.isdir(WEBSITE_DIR):
    app.mount("/site", StaticFiles(directory=WEBSITE_DIR, html=True), name="website")


# ---- Compat endpoints for the legacy website --------------------------------
# Website's app.js expects:
#   POST /api/add-sample   { pitch, roll, heading, accX, accY, accZ, gyroX, gyroY, gyroZ, timestamp, label }
#   GET  /api/dataset-stats
#   POST /api/train-model
#   POST /api/cancel-sos   { device_id }  (with X-API-Key header, ignored)
class LegacySample(BaseModel):
    label: str
    accX: float = 0
    accY: float = 0
    accZ: float = 1
    gyroX: float = 0
    gyroY: float = 0
    gyroZ: float = 0
    pitch: float = 0
    roll: float = 0
    heading: float = 0
    timestamp: Optional[int] = None


@app.post("/api/add-sample")
async def legacy_add_sample(s: LegacySample):
    """Website compat: single labelled telemetry sample → append to ML dataset."""
    row = {
        "ax": s.accX, "ay": s.accY, "az": s.accZ,
        "gx": s.gyroX, "gy": s.gyroY, "gz": s.gyroZ,
        "pitch": s.pitch, "roll": s.roll,
        "lean_angle": s.roll,  # approximate lean from roll
        "speed_kmh": 0.0,
        "label": s.label,
    }
    n = append_dataset([row])
    return {"status": "success", "added": n, "total": int(len(load_dataset()))}


@app.get("/api/dataset-stats")
async def legacy_dataset_stats():
    df = load_dataset()
    return {
        "status": "success",
        "total_samples": int(len(df)),
        "label_distribution": df["label"].value_counts().to_dict(),
    }


@app.post("/api/train-model")
async def legacy_train_model():
    try:
        r = train_model()
        return {
            "status": "success",
            "metrics": {
                "accuracy": round(r["accuracy"] * 100, 1),
                "f1": round(
                    100 * sum(
                        m.get("f1-score", 0) for m in r.get("per_class", {}).values()
                    ) / max(1, len(r.get("per_class", {}))),
                    1,
                ),
            },
            "raw": r,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


class LegacyCancelSOS(BaseModel):
    device_id: str = "device_001"


@app.post("/api/cancel-sos")
async def legacy_cancel_sos(body: LegacyCancelSOS):
    """Website compat: cancel an active SOS."""
    doc = CommandLog(
        device_id=body.device_id,
        command="cancel_sos",
        payload={"source": "website"},
        status="PENDING",
    ).model_dump(exclude={"id"})
    await db.commands.insert_one(doc)
    # NEW: Update Firebase state to cancel SOS
    try:
        # Update accident status to normal
        await firebase_service.async_update_accident_status(body.device_id, {
            "status": "normal", "timestamp": now_iso(),
        })
        # Clear alert state
        await firebase_service.async_update_alert_state(body.device_id, {
            "active": False, "alert_type": None,
            "message": "SOS cancelled", "timestamp": now_iso(), "alert_id": None,
        })
        # Send sos_off command to related alert modules
        alert_modules = await _find_related_devices(body.device_id, DeviceType.ALERT_MODULE)
        for am_id in alert_modules:
            cancel_cmd = {
                "command": "sos_off",
                "command_id": str(uuid.uuid4()),
                "status": "PENDING",
                "created_at": now_iso(),
                "received_at": None,
                "executed_at": None,
                "error": None,
            }
            await firebase_service.async_write_device_command(am_id, cancel_cmd)
    except Exception as e:
        logger.error(f"Failed to update Firebase on cancel-sos: {e}")
    # Broadcast cancellation to dashboard
    await iot_manager.broadcast_to_dashboard(body.device_id, "accident_status", {
        "status": "sos_cancelled", "device_id": body.device_id,
    })
    return {"status": "success", "message": f"SOS cancelled for {body.device_id}"}


# ---------------------------------------------------------------------------



# ---- Admin User Management --------------------------------------------------
@api.get("/admin/users")
async def list_users(status: Optional[str] = None, _ = Depends(require_role("ADMIN"))):
    q = {}
    if status:
        q["status"] = status
    cursor = db.users.find(q).sort("created_at", -1)
    docs = await cursor.to_list(length=100)
    # Filter out hashed_password for safety
    for d in docs:
        d.pop("hashed_password", None)
        d["_id"] = str(d["_id"])
    return docs

@api.patch("/admin/users/{user_id}/approve")
async def approve_user(user_id: str, _ = Depends(require_role("ADMIN"))):
    result = await db.users.update_one(
        {"_id": ObjectId(user_id)}, 
        {"$set": {"status": UserStatus.ACTIVE.value}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="User not found or already active")
    return {"status": "success", "message": f"User {user_id} approved."}

@api.patch("/admin/users/{user_id}/reject")
async def reject_user(user_id: str, _ = Depends(require_role("ADMIN"))):
    result = await db.users.update_one(
        {"_id": ObjectId(user_id)}, 
        {"$set": {"status": UserStatus.REJECTED.value}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="User not found or already rejected")
    return {"status": "success", "message": f"User {user_id} rejected."}

@app.get("/")
async def root():
    return {"service": "NovaShields Mobile SOS", "docs": "/docs"}