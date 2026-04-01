"""
migrate_drop_users_network_group_id.py
=====================================
Drop legacy `users.network_group_id` column.

We now use `user_network_groups` for memberships (many-to-many).

Usage:
  python migrate_drop_users_network_group_id.py
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

    if DATABASE_URL.startswith("sqlite"):
        print("SQLite: dropping columns requires table rebuild; skipping.")
        return

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS network_group_id"))
        try:
            conn.execute(text("DROP INDEX IF EXISTS ix_users_network_group_id"))
        except Exception:
            pass

    print("Dropped users.network_group_id")


if __name__ == "__main__":
    main()

