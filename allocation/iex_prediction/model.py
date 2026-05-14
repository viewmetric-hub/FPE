"""
Train and predict slot-wise MCP using historical IEX data.
Uses lagged features + calendar features for each slot.
"""

from __future__ import annotations

import datetime
import pickle
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    HAS_LGB = False

from sklearn.ensemble import GradientBoostingRegressor
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import mean_absolute_error, mean_squared_error

from allocation.slot_utils import SLOTS_PER_DAY

# Lag days for features
LAG_DAYS = [1, 2, 3, 7, 14]
ROLLING_WINDOW = 7


def _build_features(df: pd.DataFrame, as_of_date: datetime.date) -> pd.DataFrame:
    """
    Build feature matrix for prediction.
    df: long format (date, slot_index, mcp)
    as_of_date: we can use data up to (as_of_date - 1) for features.
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["date", "slot_index"])

    wide = df.pivot(index="date", columns="slot_index", values="mcp").reset_index()
    wide.columns = ["date"] + [f"slot_{i}" for i in range(1, 97)]

    wide["dow"] = wide["date"].dt.dayofweek
    wide["month"] = wide["date"].dt.month
    wide["day"] = wide["date"].dt.day

    wide = wide[wide["date"] < pd.Timestamp(as_of_date)].sort_values("date").reset_index(drop=True)
    if wide.empty:
        return pd.DataFrame()

    slot_cols = [f"slot_{i}" for i in range(1, 97)]
    rows = []

    for slot in range(1, SLOTS_PER_DAY + 1):
        col = f"slot_{slot}"
        if col not in wide.columns:
            continue
        series = wide[col]
        for i, (idx, row) in enumerate(wide.iterrows()):
            mcp = row[col]
            if pd.isna(mcp):
                continue
            dt = row["date"].date()

            feats = {
                "date": dt,
                "slot_index": slot,
                "dow": row["dow"],
                "month": row["month"],
                "day": row["day"],
                "slot_hour": (slot - 1) // 4 + 1,
                "slot_in_hour": (slot - 1) % 4,
                "mcp": mcp,
            }
            for lag in LAG_DAYS:
                j = i - lag
                feats[f"lag_{lag}d"] = series.iloc[j] if j >= 0 else np.nan
            start = max(0, i - ROLLING_WINDOW)
            feats["roll_mean_7d"] = series.iloc[start:i].mean() if start < i else np.nan
            rows.append(feats)

    return pd.DataFrame(rows)


def train_model(
    df: pd.DataFrame,
    test_size_days: int = 30,
    model_path: str | Path | None = None,
) -> object:
    """
    Train prediction model. Uses last test_size_days for validation.
    Returns fitted model and optionally saves to model_path.
    """
    if df.empty or len(df) < 100:
        raise ValueError("Insufficient data for training")

    max_date = df["date"].max()
    if isinstance(max_date, pd.Timestamp):
        max_date = max_date.date()
    split_date = max_date - datetime.timedelta(days=test_size_days)

    feats = _build_features(df, split_date + datetime.timedelta(days=365))
    if feats.empty:
        raise ValueError("No features generated")

    # Fill NaN lags with column mean
    lag_cols = [c for c in feats.columns if c.startswith("lag_") or c == "roll_mean_7d"]
    for c in lag_cols:
        feats[c] = feats[c].fillna(feats[c].mean())

    feature_cols = ["dow", "month", "day", "slot_index", "slot_hour", "slot_in_hour"] + lag_cols
    X = feats[feature_cols]
    y = feats["mcp"]

    train_mask = feats["date"] < split_date
    X_train, y_train = X[train_mask], y[train_mask]
    X_val, y_val = X[~train_mask], y[~train_mask]

    if len(X_train) < 50:
        raise ValueError("Too few training samples")

    if HAS_LGB:
        model = lgb.LGBMRegressor(
            n_estimators=200,
            max_depth=6,
            learning_rate=0.05,
            num_leaves=31,
            random_state=42,
            verbose=-1,
        )
    else:
        model = GradientBoostingRegressor(
            n_estimators=200,
            max_depth=6,
            learning_rate=0.05,
            random_state=42,
        )

    model.fit(X_train, y_train)
    pred_val = model.predict(X_val)
    mae = mean_absolute_error(y_val, pred_val)
    rmse = np.sqrt(mean_squared_error(y_val, pred_val))
    print(f"Validation MAE: {mae:.2f} Rs/MWh, RMSE: {rmse:.2f}")

    if model_path:
        Path(model_path).parent.mkdir(parents=True, exist_ok=True)
        with open(model_path, "wb") as f:
            pickle.dump({"model": model, "feature_cols": feature_cols}, f)

    return model
