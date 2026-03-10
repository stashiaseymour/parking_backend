from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
import time, uuid, os, bcrypt, jwt, secrets

app = FastAPI(title="Smart Parking Backend")

# =====================================================
# MongoDB
# =====================================================
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI not set")

client = MongoClient(MONGO_URI)
db = client["smart_parking"]

parking_collection  = db["parking_spaces"]
history_collection  = db["history"]
sessions_collection = db["parking_sessions"]
users_collection    = db["users"]

# =====================================================
# CORS
# =====================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================================================
# JWT Config
# =====================================================
JWT_SECRET = os.environ.get("JWT_SECRET", "parksmart-secret-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24

security = HTTPBearer(auto_error=False)

# =====================================================
# Models
# =====================================================
class SensorUpdate(BaseModel):
    node_id: str
    sensor_status: str
    distance_cm: float
    timestamp: int

class ReservationRequest(BaseModel):
    node_id: str
    reserved: bool
    duration_seconds: int = 3600  # default 1 hour

class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

# =====================================================
# Constants
# =====================================================
RESERVATION_DURATION = 300  # 5 minutes

# =====================================================
# Time Helpers
# =====================================================
def now_ts():
    return int(time.time())

def ts_to_readable(ts: int | None):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

def start_of_today():
    d = datetime.now(timezone.utc)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())

def start_of_week():
    d = datetime.now(timezone.utc)
    d = d - timedelta(days=d.weekday())
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())

# =====================================================
# JWT Helpers
# =====================================================
def create_token(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired — please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(401, "Not authenticated")
    return decode_token(credentials.credentials)

def require_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(401, "Not authenticated")
    payload = decode_token(credentials.credentials)
    if payload.get("role") != "admin":
        raise HTTPException(403, "Admin access required")
    return payload

# =====================================================
# Default Node
# =====================================================
def create_default_node(node_id: str):
    return {
        "node_id": node_id,
        "sensor_status": "FREE",
        "distance_cm": 0.0,
        "reserved": False,
        "violation": False,
        "reservation_start": None,
        "reservation_expiry": None,
        "reserved_by": None,
        "admin_mode": "NORMAL",
        "qr_token": None,
        "checked_in": False,
        "active_session_start": None,
        "last_update": now_ts()
    }

# =====================================================
# Core Logic
# =====================================================
def compute_final(node):
    if node["admin_mode"] == "MAINTENANCE":
        return "MAINTENANCE"
    if node["reserved"] and not node["checked_in"] and node["sensor_status"] == "OCCUPIED":
        return "VIOLATION"
    if node["reserved"]:
        return "RESERVED"
    return node["sensor_status"]

def enforce_expiry(node):
    if node["reserved"] and node["reservation_expiry"]:
        if now_ts() >= node["reservation_expiry"]:
            parking_collection.update_one(
                {"node_id": node["node_id"]},
                {"$set": {
                    "reserved": False,
                    "reservation_start": None,
                    "reservation_expiry": None,
                    "reserved_by": None,
                    "qr_token": None,
                    "violation": False,
                    "checked_in": False,
                    "last_update": now_ts()
                }}
            )

# =====================================================
# AUTH ENDPOINTS
# =====================================================

@app.post("/api/auth/register")
def register(req: RegisterRequest):
    # Check email already exists
    if users_collection.find_one({"email": req.email.lower().strip()}):
        raise HTTPException(400, "An account with this email already exists")

    # Validate password length
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    # Hash password
    hashed = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()

    user_id = str(uuid.uuid4())
    user = {
        "user_id": user_id,
        "name": req.name.strip(),
        "email": req.email.lower().strip(),
        "password_hash": hashed,
        "role": "user",   # default role
        "created_at": now_ts(),
        "reset_token": None,
        "reset_token_expiry": None
    }

    users_collection.insert_one(user)

    token = create_token(user_id, user["email"], user["role"])

    return {
        "token": token,
        "user": {
            "user_id": user_id,
            "name": user["name"],
            "email": user["email"],
            "role": user["role"]
        }
    }


@app.post("/api/auth/login")
def login(req: LoginRequest):
    user = users_collection.find_one({"email": req.email.lower().strip()})

    if not user:
        raise HTTPException(401, "No account found with this email")

    if not bcrypt.checkpw(req.password.encode(), user["password_hash"].encode()):
        raise HTTPException(401, "Incorrect password")

    token = create_token(user["user_id"], user["email"], user["role"])

    return {
        "token": token,
        "user": {
            "user_id": user["user_id"],
            "name": user["name"],
            "email": user["email"],
            "role": user["role"]
        }
    }


@app.get("/api/auth/me")
def get_me(current_user: dict = Depends(get_current_user)):
    user = users_collection.find_one(
        {"user_id": current_user["sub"]},
        {"_id": 0, "password_hash": 0, "reset_token": 0, "reset_token_expiry": 0}
    )
    if not user:
        raise HTTPException(404, "User not found")
    return user


@app.post("/api/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest):
    user = users_collection.find_one({"email": req.email.lower().strip()})

    # Always return success to prevent email enumeration
    if not user:
        return {"status": "ok", "message": "If that email exists, a reset link has been sent"}

    reset_token = secrets.token_urlsafe(32)
    expiry = now_ts() + 3600  # 1 hour

    users_collection.update_one(
        {"email": req.email.lower().strip()},
        {"$set": {
            "reset_token": reset_token,
            "reset_token_expiry": expiry
        }}
    )

    # In production you would send an email here
    # For now we return the token directly for testing
    return {
        "status": "ok",
        "message": "Password reset token generated",
        "reset_token": reset_token  # Remove this in production, send via email instead
    }


@app.post("/api/auth/reset-password")
def reset_password(req: ResetPasswordRequest):
    user = users_collection.find_one({
        "reset_token": req.token,
        "reset_token_expiry": {"$gt": now_ts()}
    })

    if not user:
        raise HTTPException(400, "Invalid or expired reset token")

    if len(req.new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    hashed = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()

    users_collection.update_one(
        {"reset_token": req.token},
        {"$set": {
            "password_hash": hashed,
            "reset_token": None,
            "reset_token_expiry": None
        }}
    )

    return {"status": "ok", "message": "Password reset successfully"}


# =====================================================
# ADMIN: Manage Users
# =====================================================
@app.get("/api/admin/users")
def list_users(admin: dict = Depends(require_admin)):
    users = list(users_collection.find(
        {},
        {"_id": 0, "password_hash": 0, "reset_token": 0, "reset_token_expiry": 0}
    ))
    return users


@app.post("/api/admin/users/{user_id}/role")
def set_user_role(user_id: str, role: str, admin: dict = Depends(require_admin)):
    if role not in ["user", "admin", "display"]:
        raise HTTPException(400, "Invalid role. Must be: user, admin, display")
    result = users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"role": role}}
    )
    if result.matched_count == 0:
        raise HTTPException(404, "User not found")
    return {"status": "ok", "user_id": user_id, "new_role": role}


