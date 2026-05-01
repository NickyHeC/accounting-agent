"""Monthly Expense Agent — compile a monthly expense summary from Ramp and Mercury.

Fetches ACH withdrawals from Mercury and credit-card expenses from Ramp for a
target month, categorises them according to predefined vendor / QuickBooks-
category rules, and generates an expense summary in markdown list format.

Usage:
    python monthly_expense.py                    # current month
    python monthly_expense.py --month 2026-04    # specific month
    python monthly_expense.py --dry-run          # preview prompt only
    python monthly_expense.py -o report.md       # custom output path
"""

import asyncio
import calendar
import os
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime

import requests as _requests
from dedalus_labs import AsyncDedalus, AuthenticationError, DedalusRunner
from dedalus_labs.utils.stream import stream_async
from dotenv import load_dotenv

load_dotenv()


MERCURY_MCP = os.getenv("MERCURY_MCP_SERVER", "your-org/mercury-mcp")
RAMP_MCP = os.getenv("RAMP_MCP_SERVER", "your-org/ramp-mcp")
MODEL = os.getenv("MODEL", "anthropic/claude-sonnet-4-20250514")
TIMEOUT = int(os.getenv("AGENT_TIMEOUT", "1200"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "expense_summary.md")

RAMP_BASE = "https://api.ramp.com/developer/v1"
MERCURY_BASE = "https://api.mercury.com/api/v1"

# ---------------------------------------------------------------------------
# Categorisation rules — CUSTOMIZE THESE FOR YOUR COMPANY
# ---------------------------------------------------------------------------

# Mercury counterparty-name substrings → expense category (matched upper-case)
MERCURY_VENDOR_CATEGORIES: dict[str, list[str]] = {
    # "Payroll": ["YOUR_PAYROLL_PROVIDER"],
    # "Rent": ["YOUR_LANDLORD"],
    # "Health Insurance": ["YOUR_INSURER"],
}

# Ramp merchant-name substrings → expense category (matched upper-case)
RAMP_VENDOR_CATEGORIES: dict[str, list[str]] = {
    # "Legal": ["YOUR_LAW_FIRM"],
}

# Ramp QuickBooks category (substring, case-insensitive) → expense category
RAMP_QB_CATEGORIES: dict[str, list[str]] = {
    "Meals & Wellness": ["Meals and Entertainment"],
    "Groceries & Office Supplies": ["Supplies & Materials"],
    "Travel": ["Ground Transportation", "Airfare", "Lodging"],
    "Ads": ["Advertising"],
    "API Credit Usage": ["Model & API Usage"],
    "Eng SaaS": [
        "Dev Tools & SaaS",
        "Cloud Infrastructure",
        "Observability & Monitoring",
        "DevOps & CI/CD",
    ],
    "Non-eng SaaS": ["Non-Engineering SaaS"],
}

# Fallback: Ramp sk_category_name → expense category (when QB field is absent)
RAMP_SK_CATEGORIES: dict[str, list[str]] = {
    "Ads": ["Advertising"],
    "Travel": ["Airlines", "Taxi and Rideshare", "Lodging", "Parking"],
    "Meals & Wellness": ["Restaurants", "Alcohol and Bars"],
}

# Mercury account-name substrings → expense category
MERCURY_ACCOUNT_CATEGORIES: dict[str, list[str]] = {}

# Display order for the final report
CATEGORY_ORDER: list[tuple[str, list[str]]] = [
    ("Payroll & Benefits", ["Payroll", "401k"]),
    ("Facilities & Utilities", ["Rent", "PG&E", "Furniture & Cleaning"]),
    ("Team Expenses", ["Meals & Wellness", "Groceries & Office Supplies", "Travel"]),
    ("Professional Services", ["Legal"]),
    ("Insurance", ["Health Insurance", "Business & Other Insurance"]),
    ("Growth", ["Ads", "Sales & Marketing"]),
    ("Software & Infrastructure", ["API Credit Usage", "Eng SaaS", "Non-eng SaaS"]),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ramp_headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('RAMP_TOKEN', '')}"}


def _mercury_headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('MERCURY_TOKEN', '')}"}


def _month_range(month_str: str) -> tuple[str, str]:
    """Return (first_day, last_day) as YYYY-MM-DD strings."""
    year, month = int(month_str[:4]), int(month_str[5:7])
    first = f"{year}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    last = f"{year}-{month:02d}-{last_day:02d}"
    return first, last


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


