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

-- ============ Power BI layer ============
-- What the Power BI report (powerbi/) imports, over plain HTTP. Labels are
-- joined in here so the report needs no Power Query work. Row-level billing
-- is imported (581k rows is small for Power BI) because distinct counts of
-- customers, connections, meters and invoices are only exact at that level.

-- The billing periods present, numbered so "previous period" means the
-- previous period IN THE DATA (June 2025 -> January 2026 has no months between).
CREATE OR REPLACE TABLE ub.pbi_period ENGINE = MergeTree ORDER BY period AS
SELECT period, formatDateTime(period, '%Y-%m') AS year_month,
       formatDateTime(period, '%b %Y') AS period_label, toYear(period) AS year,
       toUInt32(row_number() OVER (ORDER BY period)) AS period_index
FROM (SELECT DISTINCT period_month AS period FROM ub.fact_billing WHERE period_month > '1970-01-01');

CREATE OR REPLACE VIEW ub.pbi_billing AS
SELECT period_month AS period, utility_code, region_code, tariff_code, charge_key,
       customer_id, connection_id, meter_id, invoice_id, invoice_prefix,
       water_source, water_node, pv_connection,
       if(utility_code = 1, 'kWh', 'm3')      AS unit,
       amount, quantity,
       if(charge_type = 1, quantity, 0)      AS consumption_qty,
       if(charge_type = 1, amount, 0)        AS consumption_amount,
       if(charge_type = 2, amount, 0)        AS adjustment_amount,
       if(charge_type = 2, quantity, 0)      AS adjustment_qty,
       if(amount < 0, amount, 0)             AS credit_amount,
       toUInt8(meter_id != '')               AS has_meter
FROM ub.fact_billing;

-- sector_type groups the 14 sectors the way the KPI catalogue speaks of them.
CREATE OR REPLACE VIEW ub.pbi_tariff AS
SELECT tariff_code, tariff_desc, category_desc, sector_code, sector_desc,
       multiIf(positionCaseInsensitive(sector_desc, 'domestic') > 0, 'Domestic',
               positionCaseInsensitive(sector_desc, 'commercial') > 0, 'Commercial',
               positionCaseInsensitive(sector_desc, 'government') > 0, 'Government', 'Other') AS sector_type
FROM ub.dim_tariff;

CREATE OR REPLACE VIEW ub.pbi_charge_type AS
SELECT charge_key, charge_desc, value_desc, origin_desc, is_pv, is_free_text,
       toUInt8(charge_type = 1) AS is_consumption, toUInt8(charge_type = 2) AS is_adjustment
FROM ub.dim_charge_type;

-- One row per customer, utility and period: bill size, Pareto position,
-- whether the customer also takes the other utility.
CREATE OR REPLACE TABLE ub.pbi_customer_period
ENGINE = MergeTree ORDER BY (period, utility_code, customer_id) AS
SELECT period, utility_code, customer_id, region_code,
       toDecimal64(amt, 4) AS amount, toDecimal64(cons, 4) AS consumption_qty, inv AS invoices,
       multiIf(amt < 0, '1 Credit', amt < 100, '2 Under 100', amt < 500, '3 100-500',
               amt < 1000, '4 500-1,000', amt < 5000, '5 1,000-5,000',
               amt < 20000, '6 5,000-20,000', '7 20,000 and over') AS bill_band,
       multiIf(rn <= 0.01 * n, '1 Top 1%', rn <= 0.10 * n, '2 Top 1-10%',
               rn <= 0.50 * n, '3 Top 10-50%', '4 Bottom 50%') AS pareto_band,
       if(has_elec AND has_water, 'Electricity and water', 'One utility') AS utility_mix
FROM (
    SELECT *, row_number() OVER (PARTITION BY period, utility_code ORDER BY amt DESC) AS rn,
              count() OVER (PARTITION BY period, utility_code) AS n,
              max(utility_code = 1) OVER (PARTITION BY period, customer_id) AS has_elec,
              max(utility_code = 3) OVER (PARTITION BY period, customer_id) AS has_water
    FROM (SELECT period_month AS period, utility_code, customer_id, any(region_code) AS region_code,
                 sum(amount) AS amt,
                 sumIf(quantity, charge_type = 1) AS cons, uniqExact(invoice_id) AS inv
          FROM ub.fact_billing GROUP BY period, utility_code, customer_id));

