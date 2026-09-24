-- Measures as a dimension: one row per measure, browsable like dim_customer.
-- ponytail: a plain table, not a semantic layer. ClickHouse has no measure object,
-- so the catalog is data. `expression` is the SQL you paste into a fact query.
-- The aggregation function is a COLUMN here, not its own table — it is an attribute
-- of the measure, and a 5-row lookup joined by key would cost more than it explains.
--   ~/clickhouse client --port 9010 --queries-file dim_measure.sql < /dev/null

DROP TABLE IF EXISTS ax.dim_measure;

CREATE TABLE ax.dim_measure
(
    `measure_key`   String,                  -- stable id; matches the agg table column name
    `measure_name`  String,                  -- what a business user calls it
    `fact_table`    LowCardinality(String),  -- which fact it aggregates
    `aggregation`   LowCardinality(String),  -- sum | avg | count | uniqExact | max | ratio
    `source_column` String,                  -- fact column(s) it reads, '' for count()
    `expression`    String,                  -- paste-able SQL
    `kind`          LowCardinality(String),  -- stored | derived | ratio | count
    `unit`          LowCardinality(String),  -- currency | qty | percent | days | count
    `additive`      UInt8,                   -- 1 = safe to sum across any dimension
    `description`   String
)
ENGINE = ReplacingMergeTree
ORDER BY (fact_table, measure_key);

INSERT INTO ax.dim_measure VALUES
-- fact_sales
('revenue',       'Revenue',             'fact_sales', 'sum',       'line_amount',              'sum(line_amount)',                                    'stored',  'currency', 1, 'Order line amount, net of line discount'),
('qty',           'Quantity Ordered',    'fact_sales', 'sum',       'qty_ordered',              'sum(qty_ordered)',                                    'stored',  'qty',      1, 'Units ordered'),
('cost',          'Cost of Goods',       'fact_sales', 'sum',       'cost_price, qty_ordered',  'sum(toFloat64(cost_price) * toFloat64(qty_ordered))', 'derived', 'currency', 1, 'Unit cost x quantity — cast to Float64, Decimal(32,16) multiplication overflows'),
('discount',      'Line Discount',       'fact_sales', 'sum',       'line_disc',                'sum(line_disc)',                                      'stored',  'currency', 1, 'Discount given on the line'),
('margin',        'Margin',              'fact_sales', 'sum',       'margin',                   'sum(margin)',                                         'derived', 'currency', 1, 'Revenue minus cost; ALIAS column on the fact'),
('margin_pct',    'Margin %',            'fact_sales', 'ratio',     'margin, line_amount',      'sum(margin) / sum(line_amount) * 100',                'ratio',   'percent',  0, 'NOT additive — aggregate both sides, then divide'),
('avg_price',     'Average Sales Price', 'fact_sales', 'avg',       'sales_price',              'avg(sales_price)',                                    'ratio',   'currency', 0, 'NOT additive — re-average from sums, never average an average'),
('order_lines',   'Order Lines',         'fact_sales', 'count',     '',                         'count()',                                             'count',   'count',    1, 'Number of fact rows'),
('orders',        'Orders',              'fact_sales', 'uniqExact', 'sales_id',                 'uniqExact(sales_id)',                                 'count',   'count',    0, 'NOT additive — distinct counts do not add across slices'),
('customers',     'Customers Buying',    'fact_sales', 'uniqExact', 'cust_account',             'uniqExact(cust_account)',                             'count',   'count',    0, 'NOT additive — distinct counts do not add across slices'),
('items_sold',    'Items Sold',          'fact_sales', 'uniqExact', 'item_id',                  'uniqExact(item_id)',                                  'count',   'count',    0, 'NOT additive — distinct counts do not add across slices'),
-- fact_cust_trans
('invoiced',      'Invoiced Amount',     'fact_cust_trans', 'sum',   'amount_mst',              'sumIf(amount_mst, trans_type = 2)',                   'stored',  'currency', 1, 'Invoice postings in company currency'),
('paid',          'Payments Received',   'fact_cust_trans', 'sum',   'amount_mst',              'sumIf(amount_mst, trans_type = 15)',                  'stored',  'currency', 1, 'Payment postings in company currency (negative — they post as credits)'),
('settled',       'Settled Amount',      'fact_cust_trans', 'sum',   'settle_mst',              'sumIf(settle_mst, amount_mst > 0)',                   'stored',  'currency', 1, 'Settlement applied to debit documents. Do NOT use plain sum(settle_mst) — every settlement is posted twice, once positive on the invoice and once negative on the payment, so the two sides cancel to near zero.'),
('open_amount',   'Open Receivables',    'fact_cust_trans', 'sum',   'amount_mst, settle_mst',  'sumIf(open_amount, is_open)',                         'derived', 'currency', 1, 'Unsettled balance; ALIAS column on the fact. Goes negative when an open credit note outweighs open invoices. Does NOT reconcile to invoiced - paid within a month: payments settle earlier months invoices.'),
('open_docs',     'Open Documents',      'fact_cust_trans', 'count', 'closed',                  'countIf(is_open)',                                    'count',   'count',    1, 'Documents still not closed'),
('worst_overdue', 'Worst Days Overdue',  'fact_cust_trans', 'max',   'due_date',                'max(days_overdue)',                                   'derived', 'days',     0, 'NOT additive and NOT pre-aggregatable — depends on today()'),
('transactions',  'Transactions',        'fact_cust_trans', 'count', '',                        'count()',                                             'count',   'count',    1, 'Number of fact rows');

-- Browse it like any other dimension
SELECT fact_table, measure_key, measure_name, aggregation, kind, unit, additive, expression
FROM ax.dim_measure FINAL ORDER BY fact_table, measure_key;

-- The measures you must NOT just sum up
SELECT measure_key, measure_name, aggregation, description
FROM ax.dim_measure FINAL WHERE additive = 0 ORDER BY fact_table, measure_key;

-- The catalog agrees with the schema: derived measures are ALIAS columns, no storage
SELECT m.measure_key, m.expression, c.default_kind, c.default_expression
FROM ax.dim_measure AS m FINAL
LEFT JOIN system.columns AS c
       ON c.database = 'ax' AND c.table = m.fact_table AND m.expression LIKE '%' || c.name || '%'
WHERE c.default_kind = 'ALIAS'
ORDER BY m.measure_key;
