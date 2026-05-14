# IEX MCP Prediction

Slot-wise prediction of IEX (Indian Energy Exchange) Market Clearing Price (Rs/MWh) for the Green Day-Ahead Market. Used by the allocation AI to suggest optimal energy allocation when live IEX data is unavailable (e.g. future dates).

## Data

Historical IEX data from monthly Excel files (Market Snapshot format):

- **Default path:** `~/Downloads/redecisionalgorithm/`
- **Files:** `Jan'24.xlsx`, `Feb'24.xlsx`, ... (one per month)
- **Structure:** Date, Hour, Time Block (15-min), MCP (Rs/MWh), etc.

## Model

- **Features:** day-of-week, month, day, slot_index, slot_hour, lagged MCP (1,2,3,7,14 days), 7-day rolling mean
- **Algorithm:** LightGBM (if installed) or scikit-learn GradientBoostingRegressor
- **Output:** 96 slot-wise MCP predictions per date

## Usage

### Train the model

```bash
python manage.py train_iex_predictor

# With custom paths
python manage.py train_iex_predictor --data-path /path/to/excel/folder --model-path /path/to/model.pkl --test-days 30
```

### Integration

The allocation AI automatically uses predicted MCP when:

1. Live IEX fetch fails (e.g. network error)
2. Requested date is in the future (IEX typically has today's data only)

`ensure_iex_mcp_for_date()` in `allocation/iex_service.py` handles the fallback.

### API

- `GET /api/iex/green-day-ahead/mcp/?date=YYYY-MM-DD` – returns MCP (live or predicted) for 96 slots
- Allocation endpoints use the same underlying service

## Python API

```python
from allocation.iex_prediction import IexMcpPredictor

predictor = IexMcpPredictor(model_path="data/iex_mcp_model.pkl")

# Predict for a date
mcp_by_slot = predictor.predict(datetime.date(2025, 3, 22))

# Predict tomorrow and day-after
predictions = predictor.predict_tomorrow_and_day_after()
# {date: {slot_1: Decimal, slot_2: Decimal, ...}}
```