-- One row per connection, utility and period: consumption band, PV, and how
-- consumption moved since the previous period in the data.
CREATE OR REPLACE TABLE ub.pbi_connection_period
ENGINE = MergeTree ORDER BY (period, utility_code, connection_id) AS
SELECT period, utility_code, connection_id, region_code, pv_connection,
       if(pv_connection = 1, 'PV', 'No PV') AS pv_label,
       toDecimal64(amt, 4) AS amount, toDecimal64(cons, 4) AS consumption_qty,
       if(utility_code = 1,
          multiIf(cons <= 0, '0 None', cons <= 100, '1 1-100 kWh', cons <= 200, '2 101-200 kWh',
                  cons <= 300, '3 201-300 kWh', cons <= 500, '4 301-500 kWh', cons <= 1000, '5 501-1,000 kWh',
                  cons <= 5000, '6 1,001-5,000 kWh', '7 Over 5,000 kWh'),
          multiIf(cons <= 0, '0 None', cons <= 5, '1 1-5 m3', cons <= 10, '2 6-10 m3',
                  cons <= 20, '3 11-20 m3', cons <= 50, '4 21-50 m3', cons <= 100, '5 51-100 m3',
                  cons <= 500, '6 101-500 m3', '7 Over 500 m3')) AS consumption_band,
       multiIf(prev_idx IS NULL OR prev_idx != period_index - 1, 'First period',
               prev_cons > 0 AND cons <= 0, 'Dropped to zero',
               prev_cons > 0 AND cons >= 3 * prev_cons, 'Jumped 3x or more',
               prev_cons > 0 AND cons <= prev_cons / 3, 'Fell to a third or less',
               prev_cons <= 0 AND cons > 0, 'Resumed from zero',
               cons <= 0, 'Zero in both periods', 'Normal change') AS change_flag
FROM (
    SELECT b.*, p.period_index,
           lagInFrame(toNullable(p.period_index)) OVER w AS prev_idx,
           lagInFrame(b.cons) OVER w AS prev_cons
    FROM (SELECT period_month AS period, utility_code, connection_id, any(region_code) AS region_code,
                 max(pv_connection) AS pv_connection, sum(amount) AS amt,
                 sumIf(quantity, charge_type = 1) AS cons
          FROM ub.fact_billing GROUP BY period, utility_code, connection_id) AS b
    JOIN ub.pbi_period AS p ON p.period = b.period
    WINDOW w AS (PARTITION BY b.utility_code, b.connection_id ORDER BY p.period_index
                 ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING));

-- ============ Web app layer (webapp/) ============
-- The billing lines with every label joined in, so each dashboard query is one
-- scan with no joins. Rebuilt with the rest after every load.
CREATE OR REPLACE TABLE ub.app_billing
ENGINE = MergeTree ORDER BY (period, utility_code, region_code) AS
SELECT b.* EXCEPT (utility_code, region_code, tariff_code, charge_key),
       b.utility_code AS utility_code, b.region_code AS region_code, b.tariff_code AS tariff_code,
       b.charge_key AS charge_key, u.utility_name, r.region_name, t.tariff_desc, t.category_desc, t.sector_desc, t.sector_type,
       c.charge_desc, c.value_desc, c.origin_desc, c.is_pv, c.is_free_text
FROM ub.pbi_billing AS b
LEFT JOIN ub.dim_utility AS u ON u.utility_code = b.utility_code
LEFT JOIN ub.dim_region AS r ON r.region_code = b.region_code
LEFT JOIN ub.pbi_tariff AS t ON t.tariff_code = b.tariff_code
LEFT JOIN ub.pbi_charge_type AS c ON c.charge_key = b.charge_key;

CREATE OR REPLACE TABLE ub.app_customer_period
ENGINE = MergeTree ORDER BY (period, utility_code, customer_id) AS
SELECT c.* EXCEPT (utility_code, region_code), c.utility_code AS utility_code,
       c.region_code AS region_code, u.utility_name, r.region_name
FROM ub.pbi_customer_period AS c
LEFT JOIN ub.dim_utility AS u ON u.utility_code = c.utility_code
LEFT JOIN ub.dim_region AS r ON r.region_code = c.region_code;

CREATE OR REPLACE TABLE ub.app_connection_period
ENGINE = MergeTree ORDER BY (period, utility_code, connection_id) AS
SELECT c.* EXCEPT (utility_code, region_code), c.utility_code AS utility_code,
       c.region_code AS region_code, u.utility_name, r.region_name
FROM ub.pbi_connection_period AS c
LEFT JOIN ub.dim_utility AS u ON u.utility_code = c.utility_code
LEFT JOIN ub.dim_region AS r ON r.region_code = c.region_code;
