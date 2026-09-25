-- Utility billing statistics (the "Statistic Report" export) -> ClickHouse
-- database `ub`. Loaded by ub_load.py from the exported file, until the source
-- table in SQL Server is known (find_source.py looks for it).
--
-- Raw first: a load only stores the exported rows (raw_rows). Every report --
-- the web app's dashboards, and the Power BI tables when they are asked for --
-- is parsed and aggregated from those rows when it is requested (ub_views.sql).
--
-- Grain: one billing line = one charge on one invoice, for one connection,
-- under one tariff. The export carries no line id, and identical-looking lines
-- are real (tiered blocks, fixed parts), so line_no -- the row's position in
-- its file -- is the only key. Never de-duplicate on content.

CREATE DATABASE IF NOT EXISTS ub;

-- One file's rows on their way in: filled, copied to raw_rows, emptied, per load.
CREATE TABLE IF NOT EXISTS ub.raw_load
(
    line_no UInt32,
    ADDITIONALITEMSTYPE String, AMOUNT String, CONNECTIONID String, CURDATE String,
    CUSTID String, INVOICEDATE String, INVOICEID String, INVOICEORIGIN String,
    INVOICEVALUETYPE String, QUANTITY String, REGION String, STATCATEGORIES String,
    TARIFFGROUPCODE String, TARIFFGROUPDESC String, UTILITYTYPE String,
    MCSEXTERNALASSETID String, CURDATETICKS String, ADDITIONALITEMSDESCRIPTION String,
    CATEGORYDESCRIPTION String, CONNECTIONMEMBERTYPE String, FREETEXTINVOICE String,
    INVOICEORIGINDESCRIPTION String, INVOICEVALUEDESCRIPTION String,
    UTILITYTYPEDESCRIPTION String, SECTORDESCRIPTION String, SECTORGROUP String,
    REGIONNAME String, METERWATERNODE String, METERWATERSOURCE String,
    PVINDICATION String, BatchId String, LoadDateTime String
)
ENGINE = MergeTree
ORDER BY line_no;

-- The raw store: every exported row, every column as the text it was exported
-- as, kept for good. Nothing is aggregated at load time -- reports parse and
-- aggregate the rows they need when they are asked for (ub_views.sql).
-- A re-load of a batch drops its partition first, so nothing is counted twice.
--
-- The MATERIALIZED columns are lookup keys, not data: ClickHouse fills them from
-- the row's own text on insert, so a report for one island, utility or month
-- reads only those rows. SELECT * leaves them out; the text is untouched.
CREATE TABLE IF NOT EXISTS ub.raw_rows
(
    batch_id  LowCardinality(String),   -- BatchId, or the file name if the export has none
    file_name LowCardinality(String),
    line_no   UInt32,                   -- row position in its file: the only key a line has
    ADDITIONALITEMSTYPE String, AMOUNT String, CONNECTIONID String, CURDATE String,
    CUSTID String, INVOICEDATE String, INVOICEID String, INVOICEORIGIN String,
    INVOICEVALUETYPE String, QUANTITY String, REGION String, STATCATEGORIES String,
    TARIFFGROUPCODE String, TARIFFGROUPDESC String, UTILITYTYPE String,
    MCSEXTERNALASSETID String, CURDATETICKS String, ADDITIONALITEMSDESCRIPTION String,
    CATEGORYDESCRIPTION String, CONNECTIONMEMBERTYPE String, FREETEXTINVOICE String,
    INVOICEORIGINDESCRIPTION String, INVOICEVALUEDESCRIPTION String,
    UTILITYTYPEDESCRIPTION String, SECTORDESCRIPTION String, SECTORGROUP String,
    REGIONNAME String, METERWATERNODE String, METERWATERSOURCE String,
    PVINDICATION String, BatchId String, LoadDateTime String,
    utility_code UInt8 MATERIALIZED toUInt8OrZero(UTILITYTYPE),
    region_code  UInt8 MATERIALIZED toUInt8OrZero(REGION),
    -- the month the row itself names; 1970-01-01 = none, the batch month applies (raw_batches)
    row_month    Date  MATERIALIZED multiIf(
        match(INVOICEDATE, '^\\d{4}-\\d{2}-\\d{2}|^\\d{1,2}/\\d{1,2}/\\d{4}'), toStartOfMonth(toDate(parseDateTimeBestEffortOrZero(INVOICEDATE))),
        match(CURDATE, '^\\d{4}-\\d{2}-\\d{2}|^\\d{1,2}/\\d{1,2}/\\d{4}'), toStartOfMonth(toDate(parseDateTimeBestEffortOrZero(CURDATE))),
        toDate('1970-01-01'))
)
ENGINE = MergeTree
PARTITION BY batch_id
ORDER BY (utility_code, region_code, row_month, line_no);

