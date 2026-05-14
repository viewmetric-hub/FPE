"""
Load IEX MCP data from monthly Excel files (Market Snapshot format).
Path: /home/bharath/Downloads/redecisionalgorithm/*.xlsx
"""

from __future__ import annotations

import datetime
import glob
import re
from pathlib import Path

import pandas as pd


def _parse_date(val) -> datetime.date | None:
    if pd.isna(val):
        return None
    s = str(val).strip()
    # dd-mm-yyyy or dd/mm/yyyy
    m = re.match(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m:
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime.date(yyyy, mm, dd)
        except ValueError:
            pass
    try:
        dt = pd.to_datetime(val)
        return dt.date()
    except Exception:
        pass
    return None


def _row_to_slot_index(hour: int, block_in_hour: int) -> int:
    """Convert hour (1-24) and block (0-3) to slot_index (1-96)."""
    return (hour - 1) * 4 + block_in_hour + 1


def _col(df: pd.DataFrame, name: str):
    """Get column by name, handling possible quoting."""
    for c in df.columns:
        if name in str(c) or str(c).strip("'\"") == name:
            return c
    return name


def load_single_file(path: str | Path) -> pd.DataFrame:
    """Load one Excel file and return (date, slot_index, mcp) rows."""
    df = pd.read_excel(path, header=4)
    date_col = _col(df, "Date")
    hour_col = _col(df, "Hour")
    mcp_col = _col(df, "MCP")
    if mcp_col not in df.columns:
        return pd.DataFrame(columns=["date", "slot_index", "mcp"])

    rows = []
    block_in_hour = 0
    prev_hour = None

    for _, r in df.iterrows():
        date_val = _parse_date(r.get(date_col, r.get("Date")))
        if date_val is None:
            continue
        hour_val = r.get(hour_col, r.get("Hour"))
        if pd.isna(hour_val):
            continue
        try:
            hour = int(float(hour_val))
        except (TypeError, ValueError):
            continue
        if hour < 1 or hour > 24:
            continue
        mcp_val = r.get(mcp_col)
        if pd.isna(mcp_val):
            mcp_val = 0.0
        try:
            mcp = float(mcp_val)
        except (TypeError, ValueError):
            mcp = 0.0

        if prev_hour is not None and hour != prev_hour:
            block_in_hour = 0
        prev_hour = hour

        slot_idx = _row_to_slot_index(hour, block_in_hour)
        rows.append({"date": date_val, "slot_index": slot_idx, "mcp": mcp})
        block_in_hour = (block_in_hour + 1) % 4

    return pd.DataFrame(rows)


def load_all_data(data_dir: str | Path) -> pd.DataFrame:
    """
    Load all Excel files from data_dir.
    Returns DataFrame with columns: date, slot_index, mcp
    """
    data_dir = Path(data_dir)
    if not data_dir.exists():
        return pd.DataFrame(columns=["date", "slot_index", "mcp"])

    pattern = str(data_dir / "*.xlsx")
    files = sorted(glob.glob(pattern))
    if not files:
        pattern = str(data_dir / "*.xls")
        files = sorted(glob.glob(pattern))

    all_dfs = []
    for f in files:
        try:
            df = load_single_file(f)
            if not df.empty:
                all_dfs.append(df)
        except Exception:
            continue

    if not all_dfs:
        return pd.DataFrame(columns=["date", "slot_index", "mcp"])
    return pd.concat(all_dfs, ignore_index=True).drop_duplicates(
        subset=["date", "slot_index"], keep="last"
    )


DEFAULT_DATA_PATH = Path.home() / "Downloads" / "redecisionalgorithm"
