import os
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
print(DATABASE_URL)
ADMIN_KEY = os.getenv("ADMIN_KEY", "demo_key")

app = FastAPI(title="Techathon Demo Smart Parking API v1.4")

# ---------------------------
# CORS
# ---------------------------
origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------
# DB Pool
# ---------------------------
@app.on_event("startup")
async def startup():
    app.state.pool = await asyncpg.create_pool(
        DATABASE_URL,
        ssl="require",
        min_size=1,
        max_size=20
    )

@app.on_event("shutdown")
async def shutdown():
    await app.state.pool.close()

async def get_db():
    return app.state.pool

# ---------------------------
# MODELS
# ---------------------------
class BookingCreate(BaseModel):
    vehicle_no: str = Field(..., min_length=6)
    slot_id: str
    start_time: datetime
    end_time: Optional[datetime] = None

class RFIDEntry(BaseModel):
    vehicle_no: str
    reader_id: str
    timestamp: Optional[datetime] = None

class RFIDExit(BaseModel):
    vehicle_no: str

class VisionEvent(BaseModel):
    vehicle_no: str
    slot_id: str
    timestamp: datetime

# ---------------------------
# HEALTH
# ---------------------------
@app.get("/api/v1/health")
async def health():
    return {"status": "Techathon Demo Parking API running"}

# ---------------------------
# LOT DETAILS
# ---------------------------
@app.get("/api/v1/lots")
async def get_lot(db=Depends(get_db)):
    lot = await db.fetchrow("SELECT * FROM parking_lots LIMIT 1")
    if not lot:
        raise HTTPException(404, "No parking lots configured.")

    zones = await db.fetch("SELECT * FROM zones WHERE lot_id=$1", lot["id"])
    response_zones = []

    for zone in zones:
        slots = await db.fetch(
            """
            SELECT id, slot_label as slot_number, status
            FROM parking_slots
            WHERE zone_id=$1
            ORDER BY slot_label
            """,
            zone["id"]
        )

        reserved = await db.fetchval(
            "SELECT COUNT(*) FROM parking_slots WHERE zone_id=$1 AND status='RESERVED'",
            zone["id"]
        )

        occupied = await db.fetchval(
            "SELECT COUNT(*) FROM parking_slots WHERE zone_id=$1 AND status='OCCUPIED'",
            zone["id"]
        )

        response_zones.append({
            "id": str(zone["id"]),
            "name": zone["name"],
            "capacity": zone["capacity"],
            "reserved": reserved,
            "occupied": occupied,
            "baseRate": zone["base_rate"],
            "slots": [
                {"id": str(s["id"]), "number": s["slot_number"], "status": s["status"]}
                for s in slots
            ]
        })

    return {
        "id": str(lot["id"]),
        "name": lot["name"],
        "location": lot["location"],
        "zones": response_zones
    }

# ---------------------------
# CREATE BOOKING (Race Safe)
# ---------------------------
@app.post("/api/v1/bookings")
async def create_booking(payload: BookingCreate, db=Depends(get_db)):

    async with db.acquire() as conn:
        async with conn.transaction():

            slot = await conn.fetchrow(
                """
                SELECT ps.*, z.lot_id
                FROM parking_slots ps
                JOIN zones z ON ps.zone_id = z.id
                WHERE ps.id=$1::uuid
                """,
                payload.slot_id
            )

            if not slot:
                raise HTTPException(404, "Slot not found")

            # Race-safe update
            result = await conn.fetchrow(
                """
                UPDATE parking_slots
                SET status='RESERVED'::slot_status, vehicle_no=$1
                WHERE id=$2::uuid AND status='FREE'
                RETURNING id
                """,
                payload.vehicle_no,
                payload.slot_id
            )

            if not result:
                raise HTTPException(400, "Slot already taken")

            booking = await conn.fetchrow(
                """
                INSERT INTO bookings
                (vehicle_no, slot_id, zone_id, lot_id,
                 status, booking_type, start_time, end_time)
                VALUES
                ($1, $2::uuid, $3, $4,
                 'ACTIVE'::booking_status,
                 'ONLINE'::booking_type,
                 $5, $6)
                RETURNING id
                """,
                payload.vehicle_no,
                payload.slot_id,
                slot["zone_id"],
                slot["lot_id"],
                payload.start_time,
                payload.end_time
            )

            await conn.execute(
                """
                INSERT INTO events_log(event_type, vehicle_no, slot_id, timestamp)
                VALUES ('BOOKED'::event_type, $1, $2::uuid, NOW())
                """,
                payload.vehicle_no,
                payload.slot_id
            )

    return {"message": "Booking created", "booking_id": str(booking["id"])}

