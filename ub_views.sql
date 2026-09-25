-- The raw rows (ub.raw_rows), read as billing lines. These are views: nothing
-- here is stored, every query parses the raw text it reads, when it runs. A
-- report that filters on utility_code, region_code or period reads only those
-- rows -- the first two are raw_rows' sort key, and the web app adds a
-- row_month / batch_id condition for the period (webapp/dashboards.py).
--
-- Statements are split on ';' at end of line: keep ';' off the end of comments.

-- One billing line per raw row, typed and labelled, with the measures the
-- reports use. Amounts go float -> text -> Decimal, not float -> Decimal: the
-- direct cast truncates (1070.37 is 1070.36999.. as a float), via text the
-- shortest float repr parses exactly; float first so 2E-14 still parses.
CREATE OR REPLACE VIEW ub.v_lines AS
SELECT
    r.batch_id AS batch_id, r.file_name AS file_name, r.line_no AS line_no,
    r.row_month AS row_month,
    if(r.row_month > toDate('1970-01-01'), r.row_month, b.batch_month) AS period,
    multiIf(match(r.CURDATE, '^\\d{4}-\\d{2}-\\d{2}|^\\d{1,2}/\\d{1,2}/\\d{4}'), toDate(parseDateTimeBestEffortOrZero(r.CURDATE)),
            toFloat64OrZero(r.CURDATETICKS) > 0,
            toDate(toDateTime(toInt64(toFloat64OrZero(r.CURDATETICKS) / 1e7 - 62135596800))),
            toDate('1970-01-01')) AS stat_date,
    if(match(r.INVOICEDATE, '^\\d{4}-\\d{2}-\\d{2}|^\\d{1,2}/\\d{1,2}/\\d{4}'),
       toDate(parseDateTimeBestEffortOrZero(r.INVOICEDATE)), toDate('1970-01-01')) AS invoice_date,
    toUInt8(r.row_month > toDate('1970-01-01')) AS date_exact,
    r.INVOICEID AS invoice_id, extract(r.INVOICEID, '^[A-Za-z]+-?') AS invoice_prefix,
    r.CUSTID AS customer_id, r.CONNECTIONID AS connection_id, r.MCSEXTERNALASSETID AS meter_id,
    r.utility_code AS utility_code, r.UTILITYTYPEDESCRIPTION AS utility_name,
    r.region_code AS region_code, r.REGIONNAME AS region_name,
    r.TARIFFGROUPCODE AS tariff_code, r.TARIFFGROUPDESC AS tariff_desc,
    r.STATCATEGORIES AS category_id, r.CATEGORYDESCRIPTION AS category_desc,
    r.SECTORGROUP AS sector_code, r.SECTORDESCRIPTION AS sector_desc,
    -- the 14 sectors grouped the way the KPI catalogue speaks of them
    multiIf(positionCaseInsensitive(r.SECTORDESCRIPTION, 'domestic') > 0, 'Domestic',
            positionCaseInsensitive(r.SECTORDESCRIPTION, 'commercial') > 0, 'Commercial',
            positionCaseInsensitive(r.SECTORDESCRIPTION, 'government') > 0, 'Government', 'Other') AS sector_type,
    toUInt8OrZero(r.ADDITIONALITEMSTYPE) AS charge_type,
    if(r.ADDITIONALITEMSDESCRIPTION = '', concat('code_', r.ADDITIONALITEMSTYPE), r.ADDITIONALITEMSDESCRIPTION) AS charge_desc,
    toUInt8(r.ADDITIONALITEMSDESCRIPTION = 'PV') AS is_pv,
    toUInt8OrZero(r.INVOICEVALUETYPE) AS value_type,
    if(r.INVOICEVALUEDESCRIPTION = '', concat('code_', r.INVOICEVALUETYPE), r.INVOICEVALUEDESCRIPTION) AS value_desc,
    toUInt8OrZero(r.INVOICEORIGIN) AS invoice_origin,
    if(r.INVOICEORIGINDESCRIPTION = '', concat('code_', r.INVOICEORIGIN), r.INVOICEORIGINDESCRIPTION) AS origin_desc,
    toUInt8OrZero(r.FREETEXTINVOICE) AS is_free_text,
    toUInt8OrZero(r.CONNECTIONMEMBERTYPE) AS member_type,
    toUInt8OrZero(r.PVINDICATION) AS pv_connection,
    r.METERWATERNODE AS water_node, r.METERWATERSOURCE AS water_source,
    toDecimal64OrZero(toString(round(toFloat64OrZero(r.AMOUNT), 4)), 4) AS amount,
    toDecimal64OrZero(toString(round(toFloat64OrZero(r.QUANTITY), 4)), 4) AS quantity,  -- kWh or m3: never summed across utilities
    concat(r.ADDITIONALITEMSTYPE, '|', toString(is_pv), '|', r.INVOICEVALUETYPE, '|', r.INVOICEORIGIN, '|',
           toString(is_free_text)) AS charge_key,
    if(r.utility_code = 1, 'kWh', 'm3') AS unit,
    if(charge_type = 1, quantity, toDecimal64(0, 4)) AS consumption_qty,   -- usage lines only
    if(charge_type = 1, amount, toDecimal64(0, 4))   AS consumption_amount,
    if(charge_type = 2, amount, toDecimal64(0, 4))   AS adjustment_amount,
    if(charge_type = 2, quantity, toDecimal64(0, 4)) AS adjustment_qty,
    if(amount < 0, amount, toDecimal64(0, 4))        AS credit_amount,
    toUInt8(r.MCSEXTERNALASSETID != '') AS has_meter
FROM ub.raw_rows AS r
ANY LEFT JOIN ub.raw_batches AS b ON b.batch_id = r.batch_id;

-- The billing fact, under the name the Power BI SQL (ub_aggregates.sql) reads.
CREATE OR REPLACE VIEW ub.fact_billing AS
SELECT batch_id, line_no, period AS period_month, stat_date, invoice_date, date_exact, invoice_id,
       invoice_prefix, customer_id, connection_id, meter_id, utility_code, utility_name,
       region_code, region_name, tariff_code, tariff_desc, category_id, category_desc,
       sector_code, sector_desc, charge_type, charge_desc, is_pv, value_type, value_desc,
       invoice_origin, origin_desc, is_free_text, member_type, pv_connection, water_node,
       water_source, amount, quantity, charge_key
FROM ub.v_lines;

-- The billing periods present, numbered so "previous period" means the
-- previous period IN THE DATA (June 2025 -> January 2026 has no months between).
CREATE OR REPLACE VIEW ub.v_periods AS
SELECT period, formatDateTime(period, '%Y-%m') AS year_month,
       formatDateTime(period, '%b %Y') AS period_label, toYear(period) AS year,
       toUInt32(row_number() OVER (ORDER BY period)) AS period_index
FROM (SELECT DISTINCT if(r.row_month > toDate('1970-01-01'), r.row_month, b.batch_month) AS period
      FROM ub.raw_rows AS r ANY LEFT JOIN ub.raw_batches AS b ON b.batch_id = r.batch_id)
WHERE period > toDate('1970-01-01');

CREATE OR REPLACE VIEW ub.v_batches AS
SELECT batch_id, period AS period_month, count() AS lines, sum(amount) AS amount,
       uniqExact(invoice_id) AS invoices, max(date_exact) AS has_exact_dates
FROM ub.v_lines GROUP BY batch_id, period;
