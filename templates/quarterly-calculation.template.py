"""Quarterly Metrics Agent — compile quarterly financial metrics from Ramp and Mercury.

Fetches all transactions from Mercury (inflows + outflows) and Ramp (credit card +
bill pay) for each month in a quarter, then computes:
  - Net Burn (Cash Flow from Operating Activities)
  - Revenue (GAAP Revenue)
  - EBITDA (Earnings Before Interest, Taxes, Depreciation, and Amortization)
  - Net Income (Net Income or Loss)

Transfers between accounts and between Mercury and Ramp are excluded.

Usage:
    python quarterly-calculation.py                     # current quarter
    python quarterly-calculation.py --quarter 2026-Q2   # specific quarter
    python quarterly-calculation.py -o report.md        # custom output path
"""

import calendar
import os
import sys
from collections import defaultdict
from datetime import date, datetime

import requests as _requests
from dotenv import load_dotenv

load_dotenv()

RAMP_BASE = "https://api.ramp.com/developer/v1"
MERCURY_BASE = "https://api.mercury.com/api/v1"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "quarter-metric.md")

QUARTER_END_MONTHS = {1: 3, 2: 6, 3: 9, 4: 12}

# Counterparty substrings for Mercury-Ramp transfer detection (upper-cased)
RAMP_TRANSFER_KEYWORDS = ["RAMP"]

# Substrings to tag a transaction as a tax payment (upper-cased counterparty/merchant).
# Keep generic agency names; add your state/local tax authority names as needed.
TAX_KEYWORDS = [
    "FRANCHISE TAX", "IRS", "INTERNAL REVENUE", "STATE TAX",
    # "YOUR_STATE_TAX_AUTHORITY",
]

# Substrings to tag as interest expense
INTEREST_KEYWORDS = [
    "INTEREST CHARGE", "INTEREST PAYMENT", "FINANCE CHARGE",
]


def _ramp_headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('RAMP_TOKEN', '')}"}


def _mercury_headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('MERCURY_TOKEN', '')}"}


def _quarter_months(quarter_str: str) -> list[str]:
    """Return the 3 month strings (YYYY-MM) in a quarter."""
    year = int(quarter_str[:4])
    q = int(quarter_str[-1])
    start_month = (q - 1) * 3 + 1
    return [f"{year}-{m:02d}" for m in range(start_month, start_month + 3)]


def _month_range(month_str: str) -> tuple[str, str]:
    year, month = int(month_str[:4]), int(month_str[5:7])
    first = f"{year}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    last = f"{year}-{month:02d}-{last_day:02d}"
    return first, last


def _current_quarter() -> str:
    today = date.today()
    q = (today.month - 1) // 3 + 1
    return f"{today.year}-Q{q}"


def _is_ramp_transfer(counterparty: str) -> bool:
    upper = counterparty.upper()
    return any(kw in upper for kw in RAMP_TRANSFER_KEYWORDS)


def _is_tax(name: str) -> bool:
    upper = name.upper()
    return any(kw in upper for kw in TAX_KEYWORDS)


def _is_interest(name: str) -> bool:
    upper = name.upper()
    return any(kw in upper for kw in INTEREST_KEYWORDS)


# ---------------------------------------------------------------------------
# Mercury data fetching
# ---------------------------------------------------------------------------


def fetch_mercury_accounts() -> list[dict]:
    headers = _mercury_headers()
    resp = _requests.get(f"{MERCURY_BASE}/accounts", headers=headers)
    resp.raise_for_status()
    return [
        {"id": a["id"], "name": a.get("name", ""), "status": a.get("status", "")}
        for a in resp.json().get("accounts", [])
    ]


