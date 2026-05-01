# Accounting Agent

A suite of Python agents that pull transaction data from **Ramp** and **Mercury** APIs to generate financial reports — expense summaries, quarterly metrics, budget breakdowns, asset snapshots, and subscription audits.

## Agents

| Agent | Description | Output |
|-------|-------------|--------|
| `monthly_expense.py` | Categorises monthly expenses from Ramp (credit card + bill pay) and Mercury (ACH) into a structured summary | `expense_summary.md` |
| `quarterly-calculation.py` | Computes quarterly Net Burn, Revenue, EBITDA, and Net Income with monthly breakdown and a categorised budget summary | `quarter-metric.md` + `budget-summary.md` |
| `asset_compile.py` | Snapshots balances across Mercury bank/treasury and Ramp card statements at quarter-end or month-end (supports historical via statements) | `total_assets.md` |
| `subscription_listing.py` | Identifies recurring vendor subscriptions, verifies pricing via Brave Search, and flags service overlaps | `subscription_report.md` |

## Setup

### 1. Install dependencies

```bash
pip install -r templates/requirements.txt
```

### 2. Configure credentials

Copy the example environment file and fill in your API keys:

```bash
cp templates/.env.example .env
```

You'll need:
- **Mercury API token** — generate from Mercury Dashboard → Settings → API Tokens
- **Ramp API credentials** — create an OAuth app at [Ramp Developer Portal](https://docs.ramp.com/developer-api/v1)
  - Scopes needed: `transactions:read`, `bills:read`, `statements:read`, `business:read`
  - Generate a token:
    ```python
    import base64, requests
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = requests.post(
        "https://api.ramp.com/developer/v1/token",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "transactions:read bills:read statements:read business:read"}
    )
    print(response.json()["access_token"])
    ```
- **Dedalus API key** — required for `monthly_expense.py` and `subscription_listing.py` (LLM-powered agents)

### 3. Customise vendor mappings

Copy the templates and add your company-specific vendor categorisation rules:

```bash
cp templates/monthly_expense.template.py monthly_expense.py
cp templates/quarterly-calculation.template.py quarterly-calculation.py
cp templates/asset_compile.template.py asset_compile.py
cp templates/subscription_listing.template.py subscription_listing.py
```

Edit the `MERCURY_VENDOR_CATEGORIES`, `RAMP_VENDOR_CATEGORIES`, and similar mapping dictionaries to match your vendors. The QuickBooks and Ramp auto-category mappings work out of the box.

## Usage

```bash
# Monthly expense summary for April 2026
python monthly_expense.py --month 2026-04

# Quarterly metrics + budget summary
python quarterly-calculation.py --quarter 2026-Q1

# Asset snapshot (current quarter)
python asset_compile.py

# Historical asset snapshot
python asset_compile.py --month 2026-03

# Subscription audit
python subscription_listing.py
```

All agents support `--help` for full usage details.

## How it works

### Data sources

- **Mercury** — bank account transactions via `/account/{id}/transactions`, historical balances via `/account/{id}/statements`, treasury via `/treasury`
- **Ramp** — credit card transactions via `/transactions`, bill payments via `/bills`, card statements via `/statements`, business balance via `/business/balance`

### Transfer filtering

All agents automatically exclude:
- Internal transfers between Mercury accounts (`internalTransfer`)
- Treasury transfers between Mercury treasury and checking (`treasuryTransfer`)
- Transfers between Mercury and Ramp (counterparty = "RAMP")

### Categorisation

Expenses are categorised using a three-tier matching system:
1. **Vendor name matching** — direct substring match against Mercury counterparty or Ramp merchant names
2. **QuickBooks category matching** — matches against Ramp's `accounting_field_selections` (QB expense categories)
3. **Ramp auto-category fallback** — uses Ramp's `sk_category_name` when QB categories aren't set

## Architecture

```
templates/
├── .env.example                          # Environment variable template
├── requirements.txt                      # Python dependencies
├── monthly_expense.template.py           # Monthly expense agent
├── quarterly-calculation.template.py     # Quarterly metrics + budget agent
├── asset_compile.template.py             # Asset balance snapshot agent
└── subscription_listing.template.py      # Subscription audit agent
```

The LLM-powered agents (`monthly_expense.py`, `subscription_listing.py`) use the [Dedalus Labs](https://dedalus.dev) SDK to run an LLM that formats the pre-categorised data into polished reports. The other agents (`quarterly-calculation.py`, `asset_compile.py`) generate reports directly in Python without an LLM.

## License

MIT
