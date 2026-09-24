-- ============================================================================
-- DEMO SCRIPT — run top to bottom, one block at a time.
--   Play UI:  http://localhost:8123/play   (Ctrl+Enter runs the selected block)
--   Terminal: ~/clickhouse client --port 9010
-- Under every result Play prints elapsed + rows read. That line is the demo.
--
-- Data areas: 'usrt' has the volume (167k sales lines) but all in Nov 2012.
--             'usmf' is smaller (6k) and spread over 2011-2012 — use it for time.
-- on every ReplacingMergeTree: it dedupes on read.
-- ============================================================================


-- ############ ACT 1 — THE DIMENSIONS: what you slice BY ############

-- [1] The model at a glance: 5 dimensions, 2 facts, 2 aggregates
SELECT t, n FROM (
  SELECT 'dim_date' t, count() n FROM ax.dim_date
  UNION ALL SELECT 'dim_customer',      count() FROM ax.dim_customer
  UNION ALL SELECT 'dim_item',          count() FROM ax.dim_item
  UNION ALL SELECT 'dim_status',        count() FROM ax.dim_status
  UNION ALL SELECT 'dim_currency',      count() FROM ax.dim_currency
  UNION ALL SELECT 'fact_sales',        count() FROM ax.fact_sales
  UNION ALL SELECT 'fact_cust_trans',   count() FROM ax.fact_cust_trans
  UNION ALL SELECT 'agg_sales_monthly', count() FROM ax.agg_sales_monthly
  UNION ALL SELECT 'agg_ar_monthly',    count() FROM ax.agg_ar_monthly
) ORDER BY n DESC;

-- [2] dim_customer — descriptive attributes, no measures. This is what you group by.
SELECT customer_id, name, group_code, currency, payment_terms, credit_limit
FROM ax.dim_customer
WHERE data_area = 'usrt' ORDER BY credit_limit DESC LIMIT 10;

-- [3] dim_date — the calendar is precomputed, so no date maths in any query below
SELECT * FROM ax.dim_date WHERE date_key BETWEEN '2012-06-28' AND '2012-07-03' ORDER BY date_key;

-- [4] dim_status — the raw integer codes sitting in the facts, decoded into words
SELECT status_type, status_code, status_name FROM ax.dim_status ORDER BY status_type, status_code;


-- ############ ACT 2 — THE FACTS: what happened ############

-- [5] One row per order line: foreign keys + measures, nothing descriptive
SELECT data_area, sales_id, line_num, cust_account, item_id, order_date,
       sales_status, qty_ordered, sales_price, line_amount
FROM ax.fact_sales WHERE data_area = 'usrt' LIMIT 15;

-- [6] Measures with no dimensions: true, and useless
SELECT count()                   AS lines,
       round(sum(line_amount))   AS revenue,
       round(sum(margin))        AS margin
FROM ax.fact_sales WHERE data_area = 'usrt';


-- ############ ACT 3 — THE STAR: joining them is the point ############

-- [7] Revenue by month — fact + dim_date
SELECT d.year_month, d.month_name, count() AS lines,
       round(sum(f.line_amount)) AS revenue, round(sum(f.margin)) AS margin
FROM ax.fact_sales AS f
JOIN ax.dim_date AS d ON d.date_key = f.order_date
WHERE f.data_area = 'usmf' AND d.year IN (2011, 2012)
GROUP BY 1, 2 ORDER BY 1;

-- [8] Top customers — fact + dim_customer
SELECT c.name, c.group_code, count() AS lines,
       round(sum(f.line_amount)) AS revenue, round(sum(f.margin)) AS margin
FROM ax.fact_sales AS f
JOIN ax.dim_customer AS c ON c.data_area = f.data_area AND c.customer_id = f.cust_account
WHERE f.data_area = 'usrt'
GROUP BY 1, 2 ORDER BY revenue DESC LIMIT 15;

-- [9] Order pipeline — fact + dim_status, integers become words
SELECT s.status_name, count() AS lines, round(sum(f.line_amount)) AS amount
FROM ax.fact_sales AS f
JOIN ax.dim_status AS s ON s.status_type = 'sales_status' AND s.status_code = f.sales_status
WHERE f.data_area = 'usrt'
GROUP BY 1 ORDER BY amount DESC;

-- [10] *** FOUR dimensions in one query — this is what the star buys you ***
SELECT d.year, d.quarter, c.group_code, i.item_name, s.status_name,
       round(sum(f.line_amount)) AS revenue, round(sum(f.margin)) AS margin
FROM ax.fact_sales AS f
JOIN ax.dim_customer AS c ON c.data_area = f.data_area AND c.customer_id = f.cust_account
JOIN ax.dim_item     AS i ON i.data_area = f.data_area AND i.item_id     = f.item_id
JOIN ax.dim_date     AS d       ON d.date_key  = f.order_date
JOIN ax.dim_status   AS s ON s.status_type = 'sales_status' AND s.status_code = f.sales_status
WHERE f.data_area = 'usmf' AND d.year IN (2011, 2012)
GROUP BY 1, 2, 3, 4, 5 ORDER BY revenue DESC LIMIT 20;

-- [11] Second fact, same dimensions, different grain: AR aging
SELECT c.name, countIf(f.is_open) AS open_docs,
       round(sumIf(f.open_amount, f.is_open)) AS open_amount,
       max(f.days_overdue) AS worst_days_overdue
