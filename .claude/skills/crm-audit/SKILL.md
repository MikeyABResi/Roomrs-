---
name: crm-audit
description: Run the CRM room availability audit. Use this skill whenever the user mentions "audit", "room availability", "availability report", "check rooms", "CRM audit", "status check", "run the audit", or anything about verifying that room statuses are correct in the CRM. Also trigger when someone asks about rooms that should be available, not available, or in process — or wants to find data inconsistencies in room/deal status.
---

# CRM Availability Audit

This skill runs the daily room availability audit against the Roomrs Zoho CRM. It checks all rooms with `Operation_Status = "Available"` and flags inconsistencies between room Sales Status and active deal data.

## What it checks

1. **Should be Available** — Room has a Moved-In deal with a confirmed Move Out date, but Sales Status isn't "Available"
2. **Should be Not Available** — Room has a Moved-In deal with no Move Out date, but Sales Status is "Available"
3. **Should be In Process** — Room has an early-stage deal (Application, Qualified, or Lease Sent) but Sales Status isn't "In Process"
4. **Occupied but no deal** — Room shows as Occupied but has no Moved-In deal (data inconsistency)

## How to run

Run the audit script from the repo root. It requires three environment variables for Zoho CRM API access.

```bash
cd /Users/mikelazougui/Desktop/Roomrs

# Load credentials from .env if available
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

python3 scripts/crm_availability_audit.py
```

The script will print the full audit report to stdout. If `SLACK_WEBHOOK_URL` is set, it also posts to the #sale-team Slack channel.

## Required environment variables

These must be set before running (either exported in the shell or in a `.env` file at the repo root):

- `ZOHO_CLIENT_ID` — Zoho API client ID for the Roomrs account
- `ZOHO_CLIENT_SECRET` — Zoho API client secret
- `ZOHO_CRM_REFRESH_TOKEN` — Zoho OAuth refresh token with `ZohoCRM.modules.ALL` and `ZohoCRM.coql.READ` scopes

## Output

The report lists flagged rooms grouped by rule, with room name, current status, expected status, and tenant/deal name. A summary count appears at the top.
