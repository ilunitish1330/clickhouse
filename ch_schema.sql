-- AX 2012 -> ClickHouse. A star schema built from four source tables and nothing
-- else: SALESTABLE, SALESLINE, CUSTTABLE, CUSTTRANS.
--
--   ~/clickhouse client --port 9010 --queries-file ch_schema.sql
--
-- ponytail: NATURAL keys, not surrogate integers. A surrogate key exists to make
-- a narrow join column out of a wide string -- but LowCardinality(String) already
-- dictionary-encodes to an integer under the hood, so a surrogate would add a
-- lookup step and an ETL stage to buy back something ClickHouse gives for free.
-- Join on (data_area, customer_id) and read the keys without a decoder ring.
--
-- ponytail: no Nullable anywhere. AX writes '' and 0, never NULL, in the columns
-- that matter -- Nullable would cost a byte and a branch per row to represent a
-- state the source never produces.
--
-- data_area is the AX legal entity and it is part of EVERY key: account numbers
-- and item ids are only unique within a company. It is also the currency
-- boundary -- amounts are in the entity's own currency, so never sum across it.

CREATE DATABASE IF NOT EXISTS ax;

-- =============================================================================
-- DIMENSIONS
-- =============================================================================

-- CUSTTABLE. Grain: one customer per legal entity.
CREATE TABLE IF NOT EXISTS ax.dim_customer
(
    data_area       LowCardinality(String),
    customer_id     String,                   -- ACCOUNTNUM
    name            String,
    name_alias      String,
    group_code      LowCardinality(String),   -- CUSTGROUP
    currency        LowCardinality(String),
    payment_terms   LowCardinality(String),   -- PAYMTERMID
    credit_limit    Decimal(32, 16),
    blocked         UInt8,
    invoice_account String,
    recid           Int64,
    modified        DateTime64(3)             -- the ETL watermark
)
ENGINE = ReplacingMergeTree(modified)
ORDER BY (data_area, customer_id);

-- SALESLINE, deduplicated. Grain: one item per legal entity. AX keeps the real
-- product master in INVENTTABLE/ECORESPRODUCT, which is out of scope here, so
-- this dimension is distilled from the item id and name carried on the lines.
CREATE TABLE IF NOT EXISTS ax.dim_item
(
    data_area       LowCardinality(String),
    item_id         String,                   -- ITEMID
    item_name       String,                   -- most recently seen SALESLINE.NAME
    modified        DateTime64(3)
)
ENGINE = ReplacingMergeTree(modified)
ORDER BY (data_area, item_id);

-- Generated, not loaded: a calendar is arithmetic, and no source table holds one.
-- This is the one dimension worth keeping as a real table rather than folding
-- into the facts -- the parts would otherwise be 8 extra columns per fact row.
CREATE TABLE IF NOT EXISTS ax.dim_date
(
    date_key        Date,
    year            UInt16,
    quarter         UInt8,
    month           UInt8,
    month_name      LowCardinality(String),
    week            UInt8,
    day_of_month    UInt8,
    day_name        LowCardinality(String),
    is_weekend      UInt8,
    year_month      LowCardinality(String)    -- '2012-11', groups and sorts as text
)
ENGINE = ReplacingMergeTree()
ORDER BY date_key;

-- Distinct currency codes seen across the four sources.
CREATE TABLE IF NOT EXISTS ax.dim_currency
(
    currency_code   LowCardinality(String),
    modified        DateTime64(3)
)
ENGINE = ReplacingMergeTree(modified)
ORDER BY currency_code;

-- One row per (status_type, status_code) across all three status columns in the
-- four sources: SALESTABLE.SALESSTATUS, SALESTABLE.DOCUMENTSTATUS and
-- CUSTTRANS.TRANSTYPE. A junk dimension -- three small unrelated code sets that
-- do not each deserve a table of their own.
--
-- Codes are verified against this instance's own data. Anything unverified is
-- left labelled 'code_N' rather than given a plausible-looking name: a wrong
-- label is worse than a visible gap, and a generic AX enum map already
-- mislabelled 98% of the orders here once.
CREATE TABLE IF NOT EXISTS ax.dim_status
(
    status_type     LowCardinality(String),   -- sales_status | document_status | trans_type
    status_code     UInt16,
    status_name     LowCardinality(String)
)
ENGINE = ReplacingMergeTree()
ORDER BY (status_type, status_code);

-- =============================================================================
-- FACTS
-- =============================================================================