FROM ax.fact_cust_trans AS f
JOIN ax.dim_customer AS c ON c.data_area = f.data_area AND c.customer_id = f.cust_account
WHERE f.data_area = 'usmf'
GROUP BY 1 HAVING open_docs > 0 ORDER BY open_amount DESC LIMIT 15;


-- ############ ACT 4 — THE MEASURES: the numbers, catalogued ############

-- [12] ClickHouse has no measure object, so the catalogue is data. 18 measures.
SELECT fact_table, measure_key, measure_name, aggregation, unit, additive, expression
FROM ax.dim_measure ORDER BY fact_table, measure_key;

-- [13] `expression` is paste-able SQL. Take revenue + margin_pct straight from row 12.
SELECT sum(line_amount)                       AS revenue,
       sum(margin) / sum(line_amount) * 100   AS margin_pct
FROM ax.fact_sales WHERE data_area = 'usmf';

-- [14] *** The column that earns the table: which measures must NOT be summed ***
SELECT measure_key, measure_name, aggregation, description
FROM ax.dim_measure WHERE additive = 0 ORDER BY fact_table, measure_key;

-- [15] Derived measures are columns computed by ClickHouse on insert (DEFAULT) or
--      on read (ALIAS) -- the loader never sends them.
SELECT table, name, type, default_kind, default_expression
FROM system.columns
WHERE database = 'ax' AND table LIKE 'fact_%' AND default_kind != '';


-- ############ ACT 5 — THE AGGREGATES: same answers, pre-computed ############

-- [16] The aggregate catalogue. Row counts read live from system.tables, never stored.
SELECT agg_table, source_fact, grain, fact_rows, agg_rows, row_reduction
FROM ax.v_aggregate;

-- [17] Reading it: sums add up across rows. `orders` is a distinct count per
--      aggregate row, so it does NOT add up -- count orders from the fact.
SELECT month, round(sum(revenue)) AS revenue, round(sum(margin)) AS margin,
       sum(order_lines) AS lines
FROM ax.agg_sales_monthly WHERE data_area = 'usmf'
GROUP BY month ORDER BY month;

-- [18] *** Same question, both sides. Identical numbers -- compare rows read. ***
--      Customers and items are the aggregate's own grain keys, so counting
--      them distinct from the aggregate is exact.
SELECT 'from aggregate' AS src, uniqExact(cust_account) AS customers,
       uniqExact(item_id) AS items, round(sum(toFloat64(revenue))) AS revenue
FROM ax.agg_sales_monthly WHERE data_area = 'usrt';

SELECT 'from raw fact' AS src, uniqExact(cust_account) AS customers,
       uniqExact(item_id) AS items, round(sum(toFloat64(line_amount))) AS revenue
FROM ax.fact_sales WHERE data_area = 'usrt' AND sales_status != 4;

-- [19] An aggregate only pays when many fact rows collapse into one.
SELECT data_area, count() AS agg_rows FROM ax.agg_sales_monthly GROUP BY data_area;

-- [20] Aggregates still join to the dimensions -- same star, coarser grain
SELECT c.group_code, d.year, d.quarter, round(sum(a.revenue)) AS revenue
FROM ax.agg_sales_monthly AS a
JOIN ax.dim_customer AS c ON c.data_area = a.data_area AND c.customer_id = a.cust_account
JOIN ax.dim_date     AS d ON d.date_key  = a.month
WHERE a.data_area = 'usmf'
GROUP BY 1, 2, 3 ORDER BY revenue DESC;

-- [21] How fresh is it? One row per table from the last ax_load.py run.
--      Every run reloads the facts and rebuilds all aggregates in one go.
SELECT * FROM ax.v_last_load ORDER BY table_name;


-- ############ BACKUP SLIDES — if there is time or someone asks ############

-- [B1] "Why is ClickHouse fast?" — columnar. One column vs every column, 20M rows.
SELECT sum(revenue) FROM learn.events;
SELECT * FROM learn.events ORDER BY revenue DESC LIMIT 5;

-- [B2] "Does the sort key matter?" — same query shape, 6x the rows read.
--      country leads the ORDER BY and skips granules. device does not.
SELECT sum(revenue) FROM learn.events WHERE country = 'US';
SELECT sum(revenue) FROM learn.events WHERE device  = 'mobile';

-- [B3] The index says so out loud: 409 granules of 2442.
EXPLAIN indexes = 1 SELECT sum(revenue) FROM learn.events WHERE country = 'US';

-- [B4] "How well does it compress?" — country 96 KiB, user_id 76 MiB, same 20M rows.
SELECT name, type, formatReadableSize(sum(data_compressed_bytes)) AS on_disk,
       round(sum(data_uncompressed_bytes) / sum(data_compressed_bytes), 1) AS ratio
FROM system.columns WHERE database = 'learn' AND table = 'events'
GROUP BY name, type ORDER BY sum(data_compressed_bytes) DESC;

-- [B5] "What did all of that cost?" — every query you just ran, and its rows read.
SELECT query_duration_ms AS ms,
       formatReadableQuantity(read_rows) AS rows_read,
       substring(query, 1, 60) AS q
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 30 MINUTE
  AND query NOT LIKE '%query_log%'
ORDER BY event_time DESC LIMIT 20;
