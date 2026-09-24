-- Star schema demo: ax.dim_* (dimensions) x ax.fact_* (facts).
-- Paste a block at a time into http://localhost:8123/play (Ctrl+Enter).
-- FINAL on every ReplacingMergeTree — it dedupes on read.
-- Two data areas in play:
--   'usrt' — 167k sales lines, but ALL in Nov 2012 (use for volume/customer/item)
--   'usmf' — 6k lines spread evenly over 2011-2012 (use for anything time-based)

-- ============ THE DIMENSIONS ============

-- 1. What's in the model: 5 dimensions, 2 facts
SELECT t, n FROM (
  SELECT 'dim_date' t, count() n FROM ax.dim_date
  UNION ALL SELECT 'dim_customer',    count() FROM ax.dim_customer FINAL
  UNION ALL SELECT 'dim_item',        count() FROM ax.dim_item FINAL
  UNION ALL SELECT 'dim_status',      count() FROM ax.dim_status FINAL
  UNION ALL SELECT 'dim_currency',    count() FROM ax.dim_currency FINAL
  UNION ALL SELECT 'fact_sales',      count() FROM ax.fact_sales FINAL
  UNION ALL SELECT 'fact_cust_trans', count() FROM ax.fact_cust_trans FINAL
) ORDER BY n DESC;


SELECT * FROM ax.dim_date WHERE date_key BETWEEN '2012-06-28' AND '2012-07-03' ORDER BY date_key;


SELECT customer_id, name, group_code, currency, payment_terms, credit_limit
FROM ax.dim_customer FINAL
WHERE data_area = 'usrt' ORDER BY credit_limit DESC LIMIT 10;


SELECT status_type, status_code, status_name FROM ax.dim_status FINAL ORDER BY status_type, status_code;


SELECT data_area, sales_id, line_num, cust_account, item_id, order_date,
       sales_status, qty_ordered, sales_price, line_amount
FROM ax.fact_sales FINAL WHERE data_area = 'usrt' LIMIT 15;

-- 6. Measures alone tell you nothing without dimensions
SELECT count()                        AS lines,
       round(sum(line_amount))        AS revenue,
       round(sum(margin))             AS margin,
       round(avg(margin_pct), 1)      AS avg_margin_pct
FROM ax.fact_sales FINAL WHERE data_area = 'usrt';

-- ============ JOINING THEM — THE POINT OF THE STAR ============

-- 7. Revenue by month: fact + dim_date
SELECT d.year_month, d.month_name,
       count()                 AS lines,
       round(sum(f.line_amount)) AS revenue,
       round(sum(f.margin))      AS margin
FROM ax.fact_sales AS f FINAL
JOIN ax.dim_date AS d ON d.date_key = f.order_date
WHERE f.data_area = 'usmf' AND d.year IN (2011, 2012)
GROUP BY 1, 2 ORDER BY 1;

-- 8. Top customers: fact + dim_customer
SELECT c.name, c.group_code, c.payment_terms,
       count()                   AS orders,
       round(sum(f.line_amount)) AS revenue,
       round(sum(f.margin))      AS margin
FROM ax.fact_sales AS f FINAL
JOIN ax.dim_customer AS c FINAL ON c.data_area = f.data_area AND c.customer_id = f.cust_account
WHERE f.data_area = 'usrt'
GROUP BY 1, 2, 3 ORDER BY revenue DESC LIMIT 15;

-- 9. Top products: fact + dim_item
SELECT i.item_id, i.item_name,
       round(sum(f.qty_ordered)) AS qty,
       round(sum(f.line_amount)) AS revenue,
       round(avg(f.margin_pct), 1) AS margin_pct
FROM ax.fact_sales AS f FINAL
JOIN ax.dim_item AS i FINAL ON i.data_area = f.data_area AND i.item_id = f.item_id
WHERE f.data_area = 'usrt'
GROUP BY 1, 2 ORDER BY revenue DESC LIMIT 15;

-- 10. Order pipeline: fact + dim_status (integer codes become words)
SELECT s.status_name, count() AS lines, round(sum(f.line_amount)) AS amount
FROM ax.fact_sales AS f FINAL
JOIN ax.dim_status AS s FINAL ON s.status_type = 'sales_status' AND s.status_code = f.sales_status
WHERE f.data_area = 'usrt'
GROUP BY 1 ORDER BY amount DESC;

