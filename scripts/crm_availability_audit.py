#!/usr/bin/env python3
"""
CRM Availability Audit — Daily room status consistency check.

Queries Zoho CRM for all rooms with Operation_Status = "Available",
cross-references with active deals, and flags inconsistencies.
Sends report to Slack #sale-team and email.
"""

import os
import sys
import json
import requests
from datetime import date

# ── Zoho OAuth ──────────────────────────────────────────────────────

ZOHO_CLIENT_ID = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_CRM_REFRESH_TOKEN = os.environ["ZOHO_CRM_REFRESH_TOKEN"]
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "mikey@roorms.com")

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
ZOHO_CRM_API = "https://www.zohoapis.com/crm/v2"
ZOHO_COQL_URL = f"{ZOHO_CRM_API}/coql"


def get_access_token():
    resp = requests.post(ZOHO_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "refresh_token": ZOHO_CRM_REFRESH_TOKEN,
    })
    resp.raise_for_status()
    return resp.json()["access_token"]


def coql_query(token, query):
    """Execute a COQL query and return list of records."""
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    resp = requests.post(ZOHO_COQL_URL, headers=headers, json={"select_query": query})
    if resp.status_code == 204:
        return []
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", [])


def coql_query_paginated(token, base_query, order_field="Name"):
    """Execute a COQL query with pagination (200 per page)."""
    all_records = []
    offset = 0
    while True:
        q = f"{base_query} limit 200" + (f" offset {offset}" if offset else "")
        records = coql_query(token, q)
        all_records.extend(records)
        if len(records) < 200:
            break
        offset += 200
    return all_records


# ── Audit Logic ─────────────────────────────────────────────────────

def run_audit():
    token = get_access_token()
    today = date.today().isoformat()  # YYYY-MM-DD

    # Step 1: Fetch all Available rooms
    print("Fetching Available rooms...")
    rooms = coql_query_paginated(
        token,
        "select id, Name, Sales_Status, Status, Next_Vacancy_date, Building, Unit "
        "from Rooms where Operation_Status = 'Available' order by Name asc"
    )
    print(f"  → {len(rooms)} rooms")

    # Step 2: Fetch all Moved-In deals
    print("Fetching Moved-In deals...")
    moved_in_deals = coql_query_paginated(
        token,
        "select Deal_Name, Stage, Room, New_Move_out_Date2, Move_in_date, Move_out_date, id "
        "from Deals where Stage = 'Moved In' and Room is not null order by Deal_Name asc"
    )
    print(f"  → {len(moved_in_deals)} Moved-In deals")

    # Step 3: Fetch early-stage open deals
    print("Fetching early-stage deals...")
    early_deals = coql_query_paginated(
        token,
        "select Deal_Name, Stage, Room, id "
        "from Deals where Stage in ('Application', 'Qualified', 'Lease Sent') "
        "and Room is not null order by Deal_Name asc"
    )
    print(f"  → {len(early_deals)} early-stage deals")

    # Build mappings: room_id → deals
    mi_by_room = {}  # room_id → list of Moved-In deals
    for d in moved_in_deals:
        rid = d.get("Room", {}).get("id") if isinstance(d.get("Room"), dict) else None
        if rid:
            mi_by_room.setdefault(rid, []).append(d)

    early_by_room = {}  # room_id → list of early-stage deals
    for d in early_deals:
        rid = d.get("Room", {}).get("id") if isinstance(d.get("Room"), dict) else None
        if rid:
            early_by_room.setdefault(rid, []).append(d)

    # Step 4: Apply audit rules
    rule1 = []  # Should be Available
    rule2 = []  # Should be Not Available
    rule3 = []  # Should be In Process
    rule4 = []  # Occupied but no deal

    for room in rooms:
        room_id = room["id"]
        room_name = room.get("Name", "Unknown")
        sales_status = room.get("Sales_Status", "")
        occ_status = room.get("Status", "")
        mi_deals = mi_by_room.get(room_id, [])
        op_deals = early_by_room.get(room_id, [])

        if mi_deals:
            # Check move-out (New_Move_out_Date2 ONLY — NOT Move_out_date)
            has_move_out = False
            for d in mi_deals:
                mo = d.get("New_Move_out_Date2")
                if mo and mo >= today:
                    has_move_out = True
                    if sales_status != "Available":
                        rule1.append({
                            "room": room_name,
                            "current": sales_status,
                            "deal": d.get("Deal_Name", ""),
                            "move_out": mo,
                        })
                    break

            if not has_move_out:
                # No move-out — check if any deal truly has no move-out
                no_mo_deal = None
                for d in mi_deals:
                    if not d.get("New_Move_out_Date2"):
                        no_mo_deal = d
                        break
                if no_mo_deal and sales_status == "Available":
                    rule2.append({
                        "room": room_name,
                        "current": sales_status,
                        "deal": no_mo_deal.get("Deal_Name", ""),
                    })

        elif op_deals:
            # No Moved-In deal, but has early-stage deal
            d = op_deals[0]
            if sales_status != "In Process":
                rule3.append({
                    "room": room_name,
                    "current": sales_status,
                    "deal": d.get("Deal_Name", ""),
                    "stage": d.get("Stage", ""),
                })

        else:
            # No deals at all
            if occ_status == "Occupied":
                rule4.append({
                    "room": room_name,
                    "occ_status": occ_status,
                })

    return rooms, rule1, rule2, rule3, rule4


