"""
database.py
===========
SQLAlchemy engine, session factory, and Base declaration.

Set DATABASE_URL in your environment (or .env file) before importing this module
(or run scripts like provision_prototype.py that load .env first).
Example (PostgreSQL):
    DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/sliot

Example (SQLite for local dev):
    DATABASE_URL=sqlite:///./sliot.db
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sliot.db")

# SQLite requires connect_args; ignored for other backends
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a DB session and closes it afterwards."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
