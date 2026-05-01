"""Asset Compile Agent — snapshot of balances across Mercury and Ramp.

Pulls balances from Mercury bank/treasury accounts and Ramp card statements.
For past periods, uses Mercury monthly statements (ending balances) and Ramp
billing-cycle statements. For the current period, falls back to live balances.

Usage:
    python asset_compile.py                     # current quarter
    python asset_compile.py --quarter 2026-Q1   # specific quarter
    python asset_compile.py --month 2026-03     # specific month-end
    python asset_compile.py -o report.md        # custom output path
"""

import calendar
import os
import sys
from datetime import date, datetime

import requests as _requests
from dotenv import load_dotenv

load_dotenv()

RAMP_BASE = "https://api.ramp.com/developer/v1"
MERCURY_BASE = "https://api.mercury.com/api/v1"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "total_assets.md")

QUARTER_END_MONTHS = {1: 3, 2: 6, 3: 9, 4: 12}


def _ramp_headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('RAMP_TOKEN', '')}"}


def _mercury_headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('MERCURY_TOKEN', '')}"}


def _quarter_end_date(quarter_str: str) -> date:
    year = int(quarter_str[:4])
    q = int(quarter_str[-1])
    month = QUARTER_END_MONTHS[q]
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def _month_end_date(month_str: str) -> date:
    year, month = int(month_str[:4]), int(month_str[5:7])
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def _current_quarter() -> str:
    today = date.today()
    q = (today.month - 1) // 3 + 1
    return f"{today.year}-Q{q}"


def fetch_mercury_accounts() -> list[dict]:
    headers = _mercury_headers()
    resp = _requests.get(f"{MERCURY_BASE}/accounts", headers=headers)
    resp.raise_for_status()
    results = []
    for a in resp.json().get("accounts", []):
        results.append({
            "id": a["id"],
            "name": a.get("name", ""),
            "nickname": a.get("nickname", ""),
            "kind": a.get("kind", ""),
            "status": a.get("status", ""),
            "current_balance": a.get("currentBalance", 0.0),
            "available_balance": a.get("availableBalance", 0.0),
        })
    return results


