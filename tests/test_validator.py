"""
Tests for the trade record validator.

Run with:
    cd src/processor && python -m pytest ../../tests/test_validator.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src/processor"))

import pytest
from validator import validate_record


# ── Fixture: a known-good record ─────────────────────────────────────────────

@pytest.fixture
def valid_row():
    return {
        "trade_id":  "550e8400-e29b-41d4-a716-446655440001",
        "timestamp": "2024-03-15T09:31:00Z",
        "symbol":    "AAPL",
        "side":      "BUY",
        "quantity":  "500",
        "price":     "172.45",
        "trader_id": "TDR001",
        "venue":     "NASDAQ",
    }


# ── Happy path ────────────────────────────────────────────────────────────────

class TestValidRecord:
    def test_valid_record_returns_true(self, valid_row):
        result = validate_record(valid_row)
        assert result.valid is True
        assert result.reason is None

    def test_normalised_fields_present(self, valid_row):
        result = validate_record(valid_row)
        for field in ["trade_id", "timestamp", "symbol", "side",
                      "quantity", "price", "notional", "trader_id", "venue"]:
            assert field in result.normalised

    def test_symbol_normalised_to_uppercase(self, valid_row):
        valid_row["symbol"] = "aapl"
        result = validate_record(valid_row)
        assert result.valid is True
        assert result.normalised["symbol"] == "AAPL"

    def test_side_normalised_to_uppercase(self, valid_row):
        valid_row["side"] = "buy"
        result = validate_record(valid_row)
        assert result.valid is True
        assert result.normalised["side"] == "BUY"

    def test_notional_calculated(self, valid_row):
        result = validate_record(valid_row)
        expected = round(500 * 172.45, 2)
        assert result.normalised["notional"] == expected

    def test_whitespace_stripped(self, valid_row):
        valid_row["symbol"] = "  AAPL  "
        valid_row["side"]   = "  BUY  "
        result = validate_record(valid_row)
        assert result.valid is True

    def test_sell_side_valid(self, valid_row):
        valid_row["side"] = "SELL"
        result = validate_record(valid_row)
        assert result.valid is True


# ── Missing fields ────────────────────────────────────────────────────────────

class TestMissingFields:
    @pytest.mark.parametrize("field", [
        "trade_id", "timestamp", "symbol", "side",
        "quantity", "price", "trader_id", "venue"
    ])
    def test_missing_required_field_fails(self, valid_row, field):
        valid_row[field] = ""
        result = validate_record(valid_row)
        assert result.valid is False
        assert "missing_required_fields" in result.reason

    def test_missing_multiple_fields(self, valid_row):
        valid_row["trade_id"] = ""
        valid_row["price"] = ""
        result = validate_record(valid_row)
        assert result.valid is False
        assert "trade_id" in result.reason
        assert "price" in result.reason


# ── trade_id validation ───────────────────────────────────────────────────────

class TestTradeId:
    def test_invalid_not_uuid(self, valid_row):
        valid_row["trade_id"] = "not-a-uuid"
        result = validate_record(valid_row)
        assert result.valid is False
        assert "invalid_trade_id_format" in result.reason

    def test_invalid_uuid_v1(self, valid_row):
        valid_row["trade_id"] = "550e8400-e29b-11d4-a716-446655440099"
        result = validate_record(valid_row)
        assert result.valid is False

    def test_valid_uuid_case_insensitive(self, valid_row):
        valid_row["trade_id"] = "550E8400-E29B-41D4-A716-446655440001"
        result = validate_record(valid_row)
        assert result.valid is True


# ── Timestamp validation ──────────────────────────────────────────────────────

class TestTimestamp:
    def test_invalid_us_format(self, valid_row):
        valid_row["timestamp"] = "03/15/2024 09:31:00"
        result = validate_record(valid_row)
        assert result.valid is False
        assert "invalid_timestamp_format" in result.reason

    def test_invalid_no_z_suffix(self, valid_row):
        valid_row["timestamp"] = "2024-03-15T09:31:00"
        result = validate_record(valid_row)
        assert result.valid is False

    def test_valid_midnight(self, valid_row):
        valid_row["timestamp"] = "2024-03-15T00:00:00Z"
        result = validate_record(valid_row)
        assert result.valid is True


# ── Symbol validation ─────────────────────────────────────────────────────────

class TestSymbol:
    def test_lowercase_normalised(self, valid_row):
        valid_row["symbol"] = "aapl"
        result = validate_record(valid_row)
        assert result.valid is True
        assert result.normalised["symbol"] == "AAPL"

    def test_invalid_too_long(self, valid_row):
        valid_row["symbol"] = "TOOLNG"
        result = validate_record(valid_row)
        assert result.valid is False

    def test_invalid_too_short(self, valid_row):
        valid_row["symbol"] = "A"
        result = validate_record(valid_row)
        assert result.valid is False

    def test_invalid_contains_digit(self, valid_row):
        valid_row["symbol"] = "AAP1"
        result = validate_record(valid_row)
        assert result.valid is False

    @pytest.mark.parametrize("symbol", ["AB", "ABC", "ABCD", "ABCDE"])
    def test_valid_symbol_lengths(self, valid_row, symbol):
        valid_row["symbol"] = symbol
        result = validate_record(valid_row)
        assert result.valid is True


# ── Side validation ───────────────────────────────────────────────────────────

class TestSide:
    @pytest.mark.parametrize("side", ["HOLD", "SHORT", "1", "B", ""])
    def test_invalid_side(self, valid_row, side):
        valid_row["side"] = side
        result = validate_record(valid_row)
        assert result.valid is False

    @pytest.mark.parametrize("side", ["BUY", "SELL", "buy", "sell", "Buy", "Sell"])
    def test_valid_side_case_insensitive(self, valid_row, side):
        valid_row["side"] = side
        result = validate_record(valid_row)
        assert result.valid is True


# ── Quantity validation ───────────────────────────────────────────────────────

class TestQuantity:
    def test_negative_quantity(self, valid_row):
        valid_row["quantity"] = "-100"
        result = validate_record(valid_row)
        assert result.valid is False

    def test_zero_quantity(self, valid_row):
        valid_row["quantity"] = "0"
        result = validate_record(valid_row)
        assert result.valid is False

    def test_exceeds_max_quantity(self, valid_row):
        valid_row["quantity"] = "1000001"
        result = validate_record(valid_row)
        assert result.valid is False

    def test_float_quantity_fails(self, valid_row):
        valid_row["quantity"] = "100.5"
        result = validate_record(valid_row)
        assert result.valid is False

    def test_max_valid_quantity(self, valid_row):
        valid_row["quantity"] = "1000000"
        result = validate_record(valid_row)
        assert result.valid is True


# ── Price validation ──────────────────────────────────────────────────────────

class TestPrice:
    def test_negative_price(self, valid_row):
        valid_row["price"] = "-172.45"
        result = validate_record(valid_row)
        assert result.valid is False

    def test_zero_price(self, valid_row):
        valid_row["price"] = "0"
        result = validate_record(valid_row)
        assert result.valid is False

    def test_too_many_decimals(self, valid_row):
        valid_row["price"] = "172.12345"
        result = validate_record(valid_row)
        assert result.valid is False

    def test_four_decimals_valid(self, valid_row):
        valid_row["price"] = "172.1234"
        result = validate_record(valid_row)
        assert result.valid is True

    def test_integer_price_valid(self, valid_row):
        valid_row["price"] = "172"
        result = validate_record(valid_row)
        assert result.valid is True


# ── Venue validation ──────────────────────────────────────────────────────────

class TestVenue:
    @pytest.mark.parametrize("venue", ["UNKNOWN", "NASDA", ""])
    def test_invalid_venue(self, valid_row, venue):
        valid_row["venue"] = venue
        result = validate_record(valid_row)
        assert result.valid is False

    def test_lowercase_venue_normalised(self, valid_row):
        valid_row["venue"] = "lse"
        result = validate_record(valid_row)
        assert result.valid is True
        assert result.normalised["venue"] == "LSE"

    @pytest.mark.parametrize("venue", ["LSE", "NYSE", "NASDAQ", "BATS", "CHI-X", "DARK"])
    def test_valid_venues(self, valid_row, venue):
        valid_row["venue"] = venue
        result = validate_record(valid_row)
        assert result.valid is True
