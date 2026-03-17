#!/usr/bin/env python3
"""
CRM Audit — Deals vs. Expiring Leases

Validates data consistency between Moved-In Deals and their
associated Expiring Lease records. Flags missing EL records,
mismatched eligibility, and incorrect move-out/decision combos.
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
    headers = {"Authorization": f"Zoho-oauthtoken {token}"}
    resp = requests.post(ZOHO_COQL_URL, headers=headers, json={"select_query": query})
    if resp.status_code == 204:
        return []
    resp.raise_for_status()
    return resp.json().get("data", [])


def coql_query_paginated(token, base_query):
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

    # Step 1: Fetch all Moved-In deals
    print("Fetching Moved-In deals...")
    deals = coql_query_paginated(
        token,
        "select Deal_Name, Stage, Room, Membership_Tier, New_Move_out_Date2, "
        "Move_out_date, id "
        "from Deals where Stage = 'Moved In' and Room is not null "
        "order by Deal_Name asc"
    )
    print(f"  → {len(deals)} Moved-In deals")

    # Step 2: Fetch all Expiring Lease records
    print("Fetching Expiring Lease records...")
    leases = coql_query_paginated(
        token,
        "select Name, Eligibility, Decision, Membership_Tier, Deal, Room, "
        "Current_Lease_To, Effective_Move_Out, id "
        "from Expiring_Leases where Deal is not null "
        "order by Name asc"
    )
    print(f"  → {len(leases)} Expiring Lease records")

    # Build mapping: deal_id → list of EL records
    el_by_deal = {}
    for el in leases:
        deal_ref = el.get("Deal")
        if isinstance(deal_ref, dict) and deal_ref.get("id"):
            el_by_deal.setdefault(deal_ref["id"], []).append(el)

    # Step 3: Apply audit rules
    no_active_el = []       # Deals with no active EL
    basic_issues = []       # Basic membership data mismatches
    non_basic_issues = []   # Non-basic membership data mismatches
    declined_issues = []    # Declined decision but no move-out

    for deal in deals:
        deal_id = deal["id"]
        deal_name = deal.get("Deal_Name", "Unknown")
        membership = deal.get("Membership_Tier", "")
        move_out = deal.get("New_Move_out_Date2")
        lease_to = deal.get("Move_out_date")  # Lease To date
        room = deal.get("Room", {})
        room_name = room.get("name", "Unknown") if isinstance(room, dict) else "Unknown"
        is_basic = membership == "Basic"

        # Find active EL: Current_Lease_To matches Deal's Lease To date
        deal_els = el_by_deal.get(deal_id, [])
        active_el = None
        for el in deal_els:
            if el.get("Current_Lease_To") == lease_to:
                active_el = el
                break

        # Rule 1: Must have an active EL
        if not active_el:
            no_active_el.append({
                "deal": deal_name,
                "room": room_name,
                "membership": membership,
                "lease_to": lease_to,
                "el_count": len(deal_els),
            })
            continue

        el_eligibility = active_el.get("Eligibility", "")
        el_decision = active_el.get("Decision", "")

        # Rule 2: Basic membership checks
        if is_basic:
            issues = []
            if "not eligible" not in (el_eligibility or "").lower():
                issues.append(f"Eligibility is \"{el_eligibility}\" (expected Not Eligible)")
            if not move_out:
                issues.append("Move-Out Date is empty (should be filled)")
            if el_decision != "Declined":
                issues.append(f"Decision is \"{el_decision}\" (expected Declined)")
            if issues:
                basic_issues.append({
                    "deal": deal_name,
                    "room": room_name,
                    "membership": membership,
                    "issues": issues,
                })

        # Rule 3: Non-basic membership checks
        else:
            issues = []
            if "eligible for renewal" not in (el_eligibility or "").lower():
                issues.append(f"Eligibility is \"{el_eligibility}\" (expected Eligible for Renewal)")
            if el_decision == "Declined":
                # Exception: Declined means move-out must be filled
                if not move_out:
                    issues.append("Decision is Declined but Move-Out Date is empty")
            else:
                # Not declined: move-out should be empty
                if move_out:
                    issues.append(f"Move-Out Date is {move_out} but Decision is \"{el_decision}\" (should be empty unless Declined)")
                if el_decision not in ("Pending Decision", "Accepted"):
                    issues.append(f"Decision is \"{el_decision}\" (expected Pending Decision or Accepted)")
            if issues:
                non_basic_issues.append({
                    "deal": deal_name,
                    "room": room_name,
                    "membership": membership,
                    "decision": el_decision,
                    "issues": issues,
                })

        # Rule 4: Declined (any tier) must have move-out
        if el_decision == "Declined" and not move_out:
            declined_issues.append({
                "deal": deal_name,
                "room": room_name,
                "membership": membership,
            })

    return deals, no_active_el, basic_issues, non_basic_issues, declined_issues


# ── Report Formatting ───────────────────────────────────────────────

def format_report(deals, no_active_el, basic_issues, non_basic_issues, declined_issues):
    today_str = date.today().strftime("%B %d, %Y")
    total_issues = len(no_active_el) + len(basic_issues) + len(non_basic_issues) + len(declined_issues)

    lines = [
        f"*Deals vs. Expiring Leases Audit — {today_str}*",
        f"Deals audited: {len(deals)} | Issues found: {total_issues}",
        "",
    ]

    if total_issues == 0:
        lines.append("All clear — no inconsistencies found.")
        return "\n".join(lines)

    # No active EL
    lines.append(f"*Missing Active Expiring Lease ({len(no_active_el)}):*")
    if no_active_el:
        for r in no_active_el:
            el_note = f" ({r['el_count']} EL records, none match Lease To)" if r['el_count'] > 0 else " (no EL records at all)"
            lines.append(f"  • {r['deal']} — {r['room']} | {r['membership']} | Lease To: {r['lease_to']}{el_note}")
    else:
        lines.append("  No issues")
    lines.append("")

    # Basic mismatches
    lines.append(f"*Basic Membership Mismatches ({len(basic_issues)}):*")
    if basic_issues:
        for r in basic_issues:
            for issue in r["issues"]:
                lines.append(f"  • {r['deal']} — {r['room']} | {issue}")
    else:
        lines.append("  No issues")
    lines.append("")

    # Non-basic mismatches
    lines.append(f"*Non-Basic Membership Mismatches ({len(non_basic_issues)}):*")
    if non_basic_issues:
        for r in non_basic_issues:
            for issue in r["issues"]:
                lines.append(f"  • {r['deal']} — {r['room']} | {issue}")
    else:
        lines.append("  No issues")
    lines.append("")

    # Declined without move-out
    lines.append(f"*Declined but No Move-Out ({len(declined_issues)}):*")
    if declined_issues:
        for r in declined_issues:
            lines.append(f"  • {r['deal']} — {r['room']} | {r['membership']}")
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


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("CRM Audit — Deals vs. Expiring Leases")
    print("=" * 60)

    deals, no_active_el, basic_issues, non_basic_issues, declined_issues = run_audit()
    report = format_report(deals, no_active_el, basic_issues, non_basic_issues, declined_issues)

    print("\n" + report + "\n")

    # Save report to file
    today_str = date.today().strftime("%Y-%m-%d")
    report_path = f"report_deals_lease_{today_str}.txt"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"Report saved to {report_path}")

    # Send to Slack
    send_slack(report)

    total_issues = len(no_active_el) + len(basic_issues) + len(non_basic_issues) + len(declined_issues)
    print(f"\nDone. {total_issues} issues flagged.")
    sys.exit(0)
