"""
SLIOT — FastAPI Backend Server
Serves temperature data, predictions, and ML model results to the Next.js dashboard.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import os
import pandas as pd
import uuid

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, field_validator
from sqlalchemy import func
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
from models import Prediction, Sensor, SensorReading, User, UserRole, NetworkGroup, UserNetworkGroup
from scheduler import create_scheduler


# ── WebSocket Alert Manager ────────────────────────────────────────────────────

BLEACHING_THRESHOLD = 31.0


class AlertConnectionManager:
    """
    Tracks live WebSocket connections keyed to the authenticated user.
    Admins receive every alert; regular users receive alerts for their own sensors.
    """

    def __init__(self) -> None:
        self._connections: dict[WebSocket, tuple[int, str, set[str]]] = {}  # ws -> (user_id, role, network_ids)

    async def connect(self, ws: WebSocket, user: User, network_ids: list[str]) -> None:
        await ws.accept()
        self._connections[ws] = (user.id, user.role.value, set(network_ids))

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.pop(ws, None)

    async def broadcast_alert(self, alert: dict, network_group_id: Optional[str]) -> None:
        """Send alert to all users in the network group and all admins."""
        dead: list[WebSocket] = []
        for ws, (uid, role, network_ids) in self._connections.items():
            if role == "admin" or (network_group_id is not None and network_group_id in network_ids):
                try:
                    await ws.send_json(alert)
                except Exception:
                    dead.append(ws)
        for ws in dead:
            self._connections.pop(ws, None)


manager = AlertConnectionManager()

def _user_network_ids(db: Session, user: User) -> list[str]:
    if user.role == UserRole.admin:
        # caller should not use this for admin filtering, but keep safe
        return []
    rows = (
        db.query(UserNetworkGroup.network_group_id)
        .filter(UserNetworkGroup.user_id == user.id)
        .all()
    )
    return [r[0] for r in rows if r and r[0]]


def _membership_network_ids_for_user_id(db: Session, user_id: int) -> list[str]:
    rows = (
        db.query(UserNetworkGroup.network_group_id)
        .filter(UserNetworkGroup.user_id == user_id)
        .all()
    )
    return [r[0] for r in rows if r and r[0]]


def _first_network_group_id_for_user(db: Session, user_id: int) -> Optional[str]:
    row = (
        db.query(UserNetworkGroup.network_group_id)
        .filter(UserNetworkGroup.user_id == user_id)
        .order_by(UserNetworkGroup.created_at.asc())
        .first()
    )
    return row[0] if row else None


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


# CORS: dev defaults + optional CORS_ORIGINS (comma-separated), e.g. https://app.example.com
_cors_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
]
_extra = os.getenv("CORS_ORIGINS", "")
for _o in _extra.split(","):
    _o = _o.strip()
    if _o and _o not in _cors_origins:
        _cors_origins.append(_o)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
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
def get_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Dashboard stats sourced from the database.

    Notes:
    - total_dhw_records is kept for backward compatibility with the frontend but
      is not stored in the DB currently.
    """
    if current_user.role == UserRole.admin:
        readings_count = db.query(func.count(SensorReading.id)).scalar() or 0
        predictions_count = db.query(func.count(Prediction.id)).scalar() or 0
        min_ts, max_ts = db.query(
            func.min(SensorReading.timestamp),
            func.max(SensorReading.timestamp),
        ).one()
        unique_coords = (
            db.query(func.count())
            .select_from(
                db.query(Sensor.latitude, Sensor.longitude)
                .filter(Sensor.latitude.isnot(None), Sensor.longitude.isnot(None))
                .distinct()
                .subquery()
            )
            .scalar()
            or 0
        )
    else:
        network_ids = _user_network_ids(db, current_user)
        if not network_ids:
            return StatsResponse(
                total_sst_records=0,
                total_dhw_records=0,
                total_predictions=0,
                date_range={"start": None, "end": None},
                unique_coordinates=0,
            )

        readings_count = (
            db.query(func.count(SensorReading.id))
            .join(Sensor, Sensor.id == SensorReading.sensor_id)
            .filter(Sensor.network_group_id.in_(network_ids))
            .scalar()
            or 0
        )
        predictions_count = (
            db.query(func.count(Prediction.id))
            .join(Sensor, Sensor.id == Prediction.sensor_id)
            .filter(Sensor.network_group_id.in_(network_ids))
            .scalar()
            or 0
        )
        min_ts, max_ts = (
            db.query(func.min(SensorReading.timestamp), func.max(SensorReading.timestamp))
            .join(Sensor, Sensor.id == SensorReading.sensor_id)
            .filter(Sensor.network_group_id.in_(network_ids))
            .one()
        )
        unique_coords = (
            db.query(func.count())
            .select_from(
                db.query(Sensor.latitude, Sensor.longitude)
                .filter(
                    Sensor.network_group_id.in_(network_ids),
                    Sensor.latitude.isnot(None),
                    Sensor.longitude.isnot(None),
                )
                .distinct()
                .subquery()
            )
            .scalar()
            or 0
        )

    return StatsResponse(
        total_sst_records=int(readings_count),
        total_dhw_records=0,
        total_predictions=int(predictions_count),
        date_range={
            "start": min_ts.isoformat() if min_ts else None,
            "end": max_ts.isoformat() if max_ts else None,
        },
        unique_coordinates=int(unique_coords),
    )


