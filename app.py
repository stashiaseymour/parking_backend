from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import time
import uuid
import os
from pymongo import MongoClient

app = FastAPI(title="Smart Parking Backend")

# -----------------------------
# MongoDB Setup (Render-safe)
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
# Initialize Parking Spaces
# -----------------------------
def initialize_parking_spaces():
    for node_id in ["A1", "A2", "A3"]:
        if not parking_collection.find_one({"node_id": node_id}):
            parking_collection.insert_one(create_default_node(node_id))

initialize_parking_spaces()

# -----------------------------
# Final Decision Logic (SOURCE OF TRUTH)
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
            return True
    return False

# -----------------------------
# Sensor Update (Gateway + Sessions)
# -----------------------------
@app.post("/api/node/update")
def update_node(data: SensorUpdate):
    now = int(time.time())

    node = parking_collection.find_one({"node_id": data.node_id})
    if not node:
        node = create_default_node(data.node_id)

    previous_status = node["sensor_status"]

    enforce_expiry(node)

    # ---- SESSION START ----
    if previous_status == "FREE" and data.sensor_status == "OCCUPIED":
        node["active_session_start"] = now
        print(f"[SESSION START] {data.node_id}")

    # ---- SESSION END ----
    if previous_status == "OCCUPIED" and data.sensor_status == "FREE":
        if node.get("active_session_start"):
            duration = now - node["active_session_start"]

            sessions_collection.insert_one({
                "node_id": data.node_id,
                "start_time": node["active_session_start"],
                "end_time": now,
                "duration_seconds": duration
            })

            print(f"[SESSION END] {data.node_id} | {duration}s")

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
        {"$set": node},
        upsert=True
    )

    history_collection.insert_one({
        "node_id": data.node_id,
        "sensor_status": data.sensor_status,
        "distance_cm": data.distance_cm,
        "timestamp": now
    })

    return {"status": "ok"}

# -----------------------------
# Reservation (Website)
# -----------------------------
@app.post("/api/reserve")
def reserve_space(req: ReservationRequest):
    node = parking_collection.find_one({"node_id": req.node_id})
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    now = int(time.time())

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
# ADMIN: Maintenance ON
# -----------------------------
@app.post("/api/admin/maintenance/{node_id}")
def set_maintenance(node_id: str):
    node = parking_collection.find_one({"node_id": node_id})
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    parking_collection.update_one(
        {"node_id": node_id},
        {"$set": {
            "admin_mode": "MAINTENANCE",
            "reserved": False,
            "violation": False,
            "reservation_start": None,
            "reservation_expiry": None,
            "qr_token": None,
            "checked_in": False,
            "checkin_time": None,
            "last_update": int(time.time())
        }}
    )

    return {"status": "ok"}

# -----------------------------
# ADMIN: Resume NORMAL
# -----------------------------
@app.post("/api/admin/resume/{node_id}")
def resume_normal(node_id: str):
    node = parking_collection.find_one({"node_id": node_id})
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    parking_collection.update_one(
        {"node_id": node_id},
        {"$set": {
            "admin_mode": "NORMAL",
            "violation": False,
            "last_update": int(time.time())
        }}
    )

    return {"status": "ok"}

# -----------------------------
# Parking Status (AUTHORITATIVE)
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
            "checked_in": node["checked_in"],
            "violation": node["violation"],
            "reservation_expiry": node["reservation_expiry"],
            "admin_mode": node["admin_mode"],
            "server_timestamp": node["last_update"]
        }

    return out