def fetch_mercury_transactions(
    accounts: list[dict], month: str, max_pages: int = 30,
) -> list[dict]:
    """Fetch ALL Mercury transactions for *month* (both inflows and outflows).

    Excludes internal transfers and Mercury↔Ramp transfers.
    """
    headers = _mercury_headers()
    start, end = _month_range(month)
    txns: list[dict] = []
    for acct in accounts:
        aid = acct["id"]
        offset = 0
        for _ in range(max_pages):
            resp = _requests.get(
                f"{MERCURY_BASE}/account/{aid}/transactions",
                headers=headers,
                params={"limit": 500, "offset": offset, "start": start, "end": end},
            )
            resp.raise_for_status()
            batch = resp.json().get("transactions", [])
            for t in batch:
                date_str = (t.get("postedAt") or t.get("createdAt") or "")[:10]
                if not (start <= date_str <= end):
                    continue
                kind = t.get("kind", "")
                if kind in ("internalTransfer", "treasuryTransfer"):
                    continue
                cp = (t.get("counterpartyName") or "").strip()
                if _is_ramp_transfer(cp):
                    continue
                amount = t.get("amount", 0)
                txns.append({
                    "name": cp,
                    "amount": amount,
                    "date": date_str,
                    "kind": t.get("kind", ""),
                    "account": acct["name"],
                    "source": "Mercury",
                })
            if len(batch) < 500:
                break
            offset += 500
    return txns


# ---------------------------------------------------------------------------
# Ramp data fetching
# ---------------------------------------------------------------------------


def _extract_all_qb_names(raw_txn: dict) -> list[str]:
    names: list[str] = []
    for field in raw_txn.get("accounting_field_selections", []):
        if not isinstance(field, dict):
            continue
        for key in ("accounting_field_option", "field_option", "option"):
            opt = field.get(key)
            if isinstance(opt, dict):
                name = opt.get("name", "")
                if name and name not in names:
                    names.append(name)
        name = field.get("name") or field.get("external_name") or ""
        if name and name not in names:
            names.append(name)
    return names


