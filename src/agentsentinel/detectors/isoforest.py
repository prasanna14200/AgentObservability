"""Phase 8 — Isolation Forest anomaly detector.

Trains scikit-learn IsolationForest on normal-labeled training traces only.
Uses exactly the 13 pruned features specified in Phase 7 handoff.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from sklearn.ensemble import IsolationForest

from agentsentinel.data.split import PRUNED_FEATURES

FEATURES = PRUNED_FEATURES
RANDOM_SEED = 42


def train_isolation_forest(train_df: pd.DataFrame) -> IsolationForest:
    normal_train = train_df[train_df["label"] == "normal"].copy()
    X_train = normal_train[FEATURES].values

    model = IsolationForest(
        n_estimators=200,
        max_samples="auto",
        contamination="auto",
        random_state=RANDOM_SEED,
        n_jobs=-1,
    )
    model.fit(X_train)
    return model


def predict_isolation_forest(model: IsolationForest, df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    X = df[FEATURES].values
    df["iso_raw_score"] = model.decision_function(X)
    df["iso_prediction"] = model.predict(X)
    df["iso_anomaly"] = df["iso_prediction"] == -1
    return df


def get_isoforest_metadata() -> dict[str, Any]:
    return {
        "model": "IsolationForest",
        "n_estimators": 200,
        "max_samples": "auto",
        "contamination": "auto",
        "random_state": RANDOM_SEED,
        "features": FEATURES,
        "training_protocol": "Trained only on normal-labeled traces from the temporal train split. Anomalous traces are validation/test only.",
        "feature_set": "13 pruned features from data/features/v2/features.parquet",
    }
