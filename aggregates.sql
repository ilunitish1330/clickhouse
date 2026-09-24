-- Aggregates: the physical roll-up tables, plus ax.dim_aggregate that catalogues them.
--
-- Why a separate table and not a column on dim_measure: an aggregate is its own object
-- with its own grain, source fact and refresh mechanism, and ONE aggregate serves MANY
-- measures. That is a different entity, not an attribute of a measure.
-- The link between the two is NOT a hand-maintained bridge table — the aggregate's
-- column names ARE the measure_keys, so system.columns already knows the answer.
-- Contract: an aggregate column must be named exactly after the measure it stores.
--
--   ~/clickhouse client --port 9010 --queries-file aggregates.sql < /dev/null

-- ============ 1. THE SALES ROLL-UP ============

DROP VIEW  IF EXISTS ax.agg_sales_monthly_mv;
DROP TABLE IF EXISTS ax.agg_sales_monthly;

CREATE TABLE ax.agg_sales_monthly
(
    `data_area`    LowCardinality(String),
    `month`        Date,                                    -- FK to dim_date (first of month)
    `cust_account` String,                                  -- FK to dim_customer
    `item_id`      String,                                  -- FK to dim_item
    `revenue`      SimpleAggregateFunction(sum, Float64),
    `qty`          SimpleAggregateFunction(sum, Float64),
    `cost`         SimpleAggregateFunction(sum, Float64),
    `margin`       SimpleAggregateFunction(sum, Float64),
    `discount`     SimpleAggregateFunction(sum, Float64),
    `order_lines`  SimpleAggregateFunction(sum, UInt64),
    -- non-additive measures stored as merge states, NOT as numbers
    `orders`       AggregateFunction(uniq, String),
    `customers`    AggregateFunction(uniq, String),
    `items_sold`   AggregateFunction(uniq, String)
)
ENGINE = AggregatingMergeTree
ORDER BY (data_area, month, cust_account, item_id);

CREATE MATERIALIZED VIEW ax.agg_sales_monthly_mv TO ax.agg_sales_monthly AS
SELECT data_area,
       toStartOfMonth(order_date)                          AS month,
       cust_account,
       item_id,
       sum(toFloat64(line_amount))                         AS revenue,
       sum(toFloat64(qty_ordered))                         AS qty,
       sum(toFloat64(cost_price) * toFloat64(qty_ordered)) AS cost,
       sum(toFloat64(line_amount) - toFloat64(cost_price) * toFloat64(qty_ordered)) AS margin,
       sum(toFloat64(line_disc))                           AS discount,
       count()                                             AS order_lines,
       uniqState(sales_id)                                 AS orders,
       uniqState(cust_account)                             AS customers,
       uniqState(item_id)                                  AS items_sold
FROM ax.fact_sales
GROUP BY data_area, month, cust_account, item_id;

-- An MV only sees future inserts, so backfill what is already in the fact.
INSERT INTO ax.agg_sales_monthly
SELECT data_area, toStartOfMonth(order_date), cust_account, item_id,
       sum(toFloat64(line_amount)),
       sum(toFloat64(qty_ordered)),
       sum(toFloat64(cost_price) * toFloat64(qty_ordered)),
       sum(toFloat64(line_amount) - toFloat64(cost_price) * toFloat64(qty_ordered)),
       sum(toFloat64(line_disc)),
       count(),
       uniqState(sales_id),
       uniqState(cust_account),
       uniqState(item_id)
FROM ax.fact_sales FINAL
GROUP BY data_area, toStartOfMonth(order_date), cust_account, item_id;

-- ============ 2. THE AR ROLL-UP ============
-- worst_overdue is deliberately absent: it depends on today(), so it cannot be frozen
-- into a roll-up. Non-pre-aggregatable measures stay on the fact.

DROP VIEW  IF EXISTS ax.agg_ar_monthly_mv;
DROP TABLE IF EXISTS ax.agg_ar_monthly;

CREATE TABLE ax.agg_ar_monthly
(
    `data_area`    LowCardinality(String),
    `month`        Date,                                    -- FK to dim_date
    `cust_account` String,                                  -- FK to dim_customer
    `invoiced`     SimpleAggregateFunction(sum, Float64),
    `paid`         SimpleAggregateFunction(sum, Float64),
    `settled`      SimpleAggregateFunction(sum, Float64),
    `open_amount`  SimpleAggregateFunction(sum, Float64),
    `open_docs`    SimpleAggregateFunction(sum, UInt64),
    `transactions` SimpleAggregateFunction(sum, UInt64)
)
ENGINE = AggregatingMergeTree
ORDER BY (data_area, month, cust_account);

CREATE MATERIALIZED VIEW ax.agg_ar_monthly_mv TO ax.agg_ar_monthly AS
SELECT data_area,
       toStartOfMonth(trans_date)                        AS month,
       cust_account,
       sumIf(toFloat64(amount_mst), trans_type = 2)      AS invoiced,
       sumIf(toFloat64(amount_mst), trans_type = 15)     AS paid,
       sumIf(toFloat64(settle_mst), amount_mst > 0)      AS settled,
       sumIf(toFloat64(open_amount), is_open)            AS open_amount,
       countIf(is_open)                                  AS open_docs,
       count()                                           AS transactions
FROM ax.fact_cust_trans
GROUP BY data_area, month, cust_account;

