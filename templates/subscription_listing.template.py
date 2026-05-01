"""Subscription Listing Agent — find recurring subscriptions across Ramp and Mercury.

Pulls historic transactions from Ramp and Mercury, identifies recurring
monthly and yearly vendor subscriptions, verifies pricing against public
information via Brave Search, and flags vendors offering overlapping services.

Pre-fetches the Ramp merchant list (filtered by category) before running the
LLM agent, so the agent can focus on transaction matching, Brave Search, and
report generation instead of burning steps on merchant pagination.

Usage:
    python subscription_listing.py
    python subscription_listing.py --dry-run          # preview prompt only
    python subscription_listing.py --no-search        # skip Brave verification
    python subscription_listing.py --max-pages 10     # limit pagination depth
"""

import asyncio
import os
import sys
import webbrowser
from datetime import datetime

import requests as _requests
from dedalus_labs import AsyncDedalus, AuthenticationError, DedalusRunner
from dedalus_labs.utils.stream import stream_async
from dotenv import load_dotenv

load_dotenv()

from connections import mercury_secrets, ramp_secrets  # noqa: F401 — kept for --no-search fallback

MERCURY_MCP = os.getenv("MERCURY_MCP_SERVER", "your-org/mercury-mcp")
RAMP_MCP = os.getenv("RAMP_MCP_SERVER", "your-org/ramp-mcp")
SEARCH_MCP = os.getenv("SEARCH_MCP_SERVER", "your-org/brave-search-mcp")
MODEL = os.getenv("MODEL", "anthropic/claude-sonnet-4-20250514")
TIMEOUT = int(os.getenv("AGENT_TIMEOUT", "1200"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "subscription_report.md")

RAMP_BASE = "https://api.ramp.com/developer/v1"
MERCURY_BASE = "https://api.mercury.com/api/v1"

MERCHANT_CATEGORIES = [
    "SaaS / Software",
    "Cloud Computing",
    "Professional Services",
    "Education",
    "Books and Newspapers",
    "Clubs and memberships",
    "Insurance",
    "Office",
    "Office supplies and cleaning",
    "Internet and Phone",
    "Streaming Services",
]

# Merchants to exclude from subscription detection.
# Add vendors you track separately (e.g. LLM API providers billed by usage)
# so they don't clutter the subscription report.
#
# EXCLUDED_MERCHANTS = {
#     "anthropic", "openai", "cohere", "mistral",
#     "google ai", "together.xyz", "fireworks.ai",
# }
EXCLUDED_MERCHANTS: set[str] = set()

EXCLUDED_CATEGORIES_RAMP = {
    "Restaurants", "Supermarkets and Grocery Stores", "Alcohol and Bars",
    "Fuel and Gas", "Airlines", "Taxi and Rideshare", "Travel Misc",
    "Lodging", "Parking", "Fines", "Clothing", "General Merchandise",
    "Electronics", "Charitable donations", "Medical", "Shipping",
    "Government Services", "Entertainment", "Taxes and tax preparation",
    "Fees and Financial institutions", "Advertising", "Other",
}


def _ramp_headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('RAMP_TOKEN', '')}"}


def _mercury_headers() -> dict:
    return {"Authorization": f"Bearer {os.getenv('MERCURY_TOKEN', '')}"}


def _paginate_ramp(path: str, max_pages: int = 30, limit: int = 100) -> list[dict]:
    headers = _ramp_headers()
    results: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params: dict = {"page_size": limit}
        if cursor:
            params["start"] = cursor
        resp = _requests.get(f"{RAMP_BASE}{path}", headers=headers, params=params)
        data = resp.json()
        results.extend(data.get("data", []))
        next_url = data.get("page", {}).get("next")
        if next_url and "start=" in next_url:
            cursor = next_url.split("start=")[-1].split("&")[0]
        else:
            break
    return results


