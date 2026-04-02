"""
Insert one sensor reading at the current UTC hour for every approved sensor.

Matches the /data ingest behaviour: timestamp is floored to the hour. Skips
if a reading already exists for that sensor × hour (idempotent).

Usage (on EC2, from backend dir with .env pointing at your DB):

  python seed_current_hour_readings.py

Optional:

  python seed_current_hour_readings.py --include-unapproved
  python seed_current_hour_readings.py --dry-run
"""

from __future__ import annotations

import argparse
import math
import os
import pathlib
import random
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError


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

from database import SessionLocal, DATABASE_URL  # noqa: E402
from models import Sensor, SensorReading  # noqa: E402


def _utc_now_floor_hour() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)


def _upsert_reading(db, *, sensor_id: int, ts: datetime, temperature: float) -> str:
    exists = (
        db.query(SensorReading)
        .filter(SensorReading.sensor_id == sensor_id, SensorReading.timestamp == ts)
        .first()
    )
    if exists:
        return "skipped"
    db.add(SensorReading(sensor_id=sensor_id, timestamp=ts, temperature=temperature))
    try:
        db.commit()
        return "inserted"
    except IntegrityError:
        db.rollback()
        return "skipped"


def _synthetic_temp_for_hour(ts: datetime) -> float:
    """Same style as seed_data.py (one hourly point)."""
    base = random.uniform(28.5, 30.5)
    h = ts.hour
    wave = 0.7 * math.sin((2 * math.pi * h) / 24.0)
    noise = random.uniform(-0.15, 0.15)
    return float(round(base + wave + noise, 3))


def main() -> int:
    parser = argparse.ArgumentParser(description="Add current-hour readings for all sensors.")
    parser.add_argument(
        "--include-unapproved",
        action="store_true",
        help="Also add readings for sensors that are not approved (default: approved only).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions only; no DB writes.")
    args = parser.parse_args()

    ts = _utc_now_floor_hour()
    db = SessionLocal()
    try:
        q = db.query(Sensor)
        if not args.include_unapproved:
            q = q.filter(Sensor.is_approved.is_(True))
        sensors = q.order_by(Sensor.id).all()

        print(f"Database: {DATABASE_URL}")
        print(f"Target UTC hour: {ts.isoformat()}")
        print(f"Sensors: {len(sensors)}")

        inserted = skipped = 0
        for s in sensors:
            temp = _synthetic_temp_for_hour(ts)
            if args.dry_run:
                print(f"  [dry-run] sensor_id={s.id} uid={s.sensor_uid!r} temp={temp}")
                continue
            status = _upsert_reading(db, sensor_id=s.id, ts=ts, temperature=temp)
            if status == "inserted":
                inserted += 1
            else:
                skipped += 1

        if not args.dry_run:
            print(f"Done. inserted={inserted} skipped (already had this hour)={skipped}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