-- 11. FOUR dimensions at once — this is what a star schema buys you
SELECT d.year, d.quarter, c.group_code, i.item_name, s.status_name,
       round(sum(f.line_amount)) AS revenue, round(sum(f.margin)) AS margin
FROM ax.fact_sales AS f FINAL
JOIN ax.dim_customer AS c FINAL ON c.data_area = f.data_area AND c.customer_id = f.cust_account
JOIN ax.dim_item     AS i FINAL ON i.data_area = f.data_area AND i.item_id     = f.item_id
JOIN ax.dim_date     AS d       ON d.date_key  = f.order_date
JOIN ax.dim_status   AS s FINAL ON s.status_type = 'sales_status' AND s.status_code = f.sales_status
WHERE f.data_area = 'usmf' AND d.year IN (2011, 2012)
GROUP BY 1, 2, 3, 4, 5 ORDER BY revenue DESC LIMIT 20;

-- 12. Pivot: customer group across quarters, one row per group
SELECT c.group_code,
       round(sumIf(f.line_amount, d.quarter = 1)) AS q1,
       round(sumIf(f.line_amount, d.quarter = 2)) AS q2,
       round(sumIf(f.line_amount, d.quarter = 3)) AS q3,
       round(sumIf(f.line_amount, d.quarter = 4)) AS q4,
       round(sum(f.line_amount))                  AS total
FROM ax.fact_sales AS f FINAL
JOIN ax.dim_customer AS c FINAL ON c.data_area = f.data_area AND c.customer_id = f.cust_account
JOIN ax.dim_date     AS d       ON d.date_key  = f.order_date
WHERE f.data_area = 'usmf' AND d.year IN (2011, 2012)
GROUP BY 1 ORDER BY total DESC;

-- 13. Weekday vs weekend — a dimension attribute doing real work
SELECT d.day_name, d.is_weekend, count() AS lines, round(sum(f.line_amount)) AS revenue
FROM ax.fact_sales AS f FINAL
JOIN ax.dim_date AS d ON d.date_key = f.order_date
WHERE f.data_area = 'usmf' AND d.year IN (2011, 2012)
GROUP BY 1, 2 ORDER BY revenue DESC;

-- ============ THE SECOND FACT — same dimensions, different grain ============

-- 14. AR aging: open receivables by customer
SELECT c.name,
       countIf(f.is_open)                    AS open_docs,
       round(sumIf(f.open_amount, f.is_open)) AS open_amount,
       max(f.days_overdue)                    AS worst_days_overdue
FROM ax.fact_cust_trans AS f FINAL
JOIN ax.dim_customer AS c FINAL ON c.data_area = f.data_area AND c.customer_id = f.cust_account
WHERE f.data_area = 'usmf'
GROUP BY 1 HAVING open_docs > 0
ORDER BY open_amount DESC LIMIT 15;

-- 15. Invoices vs payments by month: fact_cust_trans + dim_date + dim_status
SELECT d.year_month, s.status_name, count() AS n, round(sum(f.amount_mst)) AS total
FROM ax.fact_cust_trans AS f FINAL
JOIN ax.dim_date   AS d       ON d.date_key = f.trans_date
JOIN ax.dim_status AS s FINAL ON s.status_type = 'trans_type' AND s.status_code = f.trans_type
WHERE f.data_area = 'usmf' AND d.year IN (2011, 2012) AND s.status_name IN ('Invoice', 'Payment')
GROUP BY 1, 2 ORDER BY 1, 2;

-- 16. Both facts, one customer dimension: what they bought vs what they paid
SELECT c.name,
       round(sum(sales.revenue))  AS sales_revenue,
       round(sum(ar.invoiced))    AS ar_invoiced
FROM ax.dim_customer AS c FINAL
JOIN (
  SELECT data_area, cust_account, sum(line_amount) AS revenue
  FROM ax.fact_sales FINAL WHERE data_area = 'usrt' GROUP BY 1, 2
) AS sales ON sales.data_area = c.data_area AND sales.cust_account = c.customer_id
JOIN (
  SELECT data_area, cust_account, sum(amount_mst) AS invoiced
  FROM ax.fact_cust_trans FINAL WHERE data_area = 'usrt' AND trans_type = 2 GROUP BY 1, 2
) AS ar ON ar.data_area = c.data_area AND ar.cust_account = c.customer_id
WHERE c.data_area = 'usrt'
GROUP BY 1 ORDER BY sales_revenue DESC LIMIT 15;
