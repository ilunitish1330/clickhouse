-- Aggregates: pre-summarised tables for Power BI, rebuilt from the facts after
-- every load (ax_load.py runs this file last; `python3 ax_load.py --aggregates`
-- runs it alone).
--
-- ponytail: CREATE OR REPLACE TABLE ... AS SELECT, not materialized views. The
-- facts are reloaded in full each run, and an MV would only see the rows of the
-- insert that fired it. REPLACE is atomic, and a full rebuild of all of these
-- from ~1M fact rows takes seconds.
--
-- ponytail: plain numbers, no AggregateFunction states. Power BI cannot read a
-- uniqState column. So the distinct counts here (orders, invoices) are exact
-- AT THE AGGREGATE'S OWN GRAIN only -- summing them across months or customers
-- over-counts. Count distinct from the fact table when you need it across rows.
--
-- Every sum is cast back to Decimal(18, 4): ClickHouse widens sum(Decimal64)
-- to Decimal128, which Power BI cannot take.
--
-- month = first day of the month, joins ax.dim_date.month_start (or date_key).
-- Rows with no date land in month 1970-01-01.
--
-- Note for editing: statements are split on ';' at end of line, so keep ';'
-- out of the end of comment lines and string literals.

-- ============ derived dimensions ============

CREATE OR REPLACE TABLE ax.dim_currency ENGINE = MergeTree ORDER BY currency_code AS
SELECT DISTINCT toString(c) AS currency_code FROM (
    SELECT currency AS c FROM ax.dim_customer
    UNION ALL SELECT currency FROM ax.dim_vendor
    UNION ALL SELECT currency FROM ax.fact_sales
    UNION ALL SELECT currency FROM ax.fact_cust_invoice
    UNION ALL SELECT currency FROM ax.fact_cust_trans
    UNION ALL SELECT currency FROM ax.fact_vend_invoice
    UNION ALL SELECT currency FROM ax.fact_vend_trans
    UNION ALL SELECT currency FROM ax.fact_proj_item_trans
) WHERE c != '';

-- Give every status code that occurs in the data a dim_status row, so no fact
-- row drops out of a Power BI relationship. Named codes are kept, unknown ones
-- become 'code_N'.
INSERT INTO ax.dim_status
SELECT t, c, concat('code_', toString(c)) FROM (
    SELECT DISTINCT 'sales_status' AS t, sales_status AS c FROM ax.fact_sales
    UNION ALL SELECT DISTINCT 'document_status', document_status FROM ax.fact_sales
    UNION ALL SELECT DISTINCT 'trans_type', trans_type FROM ax.fact_cust_trans
    UNION ALL SELECT DISTINCT 'trans_type', trans_type FROM ax.fact_vend_trans
    UNION ALL SELECT DISTINCT 'purch_status', purch_status FROM ax.dim_purch_order
    UNION ALL SELECT DISTINCT 'status_issue', status_issue FROM ax.fact_invent_trans
    UNION ALL SELECT DISTINCT 'status_receipt', status_receipt FROM ax.fact_invent_trans
    UNION ALL SELECT DISTINCT 'invent_ref', ref_category FROM ax.fact_invent_trans
    UNION ALL SELECT DISTINCT 'proj_trans_type', proj_trans_type FROM ax.fact_proj_posting
    UNION ALL SELECT DISTINCT 'posting_type', posting_type FROM ax.fact_proj_posting
    UNION ALL SELECT DISTINCT 'cost_sales', cost_sales FROM ax.fact_proj_posting
    UNION ALL SELECT DISTINCT 'proj_status', status FROM ax.dim_project
    UNION ALL SELECT DISTINCT 'proj_type', proj_type FROM ax.dim_project
) AS seen
WHERE (t, c) NOT IN (SELECT status_type, status_code FROM ax.dim_status)
GROUP BY t, c;

-- ============ sales ============

