from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict
import time
import uuid

app = FastAPI(title="Smart Parking Backend")

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
    sensor_status: str   # FREE / OCCUPIED
    distance_cm: float
    timestamp: int

class ReservationRequest(BaseModel):
    node_id: str
    reserved: bool

class QRCheckinRequest(BaseModel):
    node_id: str
    qr_token: str

# -----------------------------
# Storage (RAM)
# -----------------------------
parking_states: Dict[str, dict] = {}
RESERVATION_DURATION = 30  # seconds (testing)

# -----------------------------
# INITIAL PARKING SPACES
# -----------------------------
def create_default_node(node_id: str):
    return {
        "sensor_status": "FREE",
        "distance_cm": 0.0,
        "reserved": False,
        "violation": False,
        "reservation_start": None,
        "reservation_expiry": None,
        "admin_mode": "NORMAL",
        "last_update": None,
        "qr_token": None,
        "checked_in": False,
        "checkin_time": None,
        "final_status": "CLEAR"
    }

def initialize_parking_spaces():
    for node_id in ["A1", "A2", "A3"]:
        if node_id not in parking_states:
            parking_states[node_id] = create_default_node(node_id)

# Run once at startup
initialize_parking_spaces()

# -----------------------------
# FINAL DECISION LOGIC
# -----------------------------
def compute_final(sensor_status: str, reserved: bool, admin_mode: str, checked_in: bool) -> str:
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
def enforce_expiry(node: dict):
    now = int(time.time())
    if node["reserved"] and node["reservation_expiry"]:
        if now >= node["reservation_expiry"]:
            node.update({
                "reserved": False,
                "reservation_start": None,
                "reservation_expiry": None,
                "violation": False,
                "qr_token": None,
                "checked_in": False,
                "checkin_time": None
            })

# -----------------------------
# Sensor Update (from gateway)
# -----------------------------
@app.post("/api/node/update")
def update_node(data: SensorUpdate):
    now = int(time.time())

    # ✅ NEVER allow empty dicts
    if data.node_id not in parking_states:
        parking_states[data.node_id] = create_default_node(data.node_id)

    node = parking_states[data.node_id]

    enforce_expiry(node)

    node["sensor_status"] = data.sensor_status
    node["distance_cm"] = data.distance_cm
    node["last_update"] = now

    node["violation"] = (
        node["admin_mode"] == "NORMAL"
        and node["reserved"]
        and not node["checked_in"]
        and data.sensor_status == "OCCUPIED"
    )

    node["final_status"] = compute_final(
        data.sensor_status,
        node["reserved"],
        node["admin_mode"],
        node["checked_in"]
    )

    print(
        f"[UPDATE] {data.node_id} | "
        f"SENSOR={data.sensor_status} | "
        f"RESERVED={node['reserved']} | "
        f"CHECKED_IN={node['checked_in']} | "
        f"FINAL={node['final_status']}"
    )

    return {"status": "ok"}

# -----------------------------
# Reservation (from website)
# -----------------------------
@app.post("/api/reserve")
def reserve_space(req: ReservationRequest):
    if req.node_id not in parking_states:
        raise HTTPException(status_code=404, detail="Node not found")

    node = parking_states[req.node_id]
    now = int(time.time())

    # ✅ Reservation counts as activity
    node["last_update"] = now

    if node["admin_mode"] == "MAINTENANCE":
        raise HTTPException(status_code=400, detail="Node in maintenance")

    if req.reserved:
        node.update({
            "reserved": True,
            "reservation_start": now,
            "reservation_expiry": now + RESERVATION_DURATION,
            "qr_token": str(uuid.uuid4()),
            "checked_in": False,
            "checkin_time": None
        })
    else:
        node.update({
            "reserved": False,
            "reservation_start": None,
            "reservation_expiry": None,
            "violation": False,
            "qr_token": None,
            "checked_in": False,
            "checkin_time": None
        })

    node["final_status"] = compute_final(
        node["sensor_status"],
        node["reserved"],
        node["admin_mode"],
        node["checked_in"]
    )

    print(f"[RESERVE] {req.node_id} | RESERVED={node['reserved']}")

    return {
        "status": "ok",
        "qr_token": node["qr_token"],
        "expires_at": node["reservation_expiry"]
    }

# -----------------------------
# QR CHECK-IN
# -----------------------------
@app.post("/api/checkin")
def qr_checkin(req: QRCheckinRequest):
    node = parking_states.get(req.node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    enforce_expiry(node)

    if not node["reserved"]:
        raise HTTPException(status_code=400, detail="No active reservation")

    if node["checked_in"]:
        raise HTTPException(status_code=400, detail="Already checked in")

    if req.qr_token != node["qr_token"]:
        raise HTTPException(status_code=401, detail="Invalid QR code")

    node["checked_in"] = True
    node["checkin_time"] = int(time.time())
    node["last_update"] = int(time.time())

    node["final_status"] = compute_final(
        node["sensor_status"],
        node["reserved"],
        node["admin_mode"],
        node["checked_in"]
    )

    print(f"[CHECK-IN] {req.node_id} → SUCCESS")

    return {"status": "checked_in"}

# -----------------------------
# ADMIN: Maintenance
# -----------------------------
@app.post("/api/admin/maintenance/{node_id}")
def set_maintenance(node_id: str):
    node = parking_states.get(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    node.update({
        "admin_mode": "MAINTENANCE",
        "reserved": False,
        "violation": False,
        "reservation_start": None,
        "reservation_expiry": None,
        "qr_token": None,
        "checked_in": False,
        "checkin_time": None,
        "final_status": "MAINTENANCE",
        "last_update": int(time.time())
    })

    print(f"[ADMIN] {node_id} → MAINTENANCE")
    return {"status": "ok"}

# -----------------------------
# ADMIN: Resume NORMAL
# -----------------------------
@app.post("/api/admin/resume/{node_id}")
def resume_normal(node_id: str):
    node = parking_states.get(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    node["admin_mode"] = "NORMAL"
    node["violation"] = False
    node["last_update"] = int(time.time())

    node["final_status"] = compute_final(
        node["sensor_status"],
        node["reserved"],
        node["admin_mode"],
        node["checked_in"]
    )

    print(f"[ADMIN] {node_id} → NORMAL")
    return {"status": "ok"}

# -----------------------------
# Status (gateway + dashboard)
# -----------------------------
@app.get("/api/parking/status")
def get_status():
    out = {}

    for node_id, node in parking_states.items():
        enforce_expiry(node)

        out[node_id] = {
            "final_status": compute_final(
                node["sensor_status"],
                node["reserved"],
                node["admin_mode"],
                node["checked_in"]
            ),
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