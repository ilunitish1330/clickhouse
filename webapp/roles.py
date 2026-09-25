"""Who sees what. Roles are the PUC functions that use billing analytics.

Two independent limits apply to every user:
  * the ROLE decides which pages they open and which utilities' data they see;
  * the user's ISLAND (region_code, 0 = all islands) limits the rows further.
A regional manager on Praslin is role `regional_manager` + island 2, and every
number they see -- tiles, charts, tables -- is computed from Praslin rows only.
The server applies both limits inside the SQL; nothing is filtered in the browser.

To add or change a role: add an entry below. Pages are the keys of
dashboards.PAGES plus "load" (Data Load), "review" (Data Review) and "users"
(user admin).
"""
from dataclasses import dataclass, field

DASHBOARDS = ["executive", "electricity", "water", "sewerage", "tariff", "customers",
              "controls", "solar", "network", "consumption"]
ELECTRICITY, WASTEWATER, WATER = 1, 2, 3
ISLANDS = {0: "All islands", 1: "Mahe", 2: "Praslin", 3: "La Digue"}


@dataclass(frozen=True)
class Role:
    title: str
    description: str
    pages: list = field(default_factory=list)
    utilities: tuple | None = None       # None = every utility
    see_accounts: bool = False           # full customer account numbers
    can_load: bool = False               # upload files / run the pipeline
    can_admin: bool = False              # manage users
    can_review: bool = False             # edit uploaded rows, finalize edits, mark data reviewed


ROLES = {
    "admin": Role("System Administrator", "Everything, including data loads and user management.",
                  DASHBOARDS + ["load", "review", "users"], see_accounts=True, can_load=True, can_admin=True,
                  can_review=True),
    "executive": Role("Executive Management", "CEO and board: every dashboard, every utility, no account numbers.",
                      DASHBOARDS),
    "finance": Role("Finance & Revenue Manager", "Revenue, tariffs, customers and billing controls for all utilities.",
                    ["executive", "electricity", "water", "sewerage", "tariff", "customers", "controls"],
                    see_accounts=True),
    "electricity_manager": Role("Electricity Division Manager", "Electricity and solar PV data only.",
                                ["executive", "electricity", "solar", "tariff", "customers", "consumption"],
                                utilities=(ELECTRICITY,)),
    "water_manager": Role("Water & Sewerage Division Manager", "Water, sewerage and the water network only.",
                          ["executive", "water", "sewerage", "network", "tariff", "customers", "consumption"],
                          utilities=(WATER, WASTEWATER)),
    "regional_manager": Role("Regional Manager", "Every dashboard, limited to the manager's own island.",
                             DASHBOARDS),
    "billing_officer": Role("Billing Officer", "Billing controls, adjustments and customer billing.",
                            ["controls", "customers", "consumption"], see_accounts=True),
    "customer_service": Role("Customer Service Officer", "Customer and connection views for service queries.",
                             ["customers", "consumption"], see_accounts=True),
    "auditor": Role("Internal Auditor", "Read-only access to every dashboard and the load history.",
                    DASHBOARDS + ["load_history"], see_accounts=True),
    "reviewer": Role("Data Reviewer", "Reviews uploads: checks the dashboards and data, edits rows, finalizes "
                     "the edits and marks the data reviewed.", DASHBOARDS + ["review"], see_accounts=True,
                     can_review=True),
    "data_operator": Role("Data Operator", "Uploads billing exports and runs the pipeline; no dashboards.",
                          ["load"], can_load=True),
}

# First start creates one user per role; every password must be changed.
SEED_USERS = [
    ("admin", "System Administrator", "admin", 0),
    ("ceo", "Chief Executive Officer", "executive", 0),
    ("finance", "Finance Manager", "finance", 0),
    ("elec.manager", "Electricity Division Manager", "electricity_manager", 0),
    ("water.manager", "Water & Sewerage Manager", "water_manager", 0),
    ("mahe.manager", "Regional Manager, Mahe", "regional_manager", 1),
    ("praslin.manager", "Regional Manager, Praslin", "regional_manager", 2),
    ("ladigue.manager", "Regional Manager, La Digue", "regional_manager", 3),
    ("billing", "Billing Officer", "billing_officer", 0),
    ("service", "Customer Service Officer", "customer_service", 0),
    ("auditor", "Internal Auditor", "auditor", 0),
    ("operator", "Data Operator", "data_operator", 0),
    ("reviewer", "Data Reviewer", "reviewer", 0),
]


def mask_account(value: str) -> str:
    """CUS-049192 -> CUS-•••192 for roles that may not see account numbers."""
    head, _, tail = value.rpartition("-")
    return f"{head}-•••{tail[-3:]}" if head else "•••" + value[-3:]
