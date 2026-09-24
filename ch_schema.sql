-- AX 2012 -> ClickHouse star schema, the target of ax_load.py.
--
--   python3 ax_load.py --init          # runs this file (no clickhouse client needed)
--
-- Sources (17 AX tables):
--   DIRPARTYTABLE  CUSTTABLE  VENDTABLE  INVENTTABLE  PROJTABLE
--   SALESTABLE  SALESLINE  CUSTTRANS  CUSTINVOICEJOUR
--   PURCHTABLE  VENDTRANS  VENDINVOICEJOUR  VENDINVOICETRANS
--   INVENTTRANS  PROJTRANSPOSTING  PROJITEMTRANS
-- plus three lookups that make them readable, joined on the way in, never
-- stored on their own:
--   ECORESPRODUCT / ECORESPRODUCTTRANSLATION  item number + name (INVENTTABLE has neither)
--   INVENTTRANSORIGIN                         what an inventory transaction belongs to
--   INVENTDIM                                 site / warehouse of an inventory transaction
--
-- ponytail: plain MergeTree everywhere, no ReplacingMergeTree. ax_load.py
-- reloads every table in full and swaps it in atomically (EXCHANGE TABLES), so
-- a table never holds two versions of a row. That matters for Power BI: it
-- never writes FINAL, and a ReplacingMergeTree read without FINAL double counts
-- every row that changed since the last merge. The largest source is ~340k
-- rows; a full reload is a minute. Revisit per table past ~50M rows.
--
-- ponytail: Decimal(18, 4), not AX's numeric(32,16). Power BI's decimal type
-- is 19 digits with 4 decimals; Decimal128 columns arrive as text or fail.
-- 10^14 is ample for any amount here, and AX money is rounded to 2 places.
--
-- ponytail: no Nullable. AX writes '' and 0, never NULL. AX's empty date is
-- 1900-01-01, which Date cannot hold -- the loader writes it as 1970-01-01,
-- Date's floor. So "no date" is `= '1970-01-01'` everywhere below.
--
-- data_area is the AX legal entity and part of EVERY key: account numbers and
-- item ids are only unique within a company. Amounts named *_mst are in that
-- company's own currency -- never sum them across data_area values that have
-- different accounting currencies.
--
-- Power BI relationships join on ONE column, and every AX key is two
-- (data_area + id). So each table also carries *_key = 'usmf|C0001' columns:
-- relate fact.customer_key -> dim_customer.customer_key, and so on.
--
-- Derived columns use DEFAULT, not ALIAS/MATERIALIZED: a DEFAULT column is an
-- ordinary column that SELECT * returns, so Power BI sees it. The loader never
-- sends them, so ClickHouse computes them on insert.

CREATE DATABASE IF NOT EXISTS ax;

-- =============================================================================
-- DIMENSIONS
-- =============================================================================

-- CUSTTABLE + DIRPARTYTABLE (name). Grain: one customer per legal entity.
CREATE TABLE IF NOT EXISTS ax.dim_customer
(
    data_area       LowCardinality(String),
    customer_id     String,                   -- ACCOUNTNUM
    name            String,                   -- DIRPARTYTABLE.NAME
    name_alias      String,
    group_code      LowCardinality(String),   -- CUSTGROUP
    currency        LowCardinality(String),
    payment_terms   LowCardinality(String),   -- PAYMTERMID
    credit_limit    Decimal(18, 4),
    blocked         UInt8,
    invoice_account String,
    recid           Int64,
    customer_key    String DEFAULT concat(data_area, '|', customer_id)
)
ENGINE = MergeTree
ORDER BY (data_area, customer_id);

