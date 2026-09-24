# schema + sample row -> one CREATE TABLE per table with its sample row appended.
#   awk -f mkddl.awk samples.txt schema.txt > schema_full.txt
#   awk -v only='CUSTTABLE|SALESTABLE' -f mkddl.awk samples.txt schema.txt
# samples.txt is optional: `awk -f mkddl.awk schema.txt` gives DDL alone.
#
# ponytail: no defaults, no identity, no FKs/indexes — INFORMATION_SCHEMA.COLUMNS
# as dumped doesn't carry them. This is for reading and for mapping to ClickHouse,
# not for recreating the AX database.
BEGIN { FS = "|" }

# First file (samples.txt): stash the sample row of each table verbatim.
NR == FNR && ARGC > 2 {
    if ($0 ~ /^=== /) { split($0, h, " "); cur = h[2] }
    else if (cur != "" && $0 != "") sample[cur] = sample[cur] "-- " $0 "\n"
    next
}

/^=== COLUMNS/ { s = "C"; next }
/^=== KEYS/    { s = "K"; next }
/^=== ROW/     { s = "R"; next }

s == "C" && NF >= 9 {
    t = $1 "." $2
    if (only != "" && $2 !~ only) next
    type = $5
    if ($6 != "")                             type = type "(" ($6 == "-1" ? "max" : $6) ")"
    else if ($5 ~ /^(decimal|numeric)$/)      type = type "(" $7 "," $8 ")"
    if (!(t in col)) { n_t++; order[n_t] = t }
    col[t] = col[t] sprintf("    [%s] %s %s,\n", $4, type, ($9 == "YES" ? "NULL" : "NOT NULL"))
}

s == "K" && NF >= 5 && $5 == "PRIMARY KEY" {
    t = $1 "." $2
    pk[t] = pk[t] (pk[t] ? ", " : "") "[" $4 "]"
}

s == "R" && NF == 3 { rows[$1 "." $2] = $3 }

END {
    for (i = 1; i <= n_t; i++) {
        t = order[i]
        split(t, p, ".")
        printf "-- %s: %s rows\n", t, (t in rows ? rows[t] : "view or no stats")
        body = col[t]
        if (!(t in pk)) sub(/,\n$/, "\n", body)  # last column keeps no comma
        printf "CREATE TABLE [%s].[%s] (\n%s", p[1], p[2], body
        if (t in pk) printf "    PRIMARY KEY (%s)\n", pk[t]
        print ");"
        if (t in sample) printf "-- sample row:\n%s", sample[t]
        print ""
    }
}
