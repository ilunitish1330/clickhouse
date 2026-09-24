-- Smoke test for the ax star schema (SALESTABLE, SALESLINE, CUSTTABLE, CUSTTRANS).
--   ~/clickhouse client --port 9010 --queries-file test_star.sql
-- ponytail: plain SQL, no test framework. FINAL on every ReplacingMergeTree.

-- 1. Row counts.
SELECT t, n FROM (
  SELECT 'dim_customer' t, count() n FROM ax.dim_customer FINAL
  UNION ALL SELECT 'dim_item',        count() FROM ax.dim_item FINAL
  UNION ALL SELECT 'dim_date',        count() FROM ax.dim_date
  UNION ALL SELECT 'dim_currency',    count() FROM ax.dim_currency FINAL
  UNION ALL SELECT 'dim_status',      count() FROM ax.dim_status FINAL
  UNION ALL SELECT 'fact_sales',      count() FROM ax.fact_sales FINAL
  UNION ALL SELECT 'fact_cust_trans', count() FROM ax.fact_cust_trans FINAL
) ORDER BY n DESC;

-- 2. Referential integrity: every fact FK must resolve to a dimension row.
--    All four counters must be 0.
SELECT countIf(c.customer_id = '')                                  AS orphan_customer,
       countIf(i.item_id = '' AND f.item_id != '')                  AS orphan_item,
       countIf(d.date_key = toDate(0) AND f.order_date != toDate(0)) AS orphan_date,
       countIf(cu.currency_code = '')                               AS orphan_currency,
       count()                                                      AS rows
FROM ax.fact_sales AS f FINAL
LEFT JOIN ax.dim_customer AS c  FINAL ON c.data_area = f.data_area AND c.customer_id = f.cust_account
LEFT JOIN ax.dim_item     AS i  FINAL ON i.data_area = f.data_area AND i.item_id = f.item_id
LEFT JOIN ax.dim_date     AS d        ON d.date_key = f.order_date
LEFT JOIN ax.dim_currency AS cu FINAL ON cu.currency_code = f.currency;

-- 3. Five dimensions in one query — the point of the star.
SELECT d.year, d.quarter, c.group_code, i.item_name, s.status_name,
       round(sum(f.line_amount)) AS revenue, round(sum(f.margin)) AS margin
FROM ax.fact_sales AS f FINAL
JOIN ax.dim_customer AS c FINAL ON c.data_area = f.data_area AND c.customer_id = f.cust_account
JOIN ax.dim_item     AS i FINAL ON i.data_area = f.data_area AND i.item_id = f.item_id
JOIN ax.dim_date     AS d       ON d.date_key = f.order_date
JOIN ax.dim_status   AS s FINAL ON s.status_type = 'sales_status' AND s.status_code = f.sales_status
WHERE f.data_area = 'usrt' AND d.year = 2012
GROUP BY 1, 2, 3, 4, 5 ORDER BY revenue DESC LIMIT 10;

-- 4. AR star: customer x month x transaction type.
SELECT c.name, d.year_month, s.status_name, count() AS n, round(sum(f.amount_mst)) AS total
FROM ax.fact_cust_trans AS f FINAL
JOIN ax.dim_customer AS c FINAL ON c.data_area = f.data_area AND c.customer_id = f.cust_account
JOIN ax.dim_date     AS d       ON d.date_key = f.trans_date
JOIN ax.dim_status   AS s FINAL ON s.status_type = 'trans_type' AND s.status_code = f.trans_type
WHERE f.data_area = 'usmf'
GROUP BY 1, 2, 3 ORDER BY abs(total) DESC LIMIT 10;

-- 5. Derived ALIAS columns on both facts.
SELECT round(sum(margin)) AS margin, round(avg(margin_pct), 1) AS avg_pct
FROM ax.fact_sales FINAL WHERE data_area = 'usrt';

SELECT countIf(is_open) AS open_rows, round(sum(open_amount)) AS open_amt,
       max(days_overdue) AS worst
FROM ax.fact_cust_trans FINAL;

-- 6. dim_date carries the calendar parts, so no date arithmetic in the query.
SELECT d.year_month, d.month_name, round(sum(f.line_amount)) AS revenue
FROM ax.fact_sales AS f FINAL
JOIN ax.dim_date AS d ON d.date_key = f.ship_date
WHERE f.data_area = 'usrt' AND d.year = 2012
GROUP BY 1, 2 ORDER BY 1;
