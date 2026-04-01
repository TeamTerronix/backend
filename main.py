"""
SLIOT — FastAPI Backend Server
Serves temperature data, predictions, and ML model results to the Next.js dashboard.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import numpy as np
import os
import pandas as pd

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from auth import (
    TokenResponse,
    UserCreate,
    UserOut,
    create_access_token,
    decode_access_token,
    get_admin_user,
    get_current_user,
    hash_password,
    verify_password,
)
from database import SessionLocal, get_db
from models import Prediction, Sensor, SensorReading, User, UserRole
from scheduler import create_scheduler


# ── WebSocket Alert Manager ────────────────────────────────────────────────────

BLEACHING_THRESHOLD = 31.0


class AlertConnectionManager:
    """
    Tracks live WebSocket connections keyed to the authenticated user.
    Admins receive every alert; regular users receive alerts for their own sensors.
    """

    def __init__(self) -> None:
        self._connections: dict[WebSocket, tuple[int, str]] = {}  # ws -> (user_id, role)

    async def connect(self, ws: WebSocket, user: User) -> None:
        await ws.accept()
        self._connections[ws] = (user.id, user.role.value)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.pop(ws, None)

    async def broadcast_alert(self, alert: dict, owner_id: Optional[int]) -> None:
        """Send alert to the sensor's owner and all admin connections."""
        dead: list[WebSocket] = []
        for ws, (uid, role) in self._connections.items():
            if role == "admin" or uid == owner_id:
                try:
                    await ws.send_json(alert)
                except Exception:
                    dead.append(ws)
        for ws in dead:
            self._connections.pop(ws, None)


manager = AlertConnectionManager()


# ── Lifespan: start/stop the background scheduler ─────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = create_scheduler()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="SLIOT API",
    description="Underwater Temperature Monitoring API for Coral Reef Digital Twin",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for Next.js dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Paths ───
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE_DIR, "..", "model")
DATASET_DIR = os.path.join(MODEL_DIR, "dataset")


# ─── Data Loading (cached) ───
def load_sst():
    path = os.path.join(DATASET_DIR, "sst.csv")
    df = pd.read_csv(path, skiprows=[1])
    df["time"] = pd.to_datetime(df["time"])
    return df


def load_dhw():
    path = os.path.join(DATASET_DIR, "dhw.csv")
    df = pd.read_csv(path, skiprows=[1])
    df["time"] = pd.to_datetime(df["time"])
    return df


def load_predictions():
    path = os.path.join(MODEL_DIR, "prediction_results.csv")
    return pd.read_csv(path)


def load_training_history():
    path = os.path.join(MODEL_DIR, "training_history.csv")
    return pd.read_csv(path)


def load_triangle_data():
    path = os.path.join(DATASET_DIR, "triangle_data.csv")
    return pd.read_csv(path)


# ─── Pydantic Models ───
class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: str


class StatsResponse(BaseModel):
    total_sst_records: int
    total_dhw_records: int
    total_predictions: int
    date_range: dict
    unique_coordinates: int


# ─── Routes ───

@app.get("/", response_model=HealthResponse)
def health():
    return HealthResponse(
        status="ok",
        version="1.0.0",
        timestamp=datetime.utcnow().isoformat(),
    )


@app.get("/api/stats", response_model=StatsResponse)
def get_stats():
    df_sst = load_sst()
    df_dhw = load_dhw()
    df_pred = load_predictions()

    return StatsResponse(
        total_sst_records=len(df_sst),
        total_dhw_records=len(df_dhw),
        total_predictions=len(df_pred),
        date_range={
            "start": str(df_sst["time"].min()),
            "end": str(df_sst["time"].max()),
        },
        unique_coordinates=df_sst.groupby(["latitude", "longitude"]).ngroups,
    )


@app.get("/api/sst")
def get_sst(
    start: Optional[str] = Query(None, description="Start date ISO format"),
    end: Optional[str] = Query(None, description="End date ISO format"),
    limit: int = Query(1000, ge=1, le=10000),
):
    df = load_sst()
    if start:
        df = df[df["time"] >= pd.to_datetime(start)]
    if end:
        df = df[df["time"] <= pd.to_datetime(end)]
    df = df.head(limit)
    return df.to_dict(orient="records")


@app.get("/api/dhw")
def get_dhw(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
):
    df = load_dhw()
    if start:
        df = df[df["time"] >= pd.to_datetime(start)]
    if end:
        df = df[df["time"] <= pd.to_datetime(end)]
    df = df.head(limit)
    return df.to_dict(orient="records")


@app.get("/api/predictions")
def get_predictions(
    min_risk: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(500, ge=1, le=5000),
):
    df = load_predictions()
    if min_risk > 0:
        df = df[df["risk_score"] >= min_risk]
    df = df.head(limit)
    return df.to_dict(orient="records")


