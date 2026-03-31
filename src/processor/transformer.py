"""
Trade record transformer.

Receives a normalised record from the validator and enriches it with
derived fields before it is written to DynamoDB.

Enrichment added here (not in the source data):
  - trade_date:      YYYY-MM-DD extracted from timestamp (useful as a GSI key)
  - notional_usd:    placeholder for FX conversion (identity for now)
  - size_category:   SMALL / MEDIUM / LARGE / BLOCK based on notional
  - processed_at:    ISO 8601 UTC timestamp of when this Lambda ran
  - ttl:             Unix epoch 90 days from now — DynamoDB TTL for cost control
  - source_bucket:   S3 bucket the file came from
  - source_key:      S3 object key the file came from

Keeping enrichment separate from validation makes each concern independently
testable and easier to extend (e.g. add FX rate lookup without touching
validation logic).
"""

from datetime import datetime, timedelta, timezone
from typing import Any


# ── Size category thresholds (notional USD) ───────────────────────────────────
SIZE_THRESHOLDS = [
    (10_000,    "SMALL"),
    (100_000,   "MEDIUM"),
    (1_000_000, "LARGE"),
]


def transform_record(
    normalised: dict,
    source_bucket: str,
    source_key: str,
) -> dict[str, Any]:
    """
    Enrich a validated, normalised trade record with derived fields.
    Returns the full DynamoDB item ready for a PutItem call.
    """

    now = datetime.now(timezone.utc)

    # Parse timestamp for date extraction
    ts = datetime.fromisoformat(normalised["timestamp"])

    # Derive size category from notional
    notional = normalised["notional"]
    size_category = "BLOCK"
    for threshold, label in SIZE_THRESHOLDS:
        if notional < threshold:
            size_category = label
            break

    # TTL: 90 days from now as Unix epoch integer
    # DynamoDB deletes items automatically after this — keeps table size bounded
    ttl = int((now + timedelta(days=90)).timestamp())

    return {
        **normalised,
        "trade_date":     ts.strftime("%Y-%m-%d"),
        "notional_usd":   normalised["notional"],   # Identity — extend with FX lookup
        "size_category":  size_category,
        "processed_at":   now.isoformat(),
        "source_bucket":  source_bucket,
        "source_key":     source_key,
        "ttl":            ttl,
    }
