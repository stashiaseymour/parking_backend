from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import time
import uuid
import os
from pymongo import MongoClient
from datetime import datetime, timezone, timedelta

app = FastAPI(title="Smart Parking Backend")

# -----------------------------
# MongoDB Setup
# -----------------------------
MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable not set")

mongo_client = MongoClient(MONGO_URI)
db = mongo_client["smart_parking"]

parking_collection = db["parking_spaces"]
history_collection = db["history"]
sessions_collection = db["parking_sessions"]

# -----------------------------
# CORS
# -----------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Models
# -----------------------------
class SensorUpdate(BaseModel):
    node_id: str
    sensor_status: str
    distance_cm: float
    timestamp: int

class ReservationRequest(BaseModel):
    node_id: str
    reserved: bool

# -----------------------------
# Constants
# -----------------------------
RESERVATION_DURATION = 30  # seconds (testing)

# -----------------------------
# Time Helpers
# -----------------------------
def ts_to_readable(ts: int):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def start_of_today():
    now = datetime.now(timezone.utc)
    return int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp())

def start_of_week():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=now.weekday())
    return int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())

# -----------------------------
# Default Node Template
# -----------------------------
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
        "last_update": int(time.time()),
        "qr_token": None,
        "checked_in": False,
        "checkin_time": None,
        "active_session_start": None
    }

# -----------------------------
# Final Decision Logic
# -----------------------------
def compute_final(sensor_status, reserved, admin_mode, checked_in):
    if admin_mode == "MAINTENANCE":
        return "MAINTENANCE"
    if reserved and not checked_in and sensor_status == "OCCUPIED":
        return "VIOLATION"
    if reserved:
        return "RESERVED"
    return "CLEAR"

# -----------------------------
# Expiry Enforcement
# -----------------------------
def enforce_expiry(node):
    now = int(time.time())
    if node["reserved"] and node["reservation_expiry"]:
        if now >= node["reservation_expiry"]:
            parking_collection.update_one(
                {"node_id": node["node_id"]},
                {"$set": {
                    "reserved": False,
                    "reservation_start": None,
                    "reservation_expiry": None,
                    "violation": False,
                    "qr_token": None,
                    "checked_in": False,
                    "checkin_time": None,
                    "last_update": now
                }}
            )

# -----------------------------
# Sensor Update (Gateway)
# -----------------------------
@app.post("/api/node/update")
def update_node(data: SensorUpdate):
    now = int(time.time())

    node = parking_collection.find_one({"node_id": data.node_id})
    if not node:
        node = create_default_node(data.node_id)
        parking_collection.insert_one(node)

    previous_status = node["sensor_status"]
    enforce_expiry(node)

    # SESSION START
    if previous_status == "FREE" and data.sensor_status == "OCCUPIED":
        node["active_session_start"] = now

    # SESSION END
    if previous_status == "OCCUPIED" and data.sensor_status == "FREE":
        if node.get("active_session_start"):
            duration = now - node["active_session_start"]
            sessions_collection.insert_one({
                "node_id": data.node_id,
                "start_time": node["active_session_start"],
                "end_time": now,
                "duration_seconds": duration
            })
        node["active_session_start"] = None

    node.update({
        "sensor_status": data.sensor_status,
        "distance_cm": data.distance_cm,
        "last_update": now
    })

    node["violation"] = (
        node["admin_mode"] == "NORMAL"
        and node["reserved"]
        and not node["checked_in"]
        and data.sensor_status == "OCCUPIED"
    )

    parking_collection.update_one(
        {"node_id": data.node_id},
        {"$set": node}
    )

    history_collection.insert_one({
        "node_id": data.node_id,
        "sensor_status": data.sensor_status,
        "distance_cm": data.distance_cm,
        "timestamp": now
    })

    return {"status": "ok"}

# -----------------------------
# Reservation
# -----------------------------
@app.post("/api/reserve")
def reserve_space(req: ReservationRequest):
    now = int(time.time())

    node = parking_collection.find_one({"node_id": req.node_id})
    if not node:
        node = create_default_node(req.node_id)
        parking_collection.insert_one(node)

    if node["admin_mode"] == "MAINTENANCE":
        raise HTTPException(status_code=400, detail="Node in maintenance")

    if req.reserved:
        parking_collection.update_one(
            {"node_id": req.node_id},
            {"$set": {
                "reserved": True,
                "reservation_start": now,
                "reservation_expiry": now + RESERVATION_DURATION,
                "qr_token": str(uuid.uuid4()),
                "checked_in": False,
                "last_update": now
            }}
        )
    else:
        parking_collection.update_one(
            {"node_id": req.node_id},
            {"$set": {
                "reserved": False,
                "reservation_start": None,
                "reservation_expiry": None,
                "violation": False,
                "qr_token": None,
                "checked_in": False,
                "last_update": now
            }}
        )

    return {"status": "ok"}

# -----------------------------
# Parking Status
# -----------------------------
@app.get("/api/parking/status")
def get_status():
    out = {}
    for node in parking_collection.find():
        enforce_expiry(node)

        final_status = compute_final(
            node["sensor_status"],
            node["reserved"],
            node["admin_mode"],
            node["checked_in"]
        )

        out[node["node_id"]] = {
            "final_status": final_status,
            "sensor_status": node["sensor_status"],
            "distance_cm": node["distance_cm"],
            "reserved": node["reserved"],
            "violation": node["violation"],
            "admin_mode": node["admin_mode"],
            "last_update_readable": ts_to_readable(node["last_update"])
        }
    return out

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
        {
            "$group": {
                "_id": "$node_id",
                "total_sessions": {"$sum": 1},
                "total_time": {"$sum": "$duration_seconds"},
                "avg_time": {"$avg": "$duration_seconds"}
            }
        },
        {
            "$project": {
                "_id": 0,
                "node_id": "$_id",
                "total_sessions": 1,
                "total_time_seconds": "$total_time",
                "average_time_seconds": {"$round": ["$avg_time", 1]}
            }
        }
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

    pipeline.append({
        "$group": {
            "_id": None,
            "total_sessions": {"$sum": 1},
            "total_time": {"$sum": "$duration_seconds"},
            "avg_time": {"$avg": "$duration_seconds"}
        }
    })

    result = list(sessions_collection.aggregate(pipeline))
    if not result:
        return {"total_sessions": 0, "total_time_seconds": 0, "average_time_seconds": 0}

    r = result[0]
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

    sessions = sessions_collection.find(query, {"_id": 0}).sort("end_time", -1).limit(limit)

    out = []
    for s in sessions:
        s["start_time_readable"] = ts_to_readable(s["start_time"])
        s["end_time_readable"] = ts_to_readable(s["end_time"])
        out.append(s)

    return out