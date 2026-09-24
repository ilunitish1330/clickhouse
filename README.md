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