def fetch_ramp_merchants() -> list[dict]:
    """Fetch merchants filtered to subscription-relevant categories."""
    raw = _paginate_ramp("/merchants")
    allowed_cats = set(MERCHANT_CATEGORIES)
    seen: set[str] = set()
    merchants: list[dict] = []
    for m in raw:
        cat = m.get("sk_category_name", "")
        name = m.get("merchant_name", "")
        if cat not in allowed_cats:
            continue
        if name.lower() in EXCLUDED_MERCHANTS or name.lower() in seen:
            continue
        seen.add(name.lower())
        merchants.append({"name": name, "category": cat, "id": m.get("id", "")})
    return sorted(merchants, key=lambda x: x["name"])


def fetch_ramp_transactions(max_pages: int = 20) -> list[dict]:
    """Fetch all Ramp transactions with pagination."""
    raw = _paginate_ramp("/transactions", max_pages=max_pages, limit=100)
    txns: list[dict] = []
    for t in raw:
        name = t.get("merchant_name", "")
        holder = t.get("card_holder", {}) or {}
        emp = f"{holder.get('first_name', '')} {holder.get('last_name', '')}".strip()
        txns.append({
            "merchant": name,
            "amount": t.get("amount", 0),
            "date": (t.get("user_transaction_time") or "")[:10],
            "employee": emp or "Unknown",
            "category": t.get("sk_category_name", ""),
        })
    return txns


def fetch_mercury_transactions() -> list[dict]:
    """Fetch Mercury transactions across all accounts."""
    headers = _mercury_headers()
    resp = _requests.get(f"{MERCURY_BASE}/accounts", headers=headers)
    accounts = resp.json().get("accounts", [])
    txns: list[dict] = []
    for acct in accounts:
        aid = acct.get("id", "")
        offset = 0
        while True:
            r = _requests.get(
                f"{MERCURY_BASE}/account/{aid}/transactions",
                headers=headers,
                params={"limit": 500, "offset": offset},
            )
            batch = r.json().get("transactions", [])
            for t in batch:
                txns.append({
                    "merchant": t.get("counterpartyName", ""),
                    "amount": abs(t.get("amount", 0)),
                    "date": (t.get("postedAt") or t.get("createdAt") or "")[:10],
                    "employee": "System",
                    "source": "Mercury",
                })
            if len(batch) < 500:
                break
            offset += 500
    return txns


def match_merchants_to_transactions(
    merchants: list[dict],
    ramp_txns: list[dict],
    mercury_txns: list[dict],
) -> list[dict]:
    """Match merchants against transactions, compute subscription signals."""
    merchant_names = {m["name"].lower(): m for m in merchants}

    vendor_data: dict[str, dict] = {}
    for m in merchants:
        vendor_data[m["name"].lower()] = {
            "name": m["name"],
            "category": m["category"],
            "charges": [],
            "employees": set(),
            "sources": set(),
        }

    for t in ramp_txns:
        key = t["merchant"].lower()
        if key in vendor_data:
            vendor_data[key]["charges"].append({
                "amount": t["amount"],
                "date": t["date"],
            })
            vendor_data[key]["employees"].add(t["employee"])
            vendor_data[key]["sources"].add("Ramp")

    for t in mercury_txns:
        key = t["merchant"].lower()
        if key in vendor_data:
            vendor_data[key]["charges"].append({
                "amount": t["amount"],
                "date": t["date"],
            })
            vendor_data[key]["employees"].add(t["employee"])
            vendor_data[key]["sources"].add("Mercury")
        elif key and key not in EXCLUDED_MERCHANTS and len(key) >= 5:
            for mkey, mdata in vendor_data.items():
                if mkey == key:
                    mdata["charges"].append({
                        "amount": t["amount"],
                        "date": t["date"],
                    })
                    mdata["employees"].add(t["employee"])
                    mdata["sources"].add("Mercury")
                    break

    results: list[dict] = []
    for key, v in vendor_data.items():
        charges = sorted(v["charges"], key=lambda c: c["date"])
        if not charges:
            continue
        months = sorted(set(c["date"][:7] for c in charges if c["date"]))
        amounts = [c["amount"] for c in charges]
        avg = sum(amounts) / len(amounts) if amounts else 0

        if len(months) >= 2:
            freq = "Monthly"
        elif len(charges) == 1 and avg >= 50:
            freq = "Possible annual"
        else:
            freq = "Single/unknown"

        results.append({
            "name": v["name"],
            "category": v["category"],
            "frequency": freq,
            "charge_count": len(charges),
            "months_active": len(months),
            "avg_amount": round(avg, 2),
            "total": round(sum(amounts), 2),
            "date_range": f"{charges[0]['date']} to {charges[-1]['date']}",
            "employees": ", ".join(sorted(v["employees"])),
            "sources": ", ".join(sorted(v["sources"])),
            "charges_detail": "; ".join(
                f"${c['amount']:.2f} ({c['date']})" for c in charges
            ),
        })

    return sorted(results, key=lambda x: -x["total"])


