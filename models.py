"""
models.py
=========
SQLAlchemy ORM models for the SLIOT multi-tenant platform.

Tables
------
- users    : platform accounts with role-based access (admin / user)
- network_groups : logical clusters of sensors ("node networks")
- user_network_groups : membership join table (users can belong to multiple networks)
- sensors  : physical sensor nodes, each owned by a user and approved by an admin

NOTE: Table creation is handled manually by the operator (Base.metadata.create_all).
"""

import enum
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Float,
    Enum,
    ForeignKey,
    DateTime,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


# ─── Enums ────────────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    admin = "admin"
    user  = "user"


# ─── Models ───────────────────────────────────────────────────────────────────

class NetworkGroup(Base):
    __tablename__ = "network_groups"

    id = Column(String, primary_key=True)  # e.g. "ng_bar_reef_01"
    name = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    sensors = relationship("Sensor", back_populates="network_group")
    memberships = relationship("UserNetworkGroup", back_populates="network_group", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<NetworkGroup id={self.id!r}>"


class UserNetworkGroup(Base):
    __tablename__ = "user_network_groups"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    network_group_id = Column(String, ForeignKey("network_groups.id"), primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="network_memberships")
    network_group = relationship("NetworkGroup", back_populates="memberships")

    def __repr__(self) -> str:
        return f"<UserNetworkGroup user_id={self.user_id} network_group_id={self.network_group_id!r}>"


class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role            = Column(Enum(UserRole), default=UserRole.user, nullable=False)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    sensors = relationship("Sensor", back_populates="owner")
    network_memberships = relationship("UserNetworkGroup", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} role={self.role}>"


class Sensor(Base):
    __tablename__ = "sensors"

    id          = Column(Integer, primary_key=True, index=True)
    sensor_uid  = Column(String, unique=True, index=True, nullable=False)  # device-level ID
    owner_id    = Column(Integer, ForeignKey("users.id"), nullable=True)
    network_group_id = Column(String, ForeignKey("network_groups.id"), index=True, nullable=True)
    latitude    = Column(Float, nullable=True)
    longitude   = Column(Float, nullable=True)
    depth       = Column(Float, nullable=True)
    is_approved = Column(Boolean, default=False, nullable=False)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    owner       = relationship("User", back_populates="sensors")
    network_group = relationship("NetworkGroup", back_populates="sensors")
    readings    = relationship("SensorReading", back_populates="sensor", cascade="all, delete-orphan")
    predictions = relationship("Prediction",    back_populates="sensor", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return (
            f"<Sensor id={self.id} uid={self.sensor_uid!r} "
            f"owner_id={self.owner_id} approved={self.is_approved}>"
        )


class SensorReading(Base):
    """Time-series readings: one row per POST /data (full-resolution timestamp)."""
    __tablename__ = "sensor_readings"

    id          = Column(Integer, primary_key=True, index=True)
    sensor_id   = Column(Integer, ForeignKey("sensors.id"), nullable=False, index=True)
    timestamp   = Column(DateTime(timezone=True), nullable=False, index=True)
    temperature = Column(Float, nullable=False)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    sensor = relationship("Sensor", back_populates="readings")

    def __repr__(self) -> str:
        return f"<SensorReading sensor_id={self.sensor_id} ts={self.timestamp} temp={self.temperature}>"


class Prediction(Base):
    """PINN-generated 168-hour forecast rows, refreshed every 6 hours."""
    __tablename__ = "predictions"

    id               = Column(Integer, primary_key=True, index=True)
    sensor_id        = Column(Integer, ForeignKey("sensors.id"), nullable=False, index=True)
    target_timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    predicted_temp   = Column(Float, nullable=False)
    risk_level       = Column(Integer, nullable=False)        # 0 / 1 / 2
    risk_score       = Column(Float, nullable=True)           # 0-1 continuous risk
    anomaly          = Column(Float, nullable=True)           # °C above baseline
    days_stressed    = Column(Integer, nullable=True)         # consecutive warm days
    warming_rate     = Column(Float, nullable=True)           # °C/day
    physics_residual = Column(Float, nullable=True)           # |R|² from heat equation
    created_at       = Column(DateTime(timezone=True), server_default=func.now())

    sensor = relationship("Sensor", back_populates="predictions")

    def __repr__(self) -> str:
        return (
            f"<Prediction sensor_id={self.sensor_id} "
            f"ts={self.target_timestamp} temp={self.predicted_temp} "
            f"risk={self.risk_level} score={self.risk_score}>"
        )