# =====================================================
# Sensor Update (Gateway)
# =====================================================
@app.post("/api/node/update")
def update_node(data: SensorUpdate):
    node = parking_collection.find_one({"node_id": data.node_id})
    if not node:
        node = create_default_node(data.node_id)
        parking_collection.insert_one(node)
        node = parking_collection.find_one({"node_id": data.node_id})

    enforce_expiry(node)
    node = parking_collection.find_one({"node_id": data.node_id})

    prev = node["sensor_status"]
    session_update = {}

    if prev == "FREE" and data.sensor_status == "OCCUPIED":
        session_update["active_session_start"] = now_ts()

    if prev == "OCCUPIED" and data.sensor_status == "FREE":
        if node.get("active_session_start"):
            sessions_collection.insert_one({
                "node_id": data.node_id,
                "start_time": node["active_session_start"],
                "end_time": now_ts(),
                "duration_seconds": now_ts() - node["active_session_start"]
            })
        session_update["active_session_start"] = None

    is_violation = (
        node["admin_mode"] == "NORMAL"
        and node["reserved"]
        and not node["checked_in"]
        and data.sensor_status == "OCCUPIED"
    )

    update_fields = {
        "sensor_status": data.sensor_status,
        "distance_cm": data.distance_cm,
        "last_update": now_ts(),
        "violation": is_violation,
        **session_update
    }

    parking_collection.update_one({"node_id": data.node_id}, {"$set": update_fields})

    history_collection.insert_one({
        "node_id": data.node_id,
        "sensor_status": data.sensor_status,
        "distance_cm": data.distance_cm,
        "timestamp": now_ts()
    })

    return {"status": "ok"}


# =====================================================
# Reservation
# =====================================================
@app.post("/api/reserve")
def reserve_space(req: ReservationRequest, credentials: HTTPAuthorizationCredentials = Depends(security)):
    # Auth optional — works with or without login
    user_id = None
    if credentials:
        try:
            payload = decode_token(credentials.credentials)
            user_id = payload.get("sub")
        except:
            pass

    node = parking_collection.find_one({"node_id": req.node_id})
    if not node:
        node = create_default_node(req.node_id)
        parking_collection.insert_one(node)

    if node["admin_mode"] == "MAINTENANCE":
        raise HTTPException(400, "Node in maintenance")

    if req.reserved:
        parking_collection.update_one(
            {"node_id": req.node_id},
            {"$set": {
                "reserved": True,
                "reservation_start": now_ts(),
                "reservation_expiry": now_ts() + req.duration_seconds,
                "reserved_by": user_id,
                "qr_token": str(uuid.uuid4()),
                "checked_in": False,
                "last_update": now_ts()
            }}
        )
    else:
        parking_collection.update_one(
            {"node_id": req.node_id},
            {"$set": {
                "reserved": False,
                "reservation_start": None,
                "reservation_expiry": None,
                "reserved_by": None,
                "qr_token": None,
                "violation": False,
                "checked_in": False,
                "last_update": now_ts()
            }}
        )

    return {"status": "ok"}


