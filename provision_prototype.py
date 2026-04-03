"""
provision_prototype.py
======================
Create a dashboard user, one network group, and one approved sensor for ESP32 /data testing.

sensor_uid must match JSON "sensor_uid" in POST /data (e.g. node id "1" if you register --sensor-uid 1).

Usage:

  python provision_prototype.py --sensor-uid "1"

Optional:

  python provision_prototype.py --sensor-uid "1" --user-email prototype@sliot.local --user-password proto123
"""

from __future__ import annotations

import argparse
import os
import pathlib
import uuid

from sqlalchemy.exc import IntegrityError

from auth import hash_password
from database import SessionLocal, engine
from models import Base, NetworkGroup, Sensor, User, UserNetworkGroup, UserRole


def _load_env_file_if_needed() -> None:
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

from database import DATABASE_URL  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision prototype user + network + sensor")
    parser.add_argument(
        "--sensor-uid",
        required=True,
        help="sensor_uid for POST /data (e.g. 1 to match numeric node id in firmware)",
    )
    parser.add_argument("--user-email", default="prototype@sliot.local")
    parser.add_argument("--user-password", default="proto123")
    parser.add_argument("--network-name", default="Prototype network")
    parser.add_argument("--latitude", type=float, default=8.88)
    parser.add_argument("--longitude", type=float, default=79.525)
    parser.add_argument("--depth", type=float, default=4.0)
    args = parser.parse_args()

    sensor_uid = args.sensor_uid.strip()
    if not sensor_uid:
        print("Error: --sensor-uid must be non-empty")
        return 1

    Base.metadata.create_all(bind=engine)
    print(f"Database: {DATABASE_URL}")

    db = SessionLocal()
    try:
        existing = db.query(Sensor).filter(Sensor.sensor_uid == sensor_uid).first()
        if existing:
            print(f"Sensor '{sensor_uid}' already exists (id={existing.id}). Nothing to do.")
            return 0

        user = db.query(User).filter(User.email == args.user_email).first()
        if not user:
            user = User(
                email=args.user_email,
                hashed_password=hash_password(args.user_password),
                role=UserRole.user,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"Created user: {user.email}")
        else:
            print(f"Using existing user: {user.email}")

        ngid = f"ng_{uuid.uuid4().hex[:12]}"
        if db.query(NetworkGroup).filter(NetworkGroup.id == ngid).first():
            ngid = f"ng_{uuid.uuid4().hex[:12]}"
        db.add(NetworkGroup(id=ngid, name=args.network_name))
        db.add(UserNetworkGroup(user_id=user.id, network_group_id=ngid))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise
        print(f"Created network group: {ngid} ({args.network_name})")

        sensor = Sensor(
            sensor_uid=sensor_uid,
            owner_id=user.id,
            network_group_id=ngid,
            latitude=args.latitude,
            longitude=args.longitude,
            depth=args.depth,
            is_approved=True,
        )
        db.add(sensor)
        db.commit()
        db.refresh(sensor)

        print("")
        print("Done.")
        print(f"  sensor_uid (POST /data): {sensor.sensor_uid}")
        print(f"  sensor id (DB):         {sensor.id}")
        print(f"  network_group_id:       {ngid}")
        print(f"  owner email:            {args.user_email}")
        print("")
        print("Example POST /data JSON:")
        print(
            f'  {{"sensor_uid": "{sensor_uid}", "timestamp": "2026-04-02T12:00:00Z", "temperature": 29.0}}'
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
