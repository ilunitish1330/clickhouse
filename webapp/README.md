# PUC Analytics web app

Role-based dashboards over the ClickHouse `ub` billing data, plus a **Run** button that loads a
billing export into ClickHouse. Every dashboard is aggregated from the raw rows when it is opened.

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

Every user also has an **island**. Any role can be limited to Mahe, Praslin or La Digue, which
filters every number the user sees. The limits are applied inside the database queries on the
server, not in the browser. `test_webapp.py` checks them.

Roles are defined in `roles.py`. To add a role, add an entry there, then assign users to it on the
**Users & roles** page.

## Loading data

Go to **Data Load**, drop the Statistic Report export (`.xlsx`, `.csv` or a `.zip`) and press **Run**.
The app runs `ub_load.py` and shows its log live. The rows are stored in ClickHouse as they are,
and the stored rows and amounts are checked against the file. Nothing is aggregated then: each
dashboard reads the raw rows it needs (only that period's batches, utilities and islands) and
aggregates them in ClickHouse when it is opened, 0.2 to 0.8 s the first time. The header shows how
many raw rows it read; results are reused until new data is loaded. **Build Power BI tables** is
only needed before a Power BI refresh.

A 580k-row file takes about 30 seconds. A file with the same name replaces its earlier load.
Uploaded files are kept in `data/uploads/`.

Prefer `.xlsx` or a CSV exported straight from the billing system. A CSV that Excel has re-saved
loses its dates, and the billing month then has to be recovered from the batch.

## What's in it

- `server.py`: API, sign-in (hashed passwords, signed session cookie), roles, the load runner
- `dashboards.py`: the 10 dashboards as metrics over the raw rows (`ub.v_lines`), with customer
  and connection roll-ups (bill size, rank, change since the previous period) built per request
- `roles.py`: roles, their pages and utilities, and the default users
- `static/`: the front end (HTML, CSS, SVG charts). It has no external libraries, so it works on a
  network without internet access.
- `test_webapp.py`: role and data-scope tests
