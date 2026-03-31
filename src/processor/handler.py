"""
Trade ETL Processor — Lambda Handler

Entry point for the S3-triggered Lambda function.

Flow per invocation:
  1. Parse S3 event to get bucket + key
  2. Check idempotency (has this exact S3 object version been processed before?)
  3. Stream CSV from S3
  4. For each row: validate -> transform -> write to DynamoDB
  5. Invalid rows -> send to SQS DLQ with structured error payload
  6. Write idempotency record to DynamoDB on completion
  7. Emit structured log summary (parsed by CloudWatch Metric Filters)

Error handling strategy:
  - Per-record validation failures: routed to SQS DLQ, processing continues
  - Per-record DynamoDB write failures: logged, routed to DLQ, processing continues
  - Unrecoverable errors (S3 access denied, table not found): re-raised so Lambda
    retries and the invocation itself lands on the Lambda DLQ after exhausted retries

X-Ray tracing:
  - aws_xray_sdk patches boto3 automatically
  - Custom subsegments added for S3 read, validation loop, DynamoDB writes
"""

import csv
import io
import json
import os
from typing import Any

import boto3
from aws_xray_sdk.core import patch_all, xray_recorder

from utils.logger import get_logger
from validator import validate_record
from transformer import transform_record

# Patch all boto3 clients for X-Ray tracing
patch_all()

# ── Environment variables (set in CloudFormation template) ────────────────────
TABLE_NAME   = os.environ["DYNAMODB_TABLE_NAME"]
DLQ_URL      = os.environ["DLQ_URL"]
ENVIRONMENT  = os.environ.get("ENVIRONMENT", "dev")
IDEMPOTENCY_TABLE = os.environ["IDEMPOTENCY_TABLE_NAME"]

# ── AWS clients (module-level = reused across warm invocations) ───────────────
s3       = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
sqs      = boto3.client("sqs")

trades_table      = dynamodb.Table(TABLE_NAME)
idempotency_table = dynamodb.Table(IDEMPOTENCY_TABLE)

log = get_logger()


# ── Idempotency ───────────────────────────────────────────────────────────────

def is_already_processed(bucket: str, key: str, etag: str) -> bool:
    """
    Check whether this exact S3 object version has been processed before.
    Uses bucket+key+etag as a composite key.
    S3 event notifications have at-least-once delivery — this prevents
    double-processing if the same event is delivered twice.
    """
    idempotency_key = f"{bucket}/{key}#{etag}"
    try:
        response = idempotency_table.get_item(
            Key={"idempotency_key": idempotency_key}
        )
        return "Item" in response
    except Exception as e:
        log.warning("idempotency_check_failed", error=str(e), key=idempotency_key)
        # Fail open — allow processing rather than silently skip
        return False


def mark_as_processed(bucket: str, key: str, etag: str, summary: dict) -> None:
    """Write an idempotency record after successful processing."""
    from datetime import datetime, timedelta, timezone
    idempotency_key = f"{bucket}/{key}#{etag}"
    ttl = int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp())
    try:
        idempotency_table.put_item(Item={
            "idempotency_key": idempotency_key,
            "summary":         json.dumps(summary),
            "ttl":             ttl,
        })
    except Exception as e:
        # Non-fatal — worst case is reprocessing on duplicate event
        log.warning("idempotency_write_failed", error=str(e), key=idempotency_key)


# ── DLQ ───────────────────────────────────────────────────────────────────────

def send_to_dlq(
    row: dict,
    reason: str,
    source_bucket: str,
    source_key: str,
    row_number: int,
) -> None:
    """Send a failed record to the SQS Dead-Letter Queue."""
    payload = {
        "error":         reason,
        "row_number":    row_number,
        "source_bucket": source_bucket,
        "source_key":    source_key,
        "raw_record":    row,
    }
    try:
        sqs.send_message(
            QueueUrl=DLQ_URL,
            MessageBody=json.dumps(payload, default=str),
            MessageAttributes={
                "ErrorType": {
                    "StringValue": reason.split(":")[0],
                    "DataType":    "String",
                },
                "SourceKey": {
                    "StringValue": source_key,
                    "DataType":    "String",
                },
            },
        )
        log.warning(
            "record_sent_to_dlq",
            row_number=row_number,
            reason=reason,
            source_key=source_key,
        )
    except Exception as e:
        # Log but don't re-raise — a DLQ write failure shouldn't stop processing
        log.error(
            "dlq_send_failed",
            row_number=row_number,
            reason=reason,
            error=str(e),
        )


# ── Core processing ───────────────────────────────────────────────────────────

@xray_recorder.capture("write_trade_to_dynamodb")
def write_to_dynamodb(item: dict) -> None:
    """
    Write a transformed trade record to DynamoDB.
    Uses a condition expression to prevent overwriting an existing record
    with the same trade_id — an additional idempotency layer at the record level.
    """
    trades_table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(trade_id)",
    )


