"""
migrate_network_groups.py
========================
Create `network_groups` + `user_network_groups` tables and backfill membership
from the existing `users.network_group_id` / `sensors.network_group_id` values.

This enables: one user -> many networks.

Usage:
  python migrate_network_groups.py
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

from database import engine, DATABASE_URL  # noqa: E402


def main() -> None:
    print(f"Connecting to: {DATABASE_URL}")

    with engine.begin() as conn:
        if DATABASE_URL.startswith("sqlite"):
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS network_groups (
                      id TEXT PRIMARY KEY,
                      name TEXT NULL,
                      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS user_network_groups (
                      user_id INTEGER NOT NULL,
                      network_group_id TEXT NOT NULL,
                      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                      PRIMARY KEY (user_id, network_group_id),
                      FOREIGN KEY (user_id) REFERENCES users(id),
                      FOREIGN KEY (network_group_id) REFERENCES network_groups(id)
                    );
                    """
                )
            )
        else:
            # Postgres
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS network_groups (
                      id VARCHAR PRIMARY KEY,
                      name VARCHAR NULL,
                      created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS user_network_groups (
                      user_id INTEGER NOT NULL REFERENCES users(id),
                      network_group_id VARCHAR NOT NULL REFERENCES network_groups(id),
                      created_at TIMESTAMPTZ DEFAULT NOW(),
                      PRIMARY KEY (user_id, network_group_id)
                    );
                    """
                )
            )

        # Indexes (best-effort)
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ung_user_id ON user_network_groups (user_id)"))
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_ung_network_group_id ON user_network_groups (network_group_id)"))
        except Exception:
            pass

        # Backfill network_groups from existing ids found in users/sensors
        if DATABASE_URL.startswith("sqlite"):
            conn.execute(
                text(
                    """
                    INSERT OR IGNORE INTO network_groups (id)
                    SELECT DISTINCT network_group_id
                    FROM users
                    WHERE network_group_id IS NOT NULL AND network_group_id != '';
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT OR IGNORE INTO network_groups (id)
                    SELECT DISTINCT network_group_id
                    FROM sensors
                    WHERE network_group_id IS NOT NULL AND network_group_id != '';
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT OR IGNORE INTO user_network_groups (user_id, network_group_id)
                    SELECT id, network_group_id
                    FROM users
                    WHERE network_group_id IS NOT NULL AND network_group_id != '';
                    """
                )
            )
        else:
            conn.execute(
                text(
                    """
                    INSERT INTO network_groups (id)
                    SELECT DISTINCT network_group_id
                    FROM users
                    WHERE network_group_id IS NOT NULL AND network_group_id != ''
                    ON CONFLICT (id) DO NOTHING;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO network_groups (id)
                    SELECT DISTINCT network_group_id
                    FROM sensors
                    WHERE network_group_id IS NOT NULL AND network_group_id != ''
                    ON CONFLICT (id) DO NOTHING;
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO user_network_groups (user_id, network_group_id)
                    SELECT id, network_group_id
                    FROM users
                    WHERE network_group_id IS NOT NULL AND network_group_id != ''
                    ON CONFLICT DO NOTHING;
                    """
                )
            )

    print("Migration complete.")


if __name__ == "__main__":
    main()

