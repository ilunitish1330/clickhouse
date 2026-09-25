# PUC Analytics web app (`webapp/`)

Role-based dashboards in the browser, and a **Run** button that loads a billing export into
ClickHouse: `python3 webapp/server.py`, then open `http://<server-ip>:8020`. See `webapp/README.md`.

---

# Any raw data → ClickHouse, aggregated automatically (`auto_load.py`)

Give it a SQL query or an Excel/CSV file and nothing else. It lands the raw rows in ClickHouse,
works out from the data itself what the facts, dimensions and aggregates should be, and builds
them.

```bash
python3 auto_load.py billing --sql-file query.sql          # a query run on SQL Server (login from .env)
python3 auto_load.py billing --sql "SELECT ..." --db PUC   # inline query, another database
python3 auto_load.py billing --file "Statistic Report.zip" # .csv .tsv .txt .xlsx, or a .zip of them
python3 auto_load.py --inbox ~/inbox                       # every file dropped into a folder
```

Each dataset gets its own ClickHouse database (`billing` above), with these tables:
- `raw`: every row exactly as received
- `fact`: typed and cleaned
- `dim_*`: the dimensions it found
- `agg_monthly` or `agg_daily`, plus `agg_<entity>_monthly` for customers, meters and the like

**How it decides**, from the values rather than from anyone's description:
- Numbers named or behaving like amounts become measures. Prices and rates are averaged, not summed.
- Whole-number codes and short text with few values become dimension attributes.
- Code/description pairs and hierarchies (tariff → category → sector) are found by checking which
  column decides which.
- Many-valued IDs become entities (customer, meter) or document numbers (invoice).
- Dates are found in text, Excel serial numbers or .NET ticks. Rows without a date take the
  month of their batch, when a column predicts it.
- Empty and constant columns are dropped. Columns that Excel damaged are reported.
- Quantities in different units are detected from the price per unit (kWh vs m³), and the
  unit column is kept in every aggregate so they're never added together.
- Aggregate grains are chosen by how many rows each dimension still collapses.

**The first load decides; later loads reuse the model** (`models/<name>.json`), so reports don't
shift when next month's file arrives. `models/<name>.md` explains every decision and lists what
can't be known from data alone, like unlabelled codes, credits and duplicate rows, under **Check**.
To overrule a decision, edit the JSON and run `python3 auto_load.py <name> --rebuild`. To start
over, run `--remodel`.

**Re-loading:** a file replaces an earlier file with the same name, and a new name adds to the
dataset. A query run replaces everything, like a nightly full refresh.

**The inbox:** files in `~/inbox/<name>/` load into dataset `<name>`, and loose files are named
after the file (digits dropped). Loaded files move to `~/inbox/done/`, failures to
`~/inbox/failed/` with an `.error.txt`. Check the inbox every 15 minutes with cron:

```
*/15 * * * * cd /path/to/repo && .venv/bin/python auto_load.py --inbox ~/inbox >> auto_load.log 2>&1
```

For `.xlsx` files, run `pip install openpyxl` first. `python3 test_auto_load.py` tests all of the above.

---

# Dynamics AX 2012 → ClickHouse → Power BI

`ax_load.py` copies 17 AX tables from SQL Server into a star schema in the ClickHouse
database `ax`, then builds monthly aggregate tables for Power BI. Each run fully reloads
every table into a staging copy and swaps it in atomically (`EXCHANGE TABLES`). Power BI
never sees a half-loaded or duplicated table, and rows deleted in AX disappear. The biggest
source (INVENTTRANS) has ~340k rows, so a full run takes a few minutes.

### Run it

Run this on a machine that can reach both SQL Server (port 1433) and ClickHouse (HTTP, port 8123).
The SQL Server box itself works.

```bash
pip install pymssql httpx
cp .env.example .env          # fill in MSSQL_PASS and the ClickHouse URL/password
python3 ax_load.py --check    # connects, prints the row count of every source table
python3 ax_load.py --init     # creates database ax and all tables (safe to re-run)
python3 ax_load.py            # loads everything, then rebuilds the aggregates
python3 ax_load.py fact_sales dim_item   # reload only some tables (+ aggregates)
python3 ax_load.py --aggregates          # rebuild aggregates only
python3 test_ax_load.py       # end-to-end self-test, SQL Server faked. DROPS database ax
```

Schedule the plain `python3 ax_load.py` with cron or Windows Task Scheduler (e.g. nightly).

### What lands in ClickHouse

