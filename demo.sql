-- Demo run sheet. Paste one block at a time into http://localhost:8123/play (Ctrl+Enter).
-- The line Play prints under the results — elapsed, rows read — IS the demo.

-- 0. The table: 20M rows, ordered by (country, ts, user_id)
SELECT count() FROM learn.events;
SHOW CREATE TABLE learn.events;


SELECT * FROM learn.events LIMIT 20;


SELECT sum(revenue) FROM learn.events;
SELECT * FROM learn.events ORDER BY revenue DESC LIMIT 5;

-- 3. THE MONEY SHOT — same shape of query, 6x difference in rows read.
--    country leads the ORDER BY, so it skips granules. device does not.
SELECT sum(revenue) FROM learn.events WHERE country = 'US';      -- reads 3.3M rows
SELECT sum(revenue) FROM learn.events WHERE device  = 'mobile';  -- reads all 20M

-- 4. Why: the index says so out loud. Granules 409/2442.
EXPLAIN indexes = 1
SELECT sum(revenue) FROM learn.events WHERE country = 'US';

-- 5. Compression: country is 96 KiB, user_id is 76 MiB. Same 20M rows.
SELECT name, type,
       formatReadableSize(sum(data_compressed_bytes))   AS on_disk,
       round(sum(data_uncompressed_bytes) / sum(data_compressed_bytes), 1) AS ratio
FROM system.columns
WHERE database = 'learn' AND table = 'events'
GROUP BY name, type
ORDER BY sum(data_compressed_bytes) DESC;

-- 6. A real analytics query, sub-second over 20M rows
SELECT country,
       count()                    AS events,
       uniq(user_id)              AS users,      -- HyperLogLog, approximate by default
       round(sum(revenue), 2)     AS revenue,
       quantile(0.95)(duration_ms) AS p95_ms
FROM learn.events
GROUP BY country
ORDER BY revenue DESC;

-- 7. Time series, one line
SELECT toStartOfDay(ts) AS day, count() AS events, round(sum(revenue)) AS revenue
FROM learn.events
WHERE country = 'US'
GROUP BY day ORDER BY day;

-- 8. Materialized view = insert trigger. 696 pre-aggregated rows instead of 20M.
SELECT count() FROM learn.daily_country;
SELECT * FROM learn.daily_country ORDER BY day DESC LIMIT 10;

-- 9. Every query you just ran, and what it cost
SELECT query_duration_ms AS ms,
       formatReadableQuantity(read_rows) AS rows_read,
       formatReadableSize(read_bytes)    AS bytes_read,
       substring(query, 1, 60)           AS q
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 30 MINUTE
  AND query NOT LIKE '%query_log%'
ORDER BY event_time DESC
LIMIT 20;
