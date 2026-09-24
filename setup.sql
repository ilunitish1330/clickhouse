-- Setup: one fake analytics events table, 20M rows, generated inside ClickHouse.
-- ponytail: generated data instead of downloading a real dataset — no network, same lessons.

CREATE DATABASE IF NOT EXISTS learn;

DROP TABLE IF EXISTS learn.events;

CREATE TABLE learn.events
(
    ts          DateTime,
    user_id     UInt32,
    country     LowCardinality(String),
    device      LowCardinality(String),
    url         String,
    duration_ms UInt32,
    revenue     Decimal(10, 2)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (country, ts, user_id);

INSERT INTO learn.events
SELECT
    toDateTime('2024-01-01 00:00:00') + intDiv(number, 2) AS ts,
    rand(1) % 500000                                      AS user_id,
    ['US','IN','DE','BR','JP','GB'][(rand(2) % 6) + 1]    AS country,
    ['mobile','desktop','tablet'][(rand(3) % 3) + 1]      AS device,
    concat('/page/', toString(rand(4) % 1000))            AS url,
    rand(5) % 30000                                       AS duration_ms,
    (rand(6) % 10000) / 100                               AS revenue
FROM numbers(20000000);

OPTIMIZE TABLE learn.events FINAL;
