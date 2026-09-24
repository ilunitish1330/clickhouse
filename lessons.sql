-- ClickHouse in 8 lessons. Run them, but READ the "rows read / bytes read" line
-- the client prints after each query — that number is the whole point every time.

-- ============================================================
-- 1. Columnar storage: you pay for the columns you touch
-- ============================================================
SELECT avg(duration_ms) FROM learn.events;              -- reads 1 column
SELECT avg(duration_ms), avg(revenue) FROM learn.events; -- reads 2, ~2x bytes
SELECT * FROM learn.events LIMIT 10 FORMAT Null;         -- touches all 7

-- Takeaway: SELECT * is a tax in a column store. Row stores can't do this.


-- ============================================================
-- 2. The primary key is a SPARSE index, and it's the sort order
-- ============================================================
-- ORDER BY (country, ts, user_id) -> only prefixes of that tuple can skip data.
SELECT count() FROM learn.events WHERE country = 'US';           -- prefix hit: skips granules
SELECT count() FROM learn.events WHERE url = '/page/42';          -- not in key: full scan
SELECT count() FROM learn.events WHERE user_id = 12345;           -- 3rd key part, no prefix: full scan

-- See it decide, without running the scan:
EXPLAIN indexes = 1 SELECT count() FROM learn.events WHERE country = 'US';

-- Takeaway: one index per table, it's the ORDER BY, and left-to-right prefixes win.
-- There is no "add an index on user_id" like Postgres. You pick ORDER BY for the
-- query pattern you actually have (or make a second table / projection).


-- ============================================================
-- 3. Partitions prune whole directories before the index runs
-- ============================================================
SELECT count() FROM learn.events WHERE ts >= '2024-03-01' AND ts < '2024-04-01';
SELECT partition, rows, formatReadableSize(bytes_on_disk) AS size
FROM system.parts WHERE table = 'events' AND active ORDER BY partition;

-- Takeaway: PARTITION BY toYYYYMM is for data lifecycle (DROP PARTITION is instant)
-- and coarse pruning. Do NOT partition by something high-cardinality — thousands of
-- tiny parts is the classic self-inflicted ClickHouse wound.


-- ============================================================
-- 4. Compression is where the disk savings live
-- ============================================================
SELECT
    column,                                          -- `name` here is the PART name, not the column
    any(type)                                        AS type,
    formatReadableSize(sum(column_data_compressed_bytes))   AS compressed,
    formatReadableSize(sum(column_data_uncompressed_bytes)) AS raw,
    round(sum(column_data_uncompressed_bytes) / sum(column_data_compressed_bytes), 1) AS ratio
FROM system.parts_columns
WHERE table = 'events' AND active
GROUP BY column
ORDER BY sum(column_data_compressed_bytes) DESC;

-- Takeaway: `country` is LowCardinality(String) -> stored as a dictionary of 6 values.
-- Sorted columns compress absurdly well; that's why ORDER BY choice is also a storage
-- decision, not just an index decision.


-- ============================================================
-- 5. Aggregations, and the approximate ones you'll actually use
-- ============================================================
SELECT country, count() AS hits, round(avg(duration_ms)) AS avg_ms, sum(revenue) AS rev
FROM learn.events GROUP BY country ORDER BY hits DESC;

SELECT uniq(user_id) AS approx, uniqExact(user_id) AS exact FROM learn.events;

SELECT country, quantiles(0.5, 0.95, 0.99)(duration_ms) FROM learn.events GROUP BY country;

-- Takeaway: uniq() is HyperLogLog — ~1% error, a fraction of the memory. In analytics
-- that trade is almost always correct. quantile() is likewise approximate by default.


-- ============================================================
-- 6. Materialized views = an INSERT trigger that pre-aggregates
-- ============================================================
CREATE TABLE IF NOT EXISTS learn.daily_country
(
    day     Date,
    country LowCardinality(String),
    hits    UInt64,
    revenue Decimal(18, 2)
)
ENGINE = SummingMergeTree
ORDER BY (day, country);

CREATE MATERIALIZED VIEW IF NOT EXISTS learn.daily_country_mv TO learn.daily_country AS
SELECT toDate(ts) AS day, country, count() AS hits, sum(revenue) AS revenue
FROM learn.events GROUP BY day, country;

-- MVs only see NEW inserts, so backfill the history by hand:
TRUNCATE TABLE learn.daily_country;   -- so re-running this file doesn't double-count
INSERT INTO learn.daily_country
SELECT toDate(ts), country, count(), sum(revenue) FROM learn.events GROUP BY 1, 2;

SELECT day, country, sum(hits), sum(revenue)     -- sum() because merges are async
FROM learn.daily_country WHERE day = '2024-02-15' GROUP BY day, country;

-- Takeaway: a ClickHouse MV is not a cached SELECT. It runs on each insert block and
-- writes rows to a target table. SummingMergeTree collapses matching keys eventually —
-- always GROUP BY on read, never assume the merge already happened.


-- ============================================================
-- 7. Updates and deletes: possible, still not what you want
-- ============================================================
CREATE TABLE IF NOT EXISTS learn.users
(
    user_id UInt32,
    email   String,
    updated DateTime
)
ENGINE = ReplacingMergeTree(updated)
ORDER BY user_id;

TRUNCATE TABLE learn.users;           -- likewise
INSERT INTO learn.users VALUES (1, 'old@x.com', '2024-01-01 00:00:00');
INSERT INTO learn.users VALUES (1, 'new@x.com', '2024-06-01 00:00:00');

SELECT * FROM learn.users;                      -- both rows: dedup hasn't merged yet
SELECT * FROM learn.users FINAL;                -- newest wins, at query cost

-- The heavyweight version, which rewrites parts in the background:
ALTER TABLE learn.events UPDATE duration_ms = 0 WHERE user_id = 1;
SELECT * FROM system.mutations WHERE table = 'events';

-- Takeaway: model as an append-only log + ReplacingMergeTree. ALTER ... UPDATE is a
-- mutation: async, rewrites whole parts, fine for backfills, wrong for OLTP traffic.


-- ============================================================
-- 8. Reading what a query actually cost
-- ============================================================
SYSTEM FLUSH LOGS;

SELECT
    substring(query, 1, 60)               AS q,
    read_rows,
    formatReadableSize(read_bytes)        AS read,
    formatReadableSize(memory_usage)      AS mem,
    round(query_duration_ms)              AS ms
FROM system.query_log
WHERE type = 'QueryFinish' AND has(databases, 'learn') AND read_rows > 0
ORDER BY event_time DESC LIMIT 15;

-- Takeaway: read_rows is your feedback loop. Tuning ClickHouse means making that
-- number smaller — better ORDER BY, better partitions, fewer columns — not adding RAM.
