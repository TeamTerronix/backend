"""
auth.py
=======
JWT token generation/validation and FastAPI dependency helpers.

Environment variables (set in your shell or .env):
    SECRET_KEY   — long random string used to sign tokens (CHANGE IN PRODUCTION)
    ALGORITHM    — JWT algorithm, defaults to HS256
    ACCESS_TOKEN_EXPIRE_MINUTES — token lifetime, defaults to 60
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import User, UserRole

# ─── Configuration ────────────────────────────────────────────────────────────

SECRET_KEY  = os.getenv("SECRET_KEY", "change-me-in-production-use-a-long-random-string")
ALGORITHM   = os.getenv("ALGORITHM", "HS256")
TOKEN_TTL   = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

# ─── Helpers ──────────────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


# ─── Pydantic Schemas (auth-specific) ─────────────────────────────────────────

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: Optional[int] = None


class UserCreate(BaseModel):
    email: str
    password: str
    role: UserRole = UserRole.user


class UserOut(BaseModel):
    id: int
    email: str
    role: UserRole

    class Config:
        from_attributes = True


# ─── Password utilities ───────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ─── Token utilities ──────────────────────────────────────────────────────────

def create_access_token(user_id: int, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=TOKEN_TTL))
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> TokenData:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id_str: Optional[str] = payload.get("sub")
        if user_id_str is None:
            raise credentials_exception
        return TokenData(user_id=int(user_id_str))
    except JWTError:
        raise credentials_exception


# ─── FastAPI Dependencies ─────────────────────────────────────────────────────

def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    token_data = decode_access_token(token)
    user = db.query(User).filter(User.id == token_data.user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return current_user