@app.get("/api/training-history")
def get_training_history():
    df = load_training_history()
    records = []
    for i, row in df.iterrows():
        records.append({
            "epoch": i + 1,
            "loss": round(row["loss"], 6),
            "mae": round(row["mae"], 6),
            "val_loss": round(row["val_loss"], 6),
            "val_mae": round(row["val_mae"], 6),
            "learning_rate": row["learning_rate"],
        })
    return records


@app.get("/api/triangle-data")
def get_triangle_data(
    sensor_id: Optional[int] = Query(None),
    limit: int = Query(500, ge=1, le=15000),
):
    df = load_triangle_data()
    if sensor_id is not None:
        df = df[df["sensor_id"] == sensor_id]
    df = df.head(limit)
    return df.to_dict(orient="records")


@app.get("/api/latest-readings")
def get_latest_readings():
    """Get the most recent temperature reading per unique coordinate."""
    df = load_sst()
    latest = df.sort_values("time").groupby(["latitude", "longitude"]).last().reset_index()
    return latest.to_dict(orient="records")


@app.get("/api/risk-summary")
def get_risk_summary():
    """Get risk level distribution from predictions."""
    df = load_predictions()
    summary = {
        "total_points": len(df),
        "healthy": int((df["risk_level"] == 0).sum()),
        "warning": int((df["risk_level"] == 1).sum()),
        "danger": int((df["risk_level"] == 2).sum()),
        "avg_temperature": round(float(df["temperature"].mean()), 2),
        "max_temperature": round(float(df["temperature"].max()), 2),
        "avg_risk_score": round(float(df["risk_score"].mean()), 4),
    }
    return summary


# ─── Auth Routes ──────────────────────────────────────────────────────────────

