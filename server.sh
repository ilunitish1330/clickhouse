#!/bin/sh
# Start the ClickHouse server for this project. Config, data and logs all live in ./ch-data.
# ponytail: cwd IS the data path for the single binary — the only config that differs from
# its built-in default is tcp_port (9000 is taken here) and query_log (off by default, lesson 8 needs it).
cd "$(dirname "$0")/ch-data" || exit 1
exec ~/clickhouse server -C config.xml
