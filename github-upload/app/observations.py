"""Append-only raw gauge observations in external PostgreSQL; never use cache disk."""
import hmac
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

router = APIRouter(prefix="/api/observations")


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    observed_at: datetime
    reading_inches: Decimal = Field(ge=-1200, le=1200, max_digits=9, decimal_places=4)
    note: str = Field(default="", max_length=2000)

    @field_validator("observed_at", mode="before")
    @classmethod
    def timestamp_string(cls, value):
        if not isinstance(value, str):
            raise ValueError("Use an ISO date/time with a timezone offset")
        return value

    @field_validator("observed_at")
    @classmethod
    def valid_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Observation time must include a timezone offset")
        if value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("Observation time cannot be in the future")
        return value

    @field_validator("reading_inches", mode="before")
    @classmethod
    def numeric_reading(cls, value):
        if isinstance(value, bool):
            raise ValueError("Reading must be a number")
        return value


def settings():
    url = os.getenv("OBSERVATIONS_DATABASE_URL", "").strip()
    key = os.getenv("OBSERVATIONS_WRITE_KEY", "")
    if not url or not key:
        raise HTTPException(503, "Observation storage is not configured. Do not enter readings yet.")
    if not url.startswith(("postgresql://", "postgres://")):
        raise HTTPException(503, "Observation database configuration needs attention.")
    return url, key


def connection(url):
    return psycopg.connect(url, connect_timeout=5, options="-c statement_timeout=5000", row_factory=dict_row)


def unavailable():
    return HTTPException(503, "Observation database unavailable. Your entry has not been confirmed saved; keep it and retry.")


@router.get("")
def recent(limit: int = Query(default=10, ge=1, le=100)):
    url, _ = settings()
    try:
        with connection(url) as conn:
            rows = conn.execute("SELECT id, observed_at, submitted_at, reading_inches, note FROM local_tide_observations ORDER BY observed_at DESC, submitted_at DESC, id DESC LIMIT %s", (limit,)).fetchall()
        return {"storage": "postgresql", "ready": True, "observations": rows}
    except psycopg.Error:
        raise unavailable() from None


@router.post("")
def save(observation: Observation, x_observations_key: str = Header(default="")):
    url, key = settings()
    if not hmac.compare_digest(x_observations_key.encode(), key.encode()):
        raise HTTPException(401, "Enter the correct observation access key.")
    try:
        with connection(url) as conn:
            row = conn.execute("""INSERT INTO local_tide_observations
                (id, observed_at, reading_inches, note) VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                RETURNING id, observed_at, submitted_at, reading_inches, note""",
                (observation.id, observation.observed_at, observation.reading_inches, observation.note)).fetchone()
            if row is None:
                row = conn.execute("SELECT id, observed_at, submitted_at, reading_inches, note FROM local_tide_observations WHERE id = %s", (observation.id,)).fetchone()
                if (row["observed_at"] != observation.observed_at or row["reading_inches"] != observation.reading_inches or row["note"] != observation.note):
                    raise HTTPException(409, "This submission ID already belongs to a different observation.")
        # The transaction has committed before success is returned.
        return {"saved": True, "observation": row}
    except psycopg.Error:
        raise unavailable() from None