| ClickHouse table | AX source | Grain |
|---|---|---|
| `dim_customer` | CUSTTABLE + DIRPARTYTABLE (name) | customer |
| `dim_vendor` | VENDTABLE + DIRPARTYTABLE (name) | vendor |
| `dim_item` | INVENTTABLE + ECORESPRODUCT/TRANSLATION (name) | item |
| `dim_project` | PROJTABLE | project |
| `dim_purch_order` | PURCHTABLE (incl. PURCHNAME) | purchase order |
| `fact_sales` | SALESLINE + SALESTABLE | sales order line |
| `fact_cust_invoice` | CUSTINVOICEJOUR | customer invoice |
| `fact_cust_trans` | CUSTTRANS | AR transaction |
| `fact_vend_invoice` | VENDINVOICEJOUR | vendor invoice |
| `fact_vend_invoice_line` | VENDINVOICETRANS (+ vendor from the journal) | vendor invoice line |
| `fact_vend_trans` | VENDTRANS | AP transaction |
| `fact_invent_trans` | INVENTTRANS + INVENTTRANSORIGIN + INVENTDIM | inventory transaction |
| `fact_proj_posting` | PROJTRANSPOSTING | project posting |
| `fact_proj_item_trans` | PROJITEMTRANS | project item transaction |
| `dim_date`, `dim_status`, `dim_currency` | generated | |

Aggregates (rebuilt every run, from `aggregates.sql`): `agg_sales_monthly`,
`agg_cust_invoice_monthly`, `agg_ar_monthly`, `agg_purchases_monthly`, `agg_ap_monthly`,
`agg_purch_orders_monthly`, `agg_inventory_monthly`, `agg_inventory_onhand`,
`agg_project_monthly`, `agg_proj_items_monthly`. `SELECT * FROM ax.v_aggregate` lists each
one with its grain and size. `SELECT * FROM ax.v_last_load` shows when each table was last loaded.

Things to know when building reports:
- `data_area` is the AX company. Account and item ids are only unique within it.
- `*_mst` amounts are in the company's own currency. Other amounts are in the document currency.
- AX's empty date (1900-01-01) is stored as `1970-01-01`.
- Distinct counts in the aggregates (`orders`, `invoices`) are per aggregate row. Don't sum
  them across rows. Count distinct on the fact table instead.
- Customer-side status codes are verified against this AX instance. The purchase and inventory
  status names are the standard AX 2012 enum values, so check a few before relying on them.
  Unknown codes show up as `code_N`.

### Charts in the ClickHouse web UI

`python3 ax_dashboard.py` prints a `http://localhost:8123/dashboard#...` URL. Open it to
see 10 monthly charts: sales, customer invoices, receivables, purchases, payables,
purchase orders, inventory and projects. Change `company`, `from` and `to` in the fields at
the top of the page. Charts show one company at a time, because each company's amounts are
in its own currency.

### Power BI

