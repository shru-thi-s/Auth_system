from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal
from datetime import datetime, timezone
from threading import Lock

import joblib
import numpy as np
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ARTIFACT_DIR = Path(__file__).parent / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "isolation_forest.joblib"
SCALER_PATH = ARTIFACT_DIR / "scaler.joblib"
META_PATH = ARTIFACT_DIR / "meta.joblib"
STATIC_DIR = Path(__file__).parent / "static"
DB_PATH = Path(__file__).parent / "typing_monitor.db"


class FeaturePayload(BaseModel):
    mean_interval: float = Field(ge=0)
    std_interval: float = Field(ge=0)
    wpm: float = Field(ge=0)
    pause_ratio: float = Field(ge=0, le=1)


class PredictRequest(BaseModel):
    device_id: str
    session_id: str
    features: FeaturePayload


class PredictResponse(BaseModel):
    status: Literal["normal", "anomaly"]
    score: float
    message: str


class RuleFallback:
    def __init__(self) -> None:
        self.mean_interval_bounds = (45.0, 450.0)
        self.std_interval_max = 250.0
        self.wpm_bounds = (15.0, 130.0)
        self.pause_ratio_max = 0.45

    def score(self, vector: np.ndarray) -> float:
        mean_interval, std_interval, wpm, pause_ratio = vector.tolist()
        penalties = 0.0
        if not (self.mean_interval_bounds[0] <= mean_interval <= self.mean_interval_bounds[1]):
            penalties += 0.35
        if std_interval > self.std_interval_max:
            penalties += 0.2
        if not (self.wpm_bounds[0] <= wpm <= self.wpm_bounds[1]):
            penalties += 0.25
        if pause_ratio > self.pause_ratio_max:
            penalties += 0.2
        return min(1.0, penalties)


def _load_artifacts():
    if MODEL_PATH.exists() and SCALER_PATH.exists() and META_PATH.exists():
        model = joblib.load(MODEL_PATH)
        scaler = joblib.load(SCALER_PATH)
        meta = joblib.load(META_PATH)
        return model, scaler, float(meta.get("anomaly_threshold", 0.5))
    return None, None, 0.5


model, scaler, anomaly_threshold = _load_artifacts()
rule_fallback = RuleFallback()

app = FastAPI(title="Typing Behavior Monitor API", version="1.0.0")

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class SessionEvent(BaseModel):
    device_id: str
    session_id: str
    status: Literal["normal", "anomaly"]
    score: float
    mean_interval: float
    std_interval: float
    wpm: float
    pause_ratio: float
    timestamp: str


class SessionLogResponse(BaseModel):
    device_id: str
    session_id: str
    events: list[SessionEvent]


class OverviewResponse(BaseModel):
    total_events: int
    normal_events: int
    anomaly_events: int
    sessions_tracked: int
    latest_timestamp: str | None

_log_lock = Lock()


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _initialize_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _open_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prediction_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                score REAL NOT NULL,
                mean_interval REAL NOT NULL,
                std_interval REAL NOT NULL,
                wpm REAL NOT NULL,
                pause_ratio REAL NOT NULL,
                timestamp TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prediction_session ON prediction_events(device_id, session_id, timestamp)"
        )
        conn.commit()


@app.on_event("startup")
def _startup() -> None:
    _initialize_db()


_initialize_db()


