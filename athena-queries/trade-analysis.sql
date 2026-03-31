-- ============================================================
-- trade-analysis.sql
-- Analytical queries for the processed trades DynamoDB table.
--
-- These queries assume you have exported the DynamoDB table to S3
-- using AWS DynamoDB Pitr/Export or set up a Glue crawler on a
-- DynamoDB export. Alternatively, run these against a Glue table
-- created from a periodic export to S3.
--
-- To export DynamoDB to S3:
--   aws dynamodb export-table-to-point-in-time \
--     --table-arn <TradesTableArn> \
--     --s3-bucket <your-export-bucket> \
--     --export-format DYNAMODB_JSON
-- ============================================================


-- ── 1. Daily trade volume by symbol ──────────────────────────────
-- Shows total notional and trade count per symbol per day.
SELECT
    trade_date,
    symbol,
    COUNT(*)                              AS trade_count,
    SUM(quantity)                         AS total_shares,
    ROUND(SUM(notional_usd), 2)           AS total_notional_usd,
    ROUND(AVG(price), 4)                  AS avg_price,
    SUM(CASE WHEN side = 'BUY'  THEN quantity ELSE 0 END) AS buy_volume,
    SUM(CASE WHEN side = 'SELL' THEN quantity ELSE 0 END) AS sell_volume
FROM trades
WHERE trade_date >= DATE_FORMAT(CURRENT_DATE - INTERVAL '7' DAY, '%Y-%m-%d')
GROUP BY trade_date, symbol
ORDER BY trade_date DESC, total_notional_usd DESC;


-- ── 2. Buy/sell imbalance by symbol ──────────────────────────────
-- Positive flow = net buying pressure, negative = net selling.
-- Useful for detecting directional bias in the dataset.
SELECT
    symbol,
    SUM(CASE WHEN side = 'BUY'  THEN notional_usd ELSE 0 END) AS buy_notional,
    SUM(CASE WHEN side = 'SELL' THEN notional_usd ELSE 0 END) AS sell_notional,
    ROUND(
        SUM(CASE WHEN side = 'BUY'  THEN notional_usd ELSE 0 END) -
        SUM(CASE WHEN side = 'SELL' THEN notional_usd ELSE 0 END),
    2) AS net_flow_usd
FROM trades
WHERE trade_date = DATE_FORMAT(CURRENT_DATE, '%Y-%m-%d')
GROUP BY symbol
ORDER BY ABS(net_flow_usd) DESC;


-- ── 3. Venue distribution ────────────────────────────────────────
-- Which execution venues are most active? Useful for validating
-- that mock data covers expected venue distribution.
SELECT
    venue,
    COUNT(*)                            AS trade_count,
    ROUND(SUM(notional_usd), 2)         AS total_notional_usd,
    ROUND(AVG(notional_usd), 2)         AS avg_trade_size,
    COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () AS pct_of_total
FROM trades
GROUP BY venue
ORDER BY trade_count DESC;


-- ── 4. Trade size distribution ───────────────────────────────────
-- Breakdown of SMALL / MEDIUM / LARGE / BLOCK trades.
-- Validates the size_category enrichment applied in transformer.py.
SELECT
    size_category,
    COUNT(*)                            AS trade_count,
    ROUND(SUM(notional_usd), 2)         AS total_notional_usd,
    ROUND(MIN(notional_usd), 2)         AS min_notional,
    ROUND(MAX(notional_usd), 2)         AS max_notional
FROM trades
GROUP BY size_category
ORDER BY
    CASE size_category
        WHEN 'SMALL'  THEN 1
        WHEN 'MEDIUM' THEN 2
        WHEN 'LARGE'  THEN 3
        WHEN 'BLOCK'  THEN 4
    END;


-- ── 5. Trader activity ───────────────────────────────────────────
-- Most active traders by notional — useful for demos.
SELECT
    trader_id,
    COUNT(*)                            AS trade_count,
    COUNT(DISTINCT symbol)              AS symbols_traded,
    ROUND(SUM(notional_usd), 2)         AS total_notional_usd,
    MIN(trade_date)                     AS first_trade_date,
    MAX(trade_date)                     AS last_trade_date
FROM trades
GROUP BY trader_id
ORDER BY total_notional_usd DESC
LIMIT 20;


-- ── 6. Pipeline health — processing latency ──────────────────────
-- Compare trade timestamp vs processed_at to see how long records
-- take to move from source event to DynamoDB write.
-- (Only meaningful with real or time-accurate mock data.)
SELECT
    trade_date,
    COUNT(*)                                                    AS records,
    ROUND(AVG(
        DATE_DIFF('second',
            PARSE_DATETIME(timestamp, 'yyyy-MM-dd''T''HH:mm:ssXXX'),
            PARSE_DATETIME(processed_at, 'yyyy-MM-dd''T''HH:mm:ss.SSSSSSXXX')
        )
    ), 1)                                                       AS avg_latency_seconds
FROM trades
GROUP BY trade_date
ORDER BY trade_date DESC;
