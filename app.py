from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
import time, uuid, os

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

# =====================================================
# Constants
# =====================================================
RESERVATION_DURATION = 30  # seconds (testing)

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
                    "qr_token": None,
                    "violation": False,
                    "checked_in": False,
                    "last_update": now_ts()
                }}
            )

# =====================================================
# Sensor Update (Gateway)
# =====================================================
@app.post("/api/node/update")
def update_node(data: SensorUpdate):
    node = parking_collection.find_one({"node_id": data.node_id})
    if not node:
        node = create_default_node(data.node_id)
        parking_collection.insert_one(node)

    enforce_expiry(node)

    prev = node["sensor_status"]

    # Session start
    if prev == "FREE" and data.sensor_status == "OCCUPIED":
        node["active_session_start"] = now_ts()

    # Session end
    if prev == "OCCUPIED" and data.sensor_status == "FREE":
        if node.get("active_session_start"):
            sessions_collection.insert_one({
                "node_id": data.node_id,
                "start_time": node["active_session_start"],
                "end_time": now_ts(),
                "duration_seconds": now_ts() - node["active_session_start"]
            })
        node["active_session_start"] = None

    node.update({
        "sensor_status": data.sensor_status,
        "distance_cm": data.distance_cm,
        "last_update": now_ts()
    })

    node["violation"] = (
        node["admin_mode"] == "NORMAL"
        and node["reserved"]
        and not node["checked_in"]
        and data.sensor_status == "OCCUPIED"
    )

    parking_collection.update_one({"node_id": data.node_id}, {"$set": node})

    history_collection.insert_one({
        "node_id": data.node_id,
        "sensor_status": data.sensor_status,
        "distance_cm": data.distance_cm,
        "timestamp": now_ts()
    })

    return {"status": "ok"}

# =====================================================
# Reservation (QR generation)
# =====================================================
@app.post("/api/reserve")
def reserve_space(req: ReservationRequest):
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
                "reservation_expiry": now_ts() + RESERVATION_DURATION,
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
                "qr_token": None,
                "violation": False,
                "checked_in": False,
                "last_update": now_ts()
            }}
        )

    return {"status": "ok"}

# =====================================================
# STATUS (User + Admin)
# =====================================================
@app.get("/api/parking/status")
def get_status():
    out = {}
    for node in parking_collection.find():
        enforce_expiry(node)

        out[node["node_id"]] = {
            "final_status": compute_final(node),
            "sensor_status": node["sensor_status"],
            "distance_cm": node["distance_cm"],
            "reserved": node["reserved"],
            "violation": node["violation"],
            "admin_mode": node["admin_mode"],

            # USER PAGE NEEDS THESE
            "qr_token": node.get("qr_token"),
            "reservation_expiry": node.get("reservation_expiry"),

            # TIME
            "server_timestamp": node["last_update"],
            "last_update_readable": ts_to_readable(node["last_update"])
        }
    return out

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
        {"$set": {
            "admin_mode": "NORMAL",
            "last_update": now_ts()
        }}
    )
    return {"status": "ok"}

# =====================================================
# ADMIN ANALYTICS
# =====================================================
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
        s["end_time_readable"] = ts_to_readable(s["end_time"])
        out.append(s)

    return out