def fetch_mercury_statements(account_id: str) -> list[dict]:
    headers = _mercury_headers()
    resp = _requests.get(
        f"{MERCURY_BASE}/account/{account_id}/statements", headers=headers,
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    results = []
    for s in resp.json().get("statements", []):
        end_date = (s.get("endDate") or "")[:10]
        results.append({
            "id": s.get("id", ""),
            "start_date": (s.get("startDate") or "")[:10],
            "end_date": end_date,
            "ending_balance": s.get("endingBalance", 0.0),
        })
    return results


def _mercury_balance_at(
    account: dict, statements: list[dict], target: date,
) -> tuple[float, str]:
    target_ym = target.strftime("%Y-%m")
    for s in statements:
        if s["end_date"][:7] == target_ym:
            return s["ending_balance"], f"statement {s['end_date']}"
    return account["current_balance"], "live balance"


def fetch_mercury_treasury() -> list[dict]:
    headers = _mercury_headers()
    resp = _requests.get(f"{MERCURY_BASE}/treasury", headers=headers)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    results = []
    for a in resp.json().get("accounts", []):
        net_returns = a.get("netReturns", [])
        latest_return = net_returns[0] if net_returns else {}
        dividends = latest_return.get("dividends", [])
        fund_names = [
            d.get("securityName", "")
            for d in dividends if d.get("amount", 0) > 0
        ]
        results.append({
            "id": a["id"],
            "status": a.get("status", ""),
            "current_balance": a.get("currentBalance", 0.0),
            "available_balance": a.get("availableBalance", 0.0),
            "latest_net_return": latest_return.get("netAmount", 0.0),
            "latest_return_month": latest_return.get("month", ""),
            "fund_names": fund_names,
            "net_returns": net_returns,
        })
    return results


def _treasury_balance_at(
    treasury: dict, target: date,
) -> tuple[float, str]:
    return treasury["current_balance"], "live balance (no historical API)"


def fetch_ramp_statements() -> list[dict]:
    headers = _ramp_headers()
    results = []
    cursor = None
    for _ in range(50):
        params: dict = {"page_size": 100}
        if cursor:
            params["start"] = cursor
        resp = _requests.get(
            f"{RAMP_BASE}/statements", headers=headers, params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        for s in data.get("data", []):
            rate = s.get("ending_balance", {}).get("minor_unit_conversion_rate", 100)
            results.append({
                "id": s.get("id", ""),
                "start_date": (s.get("start_date") or "")[:10],
                "end_date": (s.get("end_date") or "")[:10],
                "ending_balance": s.get("ending_balance", {}).get("amount", 0) / rate,
                "opening_balance": s.get("opening_balance", {}).get("amount", 0) / rate,
                "charges": s.get("charges", {}).get("amount", 0) / rate,
                "payments": s.get("payments", {}).get("amount", 0) / rate,
                "credits": s.get("credits", {}).get("amount", 0) / rate,
            })
        next_url = (data.get("page") or {}).get("next")
        if next_url and "start=" in next_url:
            cursor = next_url.split("start=")[-1].split("&")[0]
        else:
            break
    return results


def _ramp_balance_at(
    statements: list[dict], target: date,
) -> tuple[dict | None, str]:
    target_str = target.isoformat()
    for s in statements:
        if s["start_date"] <= target_str <= s["end_date"]:
            return s, f"statement {s['start_date']} to {s['end_date']}"
    before = [s for s in statements if s["end_date"] <= target_str]
    if before:
        best = max(before, key=lambda s: s["end_date"])
        return best, f"nearest statement ending {best['end_date']}"
    return None, "no matching statement"


def fetch_ramp_live_balance() -> dict:
    headers = _ramp_headers()
    resp = _requests.get(f"{RAMP_BASE}/business/balance", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return {
        "card_balance_excluding_pending": data.get("card_balance_excluding_pending", 0),
        "card_balance_including_pending": data.get("card_balance_including_pending", 0),
        "statement_balance": data.get("statement_balance", 0),
        "card_limit": data.get("card_limit", 0),
        "available_card_limit": data.get("available_card_limit", 0),
        "next_billing_date": data.get("next_billing_date", ""),
        "prev_billing_date": data.get("prev_billing_date", ""),
    }


def build_report(
    label: str,
    target_date: date,
    mercury_balances: list[dict],
    mercury_treasury_balances: list[dict],
    ramp_info: dict,
) -> str:
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    is_historical = target_date < date.today()
    date_qualifier = "end of" if is_historical else "as of"

    lines.append(f"# Total Assets — {label} ({date_qualifier} {target_date})")
    lines.append("")
    lines.append(f"*Report generated: {now}*")
    lines.append("")

    mercury_bank_total = sum(b["balance"] for b in mercury_balances)

    lines.append("## **Mercury Bank Accounts**")
    lines.append("")

    for b in sorted(mercury_balances, key=lambda x: -x["balance"]):
        if b["status"] != "active" and b["balance"] == 0:
            continue
        source_tag = f" *({b['source']})*" if "statement" not in b["source"] else ""
        lines.append(
            f"- {b['display_name']}: **${b['balance']:,.2f}** "
            f"({b['kind']}){source_tag}"
        )

    lines.append("")
    lines.append(f"**Mercury Bank Total: ${mercury_bank_total:,.2f}**")
    lines.append("")

    mercury_treasury_total = sum(b["balance"] for b in mercury_treasury_balances)

    if mercury_treasury_balances:
        lines.append("## **Mercury Treasury**")
        lines.append("")
        for b in mercury_treasury_balances:
            lines.append(f"- Treasury Account: **${b['balance']:,.2f}**")
            source_tag = f" *({b['source']})*" if "live" in b["source"] else ""
            if source_tag:
                lines.append(f"  - {b['source']}")
            if b.get("fund_names"):
                for fn in b["fund_names"]:
                    lines.append(f"  - {fn}")
            if b.get("net_return"):
                lines.append(
                    f"  - Net return ({b['return_month']}): ${b['net_return']:,.2f}"
                )
        lines.append("")
        lines.append(f"**Mercury Treasury Total: ${mercury_treasury_total:,.2f}**")
        lines.append("")

    lines.append("## **Ramp Card Balance (Liability)**")
    lines.append("")
    ramp_source = ramp_info.get("source", "")
    ramp_balance = ramp_info.get("ending_balance", 0)

    if ramp_info.get("is_statement"):
        stmt = ramp_info
        lines.append(f"- Statement Balance: **${stmt['ending_balance']:,.2f}**")
        lines.append(f"  - Cycle: {stmt['start_date']} to {stmt['end_date']}")
        lines.append(f"  - Charges: ${stmt['charges']:,.2f}")
        lines.append(f"  - Payments: ${stmt['payments']:,.2f}")
        if stmt["credits"] > 0:
            lines.append(f"  - Credits: ${stmt['credits']:,.2f}")
    else:
        live = ramp_info.get("live_balance", {})
        ramp_balance = live.get("statement_balance", 0)
        lines.append(f"- Statement Balance: **${ramp_balance:,.2f}**")
        lines.append(
            f"- Balance Including Pending: "
            f"${live.get('card_balance_including_pending', 0):,.2f}"
        )
        lines.append(f"- Card Limit: ${live.get('card_limit', 0):,.2f}")
        billing = ""
        if live.get("prev_billing_date"):
            billing += f"prev: {live['prev_billing_date']}"
        if live.get("next_billing_date"):
            billing += f", next: {live['next_billing_date']}"
        if billing:
            lines.append(f"- Billing cycle: {billing}")
        lines.append(f"  - *Source: live balance (current cycle not yet closed)*")

    lines.append(f"  - *{ramp_source}*")
    lines.append("")

    total_cash = mercury_bank_total + mercury_treasury_total

    lines.append("---")
    lines.append("")
    lines.append("## **Summary**")
    lines.append("")
    lines.append("| | Balance |")
    lines.append("|---|---|")
    lines.append(f"| Mercury Bank Accounts | ${mercury_bank_total:,.2f} |")
    lines.append(f"| Mercury Treasury | ${mercury_treasury_total:,.2f} |")
    lines.append(f"| **Total Cash & Investments** | **${total_cash:,.2f}** |")
    lines.append(
        f"| Ramp Card Balance (liability) | (${ramp_balance:,.2f}) |"
    )
    lines.append(
        f"| **Net Position** | **${total_cash - ramp_balance:,.2f}** |"
    )
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    quarter = None
    month = None
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
        elif args[i] == "--month" and i + 1 < len(args):
            month = args[i + 1]
            if len(month) != 7 or month[4] != "-":
                print(f"Invalid month format: {month} (expected YYYY-MM)")
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

    if month:
        target_date = _month_end_date(month)
        label = f"{month} Month-End"
    elif quarter:
        target_date = _quarter_end_date(quarter)
        label = quarter
    else:
        quarter = _current_quarter()
        target_date = _quarter_end_date(quarter)
        label = quarter

    is_historical = target_date < date.today()

    print("Asset Compile Agent")
    print("=" * 55)
    print(f"  Period:      {label}")
    print(f"  Target date: {target_date}")
    print(f"  Mode:        {'historical (statements)' if is_historical else 'live balances'}")
    print(f"  Output:      {output_file}")
    print("=" * 55)

    print("\n[1/4] Fetching Mercury bank accounts...", flush=True)
    mercury_accounts = fetch_mercury_accounts()
    print(f"      {len(mercury_accounts)} accounts found")

    mercury_balances: list[dict] = []
    if is_historical:
        print("      Fetching historical statements...", flush=True)
        for acct in mercury_accounts:
            stmts = fetch_mercury_statements(acct["id"])
            balance, source = _mercury_balance_at(acct, stmts, target_date)
            display_name = acct["nickname"] or acct["name"]
            acct_suffix = acct["name"].split("••")[-1] if "••" in acct["name"] else ""
            if acct_suffix and acct["nickname"]:
                display_name = f"{acct['nickname']} (••{acct_suffix})"
            mercury_balances.append({
                "display_name": display_name,
                "balance": balance,
                "source": source,
                "kind": acct["kind"],
                "status": acct["status"],
            })
            if balance > 0:
                print(f"        {display_name}: ${balance:,.2f} ({source})")
    else:
        for acct in mercury_accounts:
            display_name = acct["nickname"] or acct["name"]
            acct_suffix = acct["name"].split("••")[-1] if "••" in acct["name"] else ""
            if acct_suffix and acct["nickname"]:
                display_name = f"{acct['nickname']} (••{acct_suffix})"
            mercury_balances.append({
                "display_name": display_name,
                "balance": acct["current_balance"],
                "source": "live balance",
                "kind": acct["kind"],
                "status": acct["status"],
            })

    bank_total = sum(
        b["balance"] for b in mercury_balances if b["status"] == "active"
    )
    print(f"      Mercury Bank Total: ${bank_total:,.2f}")

    print("[2/4] Fetching Mercury treasury...", flush=True)
    mercury_treasury = fetch_mercury_treasury()
    mercury_treasury_balances: list[dict] = []
    for t in mercury_treasury:
        balance, source = _treasury_balance_at(t, target_date)
        mercury_treasury_balances.append({
            "balance": balance,
            "source": source,
            "fund_names": t.get("fund_names", []),
            "net_return": t.get("latest_net_return", 0),
            "return_month": (t.get("latest_return_month") or "")[:7],
        })
    treasury_total = sum(b["balance"] for b in mercury_treasury_balances)
    print(f"      Mercury Treasury Total: ${treasury_total:,.2f}")

    print("[3/4] Fetching Ramp statements...", flush=True)
    ramp_statements = fetch_ramp_statements()
    print(f"      {len(ramp_statements)} Ramp statements found")

    ramp_info: dict
    stmt_match, stmt_source = _ramp_balance_at(ramp_statements, target_date)
    if stmt_match and is_historical:
        ramp_info = {
            **stmt_match,
            "is_statement": True,
            "source": stmt_source,
        }
        print(f"      Using {stmt_source}: ${stmt_match['ending_balance']:,.2f}")
    else:
        print("      Using live balance (current cycle)...", flush=True)
        live = fetch_ramp_live_balance()
        ramp_info = {
            "is_statement": False,
            "live_balance": live,
            "ending_balance": live["statement_balance"],
            "source": "live balance - current billing cycle not yet closed",
        }
        print(f"      Statement balance: ${live['statement_balance']:,.2f}")

    print("[4/4] Building report...", flush=True)

    report = build_report(
        label, target_date,
        mercury_balances, mercury_treasury_balances,
        ramp_info,
    )

    with open(output_file, "w") as f:
        f.write(report)
    print(f"\nReport saved to: {output_file}")

    for line in report.strip().split("\n"):
        print(f"  {line}")

    print("\nDone.")


if __name__ == "__main__":
    main()