def fetch_mercury_accounts() -> list[dict]:
    headers = _mercury_headers()
    resp = _requests.get(f"{MERCURY_BASE}/accounts", headers=headers)
    resp.raise_for_status()
    return [
        {"id": a["id"], "name": a.get("name", ""), "type": a.get("type", "")}
        for a in resp.json().get("accounts", [])
    ]


def fetch_mercury_transactions(
    accounts: list[dict], month: str, max_pages: int = 30,
) -> list[dict]:
    """Fetch Mercury debit transactions for *month* across all accounts."""
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
                amount = t.get("amount", 0)
                if amount >= 0:
                    continue
                if t.get("kind") == "internalTransfer":
                    continue
                cp = (t.get("counterpartyName") or "").strip().upper()
                if cp == "RAMP":
                    continue
                txns.append({
                    "counterparty": (t.get("counterpartyName") or "").strip(),
                    "amount": abs(amount),
                    "date": date_str,
                    "kind": t.get("kind", ""),
                    "status": t.get("status", ""),
                    "note": (t.get("note") or ""),
                    "account_name": acct["name"],
                })
            if len(batch) < 500:
                break
            offset += 500
    return txns


def _extract_all_qb_names(raw_txn: dict) -> list[str]:
    """Extract ALL category/option names from a Ramp txn's accounting fields."""
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
    for cat in raw_txn.get("accounting_categories", []):
        if isinstance(cat, dict):
            name = cat.get("category_name") or cat.get("name") or ""
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
            holder = t.get("card_holder") or {}
            employee = (
                f"{holder.get('first_name', '')} {holder.get('last_name', '')}".strip()
                or "Unknown"
            )
            results.append({
                "merchant": (
                    t.get("merchant_name") or t.get("merchant_descriptor") or ""
                ).strip(),
                "amount": abs(t.get("amount", 0)),
                "date": date_str,
                "employee": employee,
                "ramp_category": t.get("sk_category_name", ""),
                "qb_categories": _extract_all_qb_names(t),
                "memo": t.get("memo") or "",
            })
        next_url = (data.get("page") or {}).get("next")
        if next_url and "start=" in next_url:
            cursor = next_url.split("start=")[-1].split("&")[0]
        else:
            break
    return results


def _extract_bill_qb_names(bill: dict) -> list[str]:
    """Extract QB category names from a Ramp bill's line_items."""
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
                "merchant": (vendor.get("name") or "Unknown").strip(),
                "amount": amount_usd,
                "date": settle_date,
                "employee": "",
                "ramp_category": "",
                "qb_categories": _extract_bill_qb_names(b),
                "memo": b.get("memo") or "",
                "payment_method": payment.get("payment_method", ""),
            })
        next_url = (data.get("page") or {}).get("next")
        if next_url and "start=" in next_url:
            cursor = next_url.split("start=")[-1].split("&")[0]
        else:
            break
    return results


# ---------------------------------------------------------------------------
# Categorisation
# ---------------------------------------------------------------------------


def _match_mercury(txn: dict) -> str | None:
    upper_cp = txn["counterparty"].upper()
    upper_acct = txn["account_name"].upper()
    for cat, keywords in MERCURY_ACCOUNT_CATEGORIES.items():
        if any(kw in upper_acct for kw in keywords):
            return cat
    for cat, keywords in MERCURY_VENDOR_CATEGORIES.items():
        if any(kw in upper_cp for kw in keywords):
            return cat
    return None


def _match_ramp(txn: dict) -> str | None:
    upper_m = txn["merchant"].upper()
    for cat, keywords in RAMP_VENDOR_CATEGORIES.items():
        if any(kw in upper_m for kw in keywords):
            return cat
    for qb_name in txn.get("qb_categories", []):
        qb_lower = qb_name.lower()
        for cat, qb_cats in RAMP_QB_CATEGORIES.items():
            if any(expected.lower() in qb_lower for expected in qb_cats):
                return cat
        if ":" in qb_name:
            child = qb_name.split(":")[-1].strip().lower()
            for cat, qb_cats in RAMP_QB_CATEGORIES.items():
                if any(expected.lower() in child for expected in qb_cats):
                    return cat
    ramp_cat = txn.get("ramp_category", "").lower()
    if ramp_cat:
        for cat, sk_cats in RAMP_SK_CATEGORIES.items():
            if any(sc.lower() == ramp_cat for sc in sk_cats):
                return cat
    return None