# =====================================================
# STATUS
# =====================================================
@app.get("/api/parking/status")
def get_status():
    out = {}
    STALE_THRESHOLD = 300  # 5 minutes

    for node in parking_collection.find():
        enforce_expiry(node)
        is_stale = (now_ts() - node["last_update"]) > STALE_THRESHOLD

        out[node["node_id"]] = {
            "final_status": "OFFLINE" if is_stale else compute_final(node),
            "sensor_status": node["sensor_status"],
            "distance_cm": node["distance_cm"],
            "reserved": node["reserved"],
            "violation": node["violation"],
            "admin_mode": node["admin_mode"],
            "qr_token": node.get("qr_token"),
            "reservation_expiry": node.get("reservation_expiry"),
            "server_timestamp": node["last_update"],
            "last_update_readable": ts_to_readable(node["last_update"]),
            "online": not is_stale
        }
    return out


# =====================================================
# GATEWAY BOOTSTRAP
# =====================================================
@app.get("/api/nodes")
def get_nodes():
    return [
        node["node_id"]
        for node in parking_collection.find({}, {"_id": 0, "node_id": 1})
    ]


# =====================================================
# SEED KNOWN NODES
# =====================================================
@app.post("/api/admin/seed-nodes")
def seed_nodes():
    known_nodes = ["A1", "A2", "A3", "O1"]
    for node_id in known_nodes:
        if not parking_collection.find_one({"node_id": node_id}):
            parking_collection.insert_one(create_default_node(node_id))
    return {"status": "ok", "nodes": known_nodes}


# =====================================================
# ADMIN CONTROLS
# =====================================================
@app.post("/api/admin/maintenance/{node_id}")
def admin_maintenance(node_id: str):
    parking_collection.update_one(
        {"node_id": node_id},
        {"$set": {
            "admin_mode": "MAINTENANCE",
            "reserved": False,
            "qr_token": None,
            "violation": False,
            "last_update": now_ts()
        }}
    )
    return {"status": "ok"}


@app.post("/api/admin/resume/{node_id}")
def admin_resume(node_id: str):
    parking_collection.update_one(
        {"node_id": node_id},
        {"$set": {"admin_mode": "NORMAL", "last_update": now_ts()}}
    )
    return {"status": "ok"}


# =====================================================
# ADMIN ANALYTICS
# =====================================================
@app.get("/api/admin/analytics/summary")
def usage_summary(range: str | None = None):
    match = {}
    if range == "today":
        match["end_time"] = {"$gte": start_of_today()}
    elif range == "week":
        match["end_time"] = {"$gte": start_of_week()}

    pipeline = []
    if match:
        pipeline.append({"$match": match})

    pipeline.append({"$group": {
        "_id": None,
        "total_sessions": {"$sum": 1},
        "total_time": {"$sum": "$duration_seconds"},
        "avg_time": {"$avg": "$duration_seconds"}
    }})

    r = list(sessions_collection.aggregate(pipeline))
    if not r:
        return {"total_sessions": 0, "total_time_seconds": 0, "average_time_seconds": 0}

    r = r[0]
    return {
        "total_sessions": r["total_sessions"],
        "total_time_seconds": r["total_time"],
        "average_time_seconds": round(r["avg_time"], 1)
    }


@app.get("/api/admin/analytics/usage-by-node")
def usage_by_node(range: str | None = None):
    match = {}
    if range == "today":
        match["end_time"] = {"$gte": start_of_today()}
    elif range == "week":
        match["end_time"] = {"$gte": start_of_week()}

    pipeline = []
    if match:
        pipeline.append({"$match": match})

    pipeline.extend([
        {"$group": {
            "_id": "$node_id",
            "total_sessions": {"$sum": 1},
            "total_time": {"$sum": "$duration_seconds"},
            "avg_time": {"$avg": "$duration_seconds"}
        }},
        {"$project": {
            "_id": 0,
            "node_id": "$_id",
            "total_sessions": 1,
            "total_time_seconds": "$total_time",
            "average_time_seconds": {"$round": ["$avg_time", 1]}
        }}
    ])

    return list(sessions_collection.aggregate(pipeline))


@app.get("/api/admin/analytics/recent-sessions")
def recent_sessions(limit: int = 10, range: str | None = None):
    query = {}
    if range == "today":
        query["end_time"] = {"$gte": start_of_today()}
    elif range == "week":
        query["end_time"] = {"$gte": start_of_week()}

    out = []
    for s in (
        sessions_collection
        .find(query, {"_id": 0})
        .sort("end_time", -1)
        .limit(limit)
    ):
        s["start_time_readable"] = ts_to_readable(s["start_time"])
        s["end_time_readable"]   = ts_to_readable(s["end_time"])
        out.append(s)

    return out