-- Dimensions and aggregates for ub.fact_billing, rebuilt after every load by
-- ub_load.py (or `python3 ub_load.py --aggregates`). Same conventions as
-- aggregates.sql: CREATE OR REPLACE, plain numbers Power BI can read, and
-- distinct counts are per aggregate row -- do not sum them across rows.
--
-- QUANTITY is kWh for electricity and m3 for water / wastewater: every
-- quantity column below is split by utility, never summed across it.
-- consumption_qty counts consumption lines only (charge_type 1). On the other
-- charge types quantity is a unit count of a fixed charge, not usage.
--
-- Statements are split on ';' at end of line: keep ';' off the end of comments.
-- prefer_column_name_to_alias: lets sum(x) AS x sit next to sumIf(x, ...).

-- ============ dimensions ============

CREATE OR REPLACE TABLE ub.dim_utility ENGINE = MergeTree ORDER BY utility_code AS
SELECT utility_code, any(utility_name) AS utility_name,
       multiIf(utility_code = 1, 'kWh', 'm3') AS quantity_unit
FROM ub.fact_billing GROUP BY utility_code;

CREATE OR REPLACE TABLE ub.dim_region ENGINE = MergeTree ORDER BY region_code AS
SELECT region_code, any(region_name) AS region_name
FROM ub.fact_billing GROUP BY region_code;

-- tariff -> category -> sector is a strict hierarchy in the data (each tariff
-- sits in one category, each category in one sector). Region and utility are
-- NOT part of it: "Mahe Domestic" occurs on all three islands, and the sewer
-- tariffs appear under both Water and WasteWater.
CREATE OR REPLACE TABLE ub.dim_tariff ENGINE = MergeTree ORDER BY tariff_code AS
SELECT tariff_code,
       argMax(tariff_desc, period_month)   AS tariff_desc,
       argMax(category_id, period_month)   AS category_id,
       argMax(category_desc, period_month) AS category_desc,
       argMax(sector_code, period_month)   AS sector_code,
       argMax(sector_desc, period_month)   AS sector_desc
FROM ub.fact_billing GROUP BY tariff_code;

-- A junk dimension: the line flags that are too small for a table each.
-- Codes the export leaves unlabelled show as code_N.
CREATE OR REPLACE TABLE ub.dim_charge_type ENGINE = MergeTree ORDER BY charge_key AS
SELECT charge_key, charge_type,
       if(any(charge_desc) = '', concat('code_', toString(charge_type)), any(charge_desc)) AS charge_desc,
       is_pv, value_type,
       if(any(value_desc) = '', concat('code_', toString(value_type)), any(value_desc)) AS value_desc,
       invoice_origin,
       if(any(origin_desc) = '', concat('code_', toString(invoice_origin)), any(origin_desc)) AS origin_desc,
       is_free_text
FROM ub.fact_billing
GROUP BY charge_key, charge_type, is_pv, value_type, invoice_origin, is_free_text;

-- Latest known state of each connection (a connection can change customer,
-- and meters get replaced).
CREATE OR REPLACE TABLE ub.dim_connection ENGINE = MergeTree ORDER BY connection_id AS
SELECT connection_id,
       argMax(customer_id, (period_month, line_no)) AS customer_id,
       argMax(region_code, (period_month, line_no)) AS region_code,
       argMax(region_name, (period_month, line_no)) AS region_name,
       argMax(meter_id, (period_month, line_no))    AS current_meter_id,
       max(member_type)                             AS member_type,
       max(pv_connection)                           AS pv_connection,
       argMaxIf(water_node, (period_month, line_no), water_node != '')     AS water_node,
       argMaxIf(water_source, (period_month, line_no), water_source != '') AS water_source,
       min(period_month) AS first_month,
       max(period_month) AS last_month
FROM ub.fact_billing GROUP BY connection_id;

CREATE OR REPLACE TABLE ub.dim_customer ENGINE = MergeTree ORDER BY customer_id AS
SELECT customer_id,
       uniqExact(connection_id) AS connections,
       min(period_month)        AS first_month,
       max(period_month)        AS last_month
