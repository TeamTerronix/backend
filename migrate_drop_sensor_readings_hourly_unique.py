"""
migrate_drop_sensor_readings_hourly_unique.py
=============================================
Removes the old ``uq_sensor_hourly`` unique constraint so ``POST /data`` can
store every sample (not one row per hour).

Run once against the same database as production (RDS or local):

  python migrate_drop_sensor_readings_hourly_unique.py
"""

from __future__ import annotations

import os
import pathlib

from sqlalchemy import text


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

from database import DATABASE_URL, engine  # noqa: E402


def main() -> int:
    print(f"Database: {DATABASE_URL}")
    with engine.begin() as conn:
        if DATABASE_URL.startswith("postgresql"):
            conn.execute(text("ALTER TABLE sensor_readings DROP CONSTRAINT IF EXISTS uq_sensor_hourly"))
            print("Dropped uq_sensor_hourly (PostgreSQL).")
            return 0

        if DATABASE_URL.startswith("sqlite"):
            # SQLite: try named constraint (newer SQLite) or ignore if absent
            try:
                conn.execute(text("ALTER TABLE sensor_readings DROP CONSTRAINT uq_sensor_hourly"))
                print("Dropped uq_sensor_hourly (SQLite).")
            except Exception as e:
                print(f"SQLite ALTER DROP CONSTRAINT failed ({e!r}); trying legacy rebuild path.")
                # Old SQLite: unique index name may differ — best effort
                try:
                    conn.execute(text("DROP INDEX IF EXISTS uq_sensor_hourly"))
                    print("Dropped index uq_sensor_hourly if it existed.")
                except Exception as e2:
                    print(f"Could not auto-fix SQLite: {e2!r}")
                    print("If inserts still fail, export data, delete sliot.db, recreate tables, re-import.")
            return 0

        print("Unknown DATABASE_URL dialect — run manually:")
        print("  ALTER TABLE sensor_readings DROP CONSTRAINT uq_sensor_hourly;")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