# ---------------------------
# RFID ENTRY (Race Safe)
# ---------------------------
@app.post("/api/v1/rfid/entry")
async def rfid_entry(payload: RFIDEntry, db=Depends(get_db)):

    timestamp = payload.timestamp or datetime.now(timezone.utc)

    if timestamp.tzinfo is not None:
        timestamp = timestamp.replace(tzinfo=None)

    async with db.acquire() as conn:
        async with conn.transaction():

            booking = await conn.fetchrow(
                "SELECT * FROM bookings WHERE vehicle_no=$1 AND status='ACTIVE'",
                payload.vehicle_no
            )

            if booking:
                await conn.execute(
                    """
                    INSERT INTO events_log(event_type, vehicle_no, slot_id, timestamp)
                    VALUES ('FASTAG_ENTRY'::event_type, $1, $2, $3)
                    """,
                    payload.vehicle_no,
                    booking["slot_id"],
                    timestamp
                )
                return {"access": "GRANTED", "assigned_slot": str(booking["slot_id"])}

            free_slot = await conn.fetchrow(
                """
                SELECT ps.*, z.lot_id
                FROM parking_slots ps
                JOIN zones z ON ps.zone_id=z.id
                WHERE ps.status='FREE'
                ORDER BY ps.slot_label
                LIMIT 1
                """
            )

            if not free_slot:
                await conn.execute(
                    """
                    INSERT INTO events_log(event_type, vehicle_no, timestamp)
                    VALUES ('DENIED_ENTRY'::event_type, $1, $2)
                    """,
                    payload.vehicle_no,
                    timestamp
                )
                return {"access": "DENIED", "assigned_slot": None, "message": "Parking Full"}

            # Race-safe occupancy
            result = await conn.fetchrow(
                """
                UPDATE parking_slots
                SET status='OCCUPIED'::slot_status, vehicle_no=$1
                WHERE id=$2 AND status='FREE'
                RETURNING id
                """,
                payload.vehicle_no,
                free_slot["id"]
            )

            if not result:
                raise HTTPException(400, "Slot allocation conflict")

            await conn.execute(
                """
                INSERT INTO bookings
                (vehicle_no, slot_id, zone_id, lot_id,
                 status, booking_type, start_time)
                VALUES
                ($1, $2, $3, $4,
                 'ACTIVE'::booking_status,
                 'DRIVEIN'::booking_type,
                 $5)
                """,
                payload.vehicle_no,
                free_slot["id"],
                free_slot["zone_id"],
                free_slot["lot_id"],
                timestamp
            )

            await conn.execute(
                """
                INSERT INTO events_log(event_type, vehicle_no, slot_id, timestamp)
                VALUES ('FASTAG_ENTRY'::event_type, $1, $2, $3)
                """,
                payload.vehicle_no,
                free_slot["id"],
                timestamp
            )

    return {"access": "GRANTED", "assigned_slot": str(free_slot["id"])}

# ---------------------------
# RFID EXIT
# ---------------------------
@app.post("/api/v1/rfid/exit")
async def rfid_exit(payload: RFIDExit, db=Depends(get_db)):

    async with db.acquire() as conn:
        async with conn.transaction():

            booking = await conn.fetchrow(
                "SELECT * FROM bookings WHERE vehicle_no=$1 AND status='ACTIVE'",
                payload.vehicle_no
            )

            if not booking:
                raise HTTPException(404, "No active session")

            await conn.execute(
                "UPDATE bookings SET status='COMPLETED'::booking_status, end_time=NOW() WHERE id=$1",
                booking["id"]
            )

            await conn.execute(
                "UPDATE parking_slots SET status='FREE'::slot_status, vehicle_no=NULL WHERE id=$1",
                booking["slot_id"]
            )

            await conn.execute(
                """
                INSERT INTO events_log(event_type, vehicle_no, slot_id, timestamp)
                VALUES ('FASTAG_EXIT'::event_type, $1, $2, NOW())
                """,
                payload.vehicle_no,
                booking["slot_id"]
            )

    return {"message": "Exit processed"}

