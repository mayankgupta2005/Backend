"""
NovaShields Smart Black Box - Mobile SOS App Backend
Provides:
- Emergency contact management (MongoDB)
- Alert history & command logging
- AI crash analysis using Claude Sonnet 4.6 via EMERGENT_LLM_KEY
- Rule-based crash detection endpoint
- Simulator endpoints for testing without hardware
"""
import os
import uuid
import json
import collections
from datetime import datetime, timezone, timedelta
from typing import Annotated, Any, Optional

from bson import ObjectId
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
from fastapi import FastAPI, HTTPException, APIRouter, status, UploadFile, File, Depends, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from passlib.context import CryptContext
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

# ---------------------------------------------------------------------------
# Configuration / Database Mock Client
# ---------------------------------------------------------------------------
MONGO_URL = os.environ.get("MONGO_URL")
if not MONGO_URL or MONGO_URL == "mock":
    raise ValueError("Real-time DB required! Set MONGO_URL environment variable.")
    
DB_NAME = os.environ.get("DB_NAME", "novashields")
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")
EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
JWT_SECRET = os.environ.get("JWT_SECRET", "super-secret-nova-key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 1 week

CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

if CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True
    )


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
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
class User(BaseDoc):
    user_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    email: str
    hashed_password: str
    name: str
    created_at: str = Field(default_factory=now_iso)

class UserRegister(BaseModel):
    email: str
    password: str
    name: str

class UserLogin(BaseModel):
    email: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    user_id: str
    name: str

# ---- Security Utilities ----
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

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
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user_id
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

class CameraCapture(BaseDoc):
    capture_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    device_id: str = "device_001"
    image_url: str
    created_at: str = Field(default_factory=now_iso)


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
    status: str = "sent"  # sent | ack | timeout
    created_at: str = Field(default_factory=now_iso)


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
app = FastAPI(title="NovaShields Mobile SOS Backend", version="1.0.0")
api = APIRouter(prefix="/api")

class IoTConnectionManager:
    def __init__(self):
        self.active_devices: dict[str, WebSocket] = {}
        self.camera_viewers: dict[str, list[WebSocket]] = {}
        self.camera_sources: dict[str, WebSocket] = {}
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

    def disconnect_device(self, device_id: str):
        if device_id in self.active_devices:
            del self.active_devices[device_id]

    async def send_command(self, device_id: str, command: dict):
        if device_id in self.active_devices:
            await self.active_devices[device_id].send_json(command)

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

iot_manager = IoTConnectionManager()

@app.websocket("/ws/telemetry/{device_id}")
async def ws_telemetry(websocket: WebSocket, device_id: str):
    await iot_manager.connect_device(websocket, device_id)
    try:
        while True:
            data = await websocket.receive_text()
            if data.strip() == "FALSE_ALARM":
                await db.alerts.insert_one({"device_id": device_id, "status": "false_alarm", "timestamp": now_iso()})
            elif data.strip() == "CONFIRMED_ACCIDENT":
                await db.alerts.insert_one({"device_id": device_id, "status": "confirmed_accident", "timestamp": now_iso()})
                await iot_manager.save_snapshot(device_id)
                if device_id in iot_manager.camera_sources:
                    await iot_manager.camera_sources[device_id].send_text("WAKE_UP")
            else:
                # Buffer the normal telemetry data
                iot_manager.add_telemetry(device_id, data)
    except WebSocketDisconnect:
        iot_manager.disconnect_device(device_id)

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


# ---- Auth Endpoints -------------------------------------------------------
@api.post("/auth/register")
async def register(user_in: UserRegister):
    existing = await db.users.find_one({"email": user_in.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    hashed_password = get_password_hash(user_in.password)
    new_user = User(
        email=user_in.email,
        hashed_password=hashed_password,
        name=user_in.name
    )
    result = await db.users.insert_one(new_user.model_dump(exclude={"id"}))
    return {"message": "User registered successfully", "user_id": str(result.inserted_id)}

@api.post("/auth/login")
async def login(user_in: UserLogin):
    user = await db.users.find_one({"email": user_in.email})
    if not user or not verify_password(user_in.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["user_id"]}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer", "user_id": user["user_id"], "name": user["name"]}


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
    return [AlertRecord.from_mongo(d).model_dump() for d in docs]


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
async def get_latest_image(device_id: str):
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
async def list_commands(device_id: Optional[str] = None, limit: int = 50):
    q = {"device_id": device_id} if device_id else {}
    cursor = db.commands.find(q).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [CommandLog.from_mongo(d).model_dump() for d in docs]


@api.post("/commands", status_code=status.HTTP_201_CREATED)
async def log_command(body: CommandIn):
    doc = CommandLog(**body.model_dump()).model_dump(exclude={"id"})
    result = await db.commands.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
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


# ---- Simulator (for hackathon demo without hardware) ----------------------
class SimulatorProfile(BaseModel):
    scenario: str = "cruise"


@api.post("/simulate")
async def simulate(profile: SimulatorProfile):
    import random
    lat, lon = 12.9716, 77.5946
    if profile.scenario == "cruise":
        t = TelemetryFrame(
            ax=random.uniform(-0.1, 0.1), ay=random.uniform(-0.1, 0.1),
            az=1.0 + random.uniform(-0.05, 0.05),
            gx=random.uniform(-2, 2), gy=random.uniform(-2, 2), gz=random.uniform(-2, 2),
            speed_kmh=random.uniform(30, 60), latitude=lat, longitude=lon,
            battery=random.uniform(80, 100),
            lean_angle=random.uniform(-8, 8), pitch=random.uniform(-3, 3),
            roll=random.uniform(-5, 5), timestamp=now_iso(),
        )
    elif profile.scenario == "hard_brake":
        t = TelemetryFrame(ax=-1.8, ay=0.2, az=0.9, gx=-5, gy=3, gz=1,
                           speed_kmh=45, latitude=lat, longitude=lon, battery=88,
                           lean_angle=5, pitch=-6, roll=2, timestamp=now_iso())
    elif profile.scenario == "crash":
        t = TelemetryFrame(ax=2.9, ay=1.8, az=0.5, gx=180, gy=90, gz=45,
                           speed_kmh=52, latitude=lat, longitude=lon, battery=76,
                           lean_angle=68, pitch=-45, roll=72, timestamp=now_iso())
    elif profile.scenario == "freefall":
        t = TelemetryFrame(ax=0.05, ay=0.05, az=0.1, gx=10, gy=20, gz=15,
                           speed_kmh=30, latitude=lat, longitude=lon, battery=68,
                           lean_angle=25, pitch=-20, roll=15, timestamp=now_iso())
    else:
        raise HTTPException(400, "Unknown scenario")
    rule = evaluate_rules(t)
    return {"telemetry": t.model_dump(), "rule": rule.model_dump()}


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
        status="sent",
    ).model_dump(exclude={"id"})
    await db.commands.insert_one(doc)
    return {"status": "success", "message": f"SOS cancelled for {body.device_id}"}


@app.get("/")
async def root():
    return {"service": "NovaShields Mobile SOS", "docs": "/docs"}