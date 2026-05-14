"""
Train IEX MCP prediction model using historical data.
Data path: ~/Downloads/redecisionalgorithm (or --data-path)
"""

from pathlib import Path

from django.core.management.base import BaseCommand

from allocation.iex_prediction.data_loader import load_all_data, DEFAULT_DATA_PATH
from allocation.iex_prediction.model import train_model


class Command(BaseCommand):
    help = "Train IEX slot-wise MCP prediction model on historical Excel data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--data-path",
            default=str(DEFAULT_DATA_PATH),
            help="Path to folder containing IEX Excel files (default: ~/Downloads/redecisionalgorithm)",
        )
        parser.add_argument(
            "--model-path",
            default=None,
            help="Output path for trained model (default: src/data/iex_mcp_model.pkl)",
        )
        parser.add_argument(
            "--test-days",
            type=int,
            default=30,
            help="Days to hold out for validation (default: 30)",
        )

    def handle(self, *args, **options):
        data_path = Path(options["data_path"])
        model_path = options["model_path"]
        if not model_path:
            model_path = Path(__file__).resolve().parent.parent.parent.parent / "data" / "iex_mcp_model.pkl"

        self.stdout.write(f"Loading data from {data_path}...")
        df = load_all_data(data_path)
        if df.empty:
            self.stderr.write(self.style.ERROR(f"No data found at {data_path}"))
            return

        self.stdout.write(f"Loaded {len(df)} rows ({df['date'].nunique()} days)")
        self.stdout.write("Training model...")
        train_model(df, test_size_days=options["test_days"], model_path=str(model_path))
        self.stdout.write(self.style.SUCCESS(f"Model saved to {model_path}"))