def build_prompt(matched_vendors: list[dict], use_search: bool = True) -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    vendor_block = "\n".join(
        f"- **{v['name']}** [{v['category']}] | {v['frequency']} | "
        f"{v['charge_count']} charges over {v['months_active']} months | "
        f"avg ${v['avg_amount']:.2f} | total ${v['total']:.2f} | "
        f"{v['date_range']} | by: {v['employees']} | source: {v['sources']}\n"
        f"  Charges: {v['charges_detail']}"
        for v in matched_vendors
    )

    search_step = ""
    if use_search:
        search_step = """
### Step 1: Verify pricing with web search (Brave Search)

For EACH vendor listed above, search Brave:
- Query: "[vendor name] pricing plans"
- Record public pricing tiers found
- Compare the actual charge amounts to public pricing
- Mark as:
  - **Verified** — charge matches a known public plan
  - **Likely enterprise/custom** — charge doesn't match public tiers
  - **Usage-based** — vendor bills by consumption (note this but still include)
  - **Unverified** — can't find pricing info
"""

    return f"""You are a subscription auditing agent. All transaction data has been
pre-computed and matched below. Your job is to verify pricing via web search,
flag service overlaps, and produce the final report.

**Today's date: {today}**

## Pre-matched vendor subscription data ({len(matched_vendors)} vendors with charges)

Each entry shows the vendor, category, frequency, charge count, amounts, date
range, employees who charged, and source platform. This data is already filtered
to exclude food, rideshare, airlines, travel, LLM providers, Cursor, Anthropic,
and legal services.

{vendor_block}

## Steps
{search_step}
### Step {"2" if use_search else "1"}: Flag vendors with overlapping services

Group vendors by service category and suggest consolidation where applicable.

### Step {"3" if use_search else "2"}: Output the report

The FIRST line of your output MUST be `# Subscription Audit Report`.
No preamble, no thinking — go straight to the heading.

# Subscription Audit Report

**Generated:** {today}
**Sources:** Ramp, Mercury
**Transaction coverage:** [{matched_vendors[0]['date_range'].split(' to ')[0] if matched_vendors else 'N/A'}] to [{matched_vendors[0]['date_range'].split(' to ')[-1] if matched_vendors else 'N/A'}]

## Summary

| Metric | Value |
|--------|-------|
| Total subscriptions found | [N] |
| Monthly subscriptions | [N] |
| Annual / possible annual | [N] |
| Estimated total annual spend | $[amount] |
| Overlap groups found | [N] |

## Monthly Subscriptions

| Vendor | Source | Monthly Amount | Annual Cost | Public Price | Verification | Charged By | CFO Comments |
|--------|--------|---------------|-------------|-------------|--------------|------------|--------------|
| ... | ... | ... | ... | ... | ... | ... | |

## Annual / Possible Annual Subscriptions

| Vendor | Source | Amount | Public Price | Verification | Last Charge | Charged By | Notes | CFO Comments |
|--------|--------|--------|-------------|--------------|-------------|------------|-------|--------------|
| ... | ... | ... | ... | ... | ... | ... | ... | |

## Service Overlap Analysis

### [Category Name]
- **Vendors:** [list]
- **Combined annual cost:** $[amount]
- **Recommendation:** [keep both / consolidate to X / review]

## Notes
- [Important observations]

## Rules
- Include EVERY vendor from the pre-matched data — do not skip any.
- Sort tables by annual cost descending.
- Use exact dollar amounts from the pre-computed data.
- The first line of output MUST be `# Subscription Audit Report`."""


