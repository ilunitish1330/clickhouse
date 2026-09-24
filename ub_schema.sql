-- Utility billing statistics (the "Statistic Report" export) -> ClickHouse
-- database `ub`. Loaded by ub_load.py from the exported file, until the source
-- table in SQL Server is known (find_source.py looks for it).
--
-- Grain of the fact: one billing line = one charge on one invoice, for one
-- connection, under one tariff. The export carries no line id, and identical-
-- looking lines are real (tiered blocks, fixed parts), so line_no -- the row's
-- position in its file -- is the only key. Never de-duplicate on content.
--
-- ponytail: descriptions ride on the fact as LowCardinality columns and the
-- dimensions are rebuilt from the fact after every load (ub_aggregates.sql).
-- LowCardinality stores each distinct label once, so this costs ~nothing, and
-- there is no second lookup table that could drift from the data.
--
-- The fact is partitioned by batch: reloading a file first drops the batches it
-- contains, so a re-run replaces instead of doubling.

CREATE DATABASE IF NOT EXISTS ub;

-- Every column as text, exactly as exported. Filled and emptied per load.
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

CREATE TABLE IF NOT EXISTS ub.fact_billing
(
    batch_id        LowCardinality(String),   -- one statistics run
    line_no         UInt32,                   -- row position in the loaded file
    period_month    Date,                     -- billing month (see ub_load.py)
    stat_date       Date,                     -- CURDATE, or from CURDATETICKS
    invoice_date    Date,                     -- 1970-01-01 = not in the export
    date_exact      UInt8,                    -- 1 = real dates, 0 = derived from ticks
    invoice_id      String,
    invoice_prefix  LowCardinality(String),   -- E, ER, EE, W, WR, S-, A-, PUC-...
    customer_id     String,                   -- -> dim_customer
    connection_id   String,                   -- -> dim_connection
    meter_id        String,                   -- MCSEXTERNALASSETID
    utility_code    UInt8,                    -- -> dim_utility
    utility_name    LowCardinality(String),
    region_code     UInt8,                    -- -> dim_region
    region_name     LowCardinality(String),
    tariff_code     LowCardinality(String),   -- -> dim_tariff
    tariff_desc     LowCardinality(String),
    category_id     String,                   -- STATCATEGORIES (an AX RecId)
    category_desc   LowCardinality(String),
    sector_code     LowCardinality(String),
    sector_desc     LowCardinality(String),
    charge_type     UInt8,                    -- ADDITIONALITEMSTYPE, 1 = consumption
    charge_desc     LowCardinality(String),
    is_pv           UInt8,                    -- a PV (solar) consumption line
    value_type      UInt8,                    -- INVOICEVALUETYPE
    value_desc      LowCardinality(String),
    invoice_origin  UInt8,
    origin_desc     LowCardinality(String),
    is_free_text    UInt8,
    member_type     UInt8,                    -- CONNECTIONMEMBERTYPE, 0 = not given
    pv_connection   UInt8,                    -- PVINDICATION on the connection
    water_node      LowCardinality(String),
    water_source    LowCardinality(String),
    amount          Decimal(18, 4),
    quantity        Decimal(18, 4),           -- kWh or m3: NEVER sum across utilities
    charge_key      String DEFAULT concat(toString(charge_type), '|', toString(is_pv), '|',
                                          toString(value_type), '|', toString(invoice_origin), '|',
                                          toString(is_free_text))
)
ENGINE = MergeTree
PARTITION BY batch_id
ORDER BY (period_month, utility_code, tariff_code, connection_id, line_no);

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