FROM ub.fact_billing GROUP BY customer_id;

CREATE OR REPLACE TABLE ub.dim_date ENGINE = MergeTree ORDER BY date_key AS
SELECT d AS date_key, toYear(d) AS year, toQuarter(d) AS quarter, toMonth(d) AS month,
       formatDateTime(d, '%b') AS month_name, formatDateTime(d, '%Y-%m') AS year_month,
       toStartOfMonth(d) AS month_start
FROM (SELECT toDate('2015-01-01') + number AS d FROM numbers(5844));

-- ============ aggregates ============

-- The main Power BI table: revenue and consumption by month, island, utility,
-- tariff (and so category / sector) and charge type.
CREATE OR REPLACE TABLE ub.agg_billing_monthly
ENGINE = MergeTree ORDER BY (period_month, region_code, utility_code, tariff_code, charge_key) AS
SELECT period_month, region_code, utility_code, tariff_code, sector_code, charge_key,
       toDecimal64(sum(amount), 4)                        AS amount,
       toDecimal64(sum(quantity), 4)                      AS quantity,
       toDecimal64(sumIf(quantity, charge_type = 1), 4)   AS consumption_qty,
       count()                                            AS lines,
       uniqExact(invoice_id)                              AS invoices,     -- NOT additive
       uniqExact(connection_id)                           AS connections,  -- NOT additive
       uniqExact(customer_id)                             AS customers     -- NOT additive
FROM ub.fact_billing
GROUP BY period_month, region_code, utility_code, tariff_code, sector_code, charge_key
SETTINGS prefer_column_name_to_alias = 1;

-- Per customer per utility: top customers, bill size, customer counts.
CREATE OR REPLACE TABLE ub.agg_customer_monthly
ENGINE = MergeTree ORDER BY (period_month, customer_id, utility_code) AS
SELECT period_month, customer_id, utility_code,
       toDecimal64(sum(amount), 4)                        AS amount,
       toDecimal64(sumIf(quantity, charge_type = 1), 4)   AS consumption_qty,
       count()                                            AS lines,
       uniqExact(invoice_id)                              AS invoices
FROM ub.fact_billing
GROUP BY period_month, customer_id, utility_code;

-- Per connection (meter point): average usage, PV customers, high users.
CREATE OR REPLACE TABLE ub.agg_connection_monthly
ENGINE = MergeTree ORDER BY (period_month, connection_id, utility_code) AS
SELECT period_month, connection_id, utility_code,
       any(region_code)                                   AS region_code,
       max(pv_connection)                                 AS pv_connection,
       toDecimal64(sum(amount), 4)                        AS amount,
       toDecimal64(sumIf(quantity, charge_type = 1), 4)   AS consumption_qty,
       toDecimal64(sumIf(quantity, charge_type = 1 AND is_pv = 1), 4) AS pv_qty,
       toDecimal64(sumIf(amount, is_pv = 1), 4)           AS pv_amount
FROM ub.fact_billing
GROUP BY period_month, connection_id, utility_code
SETTINGS prefer_column_name_to_alias = 1;

-- Water billed per network node and source (water utility only).
CREATE OR REPLACE TABLE ub.agg_water_network_monthly
ENGINE = MergeTree ORDER BY (period_month, water_source, water_node) AS
SELECT period_month, water_source, water_node,
       toDecimal64(sumIf(quantity, charge_type = 1), 4)   AS consumption_m3,
       toDecimal64(sum(amount), 4)                        AS amount,
       uniqExact(connection_id)                           AS connections  -- NOT additive
FROM ub.fact_billing
WHERE utility_name = 'Water'
GROUP BY period_month, water_source, water_node;

CREATE OR REPLACE VIEW ub.v_batches AS
SELECT batch_id, period_month, count() AS lines, sum(amount) AS amount,
       uniqExact(invoice_id) AS invoices, max(date_exact) AS has_exact_dates
FROM ub.fact_billing GROUP BY batch_id, period_month;