-- One row per loaded batch. batch_month is the month most of the batch's
-- statistics dates fall in: the billing period of rows that name no date of
-- their own (this export gives only CURDATETICKS, so that is every row).
CREATE TABLE IF NOT EXISTS ub.raw_batches
(
    batch_id    String,
    batch_month Date,
    file_name   String,
    rows        UInt64,
    loaded_at   DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY batch_id;

CREATE TABLE IF NOT EXISTS ub.load_log
(
    loaded_at   DateTime DEFAULT now(),
    file_name   String,
    batch_id    String,
    rows        UInt64,
    amount      Decimal(18, 4)
)
ENGINE = MergeTree
ORDER BY loaded_at;

-- ============ review workflow ============
-- An upload is live on the dashboards at once, as a Draft. A reviewer can edit
-- its rows in the web app: edits wait in raw_pending until "Finalize modified
-- data" writes them into raw_rows and rebuilds; "Reviewed" makes it Final.
-- Editing a Final batch later starts a new Draft revision.

-- Every status change of a batch; its status is the latest row.
CREATE TABLE IF NOT EXISTS ub.batch_events
(
    batch_id   String,
    status     LowCardinality(String),   -- draft | final
    revision   UInt32,                   -- 1 = as uploaded, +1 per finalized set of edits
    changed_by String,
    changed_at DateTime64(3) DEFAULT now64(3),
    note       String
)
ENGINE = MergeTree
ORDER BY (batch_id, changed_at);

-- Edits not yet finalized: the whole new row, latest image per line.
-- action: update (a changed row), insert (a new row), delete (a removed row).
CREATE TABLE IF NOT EXISTS ub.raw_pending
(
    batch_id   String,
    line_no    UInt32,
    action     LowCardinality(String),
    ADDITIONALITEMSTYPE String, AMOUNT String, CONNECTIONID String, CURDATE String,
    CUSTID String, INVOICEDATE String, INVOICEID String, INVOICEORIGIN String,
    INVOICEVALUETYPE String, QUANTITY String, REGION String, STATCATEGORIES String,
    TARIFFGROUPCODE String, TARIFFGROUPDESC String, UTILITYTYPE String,
    MCSEXTERNALASSETID String, CURDATETICKS String, ADDITIONALITEMSDESCRIPTION String,
    CATEGORYDESCRIPTION String, CONNECTIONMEMBERTYPE String, FREETEXTINVOICE String,
    INVOICEORIGINDESCRIPTION String, INVOICEVALUEDESCRIPTION String,
    UTILITYTYPEDESCRIPTION String, SECTORDESCRIPTION String, SECTORGROUP String,
    REGIONNAME String, METERWATERNODE String, METERWATERSOURCE String,
    PVINDICATION String, BatchId String, LoadDateTime String,
    edited_by  String,
    edited_at  DateTime64(3) DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(edited_at)
ORDER BY (batch_id, line_no);

-- Who changed what: one row per changed cell, and one per finalize / review / discard.
CREATE TABLE IF NOT EXISTS ub.edit_log
(
    ts        DateTime64(3) DEFAULT now64(3),
    batch_id  String,
    line_no   UInt32,
    action    LowCardinality(String),    -- update | insert | delete | undo | discard | finalize | reviewed
    column    String,
    old_value String,
    new_value String,
    user      String,
    revision  UInt32
)
ENGINE = MergeTree
ORDER BY (batch_id, ts);

-- Each build of the dashboard tables; the web app's cached results are keyed on it.
CREATE TABLE IF NOT EXISTS ub.build_log
(
    ts       DateTime64(3) DEFAULT now64(3),
    batch_id String,
    rows     UInt64,
    seconds  Float32,
    reason   String
)
ENGINE = MergeTree
ORDER BY ts;

-- Formula builder recipes a reviewer saved to reuse (a rule, an action, formulas), as JSON.
CREATE TABLE IF NOT EXISTS ub.saved_formulas
(
    name       String,
    spec       String,
    saved_by   String,
    saved_at   DateTime64(3) DEFAULT now64(3),
    deleted    UInt8 DEFAULT 0
)
ENGINE = ReplacingMergeTree(saved_at)
ORDER BY name;

-- Columns a reviewer added (ub_custom.py). Values live in each row's `extra` map under
-- the column's key; the key is never reused. ub.v_custom shows them as real columns.
CREATE TABLE IF NOT EXISTS ub.custom_columns
(
    key        String,                   -- c1, c2, ...
    name       String,
    kind       LowCardinality(String),   -- number | text | date
    created_by String,
    changed_at DateTime64(3) DEFAULT now64(3),
    deleted    UInt8 DEFAULT 0
)
ENGINE = ReplacingMergeTree(changed_at)
ORDER BY key;

ALTER TABLE ub.raw_load ADD COLUMN IF NOT EXISTS extra Map(String, String);
ALTER TABLE ub.raw_rows ADD COLUMN IF NOT EXISTS extra Map(String, String);
ALTER TABLE ub.raw_pending ADD COLUMN IF NOT EXISTS extra Map(String, String);
