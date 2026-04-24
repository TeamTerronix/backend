"""
backfill_recent_readings.py
===========================
Demo utility: insert synthetic readings for *existing* sensors.

Use-case
--------
- AWS RDS has sensors ("nodes") but no recent readings (no devices online yet).
- For a demo, backfill "today + yesterday" so the dashboard has data.

Behavior
--------
- Connects using DATABASE_URL from environment or backend/.env (same loader style as seed_data.py).
- For every row in `sensors`, inserts hourly readings for the last N hours (default 48).
- Does not modify sensor metadata (approval, lat/lon, network) — only inserts readings.
- Skips any reading that already exists for (sensor_id, timestamp) so it is safe to re-run.
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

# Imports that depend on DATABASE_URL
from database import SessionLocal, DATABASE_URL  # noqa: E402
from models import Sensor, SensorReading  # noqa: E402


def _utc_now_floor_hour() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=48, help="How many hours back to fill (default: 48)")
    parser.add_argument("--seed", type=int, default=2026, help="Random seed for repeatable demo temps")
    parser.add_argument("--min-base", type=float, default=28.5, help="Min base temperature (°C)")
    parser.add_argument("--max-base", type=float, default=30.5, help="Max base temperature (°C)")
    args = parser.parse_args()

    random.seed(int(args.seed))

    print(f"Backfilling readings using: {DATABASE_URL}")
    end_ts = _utc_now_floor_hour()
    start_ts = end_ts - timedelta(hours=int(args.hours))

    db = SessionLocal()
    try:
        sensors = db.query(Sensor).all()
        if not sensors:
            print("No sensors found in DB. Create/provision sensors first, then re-run.")
            return 2

        print(f"Sensors found: {len(sensors)}")
        print(f"Time window  : {start_ts.isoformat()} → {end_ts.isoformat()} (hourly)")

        inserted = 0
        for s in sensors:
            # Stable-ish per-sensor base temp so plots look consistent.
            # (We intentionally keep it deterministic across runs with the global seed.)
            base = random.uniform(float(args.min_base), float(args.max_base))

            for h in range(int(args.hours) + 1):
                ts = start_ts + timedelta(hours=h)
                # Simple diurnal-ish cycle + small noise
                wave = 0.7 * math.sin((2 * math.pi * h) / 24.0)
                noise = random.uniform(-0.15, 0.15)
                temp = float(round(base + wave + noise, 3))

                before = db.query(SensorReading.id).filter(
                    SensorReading.sensor_id == s.id,
                    SensorReading.timestamp == ts,
                ).first()
                if before:
                    continue

                _upsert_reading(db, sensor_id=s.id, ts=ts, temperature=temp)
                inserted += 1

        print(f"Inserted readings: {inserted}")
        print("Done.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