@app.post("/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    """Create a new user account (self-service, role defaults to 'user')."""
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=UserRole.user,   # self-registration is always 'user'
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/token", response_model=TokenResponse)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """OAuth2 password flow — returns a JWT bearer token."""
    user = db.query(User).filter(User.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(user.id)
    return TokenResponse(access_token=token)


@app.get("/auth/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    """Return the currently authenticated user's profile."""
    return current_user


# ─── Sensor Pydantic Schemas ──────────────────────────────────────────────────

class SensorOut(BaseModel):
    id: int
    sensor_uid: str
    owner_id: Optional[int]
    latitude: Optional[float]
    longitude: Optional[float]
    depth: Optional[float]
    is_approved: bool

    class Config:
        from_attributes = True


class RegisterSensorRequest(BaseModel):
    sensor_id: str
    owner_email: str
    latitude: float
    longitude: float
    depth: float


# ─── Sensor Routes ────────────────────────────────────────────────────────────

@app.get("/sensors", response_model=List[SensorOut])
def get_sensors(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return sensors scoped to the caller's role:
    - admin  → all sensors
    - user   → only sensors owned by this user
    """
    if current_user.role == UserRole.admin:
        return db.query(Sensor).all()
    return db.query(Sensor).filter(Sensor.owner_id == current_user.id).all()


@app.post("/admin/register-sensor", response_model=SensorOut, status_code=status.HTTP_201_CREATED)
def admin_register_sensor(
    payload: RegisterSensorRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """
    Admin-only: register a new sensor, assign it to a user by email,
    set its geographic metadata, and mark it as approved.
    """
    owner = db.query(User).filter(User.email == payload.owner_email).first()
    if not owner:
        raise HTTPException(status_code=404, detail=f"User '{payload.owner_email}' not found")

    if db.query(Sensor).filter(Sensor.sensor_uid == payload.sensor_id).first():
        raise HTTPException(status_code=400, detail="sensor_id already registered")

    sensor = Sensor(
        sensor_uid=payload.sensor_id,
        owner_id=owner.id,
        latitude=payload.latitude,
        longitude=payload.longitude,
        depth=payload.depth,
        is_approved=True,
    )
    db.add(sensor)
    db.commit()
    db.refresh(sensor)
    return sensor


# ── Sensor data ingestion ──────────────────────────────────────────────────────

class SensorDataPayload(BaseModel):
    sensor_uid: str
    timestamp: datetime        # ISO-8601; will be floored to the nearest hour
    temperature: float


class SensorDataResponse(BaseModel):
    sensor_id: int
    timestamp: datetime
    temperature: float
    status: str                # "created" or "duplicate_skipped"


@app.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket, token: str = Query(...)):
    """
    Persistent WebSocket connection for push-based bleaching alerts.
    Authenticate by passing the JWT as ?token=<jwt>.
    Broadcasts to the sensor owner + all admins when temp > 31°C.
    """
    db = SessionLocal()
    try:
        token_data = decode_access_token(token)
        user = db.query(User).filter(User.id == token_data.user_id).first()
        if not user:
            await websocket.close(code=1008)
            return
    except Exception:
        await websocket.close(code=1008)
        return
    finally:
        db.close()

    await manager.connect(websocket, user)
    try:
        while True:
            await websocket.receive_text()  # keep-alive; ignore client messages
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.post("/data", response_model=SensorDataResponse, status_code=status.HTTP_200_OK)
async def ingest_reading(
    payload: SensorDataPayload,
    db: Session = Depends(get_db),
):
    """
    Endpoint for sensors to POST live readings.

    Hourly deduplication
    --------------------
    The timestamp is floored to the hour boundary.  If a reading already
    exists for that sensor × hour pair, the request is acknowledged but
    no duplicate is inserted (idempotent).
    """
    sensor = db.query(Sensor).filter(Sensor.sensor_uid == payload.sensor_uid).first()
    if not sensor:
        raise HTTPException(status_code=404, detail=f"Sensor '{payload.sensor_uid}' not registered")
    if not sensor.is_approved:
        raise HTTPException(status_code=403, detail="Sensor is not approved")

    # Floor to the nearest hour (UTC)
    ts = payload.timestamp.replace(minute=0, second=0, microsecond=0)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    # Check for existing reading in this hour
    existing = (
        db.query(SensorReading)
        .filter(SensorReading.sensor_id == sensor.id, SensorReading.timestamp == ts)
        .first()
    )
    if existing:
        return SensorDataResponse(
            sensor_id=sensor.id,
            timestamp=ts,
            temperature=existing.temperature,
            status="duplicate_skipped",
        )

    reading = SensorReading(
        sensor_id=sensor.id,
        timestamp=ts,
        temperature=payload.temperature,
    )
    db.add(reading)
    try:
        db.commit()
        db.refresh(reading)
    except IntegrityError:
        db.rollback()
        return SensorDataResponse(
            sensor_id=sensor.id,
            timestamp=ts,
            temperature=payload.temperature,
            status="duplicate_skipped",
        )

    # Broadcast bleaching alert to connected WS clients if threshold exceeded
    if payload.temperature >= BLEACHING_THRESHOLD:
        alert = {
            "type": "bleaching_alert",
            "sensor_id": sensor.id,
            "sensor_uid": sensor.sensor_uid,
            "location_name": sensor.sensor_uid,
            "temperature": round(payload.temperature, 2),
            "risk_level": 2,
            "timestamp": ts.isoformat(),
        }
        await manager.broadcast_alert(alert, sensor.owner_id)

    return SensorDataResponse(
        sensor_id=sensor.id,
        timestamp=reading.timestamp,
        temperature=reading.temperature,
        status="created",
    )


# ── Per-sensor readings history ────────────────────────────────────────────────

class ReadingOut(BaseModel):
    id: int
    sensor_id: int
    timestamp: datetime
    temperature: float

    class Config:
        from_attributes = True


@app.get("/sensors/{sensor_id}/readings", response_model=List[ReadingOut])
def get_sensor_readings(
    sensor_id: int,
    hours: int = Query(48, ge=1, le=720, description="How many hours back to fetch"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return up to `hours` of recent readings for a sensor."""
    sensor = db.query(Sensor).filter(Sensor.id == sensor_id).first()
    if not sensor:
        raise HTTPException(status_code=404, detail="Sensor not found")
    if current_user.role != UserRole.admin and sensor.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return (
        db.query(SensorReading)
        .filter(SensorReading.sensor_id == sensor_id, SensorReading.timestamp >= cutoff)
        .order_by(SensorReading.timestamp.asc())
        .all()
    )


# ── Per-sensor 7-day forecast ──────────────────────────────────────────────────

class PredictionOut(BaseModel):
    id: int
    sensor_id: int
    target_timestamp: datetime
    predicted_temp: float
    risk_level: int
    risk_score: Optional[float]
    anomaly: Optional[float]
    days_stressed: Optional[int]
    warming_rate: Optional[float]
    physics_residual: Optional[float]

    class Config:
        from_attributes = True


@app.get("/sensors/{sensor_id}/forecast", response_model=List[PredictionOut])
def get_sensor_forecast(
    sensor_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the latest 168-hour PINN forecast for a sensor."""
    sensor = db.query(Sensor).filter(Sensor.id == sensor_id).first()
    if not sensor:
        raise HTTPException(status_code=404, detail="Sensor not found")
    if current_user.role != UserRole.admin and sensor.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    return (
        db.query(Prediction)
        .filter(Prediction.sensor_id == sensor_id)
        .order_by(Prediction.target_timestamp.asc())
        .all()
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