@xray_recorder.capture("process_csv")
def process_csv(
    csv_content: str,
    source_bucket: str,
    source_key: str,
) -> dict[str, int]:
    """
    Process all rows in a CSV string.
    Returns a summary dict with counts for CloudWatch metric logging.
    """
    # Strip comment lines (lines starting with '#') before parsing —
    # csv.DictReader treats every non-empty line as a data row.
    cleaned = "\n".join(
        line for line in csv_content.splitlines() if not line.lstrip().startswith("#")
    )
    reader = csv.DictReader(io.StringIO(cleaned))
    counts = {
        "total":     0,
        "valid":     0,
        "invalid":   0,
        "dlq_sent":  0,
        "db_errors": 0,
    }

    for row_number, row in enumerate(reader, start=2):  # start=2 (row 1 = header)
        counts["total"] += 1

        # ── Validate ────────────────────────────────────────────────────────
        result = validate_record(row)

        if not result.valid:
            counts["invalid"] += 1
            send_to_dlq(
                row=row,
                reason=result.reason,
                source_bucket=source_bucket,
                source_key=source_key,
                row_number=row_number,
            )
            counts["dlq_sent"] += 1
            continue

        # ── Transform ───────────────────────────────────────────────────────
        item = transform_record(
            normalised=result.normalised,
            source_bucket=source_bucket,
            source_key=source_key,
        )

        # ── Write to DynamoDB ────────────────────────────────────────────────
        try:
            write_to_dynamodb(item)
            counts["valid"] += 1
            log.info(
                "record_processed",
                trade_id=item["trade_id"],
                symbol=item["symbol"],
                side=item["side"],
                notional=item["notional"],
                venue=item["venue"],
                size_category=item["size_category"],
            )
        except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
            # Duplicate trade_id — idempotent, not an error
            counts["valid"] += 1
            log.info(
                "record_duplicate_skipped",
                trade_id=item["trade_id"],
            )
        except Exception as e:
            counts["db_errors"] += 1
            log.error(
                "dynamodb_write_failed",
                trade_id=item.get("trade_id", "unknown"),
                error=str(e),
            )
            send_to_dlq(
                row=row,
                reason=f"dynamodb_write_error: {e}",
                source_bucket=source_bucket,
                source_key=source_key,
                row_number=row_number,
            )
            counts["dlq_sent"] += 1

    return counts


# ── Lambda handler ────────────────────────────────────────────────────────────

def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda entry point.
    Triggered by S3 ObjectCreated events on the raw uploads bucket.
    """
    log.set_request_id(context.aws_request_id)
    log.info("invocation_started", event_records=len(event.get("Records", [])))

    results = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key    = record["s3"]["object"]["key"]
        etag   = record["s3"]["object"].get("eTag", "unknown").strip('"')
        size   = record["s3"]["object"].get("size", 0)

        log.info(
            "processing_s3_object",
            bucket=bucket,
            key=key,
            etag=etag,
            size_bytes=size,
        )

        # ── Idempotency check ────────────────────────────────────────────────
        if is_already_processed(bucket, key, etag):
            log.info("object_already_processed", bucket=bucket, key=key, etag=etag)
            results.append({"key": key, "status": "skipped_duplicate"})
            continue

        # ── Read CSV from S3 ─────────────────────────────────────────────────
        with xray_recorder.in_subsegment("s3_get_object"):
            try:
                response = s3.get_object(Bucket=bucket, Key=key)
                csv_content = response["Body"].read().decode("utf-8")
            except Exception as e:
                log.error(
                    "s3_read_failed",
                    bucket=bucket,
                    key=key,
                    error=str(e),
                )
                raise  # Re-raise — unrecoverable, let Lambda retry + DLQ

        # ── Process ──────────────────────────────────────────────────────────
        import time
        start_ms = time.time() * 1000

        counts = process_csv(
            csv_content=csv_content,
            source_bucket=bucket,
            source_key=key,
        )

        duration_ms = round(time.time() * 1000 - start_ms, 1)

        # ── Emit summary log (parsed by CloudWatch Metric Filters) ───────────
        log.info(
            "file_processing_complete",
            bucket=bucket,
            key=key,
            duration_ms=duration_ms,
            records_total=counts["total"],
            records_valid=counts["valid"],
            records_invalid=counts["invalid"],
            dlq_messages_sent=counts["dlq_sent"],
            db_errors=counts["db_errors"],
        )

        # ── Mark as processed ─────────────────────────────────────────────────
        mark_as_processed(bucket, key, etag, counts)

        results.append({
            "key":    key,
            "status": "processed",
            **counts,
        })

    log.info("invocation_complete", results=results)
    return {"statusCode": 200, "body": json.dumps(results)}
