# PUC Analytics web app

Role-based dashboards over the ClickHouse `ub` billing data, a **Run** button that loads a billing
export into ClickHouse and builds its aggregates and dashboards, and a review workflow in which a
reviewer checks and edits the data before it is marked Final.

```bash
pip install -r webapp/requirements.txt
python3 webapp/server.py              # then open http://<server-ip>:8020
```

It reads the ClickHouse address from the repository's `.env` (`CH_URL`, `CH_USER`, `CH_PASSWORD`),
the same as the loaders. To keep it running after reboots, see `puc-analytics.service`.

## First sign-in

The first start creates one user per role, all with the password **`Puc@2026`**, and each user must
choose a new password at first sign-in. Set `APP_DEFAULT_PASSWORD` before the first start to use a
different one.

| Username | Role | Sees |
|---|---|---|
| admin | System Administrator | everything, Data Load, Users |
| ceo | Executive Management | all 10 dashboards, account numbers masked |
| finance | Finance & Revenue Manager | revenue, tariffs, customers, billing controls |
| elec.manager | Electricity Division Manager | electricity and solar PV only |
| water.manager | Water & Sewerage Division Manager | water, sewerage, network only |
| mahe.manager / praslin.manager / ladigue.manager | Regional Manager | every dashboard, their island only |
| billing | Billing Officer | billing controls, customers, consumption |
| service | Customer Service Officer | customers and consumption |
| auditor | Internal Auditor | every dashboard plus the load history, read-only |
| operator | Data Operator | Data Load only |
| reviewer | Data Reviewer | every dashboard plus Data Review: edit rows, finalize, mark reviewed |

Every user also has an **island**. Any role can be limited to Mahe, Praslin or La Digue, which
filters every number the user sees. The limits are applied inside the database queries on the
server, not in the browser. `test_webapp.py` checks them.

Roles are defined in `roles.py`. To add a role, add an entry there, then assign users to it on the
**Users & roles** page.

## Loading data

Go to **Data Load**, drop the Statistic Report export (`.xlsx`, `.csv` or a `.zip`) and press **Run**.
The app runs `ub_load.py` and shows its log live: the rows are stored in ClickHouse and checked
against the file, then the aggregates and dashboards are built. The upload is live at once, as a
**Draft**.

## Reviewing data

**Data Review** (roles `reviewer` and `admin`) lists every uploaded period with its status.

1. Check the dashboards (**Open dashboards**) and the rows. Rows can be searched and filtered, and
   edited in place: change any cell, **Add row**, or delete a row. A changed code fills in its
   labels (island, utility, tariff group). Values are checked: numbers must be numbers, Utility
   and Island 1 to 3, dates yyyy-mm-dd.
2. Edits wait; the dashboards do not change yet. **Finalize modified data** writes them and
   rebuilds that period's aggregates and dashboards (a new revision, still a Draft).
3. **Mark as reviewed** makes the period **Final**. Editing Final data later, of any period,
   starts the next Draft revision.

### Formula builder

The **Formula builder** tab changes many rows at once, in four steps:

1. **Which rows**: a query builder. Conditions (field, comparison, value) are joined by AND or
   OR, can be grouped, and a condition can also be a formula (`[Amount] > [Quantity] * 5`). The
   number of matching rows updates as you build.
2. **What to do**: change values, copy the rows as new rows, or delete them.
3. **Set values**: `field = formula`, written like in Excel, with buttons to insert fields,
   functions and operators, and examples:

   | Formula | Does |
   |---|---|
   | `ROUND([Amount] * 1.05, 2)` | 5% more, to the cent |
   | `IF([Island] = 2, [Amount] * 0.9, [Amount])` | 10% less on Praslin only |
   | `MAX([Amount], 0)` | never below zero |
   | `"EMD1"` | a fixed value (a changed code brings its labels) |
   | `TRIM(UPPER([Sector]))` | tidy text |

   Functions: ROUND, ABS, MIN, MAX, IF, AND, OR, NOT, CONCAT (or `&`), UPPER, LOWER, TRIM,
   LEFT, RIGHT, REPLACE, LEN, CONTAINS, ISBLANK, NUMBER, TEXT. ROUND rounds halves away from
   zero, like Excel (2.5 -> 3, 2.675 -> 2.68). A mistake is pointed out in the formula as you
   type. Not supported: `^`, `%`, and thousands separators (`1,000`): write `1000`.
4. **Preview**: how many rows change, the amount before and after, the period's new total, and
   the first 20 rows before and after. Values a field cannot hold (Island 7, text in Amount,
   a division by zero, an amount of 100 trillion or more, a date not written yyyy-mm-dd) are
   counted and block the apply, and so does deleting every row of a month.

**Adding a column**: choose **Add a column** in step 2, give it a name and a type (number,
text or date), and optionally a formula that fills the rows picked in step 1 (e.g. *Discount* =
`ROUND([Amount] * 0.1, 2)`, or *Band* = `IF([Amount] > 1000, "High", "Normal")`). Every other
row starts blank. The column exists for every month from then on: it shows in the grid (editable),
can be used in conditions and formulas like any field (`[Discount]`), and can be removed from the
strip at the top of the builder (its values stay in the change history). Its values reach
ClickHouse and the dashboards when you finalize:

- **ClickHouse**: `SELECT * FROM ub.v_custom` has every added column as a real, typed column
  (number, text, date) next to the billing line's keys, ready for queries or Power BI.
- **Dashboards**: every dashboard ends with an **Added columns** section, in the added columns'
  own colour (purple): each number column's total and by island, each text or date column's
  revenue by value, for the page's filters and the user's role (on the Water page, water rows
  only). Added columns are purple in the review grid and the formula builder too.
- An upload whose file has a column with the same name fills it.

Applying adds the changes to the pending edits, like hand edits: check them on the Rows tab
(**Show matching rows**), then Finalize. Formulas can be saved and loaded again on any period.
Formulas are parsed by the app (`formula.py`) and only known fields, functions and quoted
values reach the database.

Every change is logged (who, when, before, after) under **Change history**.

`python3 webapp/test_formula.py` tests the formula builder thoroughly on a throwaway test month
(it never edits the real months): the formula language, mistakes, hostile input, conditions,
the actions, added columns, Finalize end to end, roles, and speed. `test_webapp.py` also uses
a throwaway month for its review checks. The dashboards show
whether what's on screen is Draft or Final.

## Periods

Every dashboard can show one month, a range (**Range from – to…**) or all periods combined.

A 580k-row file takes about 30 seconds. A file with the same name replaces its earlier load.
Uploaded files are kept in `data/uploads/`.

Prefer `.xlsx` or a CSV exported straight from the billing system. A CSV that Excel has re-saved
loses its dates, and the billing month then has to be recovered from the batch.

## What's in it

- `server.py`: API, sign-in (hashed passwords, signed session cookie), roles, the load runner
- `dashboards.py`: the 10 dashboards as metrics over the built lines (`ub.fact_lines`), with
  customer and connection roll-ups (bill size, rank, change since the previous period)
- `review.py`: the review workflow: rows with pending edits, edit / add / delete / undo, the log
- `formula.py`: the formula and condition language, parsed and compiled to SQL
- `bulk.py`: the formula builder's preview and apply (change / copy / delete / add a column), saved formulas
- `../ub_custom.py`: added columns: definitions, the `extra` map on each row, the `ub.v_custom` view
- `roles.py`: roles, their pages and utilities, and the default users
- `static/`: the front end (HTML, CSS, SVG charts). It has no external libraries, so it works on a
  network without internet access.
- `test_webapp.py`: role and data-scope tests