-- fact_sales -> month x customer x item
CREATE OR REPLACE TABLE ax.agg_sales_monthly
ENGINE = MergeTree ORDER BY (data_area, month, cust_account, item_id) AS
SELECT data_area,
       concat(data_area, '|', cust_account) AS customer_key,
       concat(data_area, '|', item_id) AS item_key,
       toStartOfMonth(order_date)          AS month,
       cust_account,
       item_id,
       toDecimal64(sum(line_amount), 4)    AS revenue,
       toDecimal64(sum(qty_ordered), 4)    AS qty,
       toDecimal64(sum(cost_amount), 4)    AS cost,
       toDecimal64(sum(margin), 4)         AS margin,
       toDecimal64(sum(line_disc), 4)      AS discount,
       count()                             AS order_lines,
       uniqExact(sales_id)                 AS orders       -- NOT additive
FROM ax.fact_sales
WHERE sales_status != 4                    -- canceled lines are not sales
GROUP BY data_area, month, cust_account, item_id;

-- fact_cust_invoice -> month x customer. What was actually billed.
CREATE OR REPLACE TABLE ax.agg_cust_invoice_monthly
ENGINE = MergeTree ORDER BY (data_area, month, cust_account) AS
SELECT data_area,
       concat(data_area, '|', cust_account) AS customer_key,
       toStartOfMonth(invoice_date)            AS month,
       cust_account,
       toDecimal64(sum(invoice_amount_mst), 4) AS invoiced_mst,     -- incl. tax
       toDecimal64(sum(sales_balance_mst), 4)  AS net_sales_mst,    -- excl. tax
       toDecimal64(sum(tax_mst), 4)            AS tax_mst,
       toDecimal64(sum(qty), 4)                AS qty,
       count()                                 AS invoices
FROM ax.fact_cust_invoice
GROUP BY data_area, month, cust_account;

-- fact_cust_trans -> month x customer. Receivables. paid is positive.
-- Open amounts are as of the load, grouped by the month the document posted.
CREATE OR REPLACE TABLE ax.agg_ar_monthly
ENGINE = MergeTree ORDER BY (data_area, month, cust_account) AS
SELECT data_area,
       concat(data_area, '|', cust_account) AS customer_key,
       toStartOfMonth(trans_date)                          AS month,
       cust_account,
       toDecimal64(sumIf(amount_mst, trans_type = 2), 4)   AS invoiced,
       toDecimal64(-sumIf(amount_mst, trans_type = 15), 4) AS paid,
       toDecimal64(sum(amount_mst), 4)                     AS net_change,
       toDecimal64(sumIf(open_amount, is_open = 1), 4) AS open_amount,
       countIf(is_open = 1)                                AS open_docs,
       count()                                             AS transactions
FROM ax.fact_cust_trans
GROUP BY data_area, month, cust_account;

-- ============ purchasing / payables ============

-- fact_vend_invoice_line -> month x vendor x item. What was bought.
CREATE OR REPLACE TABLE ax.agg_purchases_monthly
ENGINE = MergeTree ORDER BY (data_area, month, vendor_account, item_id) AS
SELECT data_area,
       concat(data_area, '|', vendor_account) AS vendor_key,
       concat(data_area, '|', item_id) AS item_key,
       toStartOfMonth(invoice_date)          AS month,
       vendor_account,
       item_id,
       toDecimal64(sum(line_amount_mst), 4)  AS purchase_amount_mst,
       toDecimal64(sum(qty), 4)              AS qty,
       toDecimal64(sum(tax_amount), 4)       AS tax,
       count()                               AS invoice_lines,
       uniqExact(invoice_id)                 AS invoices,     -- NOT additive
       uniqExact(purch_id)                   AS purch_orders  -- NOT additive
FROM ax.fact_vend_invoice_line
GROUP BY data_area, month, vendor_account, item_id;