1. Install the [ClickHouse ODBC driver](https://github.com/ClickHouse/clickhouse-odbc/releases)
   on the PC running Power BI Desktop (and on the on-premises data gateway for scheduled refresh).
2. Power BI Desktop → Get Data → **ClickHouse** → server = ClickHouse host, port 8123,
   database `ax`. Use **Import** mode.
3. Load the `dim_*` tables plus the `agg_*` tables you need (or `fact_*` for line-level detail).
4. Relationships: every table carries single-column keys (`customer_key`, `vendor_key`,
   `item_key`, `project_key`, `purch_key`, each `data_area|id`), because Power BI can't relate
   on two columns. Relate `agg_*.month` or any fact date to `dim_date.date_key`.

### Utility billing statistics (the "Statistic Report" export)

A separate database, `ub`, holds the utility billing report: electricity, water and wastewater
for Mahe, Praslin and La Digue. Until the SQL Server table behind the report is known, it
loads from the exported file:

```bash
python3 find_source.py                                   # search every database this login can see for the source
python3 ub_load.py "Statistic Report24092026.csv"        # or the .zip; re-loading a file replaces it
python3 ub_load.py --rebuild                             # rebuild every batch's aggregates and dashboards
```

A load stores the exported rows as text in `ub.raw_rows` (plus each batch's billing month in
`ub.raw_batches`), then builds what the dashboards and Power BI read: `ub.fact_lines` (the parsed
billing lines, per batch, from the view `ub.v_lines`) and the aggregates in `ub_aggregates.sql`
(dims, `agg_*`, `pbi_*`). An upload is live at once as a **Draft**. A reviewer can edit its rows in
the web app (**Data Review**): edits wait in `ub.raw_pending` and are logged cell by cell in
`ub.edit_log`, then **Finalize modified data** (`ub_load.py --apply`) writes them into the raw rows
and rebuilds that batch, and **Mark as reviewed** makes it **Final** (`ub.batch_events`). Editing
Final data starts the next Draft revision. `SELECT * FROM ub.v_batches` shows what's loaded and
`SELECT * FROM ub.v_batch_status` its review status.

- Quantity is kWh for electricity and m³ for water. Never sum it across `utility_code`.
  `consumption_qty` counts consumption lines only.
- A file that has been through Excel has lost its dates, so each batch's billing month is
  recovered from CURDATETICKS. Ask for exports straight from SQL Server with yyyy-mm-dd dates.

### Adding a table

Add a `CREATE TABLE` to `ch_schema.sql` and a `TABLES["name"] = ("AXTABLE", "SELECT ... AS col")`
entry to `ax_load.py`. The SELECT's column aliases must match the ClickHouse columns, and
`test_ax_load.py` checks that. Then run `--init` and `python3 ax_load.py name`.

---

# ClickHouse, learned by running it

20M rows of fake analytics events, then 8 lessons that each show one thing ClickHouse
does differently from Postgres/MySQL.

Runs on the `~/clickhouse` single binary (26.8.1, `amd64compat` build — this Xeon E5-2640
has SSE4.2 but no AVX2, so the normal builds crash with `Illegal instruction`; the
official Docker images have no compat variant, which is why there's no container here).

## Run

```bash
./server.sh &                                             # data + logs in ./ch-data
~/clickhouse client --port 9010 --queries-file setup.sql    # ~13s for 20M rows
~/clickhouse client --port 9010 --queries-file lessons.sql
```

Better: go interactive and run one block at a time, reading the `Elapsed / rows / bytes`
line the client prints after each query. That line is the lesson.

```bash
~/clickhouse client --port 9010
```

Port 9010 instead of the usual 9000 because another service on this box already holds
9000. HTTP is on the default 8123 — `curl 'localhost:8123/?query=SELECT%201'`, and the
built-in web UI is at <http://localhost:8123/play>.

Stop the server with `pkill -f 'clickhouse serve[r]'`. Data persists in `./ch-data`;
delete that directory to start over.

No server at all, for lessons 1-7:

```bash
~/clickhouse --queries-file setup.sql --path ./ch-data-local
```

`clickhouse local` has no `system.query_log`, so lesson 8 needs the server.

## The lessons

| # | Topic | The point |
|---|-------|-----------|
| 1 | Columnar storage | You pay per column touched, so `SELECT *` is a tax |
| 2 | Sparse primary index | The index *is* the `ORDER BY`; only left prefixes skip data |
| 3 | Partitions | Lifecycle + coarse pruning; high cardinality here kills you |
| 4 | Compression | `country` gets 205x, random `user_id` gets 1.0x |
| 5 | Aggregations | `uniq()` is HyperLogLog; approximate is the default and it's fine |
| 6 | Materialized views | An insert trigger writing to a target table, not a cached query |
| 7 | Updates/deletes | Append-only + `ReplacingMergeTree`; mutations are for backfills |
| 8 | `system.query_log` | `read_rows` is the number you optimize |

What lesson 2 actually prints — 411 of 2442 granules read, because `country` leads the
`ORDER BY`, while a `user_id` filter reads all 20M rows:

```
PrimaryKey
  Keys: country
  Condition: (country in ['US', 'US'])
  Parts: 4/4    Granules: 411/2442
```

## The demo app

The lessons in a browser: same table, four panels, each printing the SQL it ran and
the rows it scanned to fill itself. Filter by country and device, break events down by
any dimension (country / device / url), and see the columns themselves — type, distinct
values, MB on disk, compression ratio.

```bash
python3 app.py                # http://localhost:8010
python3 test_app.py           # smoke check, needs the server up
```

Open a panel's cost line and change the filter. A week with no country filter scans
millions of rows; the same week with `country = 'US'` scans a fraction, because
`country` leads the `ORDER BY` — lesson 2, live. Filter on `device` instead and the row
count barely moves: it's not in the key, so it's a scan filter, not a skip.

`python3 dashboard_url.py` prints a URL that loads six charts into ClickHouse's own
built-in dashboard, no app required.

## Files

- `server.sh` — starts the server (config lives in `ch-data/config.xml`)
- `setup.sql` — schema + 20M generated rows
- `lessons.sql` — the 8 lessons, each with its takeaway in comments
- `app.py` + `index.html` — the demo app (FastAPI over the HTTP interface, vanilla JS)
- `dashboard_url.py` — pre-loaded built-in dashboard URL

`ch-data/config.xml` is the binary's own built-in default config with exactly two edits:
`tcp_port` 9000 → 9010, and a `query_log` section (off by default, lesson 8 needs it).

## Not included

Replication/sharding (needs multiple nodes), Kafka + S3 table engines, projections,
dictionaries, `JOIN` tuning. Each is a follow-on lesson on this same table — ask.
