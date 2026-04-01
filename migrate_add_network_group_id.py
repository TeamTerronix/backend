"""
migrate_add_network_group_id.py
==============================
One-off migration: add `network_group_id` to `users` and `sensors`.

This project uses simple scripts instead of Alembic migrations.

Usage:
  python migrate_add_network_group_id.py
"""

from __future__ import annotations

import os
import pathlib

from sqlalchemy import text

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

from database import engine, DATABASE_URL  # noqa: E402


def _col_exists(table: str, col: str) -> bool:
    with engine.connect() as conn:
        if DATABASE_URL.startswith("sqlite"):
            rows = conn.execute(text(f"PRAGMA table_info({table})")).mappings().all()
            return any(r.get("name") == col for r in rows)
        # Postgres & others that support information_schema
        rows = conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = :table
                  AND column_name = :col
                LIMIT 1
                """
            ),
            {"table": table, "col": col},
        ).all()
        return len(rows) > 0


def main() -> None:
    print(f"Connecting to: {DATABASE_URL}")
    with engine.begin() as conn:
        if not _col_exists("users", "network_group_id"):
            conn.execute(text("ALTER TABLE users ADD COLUMN network_group_id VARCHAR"))
            try:
                conn.execute(text("CREATE INDEX ix_users_network_group_id ON users (network_group_id)"))
            except Exception:
                pass
            print("Added users.network_group_id")
        else:
            print("users.network_group_id already exists")

        if not _col_exists("sensors", "network_group_id"):
            conn.execute(text("ALTER TABLE sensors ADD COLUMN network_group_id VARCHAR"))
            try:
                conn.execute(text("CREATE INDEX ix_sensors_network_group_id ON sensors (network_group_id)"))
            except Exception:
                pass
            print("Added sensors.network_group_id")
        else:
            print("sensors.network_group_id already exists")

        # ── Backfill existing rows ─────────────────────────────────────────────
        if DATABASE_URL.startswith("sqlite"):
            # Derive from email prefix: ng_<localpart with '-'→'_'>
            conn.execute(
                text(
                    """
                    UPDATE users
                    SET network_group_id = 'ng_' || REPLACE(SUBSTR(email, 1, INSTR(email, '@') - 1), '-', '_')
                    WHERE network_group_id IS NULL
                      AND email LIKE '%@%';
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE users
                    SET network_group_id = 'ng_admin'
                    WHERE network_group_id IS NULL
                      AND role = 'admin';
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE sensors
                    SET network_group_id = (
                      SELECT u.network_group_id
                      FROM users u
                      WHERE u.id = sensors.owner_id
                    )
                    WHERE network_group_id IS NULL
                      AND owner_id IS NOT NULL;
                    """
                )
            )
        else:
            # Postgres
            conn.execute(
                text(
                    """
                    UPDATE users
                    SET network_group_id = 'ng_' || REPLACE(SPLIT_PART(email, '@', 1), '-', '_')
                    WHERE network_group_id IS NULL;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE users
                    SET network_group_id = 'ng_admin'
                    WHERE role = 'admin'
                      AND (network_group_id IS NULL OR network_group_id = '');
                    """
                )
            )
            conn.execute(
                text(
                    """
                    UPDATE sensors s
                    SET network_group_id = u.network_group_id
                    FROM users u
                    WHERE s.owner_id = u.id
                      AND (s.network_group_id IS NULL OR s.network_group_id = '');
                    """
                )
            )

        print("Backfilled network_group_id values (where possible)")

    print("Migration complete.")


if __name__ == "__main__":
    main()

