"""
Tests for the trade record transformer.

Run with:
    cd src/processor && python -m pytest ../../tests/test_transformer.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src/processor"))

import pytest
from datetime import datetime, timezone
from transformer import transform_record


@pytest.fixture
def normalised_record():
    return {
        "trade_id":  "550e8400-e29b-41d4-a716-446655440001",
        "timestamp": "2024-03-15T09:31:00+00:00",
        "symbol":    "AAPL",
        "side":      "BUY",
        "quantity":  500,
        "price":     172.45,
        "notional":  86225.0,
        "trader_id": "TDR001",
        "venue":     "NASDAQ",
    }


class TestTransform:
    def test_trade_date_extracted(self, normalised_record):
        result = transform_record(normalised_record, "my-bucket", "uploads/test.csv")
        assert result["trade_date"] == "2024-03-15"

    def test_source_fields_added(self, normalised_record):
        result = transform_record(normalised_record, "my-bucket", "uploads/test.csv")
        assert result["source_bucket"] == "my-bucket"
        assert result["source_key"]    == "uploads/test.csv"

    def test_processed_at_is_utc_iso(self, normalised_record):
        result = transform_record(normalised_record, "b", "k")
        dt = datetime.fromisoformat(result["processed_at"])
        assert dt.tzinfo is not None

    def test_ttl_is_future_integer(self, normalised_record):
        result = transform_record(normalised_record, "b", "k")
        now = int(datetime.now(timezone.utc).timestamp())
        assert isinstance(result["ttl"], int)
        assert result["ttl"] > now

    def test_ttl_approximately_90_days(self, normalised_record):
        result = transform_record(normalised_record, "b", "k")
        now = int(datetime.now(timezone.utc).timestamp())
        days_ahead = (result["ttl"] - now) / 86400
        assert 89 < days_ahead < 91

    def test_original_fields_preserved(self, normalised_record):
        result = transform_record(normalised_record, "b", "k")
        for field in ["trade_id", "symbol", "side", "quantity", "price", "notional"]:
            assert result[field] == normalised_record[field]


class TestSizeCategory:
    @pytest.mark.parametrize("notional,expected", [
        (999.99,      "SMALL"),
        (9999.99,     "SMALL"),
        (10000.00,    "MEDIUM"),
        (99999.99,    "MEDIUM"),
        (100000.00,   "LARGE"),
        (999999.99,   "LARGE"),
        (1000000.00,  "BLOCK"),
        (5000000.00,  "BLOCK"),
    ])
    def test_size_categories(self, normalised_record, notional, expected):
        normalised_record["notional"] = notional
        result = transform_record(normalised_record, "b", "k")
        assert result["size_category"] == expected