def fetch_ramp_transactions(month: str, max_pages: int = 50) -> list[dict]:
    """Fetch Ramp credit-card transactions for *month*."""
    headers = _ramp_headers()
    start, end = _month_range(month)
    results: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params: dict = {
            "page_size": 100,
            "from_date": f"{start}T00:00:00Z",
            "to_date": f"{end}T23:59:59Z",
        }
        if cursor:
            params["start"] = cursor
        resp = _requests.get(
            f"{RAMP_BASE}/transactions", headers=headers, params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        for t in data.get("data", []):
            date_str = (t.get("user_transaction_time") or "")[:10]
            if date_str and not (start <= date_str <= end):
                continue
            merchant = (
                t.get("merchant_name") or t.get("merchant_descriptor") or ""
            ).strip()
            results.append({
                "name": merchant,
                "amount": -abs(t.get("amount", 0)),
                "date": date_str,
                "source": "Ramp",
                "sk_category": t.get("sk_category_name", ""),
                "qb_categories": _extract_all_qb_names(t),
            })
        next_url = (data.get("page") or {}).get("next")
        if next_url and "start=" in next_url:
            cursor = next_url.split("start=")[-1].split("&")[0]
        else:
            break
    return results


def _extract_bill_qb_names(bill: dict) -> list[str]:
    names: list[str] = []
    for li in bill.get("line_items", []):
        for afs in li.get("accounting_field_selections", []):
            name = afs.get("name", "")
            if name and name not in names and name != "false":
                names.append(name)
    return names


def fetch_ramp_bills(month: str, max_pages: int = 50) -> list[dict]:
    """Fetch Ramp bill-pay (ACH) payments settled in *month*."""
    headers = _ramp_headers()
    start, end = _month_range(month)
    results: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params: dict = {"page_size": 100}
        if cursor:
            params["start"] = cursor
        resp = _requests.get(
            f"{RAMP_BASE}/bills", headers=headers, params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        for b in data.get("data", []):
            if b.get("status") != "PAID":
                continue
            paid_at = (b.get("paid_at") or "")[:10]
            payment = b.get("payment") or {}
            payment_date = (payment.get("payment_date") or "")[:10]
            settle_date = paid_at or payment_date
            if not (start <= settle_date <= end):
                continue
            amount_obj = b.get("amount", {})
            rate = amount_obj.get("minor_unit_conversion_rate", 100)
            amount_usd = amount_obj.get("amount", 0) / rate
            vendor = b.get("vendor", {})
            results.append({
                "name": (vendor.get("name") or "Unknown").strip(),
                "amount": -amount_usd,
                "date": settle_date,
                "source": "Ramp Bill Pay",
                "qb_categories": _extract_bill_qb_names(b),
            })
        next_url = (data.get("page") or {}).get("next")
        if next_url and "start=" in next_url:
            cursor = next_url.split("start=")[-1].split("&")[0]
        else:
            break
    return results


# ---------------------------------------------------------------------------
# Metric calculations
# ---------------------------------------------------------------------------


def compute_monthly_metrics(txns: list[dict]) -> dict:
    """Given all transactions for a month, compute the 4 financial metrics.

    Transaction amounts follow sign convention: positive = inflow, negative = outflow.
    """
    revenue = 0.0
    total_expenses = 0.0
    interest_expense = 0.0
    tax_expense = 0.0

    revenue_items: list[dict] = []
    expense_items: list[dict] = []

    for t in txns:
        name = t.get("name", "")
        amount = t["amount"]

        if amount > 0:
            revenue += amount
            revenue_items.append(t)
        elif amount < 0:
            expense = abs(amount)
            total_expenses += expense
            expense_items.append(t)
            if _is_interest(name):
                interest_expense += expense
            if _is_tax(name):
                tax_expense += expense

    net_income = revenue - total_expenses
    ebitda = net_income + interest_expense + tax_expense
    net_burn = net_income

    return {
        "revenue": revenue,
        "total_expenses": total_expenses,
        "interest_expense": interest_expense,
        "tax_expense": tax_expense,
        "net_income": net_income,
        "ebitda": ebitda,
        "net_burn": net_burn,
        "revenue_items": revenue_items,
        "expense_items": expense_items,
        "transaction_count": len(txns),
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def _fmt(val: float) -> str:
    """Format a dollar value with sign: negative in parens."""
    if val < 0:
        return f"(${ abs(val):,.2f})"
    return f"${val:,.2f}"


def build_report(
    quarter: str,
    monthly_data: dict[str, dict],
) -> str:
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    months = sorted(monthly_data.keys())

    lines.append(f"# Quarterly Financial Metrics — {quarter}")
    lines.append("")
    lines.append(f"*Report generated: {now}*")
    lines.append(f"*Basis: Cash (derived from bank and credit card transactions)*")
    lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    month_headers = [MONTH_NAMES[int(m[5:7])] for m in months]
    lines.append(f"| Metric | {' | '.join(month_headers)} | **Quarter Total** |")
    lines.append(f"|---|{'---|' * len(months)}---|")

    q_revenue = sum(monthly_data[m]["revenue"] for m in months)
    q_expenses = sum(monthly_data[m]["total_expenses"] for m in months)
    q_net_income = sum(monthly_data[m]["net_income"] for m in months)
    q_ebitda = sum(monthly_data[m]["ebitda"] for m in months)
    q_burn = sum(monthly_data[m]["net_burn"] for m in months)

    row_revenue = " | ".join(_fmt(monthly_data[m]["revenue"]) for m in months)
    row_expenses = " | ".join(_fmt(monthly_data[m]["total_expenses"]) for m in months)
    row_net_income = " | ".join(_fmt(monthly_data[m]["net_income"]) for m in months)
    row_ebitda = " | ".join(_fmt(monthly_data[m]["ebitda"]) for m in months)
    row_burn = " | ".join(_fmt(monthly_data[m]["net_burn"]) for m in months)

    lines.append(f"| **Revenue** | {row_revenue} | **{_fmt(q_revenue)}** |")
    lines.append(f"| **Total Expenses** | {row_expenses} | **{_fmt(q_expenses)}** |")
    lines.append(f"| **Net Income** | {row_net_income} | **{_fmt(q_net_income)}** |")
    lines.append(f"| **EBITDA** | {row_ebitda} | **{_fmt(q_ebitda)}** |")
    lines.append(f"| **Net Burn** | {row_burn} | **{_fmt(q_burn)}** |")
    lines.append("")

    # Adjustments used for EBITDA
    q_interest = sum(monthly_data[m]["interest_expense"] for m in months)
    q_tax = sum(monthly_data[m]["tax_expense"] for m in months)
    row_interest = " | ".join(_fmt(monthly_data[m]["interest_expense"]) for m in months)
    row_tax = " | ".join(_fmt(monthly_data[m]["tax_expense"]) for m in months)
    lines.append("### EBITDA Adjustments")
    lines.append("")
    lines.append(f"| Add-back | {' | '.join(month_headers)} | **Quarter Total** |")
    lines.append(f"|---|{'---|' * len(months)}---|")
    lines.append(f"| Interest | {row_interest} | **{_fmt(q_interest)}** |")
    lines.append(f"| Taxes | {row_tax} | **{_fmt(q_tax)}** |")
    lines.append(f"| D&A | *non-cash, not in bank data* | — |")
    lines.append("")

    # Monthly detail sections
    for m in months:
        data = monthly_data[m]
        month_name = MONTH_NAMES[int(m[5:7])]
        lines.append("---")
        lines.append("")
        lines.append(f"## {month_name} {m[:4]} Detail")
        lines.append("")
        lines.append(f"*{data['transaction_count']} transactions*")
        lines.append("")

        # Revenue breakdown
        lines.append(f"### Revenue: {_fmt(data['revenue'])}")
        lines.append("")
        rev_by_name: dict[str, float] = defaultdict(float)
        for t in data["revenue_items"]:
            rev_by_name[t["name"] or "Unknown"] += t["amount"]
        if rev_by_name:
            for name, total in sorted(rev_by_name.items(), key=lambda x: -x[1]):
                lines.append(f"- {name}: {_fmt(total)}")
        else:
            lines.append("- *(no revenue recorded)*")
        lines.append("")

        # Top expenses
        lines.append(f"### Total Expenses: {_fmt(data['total_expenses'])}")
        lines.append("")
        exp_by_name: dict[str, float] = defaultdict(float)
        exp_counts: dict[str, int] = defaultdict(int)
        for t in data["expense_items"]:
            exp_by_name[t["name"] or "Unknown"] += abs(t["amount"])
            exp_counts[t["name"] or "Unknown"] += 1
        top_expenses = sorted(exp_by_name.items(), key=lambda x: -x[1])[:20]
        for name, total in top_expenses:
            count = exp_counts[name]
            count_label = f" ({count}x)" if count > 1 else ""
            lines.append(f"- {name}: {_fmt(total)}{count_label}")
        remaining = len(exp_by_name) - 20
        if remaining > 0:
            remaining_total = sum(v for _, v in sorted(exp_by_name.items(), key=lambda x: -x[1])[20:])
            lines.append(f"- *… {remaining} more vendors: {_fmt(remaining_total)}*")
        lines.append("")

        # Tax/interest callouts
        if data["tax_expense"] > 0 or data["interest_expense"] > 0:
            lines.append("### Tax & Interest (excluded from EBITDA)")
            lines.append("")
            if data["tax_expense"] > 0:
                lines.append(f"- Taxes: {_fmt(data['tax_expense'])}")
            if data["interest_expense"] > 0:
                lines.append(f"- Interest: {_fmt(data['interest_expense'])}")
            lines.append("")

    # Definitions
    lines.append("---")
    lines.append("")
    lines.append("## Definitions")
    lines.append("")
    lines.append("- **Net Burn**: Cash Flow from Operating Activities. Negative = cash decrease.")
    lines.append("- **Revenue**: Incoming non-transfer cash receipts (GAAP revenue approximation, cash basis).")
    lines.append("- **EBITDA**: Net Income + Interest + Taxes. D&A is non-cash and not reflected in bank transactions.")
    lines.append("- **Net Income**: Revenue − Total Expenses (cash basis).")
    lines.append("- *All figures are cash-basis, derived from Mercury and Ramp transaction data.*")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Budget categorisation
# ---------------------------------------------------------------------------

# Mercury counterparty → budget subcategory.
# Add your Mercury counterparty name substrings (UPPER-CASED) to the appropriate
# category. The classifier checks if any keyword appears in the counterparty name.
MERCURY_BUDGET_MAP: dict[str, list[str]] = {
    "Payroll": [
        # "YOUR_PAYROLL_PROVIDER", "YOUR_PAYROLL_SERVICE",
    ],
    "Benefits & Insurance": [
        # "YOUR_BENEFITS_ADMIN", "YOUR_HEALTH_INSURER", "YOUR_401K_PROVIDER",
    ],
    "Rent": [
        # "YOUR_LANDLORD",
    ],
    "Utilities": [
        # "YOUR_ELECTRIC_COMPANY", "YOUR_INTERNET_PROVIDER",
    ],
    "Legal": [
        # "YOUR_LAW_FIRM",
    ],
    "Insurance": [
        # "YOUR_INSURANCE_PROVIDER",
    ],
    "Bank charges": [
        # "MERCURY TECHNOLOGIES",
    ],
}

# Ramp vendor name → budget subcategory.
# Add your Ramp vendor name substrings (UPPER-CASED) for vendors that aren't
# captured by the QuickBooks or sk_category mappings below.
RAMP_VENDOR_BUDGET_MAP: dict[str, list[str]] = {
    "Rent": [
        # "YOUR_LANDLORD",
    ],
    "Legal": [
        # "YOUR_LAW_FIRM", "YOUR_OTHER_COUNSEL",
    ],
    "Insurance": [
        # "YOUR_INSURANCE_PROVIDER",
    ],
}

# Ramp QuickBooks category → budget subcategory
QB_BUDGET_MAP: dict[str, list[str]] = {
    "Payroll": ["Payroll"],
    "Benefits & Insurance": ["Employee Benefits"],
    "Contractors & Consultants": ["Contractor", "Consultant"],
    "Rent": ["Rent"],
    "Supplies & materials": ["Supplies & Materials"],
    "Equipment": ["Furniture & Fixture"],
    "Utilities": ["Telephone & Internet", "Utilities"],
    "Meals & entertainment": ["Meals and Entertainment"],
    "Airfare": ["Airfare"],
    "Ground transport": ["Ground Transportation"],
    "Lodging": ["Lodging"],
    "Software & web services": [
        "Model & API Usage", "Dev Tools & SaaS", "Non-Engineering SaaS",
        "Observability & Monitoring", "DevOps & CI/CD", "Design Tools & Assets",
        "Software",
    ],
    "Cloud infrastructure": ["Cloud Infrastructure"],
    "Advertising": ["Advertising"],
    "Promotions": ["PR & Media", "Developer Community"],
}

# Ramp sk_category fallback → budget subcategory
SK_BUDGET_MAP: dict[str, list[str]] = {
    "Advertising": ["Advertising"],
    "Airfare": ["Airlines"],
    "Ground transport": ["Taxi and Rideshare", "Parking", "Car Rental"],
    "Lodging": ["Lodging", "Hotels"],
    "Meals & entertainment": ["Restaurants", "Alcohol and Bars", "Entertainment"],
    "Software & web services": ["SaaS / Software", "Software"],
    "Supplies & materials": [
        "General Merchandise", "Supermarkets and Grocery Stores",
        "Office Supplies",
    ],
    "Equipment": ["Electronics"],
    "Training": ["Education"],
    "Benefits & Insurance": ["Medical"],
}

# Other income keywords (positive Mercury transactions that aren't product revenue).
# Add substrings (UPPER-CASED) that identify non-revenue inflows like bank interest,
# cashback, etc.
OTHER_INCOME_KEYWORDS = [
    "CASHBACK", "DIVIDEND", "INTEREST EARNED", "REWARD",
]

# Budget sections → subcategories (display order)
BUDGET_SECTIONS: list[tuple[str, list[str]]] = [
    ("People & Talent", [
        "Payroll", "Benefits & Insurance", "Payroll services & recruiting",
        "Training", "Contractors & Consultants",
    ]),
    ("Legal & Professional", ["Legal"]),
    ("Office & Facilities", [
        "Rent", "Supplies & materials", "Equipment", "Utilities",
    ]),
    ("Technology & Infrastructure", [
        "Software & web services", "Cloud infrastructure",
    ]),
    ("Sales, Marketing & Growth", ["Advertising", "Promotions"]),
    ("Travel & Community Building", [
        "Airfare", "Ground transport", "Lodging", "Meals & entertainment",
    ]),
    ("Financial & Administrative", ["Bank charges", "Insurance", "Taxes"]),
]


def _classify_budget(txn: dict) -> str:
    """Assign a single expense transaction to a budget subcategory."""
    name_upper = (txn.get("name") or "").upper()
    source = txn.get("source", "")

    if "Mercury" in source:
        for subcat, keywords in MERCURY_BUDGET_MAP.items():
            if any(kw in name_upper for kw in keywords):
                return subcat

    if "Ramp" in source:
        for subcat, keywords in RAMP_VENDOR_BUDGET_MAP.items():
            if any(kw in name_upper for kw in keywords):
                return subcat
        for qb_name in txn.get("qb_categories", []):
            qb_lower = qb_name.lower()
            for subcat, qb_keys in QB_BUDGET_MAP.items():
                if any(k.lower() in qb_lower for k in qb_keys):
                    return subcat
            if ":" in qb_name:
                child = qb_name.split(":")[-1].strip().lower()
                for subcat, qb_keys in QB_BUDGET_MAP.items():
                    if any(k.lower() in child for k in qb_keys):
                        return subcat
        sk = (txn.get("sk_category") or "").lower()
        if sk:
            for subcat, sk_keys in SK_BUDGET_MAP.items():
                if any(k.lower() == sk for k in sk_keys):
                    return subcat

    if _is_tax(name_upper):
        return "Taxes"

    return "Uncategorized"


def _is_other_income(txn: dict) -> bool:
    name_upper = (txn.get("name") or "").upper()
    return any(kw in name_upper for kw in OTHER_INCOME_KEYWORDS)


def categorize_budget(all_txns: list[dict]) -> dict:
    """Split transactions into revenue, other income, and categorised expenses."""
    revenue_items: list[dict] = []
    other_income_items: list[dict] = []
    expenses_by_cat: dict[str, list[dict]] = defaultdict(list)

    for t in all_txns:
        amount = t["amount"]
        if amount > 0:
            if _is_other_income(t):
                other_income_items.append(t)
            else:
                revenue_items.append(t)
        elif amount < 0:
            cat = _classify_budget(t)
            expenses_by_cat[cat].append(t)

    return {
        "revenue_items": revenue_items,
        "other_income_items": other_income_items,
        "expenses_by_cat": dict(expenses_by_cat),
    }


def build_budget_summary(
    quarter: str,
    months: list[str],
    all_txns: list[dict],
) -> str:
    """Build the narrative budget-summary.md report."""
    year = int(quarter[:4])
    q = int(quarter[-1])
    start_month = (q - 1) * 3 + 1
    end_month = start_month + 2
    start_date = f"{MONTH_NAMES[start_month][:3]} 1"
    end_day = calendar.monthrange(year, end_month)[1]
    end_date = f"{MONTH_NAMES[end_month][:3]} {end_day}"

    budget = categorize_budget(all_txns)
    revenue_items = budget["revenue_items"]
    other_income_items = budget["other_income_items"]
    expenses_by_cat = budget["expenses_by_cat"]

    total_revenue = sum(t["amount"] for t in revenue_items)
    total_other_income = sum(t["amount"] for t in other_income_items)
    total_expenses = sum(
        abs(t["amount"]) for txns in expenses_by_cat.values() for t in txns
    )
    net_operating_loss = total_revenue - total_expenses
    net_loss = net_operating_loss + total_other_income

    lines: list[str] = []

    # Header
    lines.append(f"# Your Company — {quarter} Budget Summary")
    lines.append("")
    lines.append(f"**Period:** {start_date} – {end_date}, {year}")
    lines.append(f"**Accounting Basis:** Cash")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Executive Snapshot
    lines.append("## Executive Snapshot")
    lines.append("")
    lines.append(f"**Revenue:** {_fmt(total_revenue)}")
    lines.append("")
    lines.append(f"**Operating Expenses:** {_fmt(total_expenses)}")
    lines.append("")
    lines.append(f"**Net Operating Loss:** {_fmt(net_operating_loss)}")
    lines.append("")
    if total_other_income > 0:
        lines.append(
            f"**Other Income (dividends, rewards):** {_fmt(total_other_income)}"
        )
        lines.append("")
    lines.append(f"**Net Loss:** {_fmt(net_loss)}")
    lines.append("")

    # Primary burn drivers — find top 5 sections by spend
    section_totals: list[tuple[str, float]] = []
    for section_name, subcats in BUDGET_SECTIONS:
        total = sum(
            abs(t["amount"])
            for sc in subcats
            for t in expenses_by_cat.get(sc, [])
        )
        if total > 0:
            section_totals.append((section_name, total))
    uncat_total = sum(abs(t["amount"]) for t in expenses_by_cat.get("Uncategorized", []))
    if uncat_total > 0:
        section_totals.append(("Uncategorized expenses", uncat_total))
    section_totals.sort(key=lambda x: -x[1])

    lines.append("**Primary burn drivers**")
    lines.append("")
    for name, _ in section_totals[:5]:
        lines.append(f"* {name}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Expense Breakdown
    lines.append("## Expense Breakdown")
    lines.append("")

    for section_name, subcats in BUDGET_SECTIONS:
        present = {sc: expenses_by_cat.get(sc, []) for sc in subcats if expenses_by_cat.get(sc)}
        if not present:
            continue

        lines.append(f"### {section_name}")
        lines.append("")

        section_total = 0.0
        for subcat in subcats:
            txns = expenses_by_cat.get(subcat, [])
            if not txns:
                continue
            sub_total = sum(abs(t["amount"]) for t in txns)
            section_total += sub_total

            # Group by vendor within subcategory
            by_vendor: dict[str, float] = defaultdict(float)
            for t in txns:
                by_vendor[t.get("name") or "Unknown"] += abs(t["amount"])

            if len(by_vendor) <= 5:
                lines.append(f"**{subcat}:** {_fmt(sub_total)}")
                for vendor, amt in sorted(by_vendor.items(), key=lambda x: -x[1]):
                    lines.append(f"- {vendor}: {_fmt(amt)}")
            else:
                lines.append(f"**{subcat}:** {_fmt(sub_total)}")
                top = sorted(by_vendor.items(), key=lambda x: -x[1])[:5]
                for vendor, amt in top:
                    lines.append(f"- {vendor}: {_fmt(amt)}")
                rest_count = len(by_vendor) - 5
                rest_total = sub_total - sum(amt for _, amt in top)
                lines.append(f"- *… {rest_count} more: {_fmt(rest_total)}*")
            lines.append("")

        lines.append(f"**{section_name} Total: {_fmt(section_total)}**")
        share = (section_total / total_expenses * 100) if total_expenses else 0
        lines.append(f"Share of total spend: ~{share:.0f}%")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Uncategorized
    uncat = expenses_by_cat.get("Uncategorized", [])
    if uncat:
        uncat_total = sum(abs(t["amount"]) for t in uncat)
        lines.append("### Uncategorized Expenses")
        lines.append("")
        lines.append(f"**{_fmt(uncat_total)}**")
        lines.append("")

        by_vendor: dict[str, float] = defaultdict(float)
        for t in uncat:
            by_vendor[t.get("name") or "Unknown"] += abs(t["amount"])
        top_uncat = sorted(by_vendor.items(), key=lambda x: -x[1])[:10]
        lines.append("Top uncategorized vendors:")
        lines.append("")
        for vendor, amt in top_uncat:
            lines.append(f"- {vendor}: {_fmt(amt)}")
        rest_count = len(by_vendor) - 10
        if rest_count > 0:
            rest_total = uncat_total - sum(amt for _, amt in top_uncat)
            lines.append(f"- *… {rest_count} more: {_fmt(rest_total)}*")
        lines.append("")
        share = (uncat_total / total_expenses * 100) if total_expenses else 0
        lines.append(
            f"⚠ {_fmt(uncat_total)} ({share:.0f}%) in uncategorized expenses "
            f"should be reviewed for investor transparency."
        )
        lines.append("")
        lines.append("---")
        lines.append("")

    # Spending Distribution
    lines.append("## Spending Distribution — Major Cost Drivers")
    lines.append("")
    lines.append(f"The largest contributors to {quarter} spending were:")
    lines.append("")
    for name, total in section_totals[:7]:
        share = (total / total_expenses * 100) if total_expenses else 0
        lines.append(f"* {name}: {_fmt(total)} ({share:.0f}%)")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Suggested Budget Framework
    lines.append("## Suggested Budget Framework Going Forward")
    lines.append("")
    lines.append("**Fixed Costs**")
    lines.append("")
    lines.append("* Payroll and benefits")
    lines.append("* Rent")
    lines.append("* Core software stack")
    lines.append("* Insurance and legal")
    lines.append("")
    lines.append("**Growth Investments**")
    lines.append("")
    lines.append("* Events and community programs")
    lines.append("* Travel")
    lines.append("* Marketing and partnerships")
    lines.append("")
    lines.append("**Variable / Operational Costs**")
    lines.append("")
    lines.append("* Contractors")
    lines.append("* Equipment and supplies")
    lines.append("* Cloud infrastructure usage")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    quarter = _current_quarter()
    output_file = OUTPUT_FILE

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--quarter" and i + 1 < len(args):
            quarter = args[i + 1].upper()
            if not (len(quarter) == 7 and quarter[4:6] == "-Q" and quarter[-1] in "1234"):
                print(f"Invalid quarter format: {quarter} (expected YYYY-Q#)")
                sys.exit(1)
            i += 2
        elif args[i] in ("--output", "-o") and i + 1 < len(args):
            output_file = args[i + 1]
            if not os.path.isabs(output_file):
                output_file = os.path.join(SCRIPT_DIR, output_file)
            i += 2
        elif args[i] in ("--help", "-h"):
            print(__doc__.strip())
            sys.exit(0)
        else:
            print(f"Unknown argument: {args[i]}")
            sys.exit(1)

    months = _quarter_months(quarter)

    print("Quarterly Metrics Agent")
    print("=" * 55)
    print(f"  Quarter: {quarter}")
    print(f"  Months:  {', '.join(months)}")
    print(f"  Output:  {output_file}")
    print("=" * 55)

    # Fetch Mercury accounts once
    print("\n[1] Fetching Mercury accounts…", flush=True)
    merc_accounts = fetch_mercury_accounts()
    active = [a for a in merc_accounts if a["status"] == "active"]
    print(f"    {len(active)} active accounts")

    budget_file = os.path.join(SCRIPT_DIR, "budget-summary.md")

    monthly_data: dict[str, dict] = {}
    all_quarter_txns: list[dict] = []

    for idx, month in enumerate(months, 1):
        print(f"\n[{idx + 1}] Processing {month}…", flush=True)

        # Mercury
        print(f"    Fetching Mercury transactions…", flush=True)
        merc_txns = fetch_mercury_transactions(active, month)
        inflows = sum(1 for t in merc_txns if t["amount"] > 0)
        outflows = sum(1 for t in merc_txns if t["amount"] < 0)
        print(f"    {len(merc_txns)} Mercury txns ({inflows} in, {outflows} out)")

        # Ramp credit card
        print(f"    Fetching Ramp credit-card transactions…", flush=True)
        ramp_txns = fetch_ramp_transactions(month)
        print(f"    {len(ramp_txns)} Ramp credit-card txns")

        # Ramp bills
        print(f"    Fetching Ramp bill payments…", flush=True)
        ramp_bills = fetch_ramp_bills(month)
        print(f"    {len(ramp_bills)} Ramp bill payments")

        all_txns = merc_txns + ramp_txns + ramp_bills
        all_quarter_txns.extend(all_txns)
        metrics = compute_monthly_metrics(all_txns)
        monthly_data[month] = metrics

        print(f"    Revenue:    {_fmt(metrics['revenue'])}")
        print(f"    Expenses:   {_fmt(metrics['total_expenses'])}")
        print(f"    Net Income: {_fmt(metrics['net_income'])}")
        print(f"    EBITDA:     {_fmt(metrics['ebitda'])}")
        print(f"    Net Burn:   {_fmt(metrics['net_burn'])}")

    # Build quarter-metric report
    print(f"\n[{len(months) + 2}] Building quarter-metric report…", flush=True)
    report = build_report(quarter, monthly_data)

    with open(output_file, "w") as f:
        f.write(report)
    print(f"  Saved to: {output_file}")

    for line in report.strip().split("\n")[:30]:
        print(f"  {line}")
    total_lines = len(report.strip().split("\n"))
    if total_lines > 30:
        print(f"  … ({total_lines - 30} more lines)")

    # Build budget-summary report
    print(f"\n[{len(months) + 3}] Building budget summary…", flush=True)
    budget_report = build_budget_summary(quarter, months, all_quarter_txns)

    with open(budget_file, "w") as f:
        f.write(budget_report)
    print(f"  Saved to: {budget_file}")

    for line in budget_report.strip().split("\n")[:30]:
        print(f"  {line}")
    budget_lines = len(budget_report.strip().split("\n"))
    if budget_lines > 30:
        print(f"  … ({budget_lines - 30} more lines)")

    print("\nDone.")


if __name__ == "__main__":
    main()