-- VENDTABLE + DIRPARTYTABLE (name). Grain: one vendor per legal entity.
CREATE TABLE IF NOT EXISTS ax.dim_vendor
(
    data_area       LowCardinality(String),
    vendor_id       String,                   -- ACCOUNTNUM
    name            String,                   -- DIRPARTYTABLE.NAME
    name_alias      String,
    group_code      LowCardinality(String),   -- VENDGROUP
    currency        LowCardinality(String),
    payment_terms   LowCardinality(String),
    blocked         UInt8,
    invoice_account String,
    recid           Int64,
    vendor_key      String DEFAULT concat(data_area, '|', vendor_id)
)
ENGINE = MergeTree
ORDER BY (data_area, vendor_id);

-- INVENTTABLE, the item master. Name and product number come from the global
-- product (ECORESPRODUCT*), one row per item: English name if there is one,
-- else any translation, so a second language cannot fan the grain out.
CREATE TABLE IF NOT EXISTS ax.dim_item
(
    data_area       LowCardinality(String),
    item_id         String,                   -- ITEMID
    item_name       String,                   -- ECORESPRODUCTTRANSLATION.NAME
    name_alias      String,                   -- INVENTTABLE.NAMEALIAS (search name)
    product_number  String,                   -- ECORESPRODUCT.DISPLAYPRODUCTNUMBER
    item_type       UInt16,                   -- 0 Item, 1 BOM, 2 Service
    primary_vendor  String,                   -- PRIMARYVENDORID -> dim_vendor
    buyer_group     LowCardinality(String),   -- ITEMBUYERGROUPID
    cost_group      LowCardinality(String),   -- COSTGROUPID
    prod_group      LowCardinality(String),   -- PRODGROUPID
    recid           Int64,
    item_key        String DEFAULT concat(data_area, '|', item_id),
    vendor_key      String DEFAULT concat(data_area, '|', primary_vendor)
)
ENGINE = MergeTree
ORDER BY (data_area, item_id);

-- PROJTABLE. Grain: one project per legal entity.
CREATE TABLE IF NOT EXISTS ax.dim_project
(
    data_area       LowCardinality(String),
    proj_id         String,
    name            String,
    proj_group      LowCardinality(String),   -- PROJGROUPID
    parent_id       String,                   -- PARENTID, '' for top level
    cust_account    String,                   -- -> dim_customer
    status          UInt16,                   -- -> dim_status ('proj_status', code)
    proj_type       UInt16,                   -- -> dim_status ('proj_type', code)
    created_date    Date,
    start_date      Date,
    end_date        Date,
    recid           Int64,
    project_key     String DEFAULT concat(data_area, '|', proj_id),
    customer_key    String DEFAULT concat(data_area, '|', cust_account)
)
ENGINE = MergeTree
ORDER BY (data_area, proj_id);

-- PURCHTABLE, including PURCHNAME. Header only: amounts live on the vendor
-- invoice lines, which point back here by purch_id. Grain: one purchase order.
CREATE TABLE IF NOT EXISTS ax.dim_purch_order
(
    data_area       LowCardinality(String),
    purch_id        String,
    purch_name      String,                   -- PURCHNAME
    vendor_account  String,                   -- ORDERACCOUNT -> dim_vendor
    invoice_account String,
    vend_group      LowCardinality(String),
    currency        LowCardinality(String),
    purch_status    UInt16,                   -- -> dim_status ('purch_status', code)
    document_status UInt16,
    purchase_type   UInt16,
    delivery_date   Date,
    created_date    Date,
    proj_id         String,
    site            LowCardinality(String),   -- INVENTSITEID
    warehouse       LowCardinality(String),   -- INVENTLOCATIONID
    recid           Int64,
    purch_key       String DEFAULT concat(data_area, '|', purch_id),
    vendor_key      String DEFAULT concat(data_area, '|', vendor_account)
)
ENGINE = MergeTree
ORDER BY (data_area, purch_id);

-- One row per table per load run: what Power BI can show as "data as of".
CREATE TABLE IF NOT EXISTS ax.load_log
(
    loaded_at       DateTime DEFAULT now(),
    table_name      LowCardinality(String),
    rows            UInt64,
    seconds         Float32
)
ENGINE = MergeTree
ORDER BY (table_name, loaded_at);