-- fact_vend_trans -> month x vendor. Payables, shown positive = owed / paid.
-- AP amounts are negative for what we owe, so billed is the negative side and
-- paid_or_credited the positive side (payments, credit notes, reversals).
CREATE OR REPLACE TABLE ax.agg_ap_monthly
ENGINE = MergeTree ORDER BY (data_area, month, vendor_account) AS
SELECT data_area,
       concat(data_area, '|', vendor_account) AS vendor_key,
       toStartOfMonth(trans_date)                           AS month,
       vendor_account,
       toDecimal64(-sumIf(amount_mst, amount_mst < 0), 4)   AS billed,
       toDecimal64(sumIf(amount_mst, amount_mst > 0), 4)    AS paid_or_credited,
       toDecimal64(sum(amount_mst), 4)                      AS net_change,
       toDecimal64(-sumIf(open_amount, is_open = 1), 4) AS open_owed,
       countIf(is_open = 1)                                 AS open_docs,
       count()                                              AS transactions
FROM ax.fact_vend_trans
GROUP BY data_area, month, vendor_account;

-- dim_purch_order -> month x vendor x status. Order pipeline counts.
CREATE OR REPLACE TABLE ax.agg_purch_orders_monthly
ENGINE = MergeTree ORDER BY (data_area, month, vendor_account, purch_status) AS
SELECT data_area,
       concat(data_area, '|', vendor_account) AS vendor_key,
       toStartOfMonth(created_date) AS month,
       vendor_account,
       purch_status,
       count()                      AS purch_orders
FROM ax.dim_purch_order
GROUP BY data_area, month, vendor_account, purch_status;

-- ============ inventory ============

-- fact_invent_trans -> month x item x site x warehouse x source. Movements that
-- actually happened (is_posted), not orders or reservations.
CREATE OR REPLACE TABLE ax.agg_inventory_monthly
ENGINE = MergeTree ORDER BY (data_area, month, item_id, site, warehouse, ref_category) AS
SELECT data_area,
       concat(data_area, '|', item_id) AS item_key,
       toStartOfMonth(movement_date)       AS month,
       item_id,
       site,
       warehouse,
       ref_category,
       toDecimal64(sumIf(qty, qty > 0), 4) AS qty_in,
       toDecimal64(-sumIf(qty, qty < 0), 4) AS qty_out,
       toDecimal64(sum(qty), 4)            AS net_qty,
       toDecimal64(sum(cost_amount), 4)    AS cost_amount,
       count()                             AS transactions
FROM ax.fact_invent_trans
WHERE is_posted = 1
GROUP BY data_area, month, item_id, site, warehouse, ref_category;

-- Current on-hand per item x site x warehouse, as of the load.
-- Value = financial cost where financially updated (Sold/Purchased), else the
-- physical cost. Picked/Registered carry no cost yet, so they add qty only.
-- This mirrors AX's on-hand value closely but is not the closing-run figure.
CREATE OR REPLACE TABLE ax.agg_inventory_onhand
ENGINE = MergeTree ORDER BY (data_area, item_id, site, warehouse) AS
SELECT data_area,
       concat(data_area, '|', item_id) AS item_key,
       item_id,
       site,
       warehouse,
       toDecimal64(sum(qty), 4) AS qty_on_hand,
       toDecimal64(sum(if(status_issue = 1 OR status_receipt = 1,
                          cost_amount, cost_amount_physical)), 4) AS inventory_value,
       max(movement_date)       AS last_movement
FROM ax.fact_invent_trans
WHERE is_posted = 1
GROUP BY data_area, item_id, site, warehouse;

-- ============ projects ============

-- fact_proj_posting -> month x project x transaction type x cost/revenue side
CREATE OR REPLACE TABLE ax.agg_project_monthly
ENGINE = MergeTree ORDER BY (data_area, month, proj_id, proj_trans_type, cost_sales) AS
SELECT data_area,
       concat(data_area, '|', proj_id) AS project_key,
       toStartOfMonth(trans_date)       AS month,
       proj_id,
       proj_trans_type,
       cost_sales,
       toDecimal64(sum(amount_mst), 4)  AS amount_mst,
       toDecimal64(sum(qty), 4)         AS qty,
       count()                          AS postings