-- SALESLINE + SALESTABLE header. Grain: one sales order line.
-- Foreign keys are the natural keys of the dimensions above:
--   (data_area, cust_account)      -> dim_customer
--   (data_area, item_id)           -> dim_item
--   order_date / ship_date         -> dim_date
--   currency                       -> dim_currency
--   ('sales_status', sales_status) -> dim_status
CREATE TABLE IF NOT EXISTS ax.fact_sales
(
    data_area       LowCardinality(String),
    sales_id        String,                   -- SALESID, the order this line sits on
    line_num        Decimal(32, 16),          -- LINENUM is numeric in AX, not an int
    cust_account    String,                   -- -> dim_customer.customer_id
    item_id         String,                   -- -> dim_item.item_id
    currency        LowCardinality(String),   -- -> dim_currency.currency_code
    order_date      Date,                     -- -> dim_date.date_key
    ship_date       Date,                     -- -> dim_date.date_key
    sales_status    UInt16,                   -- -> dim_status ('sales_status', code)
    document_status UInt16,                   -- -> dim_status ('document_status', code)
    -- measures
    qty_ordered     Decimal(32, 16),
    sales_price     Decimal(32, 16),          -- unit price
    cost_price      Decimal(32, 16),          -- unit cost
    line_amount     Decimal(32, 16),          -- revenue, net of discount; SUM THIS
    line_disc       Decimal(32, 16),
    -- degenerate dimensions: attributes with no dimension table of their own
    sales_name      String,                   -- SALESTABLE.SALESNAME
    invent_trans_id String,                   -- INVENTTRANSID, the AX line key
    recid           Int64,
    modified        DateTime64(3),
    -- ponytail: Float64, not Decimal. Decimal(32,16) * Decimal(32,16) wants 32
    -- decimal places and overflows ClickHouse's precision ceiling. Float64 carries
    -- ~15 significant digits, exact to the cent at these magnitudes, and this is a
    -- derived analytic column -- the stored measures stay Decimal.
    margin          Float64 ALIAS toFloat64(line_amount)
                                  - toFloat64(cost_price) * toFloat64(qty_ordered),
    margin_pct      Float64 ALIAS if(line_amount = 0, 0,
                                     margin / toFloat64(line_amount) * 100)
)
ENGINE = ReplacingMergeTree(modified)
ORDER BY (data_area, sales_id, line_num, recid);

-- CUSTTRANS. Grain: one posted AR transaction (invoice, payment, adjustment).
CREATE TABLE IF NOT EXISTS ax.fact_cust_trans
(
    data_area       LowCardinality(String),
    cust_account    String,                   -- -> dim_customer.customer_id
    currency        LowCardinality(String),   -- -> dim_currency.currency_code
    trans_date      Date,                     -- -> dim_date.date_key
    due_date        Date,                     -- -> dim_date.date_key
    trans_type      UInt16,                   -- -> dim_status ('trans_type', code)
    -- measures
    amount_cur      Decimal(32, 16),          -- transaction currency
    amount_mst      Decimal(32, 16),          -- legal entity currency
    settle_cur      Decimal(32, 16),
    settle_mst      Decimal(32, 16),
    -- degenerate dimensions
    voucher         String,
    invoice         String,
    txt             String,
    -- AX writes 1900-01-01 into CLOSED while a transaction is still outstanding.
    -- Date's floor is 1970-01-01, so that empty value CLAMPS to 1970 on the way
    -- in -- is_open below tests for the floor, not for 1900. Storing these as
    -- Date rather than DateTime64 is deliberate: they are join keys into
    -- dim_date, which is keyed on Date.
    closed          Date,
    recid           Int64,
    modified        DateTime64(3),
    is_open         UInt8 ALIAS toUInt8(toYear(closed) <= 1970),
    open_amount     Decimal(32, 16) ALIAS amount_mst - settle_mst,
    days_overdue    Int32 ALIAS if(is_open = 0, 0,
                                   toInt32(dateDiff('day', due_date, today())))
)
ENGINE = ReplacingMergeTree(modified)
ORDER BY (data_area, cust_account, trans_date, recid);

-- =============================================================================
-- Seed the two generated dimensions. Both are idempotent: ReplacingMergeTree
-- collapses a re-run on the ORDER BY key, so this file is safe to replay.
-- =============================================================================

-- 2000-01-01 .. 2030-12-31
INSERT INTO ax.dim_date
SELECT d AS date_key, toYear(d), toQuarter(d), toMonth(d),
       formatDateTime(d, '%b'), toISOWeek(d), toDayOfMonth(d),
       formatDateTime(d, '%a'), toUInt8(toDayOfWeek(d) >= 6),
       formatDateTime(d, '%Y-%m')
FROM (SELECT toDate('2000-01-01') + number AS d FROM numbers(11323));

INSERT INTO ax.dim_status VALUES
    ('sales_status',      1, 'Backorder'),
    ('sales_status',      2, 'Delivered'),
    ('sales_status',      3, 'Invoiced'),
    ('sales_status',      4, 'Canceled'),
    ('document_status',   0, 'None'),
    ('document_status',   3, 'Invoice'),
    ('document_status',   4, 'Quotation'),
    ('document_status',   5, 'Confirmation'),
    ('document_status',   7, 'code_7'),
    ('document_status',  10, 'code_10'),
    ('document_status', 101, 'code_101'),
    ('document_status', 102, 'code_102'),
    ('trans_type',        2, 'Invoice'),
    ('trans_type',       15, 'Payment'),
    ('trans_type',        0, 'code_0'),
    ('trans_type',        6, 'code_6'),
    ('trans_type',        8, 'code_8'),
    ('trans_type',        9, 'code_9'),
    ('trans_type',       13, 'code_13'),
    ('trans_type',       14, 'code_14'),
    ('trans_type',       24, 'code_24'),
    ('trans_type',       27, 'code_27'),
    ('trans_type',       29, 'code_29'),
    ('trans_type',       36, 'code_36');