@app.get("/api/sst")
def get_sst(
    start: Optional[str] = Query(None, description="Start date ISO format"),
    end: Optional[str] = Query(None, description="End date ISO format"),
    limit: int = Query(1000, ge=1, le=10000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return sensor readings joined with sensor coordinates.

    Response shape is aligned with the old CSV-backed endpoint:
    - time, latitude, longitude, temperature
    """
    q = (
        db.query(
            SensorReading.timestamp.label("time"),
            Sensor.sensor_uid.label("sensor_uid"),
            Sensor.id.label("sensor_id"),
            Sensor.latitude.label("latitude"),
            Sensor.longitude.label("longitude"),
            SensorReading.temperature.label("temperature"),
        )
        .join(Sensor, Sensor.id == SensorReading.sensor_id)
        .filter(Sensor.latitude.isnot(None), Sensor.longitude.isnot(None))
        .order_by(SensorReading.timestamp.desc())
    )
    if current_user.role != UserRole.admin:
        network_ids = _user_network_ids(db, current_user)
        if not network_ids:
            return []
        q = q.filter(Sensor.network_group_id.in_(network_ids))

    if start:
        q = q.filter(SensorReading.timestamp >= pd.to_datetime(start))
    if end:
        q = q.filter(SensorReading.timestamp <= pd.to_datetime(end))

    rows = q.limit(limit).all()
    return [
        {
            "time": r.time.isoformat(),
            "sensor_uid": r.sensor_uid,
            "sensor_id": r.sensor_id,
            "latitude": r.latitude,
            "longitude": r.longitude,
            "temperature": r.temperature,
        }
        for r in rows
    ]


@app.get("/api/dhw")
def get_dhw(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(1000, ge=1, le=10000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Degree Heating Weeks (DHW) is not stored in the database yet.

    For now this endpoint derives a simple rolling heat-stress proxy from readings:
    - hotspot = max(0, temperature - 30.0)
    - dhw = 7-day rolling sum of hotspot / 7

    This keeps the endpoint functional for the dashboard while you decide on the
    exact DHW definition/climatology you want to persist.
    """
    q = (
        db.query(SensorReading.timestamp, SensorReading.temperature)
        .join(Sensor, Sensor.id == SensorReading.sensor_id)
        .order_by(SensorReading.timestamp.asc())
    )
    if current_user.role != UserRole.admin:
        network_ids = _user_network_ids(db, current_user)
        if not network_ids:
            return []
        q = q.filter(Sensor.network_group_id.in_(network_ids))
    if start:
        q = q.filter(SensorReading.timestamp >= pd.to_datetime(start))
    if end:
        q = q.filter(SensorReading.timestamp <= pd.to_datetime(end))
    rows = q.limit(limit).all()

    if not rows:
        return []

    df = pd.DataFrame([{"time": r.timestamp, "temperature": r.temperature} for r in rows])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.sort_values("time")
    df["hotspot"] = (df["temperature"] - 30.0).clip(lower=0.0)
    df["dhw"] = df["hotspot"].rolling(window=24 * 7, min_periods=1).sum() / 7.0
    return [
        {"time": t.isoformat(), "dhw": float(v)}
        for t, v in zip(df["time"].tolist(), df["dhw"].tolist())
    ]


@app.get("/api/predictions")
def get_predictions(
    min_risk: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(500, ge=1, le=5000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(Prediction).order_by(Prediction.target_timestamp.asc())
    if current_user.role != UserRole.admin:
        network_ids = _user_network_ids(db, current_user)
        if not network_ids:
            return []
        q = q.join(Sensor, Sensor.id == Prediction.sensor_id).filter(
            Sensor.network_group_id.in_(network_ids)
        )
    if min_risk > 0:
        q = q.filter(Prediction.risk_score.isnot(None), Prediction.risk_score >= min_risk)
    items = q.limit(limit).all()
    return [
        {
            "sensor_id": p.sensor_id,
            "target_timestamp": p.target_timestamp.isoformat(),
            "predicted_temp": p.predicted_temp,
            "risk_level": p.risk_level,
            "risk_score": p.risk_score,
            "anomaly": p.anomaly,
            "days_stressed": p.days_stressed,
            "warming_rate": p.warming_rate,
            "physics_residual": p.physics_residual,
        }
        for p in items
    ]


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
def get_latest_readings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get the most recent temperature reading per sensor coordinate."""
    latest_subq = (
        db.query(
            SensorReading.sensor_id.label("sensor_id"),
            func.max(SensorReading.timestamp).label("max_ts"),
        )
        .group_by(SensorReading.sensor_id)
        .subquery()
    )

    rows = (
        db.query(
            Sensor.id.label("sensor_id"),
            Sensor.sensor_uid.label("sensor_uid"),
            Sensor.network_group_id.label("network_group_id"),
            Sensor.latitude.label("latitude"),
            Sensor.longitude.label("longitude"),
            SensorReading.timestamp.label("time"),
            SensorReading.temperature.label("temperature"),
        )
        .join(latest_subq, latest_subq.c.sensor_id == Sensor.id)
        .join(
            SensorReading,
            (SensorReading.sensor_id == latest_subq.c.sensor_id)
            & (SensorReading.timestamp == latest_subq.c.max_ts),
        )
        .filter(Sensor.latitude.isnot(None), Sensor.longitude.isnot(None))
    )
    if current_user.role != UserRole.admin:
        network_ids = _user_network_ids(db, current_user)
        if not network_ids:
            return []
        rows = rows.filter(Sensor.network_group_id.in_(network_ids))
    rows = rows.all()

    return [
        {
            "sensor_id": r.sensor_id,
            "sensor_uid": r.sensor_uid,
            "network_group_id": r.network_group_id,
            "time": r.time.isoformat(),
            "latitude": r.latitude,
            "longitude": r.longitude,
            "temperature": r.temperature,
        }
        for r in rows
    ]


class NetworkGroupOut(BaseModel):
    id: str
    name: Optional[str] = None
    center_lat: Optional[float] = None
    center_lon: Optional[float] = None


def _network_groups_to_out(db: Session, groups: List[NetworkGroup]) -> List[NetworkGroupOut]:
    if not groups:
        return []
    gid_list = [g.id for g in groups]
    center_rows = (
        db.query(
            Sensor.network_group_id,
            func.avg(Sensor.latitude),
            func.avg(Sensor.longitude),
        )
        .filter(Sensor.network_group_id.in_(gid_list))
        .group_by(Sensor.network_group_id)
        .all()
    )
    cent_map: dict[str, tuple[Optional[float], Optional[float]]] = {
        row[0]: (float(row[1]) if row[1] is not None else None, float(row[2]) if row[2] is not None else None)
        for row in center_rows
    }
    return [
        NetworkGroupOut(
            id=g.id,
            name=g.name,
            center_lat=cent_map.get(g.id, (None, None))[0],
            center_lon=cent_map.get(g.id, (None, None))[1],
        )
        for g in groups
    ]


@app.get("/api/network-groups", response_model=List[NetworkGroupOut])
def list_network_groups(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Network groups visible to the caller:
    - admin → all groups in the database
    - user  → only groups the user is a member of
    """
    if current_user.role == UserRole.admin:
        groups = db.query(NetworkGroup).order_by(NetworkGroup.id.asc()).all()
    else:
        ids = _user_network_ids(db, current_user)
        if not ids:
            return []
        groups = (
            db.query(NetworkGroup)
            .filter(NetworkGroup.id.in_(ids))
            .order_by(NetworkGroup.id.asc())
            .all()
        )
    if not groups:
        return []
    return _network_groups_to_out(db, groups)


@app.get("/api/risk-summary")
def get_risk_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get risk level distribution from stored predictions."""
    base = db.query(Prediction)
    if current_user.role != UserRole.admin:
        network_ids = _user_network_ids(db, current_user)
        if not network_ids:
            return {
                "total_points": 0,
                "healthy": 0,
                "warning": 0,
                "danger": 0,
                "avg_temperature": None,
                "max_temperature": None,
                "avg_risk_score": None,
            }
        base = (
            base.join(Sensor, Sensor.id == Prediction.sensor_id)
            .filter(Sensor.network_group_id.in_(network_ids))
        )

    total = base.with_entities(func.count(Prediction.id)).scalar() or 0
    healthy = base.filter(Prediction.risk_level == 0).with_entities(func.count(Prediction.id)).scalar() or 0
    warning = base.filter(Prediction.risk_level == 1).with_entities(func.count(Prediction.id)).scalar() or 0
    danger = base.filter(Prediction.risk_level == 2).with_entities(func.count(Prediction.id)).scalar() or 0
    avg_temp = base.with_entities(func.avg(Prediction.predicted_temp)).scalar()
    max_temp = base.with_entities(func.max(Prediction.predicted_temp)).scalar()
    avg_risk = (
        base.filter(Prediction.risk_score.isnot(None))
        .with_entities(func.avg(Prediction.risk_score))
        .scalar()
    )

    return {
        "total_points": int(total),
        "healthy": int(healthy),
        "warning": int(warning),
        "danger": int(danger),
        "avg_temperature": round(float(avg_temp), 2) if avg_temp is not None else None,
        "max_temperature": round(float(max_temp), 2) if max_temp is not None else None,
        "avg_risk_score": round(float(avg_risk), 4) if avg_risk is not None else None,
    }


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
    # create the default network group and membership
    ngid = f"ng_{uuid.uuid4().hex[:12]}"
    if ngid:
        if not db.query(NetworkGroup).filter(NetworkGroup.id == ngid).first():
            db.add(NetworkGroup(id=ngid, name=None))
            db.commit()
        if not db.query(UserNetworkGroup).filter(
            UserNetworkGroup.user_id == user.id,
            UserNetworkGroup.network_group_id == ngid,
        ).first():
            db.add(UserNetworkGroup(user_id=user.id, network_group_id=ngid))
            db.commit()
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


# ─── Admin provisioning (users & networks) ────────────────────────────────────


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str


class AdminUserListItem(BaseModel):
    id: int
    email: str
    role: UserRole

    class Config:
        from_attributes = True


class AdminCreateNetworkGroupRequest(BaseModel):
    name: Optional[str] = None
    user_email: str


@app.post("/admin/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def admin_create_user(
    payload: AdminCreateUserRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin-only: create a user account with a default empty network group; admin is added to that network too."""
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role=UserRole.user,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    ngid = f"ng_{uuid.uuid4().hex[:12]}"
    if not db.query(NetworkGroup).filter(NetworkGroup.id == ngid).first():
        db.add(NetworkGroup(id=ngid, name=None))
        db.commit()
    db.add(UserNetworkGroup(user_id=user.id, network_group_id=ngid))
    db.add(UserNetworkGroup(user_id=admin.id, network_group_id=ngid))
    db.commit()
    return user


@app.get("/admin/users", response_model=List[AdminUserListItem])
def admin_list_users(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin-only: list all users (for provisioning UI)."""
    return db.query(User).order_by(User.id.asc()).all()


@app.post("/admin/network-groups", response_model=NetworkGroupOut)
def admin_create_network_group(
    payload: AdminCreateNetworkGroupRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin-only: create a new node network, attach it to the user, and add the creating admin as a member."""
    owner = db.query(User).filter(User.email == payload.user_email).first()
    if not owner:
        raise HTTPException(status_code=404, detail=f"User '{payload.user_email}' not found")
    ngid = f"ng_{uuid.uuid4().hex[:12]}"
    if db.query(NetworkGroup).filter(NetworkGroup.id == ngid).first():
        raise HTTPException(status_code=500, detail="Network id collision — retry")
    db.add(NetworkGroup(id=ngid, name=payload.name))
    db.add(UserNetworkGroup(user_id=owner.id, network_group_id=ngid))
    # So the admin account also sees this network in the dashboard / API scoping (unless admin is the owner).
    if admin.id != owner.id:
        db.add(UserNetworkGroup(user_id=admin.id, network_group_id=ngid))
    db.commit()
    ng = db.query(NetworkGroup).filter(NetworkGroup.id == ngid).first()
    if ng is None:
        raise HTTPException(status_code=500, detail="Failed to create network group")
    return _network_groups_to_out(db, [ng])[0]


@app.get("/admin/user-network-groups", response_model=List[NetworkGroupOut])
def admin_user_network_groups(
    user_email: str = Query(..., description="User email"),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Admin-only: list node networks a user belongs to (oldest membership first — matches default sensor routing)."""
    owner = db.query(User).filter(User.email == user_email).first()
    if not owner:
        raise HTTPException(status_code=404, detail=f"User '{user_email}' not found")
    groups = (
        db.query(NetworkGroup)
        .join(UserNetworkGroup, UserNetworkGroup.network_group_id == NetworkGroup.id)
        .filter(UserNetworkGroup.user_id == owner.id)
        .order_by(UserNetworkGroup.created_at.asc())
        .all()
    )
    return _network_groups_to_out(db, groups)


# ─── Sensor Pydantic Schemas ──────────────────────────────────────────────────

class SensorOut(BaseModel):
    id: int
    sensor_uid: str
    owner_id: Optional[int]
    network_group_id: Optional[str]
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
    network_group_id: Optional[str] = None

    @field_validator("sensor_id", "owner_email", mode="before")
    @classmethod
    def strip_required_strings(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("network_group_id", mode="before")
    @classmethod
    def empty_network_to_none(cls, v: object) -> object:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v


# ─── Sensor Routes ────────────────────────────────────────────────────────────

@app.get("/sensors", response_model=List[SensorOut])
def get_sensors(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return sensors scoped to the caller's role:
    - admin  → all sensors
    - user   → only sensors in user's network group
    """
    if current_user.role == UserRole.admin:
        return db.query(Sensor).all()
    network_ids = _user_network_ids(db, current_user)
    if not network_ids:
        return []
    return db.query(Sensor).filter(Sensor.network_group_id.in_(network_ids)).all()


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

    membership_ids = _membership_network_ids_for_user_id(db, owner.id)

    ng_id: Optional[str] = None
    if payload.network_group_id:
        ng = db.query(NetworkGroup).filter(NetworkGroup.id == payload.network_group_id).first()
        if not ng:
            raise HTTPException(status_code=404, detail="Network group not found")
        membership = db.query(UserNetworkGroup).filter(
            UserNetworkGroup.user_id == owner.id,
            UserNetworkGroup.network_group_id == payload.network_group_id,
        ).first()
        if not membership:
            try:
                db.add(UserNetworkGroup(user_id=owner.id, network_group_id=payload.network_group_id))
                db.commit()
            except IntegrityError:
                db.rollback()
        ng_id = payload.network_group_id
    else:
        if len(membership_ids) > 1:
            raise HTTPException(
                status_code=400,
                detail="Owner belongs to multiple node networks; send network_group_id to choose one.",
            )
        ng_id = _first_network_group_id_for_user(db, owner.id)

    if not ng_id:
        raise HTTPException(status_code=400, detail="Owner has no network group membership")

    sensor = Sensor(
        sensor_uid=payload.sensor_id,
        owner_id=owner.id,
        network_group_id=ng_id,
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

    db2 = SessionLocal()
    try:
        network_ids = _user_network_ids(db2, user) if user.role != UserRole.admin else []
    finally:
        db2.close()

    await manager.connect(websocket, user, network_ids)
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
        await manager.broadcast_alert(alert, sensor.network_group_id)

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
    if current_user.role != UserRole.admin:
        network_ids = _user_network_ids(db, current_user)
        if not network_ids or sensor.network_group_id not in network_ids:
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
    if current_user.role != UserRole.admin:
        network_ids = _user_network_ids(db, current_user)
        if not network_ids or sensor.network_group_id not in network_ids:
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