# ---------------------------
# VISION EVENT
# ---------------------------
@app.post("/api/v1/vision/event")
async def vision_event(payload: VisionEvent, db=Depends(get_db)):

    async with db.acquire() as conn:
        async with conn.transaction():

            booking = await conn.fetchrow(
                "SELECT * FROM bookings WHERE vehicle_no=$1 AND status='ACTIVE'",
                payload.vehicle_no
            )

            if not booking:
                raise HTTPException(404, "No active session")

            if str(booking["slot_id"]) != payload.slot_id:

                new_slot = await conn.fetchrow(
                    """
                    SELECT ps.*, z.lot_id
                    FROM parking_slots ps
                    JOIN zones z ON ps.zone_id=z.id
                    WHERE ps.id=$1::uuid
                    """,
                    payload.slot_id
                )

                if not new_slot:
                    raise HTTPException(400, "Invalid slot")

                await conn.execute(
                    "UPDATE parking_slots SET status='FREE'::slot_status, vehicle_no=NULL WHERE id=$1",
                    booking["slot_id"]
                )

                await conn.execute(
                    "UPDATE parking_slots SET status='OCCUPIED'::slot_status, vehicle_no=$1 WHERE id=$2::uuid",
                    payload.vehicle_no,
                    payload.slot_id
                )

                await conn.execute(
                    """
                    UPDATE bookings
                    SET slot_id=$1::uuid,
                        zone_id=$2,
                        lot_id=$3
                    WHERE id=$4
                    """,
                    payload.slot_id,
                    new_slot["zone_id"],
                    new_slot["lot_id"],
                    booking["id"]
                )

                await conn.execute(
                    """
                    INSERT INTO events_log(event_type, vehicle_no, slot_id, timestamp)
                    VALUES ('VISION_CORRECTION'::event_type, $1, $2::uuid, $3)
                    """,
                    payload.vehicle_no,
                    payload.slot_id,
                    payload.timestamp
                )

                return {"message": "Slot corrected"}

            await conn.execute(
                """
                INSERT INTO events_log(event_type, vehicle_no, slot_id, timestamp)
                VALUES ('VISION_VERIFIED'::event_type, $1, $2::uuid, $3)
                """,
                payload.vehicle_no,
                payload.slot_id,
                payload.timestamp
            )

        return {"message": "Slot verified"}

    # ---------------------------
# ADMIN: RESET
# ---------------------------
@app.post(
    "/api/v1/admin/reset",
    tags=["Admin"],
    summary="Reset system state",
    description="Clears bookings and events. Optional full_wipe deletes lots, zones and slots. Requires admin_key header."
)
async def reset_system(
    admin_key: str = Header(..., description="Admin secret key"),
    full_wipe: bool = False,
    db=Depends(get_db)
):
    if admin_key != ADMIN_KEY:
        raise HTTPException(403, "Unauthorized")

    async with db.acquire() as conn:
        async with conn.transaction():

            await conn.execute("DELETE FROM bookings")
            await conn.execute("DELETE FROM events_log")

            if full_wipe:
                await conn.execute("DELETE FROM parking_slots")
                await conn.execute("DELETE FROM zones")
                await conn.execute("DELETE FROM parking_lots")
                return {"message": "Complete system wipe successful"}

            await conn.execute(
                "UPDATE parking_slots SET status='FREE'::slot_status, vehicle_no=NULL"
            )

    return {"message": "Bookings cleared and slots reset"}

# ---------------------------
# ADMIN: DEMO SEED
# ---------------------------
@app.post(
    "/api/v1/admin/demo",
    tags=["Admin"],
    summary="Seed demo data for hackathon",
    description="Creates 2 demo parking lots (Premium & Standard) with 6 slots each (A1-A6). Requires admin_key header."
)
async def setup_demo(
    admin_key: str = Header(..., description="Admin secret key"),
    db=Depends(get_db)
):
    if admin_key != ADMIN_KEY:
        raise HTTPException(403, "Unauthorized")

    async with db.acquire() as conn:
        async with conn.transaction():

            # ==========================
            # 1. LOT 1: PREMIUM LOT (₹30)
            # ==========================
            lot_1_id = await conn.fetchval(
                """
                INSERT INTO parking_lots (name, location, total_capacity)
                VALUES ('Techathon Premium Lot', 'Main Campus - VIP Gate', 6)
                RETURNING id
                """
            )

            zone_1_id = await conn.fetchval(
                """
                INSERT INTO zones (lot_id, name, capacity, base_rate)
                VALUES ($1, 'Premium Zone', 6, 30.0)
                RETURNING id
                """,
                lot_1_id
            )

            # Generate A1 to A6 for Premium Lot
            for i in range(1, 7):
                await conn.execute(
                    "INSERT INTO parking_slots (zone_id, slot_label, status) VALUES ($1, $2, 'FREE'::slot_status)",
                    zone_1_id,
                    f"A{i}"
                )

            # ==========================
            # 2. LOT 2: STANDARD LOT (₹20)
            # ==========================
            lot_2_id = await conn.fetchval(
                """
                INSERT INTO parking_lots (name, location, total_capacity)
                VALUES ('Techathon Standard Lot', 'Main Campus - General', 6)
                RETURNING id
                """
            )

            zone_2_id = await conn.fetchval(
                """
                INSERT INTO zones (lot_id, name, capacity, base_rate)
                VALUES ($1, 'Standard Zone', 6, 20.0)
                RETURNING id
                """,
                lot_2_id
            )

            # Generate A1 to A6 for Standard Lot
            for i in range(1, 7):
                await conn.execute(
                    "INSERT INTO parking_slots (zone_id, slot_label, status) VALUES ($1, $2, 'FREE'::slot_status)",
                    zone_2_id,
                    f"A{i}"
                )

    return {"message": "Demo data successfully created: 2 Lots, 12 Total Slots (A1-A6)!"}