FROM ax.fact_proj_posting
GROUP BY data_area, month, proj_id, proj_trans_type, cost_sales;

-- fact_proj_item_trans -> month x project x item
CREATE OR REPLACE TABLE ax.agg_proj_items_monthly
ENGINE = MergeTree ORDER BY (data_area, month, proj_id, item_id) AS
SELECT data_area,
       concat(data_area, '|', proj_id) AS project_key,
       concat(data_area, '|', item_id) AS item_key,
       toStartOfMonth(trans_date)         AS month,
       proj_id,
       item_id,
       toDecimal64(sum(qty), 4)           AS qty,
       toDecimal64(sum(cost_amount), 4)   AS cost_amount,
       toDecimal64(sum(sales_amount), 4)  AS sales_amount,
       count()                            AS transactions
FROM ax.fact_proj_item_trans
GROUP BY data_area, month, proj_id, item_id;

-- ============ the aggregate catalog ============
-- Static description only. Row counts come live from system.tables in v_aggregate.

CREATE OR REPLACE TABLE ax.dim_aggregate
(
    agg_table   String,
    source_fact LowCardinality(String),
    grain       String,
    description String
)
ENGINE = MergeTree ORDER BY agg_table;

INSERT INTO ax.dim_aggregate VALUES
('agg_sales_monthly',        'fact_sales',             'data_area x month x cust_account x item_id', 'Sales order lines, canceled excluded. orders is distinct per row, not additive.'),
('agg_cust_invoice_monthly', 'fact_cust_invoice',      'data_area x month x cust_account',           'Posted customer invoices in company currency.'),
('agg_ar_monthly',           'fact_cust_trans',        'data_area x month x cust_account',           'Receivables: invoiced, paid (positive), open as of load.'),
('agg_purchases_monthly',    'fact_vend_invoice_line', 'data_area x month x vendor_account x item_id','Vendor invoice lines in company currency.'),
('agg_ap_monthly',           'fact_vend_trans',        'data_area x month x vendor_account',         'Payables, positive = owed. billed = negative AP side.'),
('agg_purch_orders_monthly', 'dim_purch_order',        'data_area x month x vendor_account x purch_status', 'Purchase order counts by creation month.'),
('agg_inventory_monthly',    'fact_invent_trans',      'data_area x month x item x site x warehouse x ref_category', 'Posted inventory movements in / out and cost.'),
('agg_inventory_onhand',     'fact_invent_trans',      'data_area x item x site x warehouse',        'On hand quantity and value as of load.'),
('agg_project_monthly',      'fact_proj_posting',      'data_area x month x proj_id x proj_trans_type x cost_sales', 'Project postings in company currency.'),
('agg_proj_items_monthly',   'fact_proj_item_trans',   'data_area x month x proj_id x item_id',      'Items consumed on projects: cost and sales value.');

CREATE OR REPLACE VIEW ax.v_aggregate AS
SELECT a.agg_table, a.source_fact, a.grain,
       f.total_rows                          AS fact_rows,
       g.total_rows                          AS agg_rows,
       round(f.total_rows / greatest(g.total_rows, 1), 1) AS row_reduction,
       a.description
FROM ax.dim_aggregate AS a
LEFT JOIN system.tables AS g ON g.database = 'ax' AND g.name = a.agg_table
LEFT JOIN system.tables AS f ON f.database = 'ax' AND f.name = a.source_fact;

-- When each table was last loaded: the "data as of" line for a report.
CREATE OR REPLACE VIEW ax.v_last_load AS
SELECT table_name, argMax(rows, loaded_at) AS last_rows, max(loaded_at) AS last_loaded_at
FROM ax.load_log
GROUP BY table_name;