def _append_session_event(event: SessionEvent) -> None:
    with _log_lock:
        with _open_db() as conn:
            conn.execute(
                """
                INSERT INTO prediction_events (
                    device_id, session_id, status, score,
                    mean_interval, std_interval, wpm, pause_ratio, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.device_id,
                    event.session_id,
                    event.status,
                    event.score,
                    event.mean_interval,
                    event.std_interval,
                    event.wpm,
                    event.pause_ratio,
                    event.timestamp,
                ),
            )
            conn.commit()


def _build_session_event(payload: PredictRequest, response: PredictResponse) -> SessionEvent:
    return SessionEvent(
        device_id=payload.device_id,
        session_id=payload.session_id,
        status=response.status,
        score=response.score,
        mean_interval=payload.features.mean_interval,
        std_interval=payload.features.std_interval,
        wpm=payload.features.wpm,
        pause_ratio=payload.features.pause_ratio,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _row_to_event(row: sqlite3.Row) -> SessionEvent:
    return SessionEvent(
        device_id=row["device_id"],
        session_id=row["session_id"],
        status=row["status"],
        score=float(row["score"]),
        mean_interval=float(row["mean_interval"]),
        std_interval=float(row["std_interval"]),
        wpm=float(row["wpm"]),
        pause_ratio=float(row["pause_ratio"]),
        timestamp=row["timestamp"],
    )


def _load_session_events(device_id: str | None = None, session_id: str | None = None, limit: int = 100) -> list[SessionEvent]:
    clauses: list[str] = []
    params: list[str | int] = []
    if device_id:
        clauses.append("device_id = ?")
        params.append(device_id)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT * FROM prediction_events {where_sql} ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _log_lock:
        with _open_db() as conn:
            rows = conn.execute(query, params).fetchall()
    return [_row_to_event(row) for row in reversed(rows)]


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "model_loaded": bool(model is not None and scaler is not None),
        "mode": "ml" if model is not None and scaler is not None else "rule-fallback",
    }


@app.get("/overview", response_model=OverviewResponse)
def overview() -> OverviewResponse:
    with _log_lock:
        with _open_db() as conn:
            total_events = conn.execute("SELECT COUNT(*) FROM prediction_events").fetchone()[0]
            normal_events = conn.execute("SELECT COUNT(*) FROM prediction_events WHERE status = 'normal'").fetchone()[0]
            anomaly_events = conn.execute("SELECT COUNT(*) FROM prediction_events WHERE status = 'anomaly'").fetchone()[0]
            sessions_tracked = conn.execute("SELECT COUNT(DISTINCT device_id || ':' || session_id) FROM prediction_events").fetchone()[0]
            latest_timestamp = conn.execute("SELECT timestamp FROM prediction_events ORDER BY id DESC LIMIT 1").fetchone()

    return OverviewResponse(
        total_events=int(total_events),
        normal_events=int(normal_events),
        anomaly_events=int(anomaly_events),
        sessions_tracked=int(sessions_tracked),
        latest_timestamp=str(latest_timestamp[0]) if latest_timestamp else None,
    )


@app.post("/predict", response_model=PredictResponse)
def predict(payload: PredictRequest) -> PredictResponse:
    vector = np.array(
        [
            payload.features.mean_interval,
            payload.features.std_interval,
            payload.features.wpm,
            payload.features.pause_ratio,
        ],
        dtype=float,
    )

    if model is not None and scaler is not None:
        scaled = scaler.transform([vector])
        raw_score = float(-model.score_samples(scaled)[0])
        status: Literal["normal", "anomaly"] = "anomaly" if raw_score >= anomaly_threshold else "normal"
        message = "Unusual typing pattern detected" if status == "anomaly" else "Typing pattern is stable"
        response = PredictResponse(status=status, score=round(raw_score, 4), message=message)
        _append_session_event(_build_session_event(payload, response))
        return response

    fallback_score = rule_fallback.score(vector)
    status = "anomaly" if fallback_score >= anomaly_threshold else "normal"
    message = "Fallback mode anomaly" if status == "anomaly" else "Fallback mode normal"
    response = PredictResponse(status=status, score=round(float(fallback_score), 4), message=message)
    _append_session_event(_build_session_event(payload, response))
    return response


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    return FileResponse(index_path)


@app.get("/sessions/{device_id}/{session_id}", response_model=SessionLogResponse)
def get_session_history(device_id: str, session_id: str) -> SessionLogResponse:
    events = _load_session_events(device_id=device_id, session_id=session_id, limit=100)
    return SessionLogResponse(device_id=device_id, session_id=session_id, events=events)


@app.get("/sessions", response_model=list[SessionLogResponse])
def list_sessions() -> list[SessionLogResponse]:
    with _log_lock:
        with _open_db() as conn:
            rows = conn.execute(
                """
                SELECT device_id, session_id,
                       MAX(id) AS last_id
                FROM prediction_events
                GROUP BY device_id, session_id
                ORDER BY last_id DESC
                """
            ).fetchall()

    return [
        SessionLogResponse(
            device_id=row["device_id"],
            session_id=row["session_id"],
            events=_load_session_events(row["device_id"], row["session_id"], limit=100),
        )
        for row in rows
    ]
