"""
IEX MCP slot-wise prediction using historical data.
Predicts Market Clearing Price (Rs/MWh) for 96 slots for a given date.
"""

__all__ = ["IexMcpPredictor"]


def __getattr__(name):
    if name == "IexMcpPredictor":
        from .predictor import IexMcpPredictor
        return IexMcpPredictor
    raise AttributeError(name)
