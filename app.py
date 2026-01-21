from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict
import time

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

# -----------------------------
# Storage
# -----------------------------
parking_states: Dict[str, dict] = {}
RESERVATION_DURATION = 30  # seconds (testing)

# -----------------------------
# FINAL DECISION LOGIC (FIXED)
# -----------------------------
def compute_final(sensor_status: str, reserved: bool, admin_mode: str) -> str:
    # 1️⃣ Maintenance overrides everything
    if admin_mode == "MAINTENANCE":
        return "MAINTENANCE"

    # 2️⃣ Violation = reserved + occupied
    if reserved and sensor_status == "OCCUPIED":
        return "VIOLATION"

    # 3️⃣ Reserved but not occupied
    if reserved:
        return "RESERVED"

    # 4️⃣ No override
    return "CLEAR"

# -----------------------------
# Expiry Enforcement
# -----------------------------
def enforce_expiry(node: dict):
    now = int(time.time())
    if node["reserved"] and node["reservation_expiry"]:
        if now >= node["reservation_expiry"]:
            node["reserved"] = False
            node["reservation_start"] = None
            node["reservation_expiry"] = None
            node["violation"] = False

# -----------------------------
# Sensor Update (from gateway)
# -----------------------------
@app.post("/api/node/update")
def update_node(data: SensorUpdate):
    now = int(time.time())

    node = parking_states.setdefault(data.node_id, {
        "sensor_status": "FREE",
        "distance_cm": 0.0,
        "reserved": False,
        "violation": False,
        "reservation_start": None,
        "reservation_expiry": None,
        "admin_mode": "NORMAL",
        "last_update": None
    })

    enforce_expiry(node)

    node["sensor_status"] = data.sensor_status
    node["distance_cm"] = data.distance_cm
    node["last_update"] = now

    # Violation logic (metadata)
    node["violation"] = (
        node["admin_mode"] == "NORMAL"
        and node["reserved"]
        and data.sensor_status == "OCCUPIED"
    )

    node["final_status"] = compute_final(
        data.sensor_status,
        node["reserved"],
        node["admin_mode"]
    )

    print(
        f"[UPDATE] {data.node_id} | "
        f"SENSOR={data.sensor_status} | "
        f"MODE={node['admin_mode']} | "
        f"FINAL={node['final_status']} | "
        f"VIOLATION={node['violation']}"
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

    if node["admin_mode"] == "MAINTENANCE":
        raise HTTPException(status_code=400, detail="Node in maintenance")

    if req.reserved:
        node["reserved"] = True
        node["reservation_start"] = now
        node["reservation_expiry"] = now + RESERVATION_DURATION
    else:
        node["reserved"] = False
        node["reservation_start"] = None
        node["reservation_expiry"] = None
        node["violation"] = False

    node["final_status"] = compute_final(
        node["sensor_status"],
        node["reserved"],
        node["admin_mode"]
    )

    print(f"[RESERVE] {req.node_id} | RESERVED={node['reserved']}")
    return {"status": "ok"}

# -----------------------------
# ADMIN: Maintenance ON
# -----------------------------
@app.post("/api/admin/maintenance/{node_id}")
def set_maintenance(node_id: str):
    node = parking_states.get(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    node["admin_mode"] = "MAINTENANCE"
    node["reserved"] = False
    node["violation"] = False
    node["reservation_start"] = None
    node["reservation_expiry"] = None

    node["final_status"] = "MAINTENANCE"

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

    node["final_status"] = compute_final(
        node["sensor_status"],
        node["reserved"],
        node["admin_mode"]
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
                node["admin_mode"]
            ),
            "sensor_status": node["sensor_status"],
            "distance_cm": node["distance_cm"],
            "reserved": node["reserved"],
            "violation": node["violation"],
            "reservation_expiry": node["reservation_expiry"],
            "server_timestamp": node["last_update"],
            "admin_mode": node["admin_mode"]
        }

    return out