# ── Report Formatting ───────────────────────────────────────────────

def format_report(rooms, rule1, rule2, rule3, rule4):
    today_str = date.today().strftime("%B %d, %Y")
    total_issues = len(rule1) + len(rule2) + len(rule3) + len(rule4)

    lines = [
        f"*Room Availability Audit — {today_str}*",
        f"Rooms audited: {len(rooms)} | Issues found: {total_issues}",
        "",
    ]

    if total_issues == 0:
        lines.append("All clear — no inconsistencies found.")
        return "\n".join(lines)

    # Rule 1
    lines.append(f"*Should be Available ({len(rule1)}):*")
    if rule1:
        for r in rule1:
            lines.append(f"  • {r['room']} — currently \"{r['current']}\", "
                         f"{r['deal']} moving out {r['move_out']}")
    else:
        lines.append("  No issues")
    lines.append("")

    # Rule 2
    lines.append(f"*Should be Not Available ({len(rule2)}):*")
    if rule2:
        for r in rule2:
            lines.append(f"  • {r['room']} — currently \"{r['current']}\", "
                         f"{r['deal']} has no move-out planned")
    else:
        lines.append("  No issues")
    lines.append("")

    # Rule 3
    lines.append(f"*Should be In Process ({len(rule3)}):*")
    if rule3:
        for r in rule3:
            lines.append(f"  • {r['room']} — currently \"{r['current']}\", "
                         f"{r['deal']} at {r['stage']}")
    else:
        lines.append("  No issues")
    lines.append("")

    # Rule 4
    lines.append(f"*Occupied but no Moved-In deal ({len(rule4)}):*")
    if rule4:
        for r in rule4:
            lines.append(f"  • {r['room']} — Status: {r['occ_status']}")
    else:
        lines.append("  No issues")

    return "\n".join(lines)


# ── Delivery ────────────────────────────────────────────────────────

def send_slack(report):
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set — skipping Slack")
        return
    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": report})
    if resp.status_code == 200:
        print("Slack message sent to #sale-team")
    else:
        print(f"Slack send failed: {resp.status_code} {resp.text}")


def send_email(token, report):
    """Send email via Zoho CRM Send Mail API (best-effort)."""
    today_str = date.today().strftime("%Y-%m-%d")
    # Zoho CRM Send Mail requires a record context — use org user record
    # This is best-effort; if it fails, the Slack message is the primary delivery
    try:
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        # Get current user info for record context
        resp = requests.get(f"{ZOHO_CRM_API}/users?type=CurrentUser", headers=headers)
        resp.raise_for_status()
        user = resp.json()["users"][0]
        user_email = user.get("email", "")

        # Use Zoho ZeptoMail or CRM notification — for now, log as TODO
        print(f"Email delivery to {EMAIL_TO} — would need Zoho Mail/ZeptoMail integration")
        print(f"  Subject: Daily Room Availability Audit — {today_str}")
    except Exception as e:
        print(f"Email send skipped: {e}")


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("CRM Availability Audit")
    print("=" * 60)

    rooms, rule1, rule2, rule3, rule4 = run_audit()
    report = format_report(rooms, rule1, rule2, rule3, rule4)

    print("\n" + report + "\n")

    # Save report to file
    today_str = date.today().strftime("%Y-%m-%d")
    report_path = f"report_{today_str}.txt"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"Report saved to {report_path}")

    # Send to Slack
    send_slack(report)

    # Attempt email
    token = get_access_token()
    send_email(token, report)

    total_issues = len(rule1) + len(rule2) + len(rule3) + len(rule4)
    print(f"\nDone. {total_issues} issues flagged.")
    sys.exit(0)
