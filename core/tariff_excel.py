"""
Parse Linde MOD/CUF-style Excel files.
TOD (Hour) sheet: plant names in headers, hourly grid/RE tariffs.
Structure: DISCOM (grid) | 4PEL (RE) | Tariff difference blocks.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd


# Column layout for TOD (Hour) sheet - linde format
# Row 0: Hour, GJ, AP, PJ, UK, OD | Hour, GJ, AP, PJ, UK, OD | Hour, GJ, AP, PJ, UK, OD
PLANT_COL_INDICES = {
    "grid": [1, 2, 3, 4, 5],   # DISCOM landed (grid tariff)
    "re": [8, 9, 10, 11, 12],  # 4PEL landed (RE tariff)
}
# Row 0 column indices for plant names (same order: GJ, AP, PJ, UK, OD)
PLANT_NAME_COL_INDICES = [1, 2, 3, 4, 5]
TOD_SHEET_NAMES = ["TOD (Hour)", "TOD(hour)", "TOD(hour) ", "TOD (hour)"]


def _find_tod_sheet(xls: pd.ExcelFile) -> str | None:
    for name in TOD_SHEET_NAMES:
        if name in xls.sheet_names:
            return name
    # Fallback: first sheet containing "TOD"
    for name in xls.sheet_names:
        if "TOD" in name.upper() and "HOUR" in name.upper():
            return name
    return None


def parse_tariff_excel(file) -> dict[str, Any]:
    """
    Parse Excel file (linde MOD/CUF format).
    Returns:
      {
        "plants": [
          {"excel_name": "GJ", "avg_grid_tariff": 7.9, "avg_re_tariff": 5.4, "hourly_grid": [...], "hourly_re": [...]},
          ...
        ],
        "error": null or str
      }
    """
    try:
        xls = pd.ExcelFile(file)
        sheet_name = _find_tod_sheet(xls)
        if not sheet_name:
            return {"plants": [], "error": "No 'TOD (Hour)' sheet found in Excel."}

        df = pd.read_excel(xls, sheet_name=sheet_name, header=None)

        if df.shape[0] < 2 or df.shape[1] < 13:
            return {"plants": [], "error": "Excel sheet has insufficient rows/columns."}

        # Row 0 = section headers, row 1 = column headers (Hour, GJ, AP, ...), row 2+ = data
        plant_names = []
        for idx in PLANT_NAME_COL_INDICES:
            if idx < df.shape[1]:
                val = df.iloc[1, idx]  # Plant names in row 1
                name = str(val).strip() if pd.notna(val) else ""
                if name and name.upper() != "HOUR" and name != "nan":
                    plant_names.append(name)
                else:
                    plant_names.append(f"Col{idx}")

        grid_cols = PLANT_COL_INDICES["grid"][: len(plant_names)]
        re_cols = PLANT_COL_INDICES["re"][: len(plant_names)]

        # Data rows: row 2 to 25 (hours 1-24)
        data_start_row = 2
        data_end_row = min(26, len(df))

        results = []
        for i, excel_name in enumerate(plant_names):
            gcol = grid_cols[i] if i < len(grid_cols) else None
            rcol = re_cols[i] if i < len(re_cols) else None

            hourly_grid = []
            hourly_re = []

            for row_idx in range(data_start_row, data_end_row):
                def _to_float(val, default=0.0):
                    if pd.isna(val):
                        return default
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        return default

                gval = _to_float(df.iloc[row_idx, gcol]) if gcol is not None and gcol < df.shape[1] else 0.0
                rval = _to_float(df.iloc[row_idx, rcol]) if rcol is not None and rcol < df.shape[1] else 0.0
                hourly_grid.append(gval)
                hourly_re.append(rval)

            avg_grid = sum(hourly_grid) / len(hourly_grid) if hourly_grid else 0.0
            avg_re = sum(hourly_re) / len(hourly_re) if hourly_re else 0.0

            results.append({
                "excel_name": excel_name,
                "avg_grid_tariff": round(avg_grid, 4),
                "avg_re_tariff": round(avg_re, 4),
                "hourly_grid": hourly_grid,
                "hourly_re": hourly_re,
            })

        return {"plants": results, "error": None}
    except Exception as e:
        return {"plants": [], "error": str(e)}
