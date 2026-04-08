from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

ARTIFACT_DIR = Path(__file__).parent / "artifacts"
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_PATH = DATA_DIR / "normal_typing_sessions.csv"

FEATURE_COLUMNS = ["mean_interval", "std_interval", "wpm", "pause_ratio"]


def synthesize_normal_data(rows: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    return pd.DataFrame(
        {
            "mean_interval": rng.normal(150, 22, size=rows).clip(70, 260),
            "std_interval": rng.normal(45, 15, size=rows).clip(10, 120),
            "wpm": rng.normal(42, 8, size=rows).clip(20, 85),
            "pause_ratio": rng.normal(0.12, 0.05, size=rows).clip(0.0, 0.35),
        }
    )


def load_training_data() -> pd.DataFrame:
    if DATA_PATH.exists():
        data = pd.read_csv(DATA_PATH)
        missing = [c for c in FEATURE_COLUMNS if c not in data.columns]
        if missing:
            raise ValueError(f"Missing columns in {DATA_PATH.name}: {missing}")
        return data[FEATURE_COLUMNS].copy()
    return synthesize_normal_data()


def main() -> None:
    data = load_training_data()
    scaler = StandardScaler()
    x_scaled = scaler.fit_transform(data.values)

    model = IsolationForest(
        n_estimators=300,
        contamination=0.08,
        random_state=42,
    )
    model.fit(x_scaled)

    scores = -model.score_samples(x_scaled)
    threshold = float(np.percentile(scores, 92))

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, ARTIFACT_DIR / "isolation_forest.joblib")
    joblib.dump(scaler, ARTIFACT_DIR / "scaler.joblib")
    joblib.dump(
        {
            "anomaly_threshold": threshold,
            "feature_columns": FEATURE_COLUMNS,
            "rows_trained": len(data),
        },
        ARTIFACT_DIR / "meta.joblib",
    )

    print(f"Model trained on {len(data)} rows")
    print(f"Anomaly threshold: {threshold:.4f}")
    print(f"Artifacts saved to: {ARTIFACT_DIR}")


if __name__ == "__main__":
    main()
