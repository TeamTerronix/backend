"""
seed_data.py
============
Populate the database with test data for local development.

Creates (if missing):
- 1 admin user + 4 regular users (one per monitoring area)
- 4 node networks (one per monitoring area) with >4 sensors each (default: 6 per network)
- hourly sensor readings for the last 48 hours
- 168-hour predictions for each sensor (simple synthetic forecast)

Usage (PowerShell):
  python seed_data.py

Optional:
  python seed_data.py --sensors 3 --reading-hours 72
"""

from __future__ import annotations

import argparse
import math
import os
import pathlib
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

def _load_env_file_if_needed() -> None:
    """
    Minimal .env loader (no external dependency).
    Only sets keys that aren't already present in the environment.
    """
    here = pathlib.Path(__file__).resolve().parent
    env_path = here / ".env"
    if not env_path.exists():
        return

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and (k not in os.environ):
            os.environ[k] = v


_load_env_file_if_needed()

# Imports that depend on environment variables (DATABASE_URL, SECRET_KEY, ...)
from database import SessionLocal, engine, DATABASE_URL  # noqa: E402
from models import Base, Prediction, Sensor, SensorReading, User, UserRole, NetworkGroup, UserNetworkGroup  # noqa: E402
from auth import hash_password  # noqa: E402


def _utc_now_floor_hour() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)