def _extract_connect_url(err: AuthenticationError) -> str | None:
    body = err.body if isinstance(err.body, dict) else {}
    return body.get("connect_url") or body.get("detail", {}).get("connect_url")


def _prompt_oauth(service: str, url: str) -> None:
    print(f"\n{service} OAuth required. Opening browser...", flush=True)
    print(f"   URL: {url}", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    if sys.stdin.isatty():
        input(f"\n   Press Enter after completing {service} OAuth...")
    else:
        wait = int(os.getenv("OAUTH_WAIT_SECONDS", "30"))
        print(f"   Non-interactive — waiting {wait}s for OAuth...", flush=True)
        import time
        time.sleep(wait)


async def main() -> None:
    if not os.getenv("DEDALUS_API_KEY"):
        print("Error: DEDALUS_API_KEY not set. See .env")
        sys.exit(1)

    use_search = True
    dry_run = False
    max_pages = MAX_PAGES
    output_file = OUTPUT_FILE

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--dry-run":
            dry_run = True
            i += 1
        elif args[i] == "--no-search":
            use_search = False
            i += 1
        elif args[i] == "--max-pages" and i + 1 < len(args):
            max_pages = int(args[i + 1])
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

    mcp_servers = []
    credentials = []
    if use_search:
        mcp_servers.append(SEARCH_MCP)

    print("Subscription Listing Agent")
    print("=" * 55)
    print(f"  Model:      {MODEL}")
    print(f"  MCP:        {', '.join(mcp_servers)}")
    print(f"  Search:     {SEARCH_MCP if use_search else 'disabled'}")
    print(f"  Max pages:  {max_pages}")
    print(f"  Output:     {output_file}")
    print(f"  Dry run:    {dry_run}")
    print("=" * 55)

    # --- Pre-fetch and match data ---
    print("\n[1/4] Fetching Ramp merchants by category...", flush=True)
    merchants = fetch_ramp_merchants()
    print(f"      {len(merchants)} merchants across {len(MERCHANT_CATEGORIES)} categories")

    print("[2/4] Fetching Ramp transactions...", flush=True)
    ramp_txns = fetch_ramp_transactions(max_pages=max_pages)
    print(f"      {len(ramp_txns)} transactions")

    print("[3/4] Fetching Mercury transactions...", flush=True)
    mercury_txns = fetch_mercury_transactions()
    print(f"      {len(mercury_txns)} transactions")

    print("[4/4] Matching merchants to transactions...", flush=True)
    matched = match_merchants_to_transactions(merchants, ramp_txns, mercury_txns)
    print(f"      {len(matched)} vendors with charges found")
    for v in matched:
        print(f"      - {v['name']}: {v['charge_count']} charges, "
              f"${v['total']:.2f} total, {v['frequency']}, by {v['employees']}")

    if not matched:
        print("\nNo subscription vendors found. Exiting.")
        return

    prompt = build_prompt(matched, use_search=use_search)

    if dry_run:
        print("\n--- PROMPT ---")
        print(prompt)
        print("--- END PROMPT ---")
        return

    client = AsyncDedalus(timeout=TIMEOUT)
    runner = DedalusRunner(client)

    print("\nRunning subscription analysis...", flush=True)
    print("(This may take several minutes to paginate through all transactions)\n",
          flush=True)

    async def _run():
        stream = runner.run(
            input=prompt,
            model=MODEL,
            mcp_servers=mcp_servers,
            credentials=credentials,
            stream=True,
            max_steps=200,
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

    report = raw
    marker = "# Subscription Audit Report"
    idx = raw.rfind(marker)
    if idx > 0:
        report = raw[idx:]

    if report.strip():
        with open(output_file, "w") as f:
            f.write(report)
        print(f"\nReport saved to: {output_file}")

        lines = report.strip().split("\n")
        for line in lines[:30]:
            print(f"  {line}")
        if len(lines) > 30:
            print(f"  ... ({len(lines) - 30} more lines)")
    else:
        print("\nWarning: agent returned empty output.")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