def categorise(
    mercury_txns: list[dict],
    ramp_txns: list[dict],
    ramp_bills: list[dict] | None = None,
) -> tuple[dict[str, list[dict]], list[dict], list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    uncat_m: list[dict] = []
    uncat_r: list[dict] = []
    for t in mercury_txns:
        cat = _match_mercury(t)
        if cat:
            grouped[cat].append({**t, "source": "Mercury"})
        else:
            uncat_m.append(t)
    for t in ramp_txns:
        cat = _match_ramp(t)
        if cat:
            grouped[cat].append({**t, "source": "Ramp"})
        else:
            uncat_r.append(t)
    for t in (ramp_bills or []):
        cat = _match_ramp(t)
        source = f"Ramp Bill Pay ({t.get('payment_method', 'ACH')})"
        if cat:
            grouped[cat].append({**t, "source": source})
        else:
            uncat_r.append({**t, "source": source})
    return dict(grouped), uncat_m, uncat_r


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def build_prompt(
    month: str,
    grouped: dict[str, list[dict]],
    uncat_mercury: list[dict],
    uncat_ramp: list[dict],
) -> str:
    sections: list[str] = []
    for group_name, cats in CATEGORY_ORDER:
        lines: list[str] = []
        for cat in cats:
            txns = grouped.get(cat, [])
            total = sum(t["amount"] for t in txns)
            detail = "\n".join(
                f"      - {t.get('counterparty') or t.get('merchant', '?')}: "
                f"${t['amount']:,.2f} on {t['date']} ({t['source']})"
                for t in sorted(txns, key=lambda x: -x["amount"])
            )
            lines.append(
                f"    {cat}: ${total:,.2f} ({len(txns)} txns)\n{detail}"
                if txns else f"    {cat}: $0.00 (0 txns)"
            )
        sections.append(f"  {group_name}\n" + "\n".join(lines))

    categorised_block = "\n\n".join(sections)

    uncat_lines: list[str] = []
    for t in sorted(uncat_mercury, key=lambda x: -x["amount"]):
        uncat_lines.append(
            f"  - [Mercury] {t['counterparty']}: ${t['amount']:,.2f} on {t['date']} "
            f"| kind={t['kind']} | account={t['account_name']} | note={t['note']}"
        )
    for t in sorted(uncat_ramp, key=lambda x: -x["amount"]):
        src_label = t.get("source", "Ramp")
        uncat_lines.append(
            f"  - [{src_label}] {t['merchant']}: ${t['amount']:,.2f} on {t['date']} "
            f"| ramp_cat={t.get('ramp_category', '')} "
            f"| qb_cats={', '.join(t.get('qb_categories', []))} "
            f"| by={t.get('employee', '')} | memo={t.get('memo', '')}"
        )
    uncat_block = "\n".join(uncat_lines) if uncat_lines else "  (none)"

    grand = (
        sum(t["amount"] for txns in grouped.values() for t in txns)
        + sum(t["amount"] for t in uncat_mercury)
        + sum(t["amount"] for t in uncat_ramp)
    )

    return f"""You are a financial reporting agent. Compile a monthly expense summary
for {month} using the pre-categorised data below.

OUTPUT RULES
- The FIRST line MUST be exactly: `# Monthly Expense Summary — {month}`
- Use **bullet-point lists only** — NO tables anywhere.
- Group expenses into the sections listed in CATEGORY ORDER.
- For each section show a bold section heading with its total, then each
  category with its total and individual line items (vendor, amount, date,
  source).
- After all sections, add **Other / Uncategorised** listing every
  uncategorised transaction with full details (vendor, amount, date, source,
  any available category / memo info).
- End with a **Grand Total**.
- Sort line items within each category by amount descending.

CATEGORY ORDER (use this exact sequence of sections and categories):
{chr(10).join(f'{i+1}. **{name}** — {", ".join(cats)}' for i, (name, cats) in enumerate(CATEGORY_ORDER))}

PRE-CATEGORISED DATA  (Grand total: ${grand:,.2f})

{categorised_block}

UNCATEGORISED TRANSACTIONS

{uncat_block}

Remember: first line must be `# Monthly Expense Summary — {month}`. No tables."""


# ---------------------------------------------------------------------------
# OAuth helper
# ---------------------------------------------------------------------------


def _extract_connect_url(err: AuthenticationError) -> str | None:
    body = err.body if isinstance(err.body, dict) else {}
    return body.get("connect_url") or body.get("detail", {}).get("connect_url")


def _prompt_oauth(service: str, url: str) -> None:
    print(f"\n{service} OAuth required. Opening browser…", flush=True)
    print(f"   URL: {url}", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    if sys.stdin.isatty():
        input(f"\n   Press Enter after completing {service} OAuth…")
    else:
        wait = int(os.getenv("OAUTH_WAIT_SECONDS", "30"))
        print(f"   Non-interactive — waiting {wait}s…", flush=True)
        import time
        time.sleep(wait)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    if not os.getenv("DEDALUS_API_KEY"):
        print("Error: DEDALUS_API_KEY not set. See .env")
        sys.exit(1)

    dry_run = False
    target_month = datetime.now().strftime("%Y-%m")
    output_file = OUTPUT_FILE

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--dry-run":
            dry_run = True
            i += 1
        elif args[i] == "--month" and i + 1 < len(args):
            target_month = args[i + 1]
            if len(target_month) != 7 or target_month[4] != "-":
                print(f"Invalid month format: {target_month} (expected YYYY-MM)")
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
            print("Run with --help for usage.")
            sys.exit(1)

    print("Monthly Expense Agent")
    print("=" * 55)
    print(f"  Model:   {MODEL}")
    print(f"  Month:   {target_month}")
    print(f"  Output:  {output_file}")
    print(f"  Dry run: {dry_run}")
    print("=" * 55)

    print("\n[1/3] Fetching Mercury accounts & transactions…", flush=True)
    accounts = fetch_mercury_accounts()
    print(
        f"      {len(accounts)} accounts: "
        + ", ".join(a["name"] or a["id"][:8] for a in accounts)
    )
    mercury_txns = fetch_mercury_transactions(accounts, target_month)
    print(f"      {len(mercury_txns)} outgoing Mercury transactions for {target_month}")

    print("[2/3] Fetching Ramp credit-card transactions…", flush=True)
    ramp_txns = fetch_ramp_transactions(target_month)
    print(f"      {len(ramp_txns)} Ramp credit-card transactions for {target_month}")

    print("      Fetching Ramp bill-pay (ACH) payments…", flush=True)
    ramp_bills = fetch_ramp_bills(target_month)
    print(f"      {len(ramp_bills)} Ramp bill payments for {target_month}")

    print("[3/3] Categorising…", flush=True)
    grouped, uncat_m, uncat_r = categorise(mercury_txns, ramp_txns, ramp_bills)
    for cat, txns in sorted(grouped.items()):
        total = sum(t["amount"] for t in txns)
        print(f"      {cat}: {len(txns)} txns, ${total:,.2f}")
    print(f"      Uncategorised: {len(uncat_m)} Mercury, {len(uncat_r)} Ramp")

    prompt = build_prompt(target_month, grouped, uncat_m, uncat_r)

    if dry_run:
        print("\n--- PROMPT ---")
        print(prompt)
        print("--- END PROMPT ---")
        return

    client = AsyncDedalus(timeout=TIMEOUT)
    runner = DedalusRunner(client)

    print("\nGenerating expense summary…", flush=True)
    print("(This may take a minute or two)\n", flush=True)

    async def _run():
        stream = runner.run(
            input=prompt,
            model=MODEL,
            mcp_servers=[],
            credentials=[],
            stream=True,
            max_steps=50,
        )
        return await stream_async(stream)

    try:
        result = await _run()
    except AuthenticationError as err:
        url = _extract_connect_url(err)
        if not url:
            raise
        _prompt_oauth("MCP Server", url)
        result = await _run()

    raw = result.content
    marker = "# Monthly Expense Summary"
    idx = raw.rfind(marker)
    report = raw[idx:] if idx >= 0 else raw

    if report.strip():
        with open(output_file, "w") as f:
            f.write(report)
        print(f"\nReport saved to: {output_file}")

        lines = report.strip().split("\n")
        for line in lines[:40]:
            print(f"  {line}")
        if len(lines) > 40:
            print(f"  … ({len(lines) - 40} more lines)")
    else:
        print("\nWarning: agent returned empty output.")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