def _get_or_create_user(db, *, email: str, password: str, role: UserRole) -> User:
    user = db.query(User).filter(User.email == email).first()
    if user:
        return user
    # Each user gets a default network group; sensors will inherit it.
    user = User(email=email, hashed_password=hash_password(password), role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _get_or_create_network_group(db, *, ngid: str, name: str | None = None) -> NetworkGroup:
    ng = db.query(NetworkGroup).filter(NetworkGroup.id == ngid).first()
    if ng:
        if name and not ng.name:
            ng.name = name
            db.commit()
            db.refresh(ng)
        return ng
    ng = NetworkGroup(id=ngid, name=name)
    db.add(ng)
    db.commit()
    db.refresh(ng)
    return ng


def _ensure_membership(db, *, user_id: int, network_group_id: str) -> None:
    exists = (
        db.query(UserNetworkGroup)
        .filter(
            UserNetworkGroup.user_id == user_id,
            UserNetworkGroup.network_group_id == network_group_id,
        )
        .first()
    )
    if exists:
        return
    db.add(UserNetworkGroup(user_id=user_id, network_group_id=network_group_id))
    db.commit()


def _get_or_create_sensor(
    db,
    *,
    sensor_uid: str,
    owner_id: int,
    latitude: float,
    longitude: float,
    depth: float,
    network_group_id: str | None = None,
) -> Sensor:
    sensor = db.query(Sensor).filter(Sensor.sensor_uid == sensor_uid).first()
    if sensor:
        # Keep existing ownership/approval; just fill missing metadata if needed.
        changed = False
        if sensor.owner_id is None:
            sensor.owner_id = owner_id
            changed = True
        if sensor.latitude is None:
            sensor.latitude = latitude
            changed = True
        if sensor.longitude is None:
            sensor.longitude = longitude
            changed = True
        if sensor.depth is None:
            sensor.depth = depth
            changed = True
        if sensor.is_approved is False:
            sensor.is_approved = True
            changed = True
        if sensor.network_group_id is None and network_group_id is not None:
            sensor.network_group_id = network_group_id
            changed = True
        if changed:
            db.commit()
            db.refresh(sensor)
        return sensor

    sensor = Sensor(
        sensor_uid=sensor_uid,
        owner_id=owner_id,
        network_group_id=network_group_id,
        latitude=latitude,
        longitude=longitude,
        depth=depth,
        is_approved=True,
    )
    db.add(sensor)
    db.commit()
    db.refresh(sensor)
    return sensor


def _upsert_reading(db, *, sensor_id: int, ts: datetime, temperature: float) -> None:
    exists = (
        db.query(SensorReading)
        .filter(SensorReading.sensor_id == sensor_id, SensorReading.timestamp == ts)
        .first()
    )
    if exists:
        return
    db.add(SensorReading(sensor_id=sensor_id, timestamp=ts, temperature=temperature))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()


def _replace_predictions(db, *, sensor_id: int, base_temp: float, start_ts: datetime) -> None:
    db.query(Prediction).filter(Prediction.sensor_id == sensor_id).delete()

    for h in range(168):
        ts = start_ts + timedelta(hours=h + 1)
        # Simple diurnal-ish pattern + slow drift.
        drift = 0.02 * (h / 24.0)
        wave = 0.6 * math.sin((2 * math.pi * h) / 24.0)
        temp = base_temp + drift + wave

        # Naive risk: > 30 warning, > 31 danger
        if temp >= 31.0:
            risk_level = 2
            risk_score = min(1.0, 0.7 + (temp - 31.0) * 0.15)
        elif temp >= 30.0:
            risk_level = 1
            risk_score = min(1.0, 0.3 + (temp - 30.0) * 0.4)
        else:
            risk_level = 0
            risk_score = max(0.0, (temp - 29.0) * 0.1)

        db.add(
            Prediction(
                sensor_id=sensor_id,
                target_timestamp=ts,
                predicted_temp=float(round(temp, 3)),
                risk_level=risk_level,
                risk_score=float(round(risk_score, 4)),
                anomaly=None,
                days_stressed=None,
                warming_rate=None,
                physics_residual=float(round(random.uniform(0.0, 0.05), 6)),
            )
        )
    db.commit()

def _km_to_lat_deg(km: float) -> float:
    return km / 111.0


def _km_to_lon_deg(km: float, lat_deg: float) -> float:
    return km / (111.0 * max(0.2, math.cos(math.radians(lat_deg))))


def _random_offset_within_km(radius_km: float, lat_center: float) -> tuple[float, float]:
    """
    Return (dlat, dlon) so the point is within radius_km of the center.

    We keep points within a circle of radius <= 0.7 km by default so that
    the network diameter stays < 1.5 km (max pairwise distance).
    """
    # Sample uniformly in disk
    r = radius_km * math.sqrt(random.random())
    theta = random.random() * 2.0 * math.pi
    dlat_km = r * math.sin(theta)
    dlon_km = r * math.cos(theta)
    return _km_to_lat_deg(dlat_km), _km_to_lon_deg(dlon_km, lat_center)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-email", default="admin@sliot.local")
    parser.add_argument("--admin-password", default="admin123")
    parser.add_argument("--user-password", default="user123")
    parser.add_argument("--nodes-per-network", type=int, default=6)
    parser.add_argument("--network-radius-km", type=float, default=0.7)
    parser.add_argument("--reading-hours", type=int, default=48)
    parser.add_argument("--with-predictions", action="store_true", default=True)
    args = parser.parse_args()

    # Ensure tables exist (safe if already created).
    Base.metadata.create_all(bind=engine)
    print(f"Seeding database: {DATABASE_URL}")

    db = SessionLocal()
    try:
        admin = _get_or_create_user(
            db, email=args.admin_email, password=args.admin_password, role=UserRole.admin
        )
        # One user per monitoring area (aligns with dashboard "4 places")
        area_users = [
            ("bar-reef@sliot.local", (8.877, 79.525, 4.0), "bar-reef"),
            ("pigeon-island@sliot.local", (8.725, 81.180, 5.0), "pigeon-island"),
            ("hikkaduwa@sliot.local", (6.139, 80.092, 3.0), "hikkaduwa"),
            ("rumassala@sliot.local", (6.003, 80.239, 6.0), "rumassala"),
        ]

        users: list[User] = []
        for email, _, _ in area_users:
            users.append(_get_or_create_user(db, email=email, password=args.user_password, role=UserRole.user))

        # Create network_groups and memberships (one per area by default)
        area_networks: dict[str, str] = {}
        for (email, _, area_key) in area_users:
            owner = db.query(User).filter(User.email == email).first()
            if not owner:
                continue
            ngid = f"ng_{area_key}_01"
            area_networks[area_key] = ngid
            _get_or_create_network_group(db, ngid=ngid, name=area_key)
            _ensure_membership(db, user_id=owner.id, network_group_id=ngid)
        sensors: list[Sensor] = []
        for (email, (lat, lon, depth), area_key) in area_users:
            owner = db.query(User).filter(User.email == email).first()
            if not owner:
                continue
            # One network per area_key (4 total). Each network has >4 nodes.
            for i in range(max(5, int(args.nodes_per_network))):
                dlat, dlon = _random_offset_within_km(float(args.network_radius_km), lat)
                sensor_uid = f"{area_key}-net-01-node-{i+1:02d}"
                sensors.append(
                    _get_or_create_sensor(
                        db,
                        sensor_uid=sensor_uid,
                        owner_id=owner.id,
                        network_group_id=area_networks.get(area_key) or owner.network_group_id,
                        latitude=lat + dlat,
                        longitude=lon + dlon,
                        depth=depth,
                    )
                )

        end_ts = _utc_now_floor_hour()
        start_ts = end_ts - timedelta(hours=args.reading_hours)

        # Create readings
        for s in sensors:
            base = random.uniform(28.5, 30.5)
            for h in range(args.reading_hours + 1):
                ts = start_ts + timedelta(hours=h)
                wave = 0.7 * math.sin((2 * math.pi * h) / 24.0)
                noise = random.uniform(-0.15, 0.15)
                temp = base + wave + noise
                _upsert_reading(db, sensor_id=s.id, ts=ts, temperature=float(round(temp, 3)))

            if args.with_predictions:
                _replace_predictions(db, sensor_id=s.id, base_temp=base, start_ts=end_ts)

        print("Seed complete.")
        print(f"Admin login: {admin.email} / {args.admin_password}")
        print("Users:")
        for u in users:
            print(f"  - {u.email} / {args.user_password}")
        print(f"Sensors    : {len(sensors)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