INSERT INTO ax.agg_ar_monthly
SELECT data_area, toStartOfMonth(trans_date), cust_account,
       sumIf(toFloat64(amount_mst), trans_type = 2),
       sumIf(toFloat64(amount_mst), trans_type = 15),
       sumIf(toFloat64(settle_mst), amount_mst > 0),
       sumIf(toFloat64(open_amount), is_open),
       countIf(is_open),
       count()
FROM ax.fact_cust_trans FINAL
GROUP BY data_area, toStartOfMonth(trans_date), cust_account;

-- ============ 3. THE AGGREGATE CATALOG ============
-- Static metadata only. Row counts are NOT stored here — they would go stale the next
-- time anything inserts. v_aggregate below reads them live from system.tables.

DROP TABLE IF EXISTS ax.dim_aggregate;

CREATE TABLE ax.dim_aggregate
(
    `agg_key`     String,
    `agg_name`    String,
    `agg_table`   LowCardinality(String),  -- the physical roll-up
    `source_fact` LowCardinality(String),  -- which fact it rolls up
    `grain`       String,                  -- what one row means
    `engine`      LowCardinality(String),
    `refresh`     String,                  -- how it stays current
    `description` String
)
ENGINE = ReplacingMergeTree
ORDER BY agg_key;

INSERT INTO ax.dim_aggregate VALUES
('agg_sales_monthly', 'Sales by Month / Customer / Item', 'agg_sales_monthly', 'fact_sales',
 'data_area x month x cust_account x item_id', 'AggregatingMergeTree',
 'materialized view agg_sales_monthly_mv (insert trigger) + one-off backfill',
 'Sales roll-up. orders/customers/items_sold are uniq states — uniqMerge(), never sum().'),
('agg_ar_monthly', 'Receivables by Month / Customer', 'agg_ar_monthly', 'fact_cust_trans',
 'data_area x month x cust_account', 'AggregatingMergeTree',
 'materialized view agg_ar_monthly_mv (insert trigger) + one-off backfill',
 'AR roll-up. Excludes worst_overdue: it depends on today() and cannot be frozen. settled is the debit side only — plain sum(settle_mst) cancels itself out.');

-- Browse the aggregates, with live size and how much they save
CREATE OR REPLACE VIEW ax.v_aggregate AS
SELECT a.agg_key, a.agg_name, a.source_fact, a.grain, a.engine,
       f.total_rows                            AS fact_rows,
       g.total_rows                            AS agg_rows,
       round(f.total_rows / g.total_rows, 1)   AS row_reduction,
       formatReadableSize(g.total_bytes)       AS agg_size,
       a.refresh, a.description
FROM ax.dim_aggregate AS a FINAL
JOIN system.tables AS g ON g.database = 'ax' AND g.name = a.agg_table
JOIN system.tables AS f ON f.database = 'ax' AND f.name = a.source_fact;

-- Which measures each aggregate can answer, derived from the column names.
-- No bridge table: the naming contract IS the join.
CREATE OR REPLACE VIEW ax.v_aggregate_measure AS
SELECT a.agg_key, m.measure_key, m.measure_name, m.aggregation, m.unit, m.additive,
       c.type AS stored_as
FROM ax.dim_aggregate AS a FINAL
JOIN ax.dim_measure   AS m FINAL ON m.fact_table = a.source_fact
LEFT JOIN system.columns AS c ON c.database = 'ax' AND c.table = a.agg_table AND c.name = m.measure_key
ORDER BY a.agg_key, m.measure_key;

-- ============ 4. USING IT ============

SELECT * FROM ax.v_aggregate;

-- Covered vs not covered, per aggregate
SELECT agg_key,
       countIf(stored_as != '')                          AS covered,
       countIf(stored_as = '')                           AS not_covered,
       groupArrayIf(measure_key, stored_as = '')         AS must_use_the_fact
FROM ax.v_aggregate_measure GROUP BY agg_key;

-- Read the sales aggregate: sums merge themselves, uniq needs uniqMerge()
SELECT month, round(sum(revenue)) AS revenue, round(sum(margin)) AS margin,
       sum(order_lines) AS lines, uniqMerge(orders) AS orders
FROM ax.agg_sales_monthly WHERE data_area = 'usmf'
GROUP BY month ORDER BY month;

-- Aggregates still join to the dimensions — same star, coarser grain
SELECT c.group_code, d.year, d.quarter,
       round(sum(a.revenue)) AS revenue, round(sum(a.margin)) AS margin,
       uniqMerge(a.orders)   AS orders
FROM ax.agg_sales_monthly AS a
JOIN ax.dim_customer AS c FINAL ON c.data_area = a.data_area AND c.customer_id = a.cust_account
JOIN ax.dim_date     AS d       ON d.date_key  = a.month
WHERE a.data_area = 'usmf'
GROUP BY 1, 2, 3 ORDER BY revenue DESC;

-- AR aggregate, same shape
SELECT d.year_month, round(sum(a.invoiced)) AS invoiced, round(sum(a.paid)) AS paid,
       sum(a.open_docs) AS open_docs
FROM ax.agg_ar_monthly AS a
JOIN ax.dim_date AS d ON d.date_key = a.month
WHERE a.data_area = 'usmf' AND d.year IN (2011, 2012)
GROUP BY 1 ORDER BY 1;

-- Same answer, both sides — watch the rows read
SELECT round(sum(revenue)) AS revenue FROM ax.agg_sales_monthly WHERE data_area = 'usmf';
SELECT round(sum(toFloat64(line_amount))) AS revenue FROM ax.fact_sales FINAL WHERE data_area = 'usmf';