-- Generated, not loaded: 2000-01-01 .. 2030-12-31.
CREATE OR REPLACE TABLE ax.dim_date
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
    year_month      LowCardinality(String),   -- '2012-11', groups and sorts as text
    month_start     Date                      -- joins the monthly aggregates
)
ENGINE = MergeTree
ORDER BY date_key;

INSERT INTO ax.dim_date
SELECT d, toYear(d), toQuarter(d), toMonth(d), formatDateTime(d, '%b'), toISOWeek(d),
       toDayOfMonth(d), formatDateTime(d, '%a'), toUInt8(toDayOfWeek(d) >= 6),
       formatDateTime(d, '%Y-%m'), toStartOfMonth(d)
FROM (SELECT toDate('2000-01-01') + number AS d FROM numbers(11323));

-- One row per (status_type, status_code): a junk dimension for every enum
-- column in the facts. Codes seen in the data but not named here get a
-- 'code_N' row from aggregates.sql, so every code joins to something.
--
-- sales_status / document_status / trans_type (customer side) were verified
-- against this instance's data. The rest are the standard AX 2012 enum values
-- (PurchStatus, StatusIssue, StatusReceipt, InventTransType) -- check them with
-- the "unverified labels" query in aggregates.sql before relying on them.
-- Unknown codes are left as code_N on purpose: a wrong label is worse than a gap.
CREATE OR REPLACE TABLE ax.dim_status
(
    status_type     LowCardinality(String),
    status_code     UInt16,
    status_name     LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY (status_type, status_code);

INSERT INTO ax.dim_status VALUES
    ('sales_status', 0, 'None'), ('sales_status', 1, 'Backorder'),
    ('sales_status', 2, 'Delivered'), ('sales_status', 3, 'Invoiced'),
    ('sales_status', 4, 'Canceled'),
    ('document_status', 0, 'None'), ('document_status', 3, 'Invoice'),
    ('document_status', 4, 'Quotation'), ('document_status', 5, 'Confirmation'),
    ('trans_type', 2, 'Invoice'), ('trans_type', 15, 'Payment'),
    ('purch_status', 0, 'None'), ('purch_status', 1, 'Backorder'),
    ('purch_status', 2, 'Received'), ('purch_status', 3, 'Invoiced'),
    ('purch_status', 4, 'Canceled'),
    ('status_issue', 0, 'None'), ('status_issue', 1, 'Sold'),
    ('status_issue', 2, 'Deducted'), ('status_issue', 3, 'Picked'),
    ('status_issue', 4, 'Reserved physical'), ('status_issue', 5, 'Reserved ordered'),
    ('status_issue', 6, 'On order'), ('status_issue', 7, 'Quotation issue'),
    ('status_receipt', 0, 'None'), ('status_receipt', 1, 'Purchased'),
    ('status_receipt', 2, 'Received'), ('status_receipt', 3, 'Registered'),
    ('status_receipt', 4, 'Arrived'), ('status_receipt', 5, 'Ordered'),
    ('status_receipt', 6, 'Quotation receipt'),
    ('invent_ref', 0, 'Sales order'), ('invent_ref', 2, 'Production'),
    ('invent_ref', 3, 'Purchase order'), ('invent_ref', 4, 'Inventory journal'),
    ('invent_ref', 5, 'Profit/loss journal'), ('invent_ref', 6, 'Transfer journal');

-- =============================================================================
-- FACTS
-- =============================================================================

-- SALESLINE + SALESTABLE header. Grain: one sales order line.
CREATE TABLE IF NOT EXISTS ax.fact_sales
(
    data_area       LowCardinality(String),
    sales_id        String,
    line_num        Decimal(18, 4),           -- LINENUM is numeric in AX
    cust_account    String,                   -- -> dim_customer
    item_id         String,                   -- -> dim_item
    currency        LowCardinality(String),
    order_date      Date,                     -- SALESTABLE.CREATEDDATETIME
    ship_date       Date,                     -- SHIPPINGDATEREQUESTED
    sales_status    UInt16,                   -- -> dim_status ('sales_status')
    document_status UInt16,                   -- -> dim_status ('document_status')
    qty_ordered     Decimal(18, 4),
    sales_price     Decimal(18, 4),           -- unit price
    cost_price      Decimal(18, 4),           -- unit cost
    line_amount     Decimal(18, 4),           -- revenue net of discount: SUM THIS
    line_disc       Decimal(18, 4),
    sales_name      String,
    proj_id         String,
    invent_trans_id String,
    recid           Int64,
    cost_amount     Decimal(18, 4) DEFAULT toDecimal64(cost_price * qty_ordered, 4),
    margin          Decimal(18, 4) DEFAULT toDecimal64(line_amount - cost_price * qty_ordered, 4),
    margin_pct      Float64 DEFAULT if(line_amount = 0, 0, toFloat64(margin) / toFloat64(line_amount) * 100),
    customer_key    String DEFAULT concat(data_area, '|', cust_account),
    item_key        String DEFAULT concat(data_area, '|', item_id)
)
ENGINE = MergeTree
ORDER BY (data_area, order_date, sales_id, line_num);

-- CUSTINVOICEJOUR. Grain: one posted customer invoice.
CREATE TABLE IF NOT EXISTS ax.fact_cust_invoice
(
    data_area       LowCardinality(String),
    invoice_id      String,
    invoice_date    Date,
    due_date        Date,
    sales_id        String,                   -- -> fact_sales.sales_id ('' if free text)
    cust_account    String,                   -- INVOICEACCOUNT -> dim_customer
    order_account   String,
    cust_group      LowCardinality(String),
    currency        LowCardinality(String),
    qty             Decimal(18, 4),
    invoice_amount  Decimal(18, 4),           -- incl. tax, invoice currency
    invoice_amount_mst Decimal(18, 4),        -- incl. tax, company currency
    sales_balance_mst  Decimal(18, 4),        -- net of tax, company currency
    tax_mst         Decimal(18, 4),
    line_disc_mst   Decimal(18, 4),
    ledger_voucher  String,
    recid           Int64,
    customer_key    String DEFAULT concat(data_area, '|', cust_account)
)
ENGINE = MergeTree
ORDER BY (data_area, invoice_date, invoice_id);

-- CUSTTRANS. Grain: one posted AR transaction (invoice, payment, adjustment).
CREATE TABLE IF NOT EXISTS ax.fact_cust_trans
(
    data_area       LowCardinality(String),
    cust_account    String,                   -- -> dim_customer
    currency        LowCardinality(String),
    trans_date      Date,
    due_date        Date,
    closed          Date,                     -- 1970-01-01 = still open
    trans_type      UInt16,                   -- -> dim_status ('trans_type')
    amount_cur      Decimal(18, 4),
    amount_mst      Decimal(18, 4),
    settle_cur      Decimal(18, 4),
    settle_mst      Decimal(18, 4),
    voucher         String,
    invoice         String,
    txt             String,
    recid           Int64,
    is_open         UInt8          DEFAULT toUInt8(closed = '1970-01-01'),
    open_amount Decimal(18, 4) DEFAULT amount_mst - settle_mst,
    -- ALIAS, not DEFAULT: it moves with today(). Queryable by name, not in SELECT *.
    days_overdue Int32 ALIAS if(is_open = 0, 0, toInt32(dateDiff('day', due_date, today()))),
    customer_key    String DEFAULT concat(data_area, '|', cust_account)
)
ENGINE = MergeTree
ORDER BY (data_area, cust_account, trans_date, recid);

-- VENDINVOICEJOUR. Grain: one posted vendor invoice.
CREATE TABLE IF NOT EXISTS ax.fact_vend_invoice
(
    data_area       LowCardinality(String),
    invoice_id      String,
    invoice_date    Date,
    due_date        Date,
    purch_id        String,                   -- -> dim_purch_order
    vendor_account  String,                   -- INVOICEACCOUNT -> dim_vendor
    order_account   String,
    vend_group      LowCardinality(String),
    currency        LowCardinality(String),
    qty             Decimal(18, 4),
    invoice_amount  Decimal(18, 4),
    invoice_amount_mst Decimal(18, 4),
    tax             Decimal(18, 4),
    line_disc       Decimal(18, 4),
    ledger_voucher  String,
    internal_invoice_id String,
    recid           Int64,
    vendor_key      String DEFAULT concat(data_area, '|', vendor_account),
    purch_key       String DEFAULT concat(data_area, '|', purch_id)
)
ENGINE = MergeTree
ORDER BY (data_area, invoice_date, invoice_id);

-- VENDINVOICETRANS + vendor from its VENDINVOICEJOUR header.
-- Grain: one vendor invoice line.
CREATE TABLE IF NOT EXISTS ax.fact_vend_invoice_line
(
    data_area       LowCardinality(String),
    invoice_id      String,
    invoice_date    Date,
    line_num        Decimal(18, 4),
    purch_id        String,                   -- -> dim_purch_order
    vendor_account  String,                   -- from VENDINVOICEJOUR -> dim_vendor
    item_id         String,                   -- -> dim_item
    item_name       String,                   -- NAME as invoiced
    currency        LowCardinality(String),
    qty             Decimal(18, 4),
    purch_price     Decimal(18, 4),
    line_amount     Decimal(18, 4),           -- invoice currency
    line_amount_mst Decimal(18, 4),           -- company currency: SUM THIS across currencies
    tax_amount      Decimal(18, 4),
    disc_amount     Decimal(18, 4),
    invent_trans_id String,
    recid           Int64,
    vendor_key      String DEFAULT concat(data_area, '|', vendor_account),
    item_key        String DEFAULT concat(data_area, '|', item_id),
    purch_key       String DEFAULT concat(data_area, '|', purch_id)
)
ENGINE = MergeTree
ORDER BY (data_area, invoice_date, invoice_id, line_num);

-- VENDTRANS. Grain: one posted AP transaction. AP amounts are signed from the
-- company's side: an invoice is negative (we owe), a payment positive.
CREATE TABLE IF NOT EXISTS ax.fact_vend_trans
(
    data_area       LowCardinality(String),
    vendor_account  String,                   -- -> dim_vendor
    currency        LowCardinality(String),
    trans_date      Date,
    due_date        Date,
    closed          Date,                     -- 1970-01-01 = still open
    trans_type      UInt16,                   -- -> dim_status ('trans_type')
    amount_cur      Decimal(18, 4),
    amount_mst      Decimal(18, 4),
    settle_cur      Decimal(18, 4),
    settle_mst      Decimal(18, 4),
    voucher         String,
    invoice         String,
    txt             String,
    recid           Int64,
    is_open         UInt8          DEFAULT toUInt8(closed = '1970-01-01'),
    open_amount Decimal(18, 4) DEFAULT amount_mst - settle_mst,
    -- ALIAS, not DEFAULT: it moves with today(). Queryable by name, not in SELECT *.
    days_overdue Int32 ALIAS if(is_open = 0, 0, toInt32(dateDiff('day', due_date, today()))),
    vendor_key      String DEFAULT concat(data_area, '|', vendor_account)
)
ENGINE = MergeTree
ORDER BY (data_area, vendor_account, trans_date, recid);

-- INVENTTRANS + INVENTTRANSORIGIN (what it belongs to) + INVENTDIM (where).
-- Grain: one inventory transaction. qty is signed: + receipt, - issue.
-- Financial cost = cost_amount_posted + cost_amount_adjustment.
CREATE TABLE IF NOT EXISTS ax.fact_invent_trans
(
    data_area       LowCardinality(String),
    item_id         String,                   -- -> dim_item
    ref_category    UInt16,                   -- -> dim_status ('invent_ref')
    ref_id          String,                   -- sales id, purch id, journal id...
    status_issue    UInt16,                   -- -> dim_status ('status_issue')
    status_receipt  UInt16,                   -- -> dim_status ('status_receipt')
    date_physical   Date,
    date_financial  Date,
    date_status     Date,
    qty             Decimal(18, 4),
    cost_amount_posted     Decimal(18, 4),
    cost_amount_physical   Decimal(18, 4),
    cost_amount_adjustment Decimal(18, 4),
    revenue_amount_physical Decimal(18, 4),
    site            LowCardinality(String),   -- INVENTDIM.INVENTSITEID
    warehouse       LowCardinality(String),   -- INVENTDIM.INVENTLOCATIONID
    currency        LowCardinality(String),
    invoice_id      String,
    voucher         String,
    proj_id         String,
    invent_trans_id String,                   -- INVENTTRANSORIGIN.INVENTTRANSID
    recid           Int64,
    -- the day it counts: financial update, else physical, else last status change
    movement_date   Date DEFAULT multiIf(date_financial > '1970-01-01', date_financial,
                                         date_physical  > '1970-01-01', date_physical,
                                         date_status),
    -- 1 = physically or financially updated (on hand), 0 = ordered/reserved/quoted
    is_posted       UInt8 DEFAULT toUInt8(status_issue IN (1, 2, 3) OR status_receipt IN (1, 2, 3)),
    cost_amount     Decimal(18, 4) DEFAULT cost_amount_posted + cost_amount_adjustment,
    item_key        String DEFAULT concat(data_area, '|', item_id)
)
ENGINE = MergeTree
ORDER BY (data_area, item_id, movement_date, recid);

-- PROJTRANSPOSTING. Grain: one project ledger posting (cost or revenue side).
CREATE TABLE IF NOT EXISTS ax.fact_proj_posting
(
    data_area       LowCardinality(String),
    proj_id         String,                   -- -> dim_project
    proj_trans_type UInt16,                   -- -> dim_status ('proj_trans_type')
    posting_type    UInt16,                   -- -> dim_status ('posting_type')
    cost_sales      UInt16,                   -- -> dim_status ('cost_sales')
    trans_date      Date,                     -- PROJTRANSDATE
    ledger_date     Date,                     -- LEDGERTRANSDATE
    amount_mst      Decimal(18, 4),
    qty             Decimal(18, 4),
    category_id     LowCardinality(String),
    item_id         String,                   -- EMPLITEMID: item or worker
    voucher         String,
    trans_id        String,
    recid           Int64,
    project_key     String DEFAULT concat(data_area, '|', proj_id)
)
ENGINE = MergeTree
ORDER BY (data_area, proj_id, trans_date, recid);

-- PROJITEMTRANS. Grain: one item consumed on a project.
CREATE TABLE IF NOT EXISTS ax.fact_proj_item_trans
(
    data_area       LowCardinality(String),
    proj_id         String,                   -- -> dim_project
    item_id         String,                   -- -> dim_item
    category_id     LowCardinality(String),
    trans_date      Date,
    currency        LowCardinality(String),
    qty             Decimal(18, 4),
    cost_amount     Decimal(18, 4),           -- TOTALCOSTAMOUNTCUR
    sales_amount    Decimal(18, 4),           -- TOTALSALESAMOUNTCUR
    txt             String,
    proj_trans_id   String,
    recid           Int64,
    project_key     String DEFAULT concat(data_area, '|', proj_id),
    item_key        String DEFAULT concat(data_area, '|', item_id)
)
ENGINE = MergeTree
ORDER BY (data_area, proj_id, trans_date, recid);
