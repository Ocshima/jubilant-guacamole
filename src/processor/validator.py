"""
Trade record validator.

Each row read from the uploaded CSV is passed through validate_record().
Returns a ValidationResult indicating whether the record is valid and,
if not, a human-readable reason used in the DLQ message and structured logs.

Validation rules:
  1.  Required fields present and non-empty
  2.  trade_id:   UUID v4 format
  3.  timestamp:  ISO 8601 UTC (YYYY-MM-DDTHH:MM:SSZ)
  4.  symbol:     2–5 uppercase alpha characters  (e.g. AAPL, MSFT, BT.A not supported)
  5.  side:       BUY or SELL (case-insensitive, normalised to uppercase)
  6.  quantity:   positive integer, max 1,000,000
  7.  price:      positive float, max 1,000,000.00, max 4 decimal places
  8.  trader_id:  alphanumeric, 4–20 characters
  9.  venue:      one of the known execution venues
  10. notional:   derived check — quantity × price must be > 0
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional


# ── Constants ─────────────────────────────────────────────────────────────────

REQUIRED_FIELDS = [
    "trade_id",
    "timestamp",
    "symbol",
    "side",
    "quantity",
    "price",
    "trader_id",
    "venue",
]

VALID_SIDES = {"BUY", "SELL"}

KNOWN_VENUES = {
    "LSE",    # London Stock Exchange
    "NYSE",   # New York Stock Exchange
    "NASDAQ", # NASDAQ
    "BATS",   # CBOE (formerly BATS)
    "CHI-X",  # Cboe Europe
    "DARK",   # Dark pool (generic)
}

# UUID v4: 8-4-4-4-12 hex, version nibble = 4, variant nibble = 8/9/a/b
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

SYMBOL_RE = re.compile(r"^[A-Z]{2,5}$")

TRADER_ID_RE = re.compile(r"^[A-Za-z0-9]{4,20}$")

MAX_QUANTITY = 1_000_000
MAX_PRICE    = 1_000_000.00
MAX_DECIMALS = 4


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    valid: bool
    reason: Optional[str] = None
    # Normalised / type-coerced record returned on success
    normalised: dict = field(default_factory=dict)


# ── Public API ────────────────────────────────────────────────────────────────

def validate_record(row: dict) -> ValidationResult:
    """
    Validate a single CSV row (dict with string values).
    Returns ValidationResult with valid=True and a normalised record on success,
    or valid=False and a reason string on failure.
    """

    # 1. Required fields
    missing = [f for f in REQUIRED_FIELDS if not (row.get(f) or "").strip()]
    if missing:
        return ValidationResult(
            valid=False,
            reason=f"missing_required_fields: {', '.join(missing)}"
        )

    # Work on stripped copies
    # Skip non-string keys: csv.DictReader stores overflow columns under key None
    r = {k: (v or "").strip() for k, v in row.items() if isinstance(k, str)}

    # 2. trade_id — UUID v4
    if not UUID_RE.match(r["trade_id"]):
        return ValidationResult(
            valid=False,
            reason=f"invalid_trade_id_format: '{r['trade_id']}'"
        )

    # 3. timestamp — ISO 8601 UTC
    try:
        ts = datetime.strptime(r["timestamp"], TIMESTAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return ValidationResult(
            valid=False,
            reason=f"invalid_timestamp_format: '{r['timestamp']}' — expected YYYY-MM-DDTHH:MM:SSZ"
        )

    # 4. symbol
    symbol = r["symbol"].upper()
    if not SYMBOL_RE.match(symbol):
        return ValidationResult(
            valid=False,
            reason=f"invalid_symbol: '{r['symbol']}' — must be 2–5 uppercase letters"
        )

    # 5. side
    side = r["side"].upper()
    if side not in VALID_SIDES:
        return ValidationResult(
            valid=False,
            reason=f"invalid_side: '{r['side']}' — must be BUY or SELL"
        )

    # 6. quantity
    try:
        quantity = int(r["quantity"])
        if quantity <= 0:
            raise ValueError("non-positive")
        if quantity > MAX_QUANTITY:
            raise ValueError("exceeds maximum")
    except ValueError as e:
        return ValidationResult(
            valid=False,
            reason=f"invalid_quantity: '{r['quantity']}' — {e}"
        )

    # 7. price — parsed as Decimal to avoid float precision issues and DynamoDB compatibility
    try:
        price = Decimal(r["price"])
        if price <= 0:
            raise ValueError("non-positive")
        if price > MAX_PRICE:
            raise ValueError("exceeds maximum")
        # Check decimal places
        decimal_part = r["price"].split(".")[-1] if "." in r["price"] else ""
        if len(decimal_part) > MAX_DECIMALS:
            raise ValueError(f"more than {MAX_DECIMALS} decimal places")
    except (ValueError, InvalidOperation) as e:
        return ValidationResult(
            valid=False,
            reason=f"invalid_price: '{r['price']}' — {e}"
        )

    # 8. trader_id
    if not TRADER_ID_RE.match(r["trader_id"]):
        return ValidationResult(
            valid=False,
            reason=f"invalid_trader_id: '{r['trader_id']}' — must be 4–20 alphanumeric characters"
        )

    # 9. venue
    if r["venue"].upper() not in KNOWN_VENUES:
        return ValidationResult(
            valid=False,
            reason=f"unknown_venue: '{r['venue']}' — must be one of {sorted(KNOWN_VENUES)}"
        )

    # 10. Notional sanity check
    notional = quantity * price
    if notional <= 0:
        return ValidationResult(
            valid=False,
            reason=f"invalid_notional: quantity={quantity} × price={price} = {notional}"
        )

    return ValidationResult(
        valid=True,
        normalised={
            "trade_id":  r["trade_id"].lower(),
            "timestamp": ts.isoformat(),
            "symbol":    symbol,
            "side":      side,
            "quantity":  quantity,
            "price":     round(price, MAX_DECIMALS),    # Decimal — DynamoDB compatible
            "notional":  round(notional, 2),            # Decimal — DynamoDB compatible
            "trader_id": r["trader_id"],
            "venue":     r["venue"].upper(),
        },
    )
