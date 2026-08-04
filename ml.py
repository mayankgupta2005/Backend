"""ML training + inference for NovaShields.

Simple pipeline:
- Dataset shape: CSV with columns [ax, ay, az, gx, gy, gz, pitch, roll, lean_angle, speed_kmh, label]
- label in {normal, hard_brake, pothole, hard_lean, bike_fall, collision}
- Train a RandomForest and persist to /app/backend/data/model.pkl
- Inference returns predicted class + probability vector
"""
import io
import os
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, classification_report
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

FEATURES = ["ax", "ay", "az", "gx", "gy", "gz", "pitch", "roll", "lean_angle", "speed_kmh"]
LABEL = "label"

MODEL_DIR = Path(__file__).resolve().parent / "data"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
MODEL_PATH = MODEL_DIR / "model.pkl"
DATASET_PATH = MODEL_DIR / "dataset.csv"


def synth_seed_dataset(n_per_class: int = 40) -> pd.DataFrame:
    """Generate a small labelled dataset so users can retrain out-of-the-box."""
    rng = np.random.default_rng(7)
    rows = []
    scenarios = {
        "normal": dict(ax=(-0.15, 0.15), ay=(-0.1, 0.1), az=(0.95, 1.05),
                       gx=(-5, 5), gy=(-5, 5), gz=(-5, 5),
                       pitch=(-4, 4), roll=(-6, 6), lean=(-10, 10), spd=(20, 60)),
        "hard_brake": dict(ax=(-2.3, -1.4), ay=(-0.3, 0.3), az=(0.7, 1.0),
                           gx=(-15, 5), gy=(-10, 10), gz=(-8, 8),
                           pitch=(-15, -3), roll=(-4, 4), lean=(-8, 8), spd=(30, 70)),
        "pothole": dict(ax=(-0.6, 0.6), ay=(-0.6, 0.6), az=(0.4, 1.8),
                        gx=(-30, 30), gy=(-30, 30), gz=(-15, 15),
                        pitch=(-8, 8), roll=(-8, 8), lean=(-6, 6), spd=(20, 55)),
        "hard_lean": dict(ax=(-0.3, 0.3), ay=(-1.1, 1.1), az=(0.6, 1.0),
                          gx=(-20, 20), gy=(-40, 40), gz=(-25, 25),
                          pitch=(-6, 6), roll=(-8, 8), lean=(30, 45), spd=(35, 70)),
        "bike_fall": dict(ax=(0.1, 0.4), ay=(-0.5, 0.5), az=(0.05, 0.25),
                          gx=(-20, 40), gy=(-40, 40), gz=(-40, 40),
                          pitch=(-40, -15), roll=(-20, 45), lean=(45, 80), spd=(15, 45)),
        "collision": dict(ax=(1.6, 3.2), ay=(-1.8, 1.8), az=(0.3, 1.4),
                          gx=(-200, 200), gy=(-150, 150), gz=(-80, 80),
                          pitch=(-45, 15), roll=(-50, 50), lean=(30, 80), spd=(30, 80)),
    }
    for label, r in scenarios.items():
        for _ in range(n_per_class):
            row = {
                "ax": rng.uniform(*r["ax"]),
                "ay": rng.uniform(*r["ay"]),
                "az": rng.uniform(*r["az"]),
                "gx": rng.uniform(*r["gx"]),
                "gy": rng.uniform(*r["gy"]),
                "gz": rng.uniform(*r["gz"]),
                "pitch": rng.uniform(*r["pitch"]),
                "roll": rng.uniform(*r["roll"]),
                "lean_angle": rng.uniform(*r["lean"]),
                "speed_kmh": rng.uniform(*r["spd"]),
                "label": label,
            }
            rows.append(row)
    return pd.DataFrame(rows).sample(frac=1, random_state=7).reset_index(drop=True)


def ensure_seed_dataset():
    if not DATASET_PATH.exists():
        df = synth_seed_dataset()
        df.to_csv(DATASET_PATH, index=False)


def load_dataset() -> pd.DataFrame:
    ensure_seed_dataset()
    return pd.read_csv(DATASET_PATH)


def append_dataset(rows: list[dict]) -> int:
    ensure_seed_dataset()
    df = pd.read_csv(DATASET_PATH)
    new = pd.DataFrame(rows)
    keep_cols = FEATURES + [LABEL]
    new = new[[c for c in keep_cols if c in new.columns]]
    if LABEL not in new.columns:
        raise ValueError("dataset rows missing 'label' column")
    df = pd.concat([df, new], ignore_index=True)
    df.to_csv(DATASET_PATH, index=False)
    return len(new)


def replace_dataset_from_csv(csv_bytes: bytes) -> dict:
    df = pd.read_csv(io.BytesIO(csv_bytes))
    missing = [c for c in FEATURES + [LABEL] if c not in df.columns]
    if missing:
        raise ValueError(f"CSV missing columns: {missing}")
    df.to_csv(DATASET_PATH, index=False)
    return {"rows": len(df), "labels": df[LABEL].value_counts().to_dict()}


def train_model() -> dict:
    df = load_dataset()
    if df[LABEL].nunique() < 2:
        raise ValueError("Need at least 2 label classes to train")
    X, y = df[FEATURES].values, df[LABEL].values
    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", RandomForestClassifier(n_estimators=200, random_state=42, class_weight="balanced")),
    ])
    pipe.fit(X_tr, y_tr)
    preds = pipe.predict(X_te)
    acc = accuracy_score(y_te, preds)
    report = classification_report(y_te, preds, output_dict=True, zero_division=0)
    joblib.dump({"model": pipe, "features": FEATURES, "classes": list(pipe.classes_)}, MODEL_PATH)
    return {
        "accuracy": float(acc),
        "n_train": int(len(X_tr)),
        "n_test": int(len(X_te)),
        "classes": list(pipe.classes_),
        "per_class": {k: v for k, v in report.items() if k not in ("accuracy", "macro avg", "weighted avg")},
    }


_loaded: dict[str, Any] | None = None


def _load():
    global _loaded
    if _loaded is None and MODEL_PATH.exists():
        _loaded = joblib.load(MODEL_PATH)
    return _loaded


def predict_frame(frame: dict) -> dict:
    m = _load()
    if not m:
        return {"available": False, "reason": "model not trained yet"}
    x = np.array([[float(frame.get(k, 0) or 0) for k in FEATURES]])
    proba = m["model"].predict_proba(x)[0]
    idx = int(np.argmax(proba))
    return {
        "available": True,
        "verdict": str(m["classes"][idx]),
        "confidence": float(proba[idx]),
        "probs": {str(c): float(p) for c, p in zip(m["classes"], proba)},
    }


def model_status() -> dict:
    return {
        "trained": MODEL_PATH.exists(),
        "path": str(MODEL_PATH),
        "dataset_rows": int(len(load_dataset())),
    }


def reset_dataset():
    if DATASET_PATH.exists():
        DATASET_PATH.unlink()
    ensure_seed_dataset()
    return {"rows": int(len(load_dataset()))}
