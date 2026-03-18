#!/usr/bin/env python3
"""
CRM Audit — Deals vs. Expiring Leases

Validates data consistency between Moved-In Deals and their
associated Expiring Lease records. Flags missing EL records,
mismatched eligibility, and incorrect move-out/decision combos.

Rules:
  - Every Moved-In deal must have an active EL (Current_Lease_To = Deal Lease To, not in past)
  - Basic membership: Eligibility = "Not eligible for renewal", Move-Out filled if Declined
  - Non-Basic (Legacy, Plus, Premium, Full Apartment): Eligibility = "Eligible for renewal"
  - Declined (any tier): Move-Out must be filled
  - Non-Basic + not Declined: Move-Out should be empty
  - Pending Decision is acceptable for any tier (not officially decided yet)
"""

import os
import sys
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
    today = date.today().isoformat()

    # Step 1: Fetch all Moved-In deals
    print("Fetching Moved-In deals...")
    deals = coql_query_paginated(
        token,
        "select Deal_Name, Stage, Room, Membership_Tier, New_Move_out_Date2, "
        "Move_out_date, Renewal_Fee, id "
        "from Deals where Stage = 'Moved In' and Room is not null "
        "order by Deal_Name asc"
    )
    print(f"  -> {len(deals)} Moved-In deals")

    # Step 2: Fetch all Expiring Lease records with a Deal link
    print("Fetching Expiring Lease records...")
    leases = coql_query_paginated(
        token,
        "select Name, Eligibility, Decision, Membership_Tier, Deal, Room, "
        "Current_Lease_To, Effective_Move_Out, Status, id "
        "from Expiring_Leases where Deal is not null "
        "order by Name asc"
    )
    print(f"  -> {len(leases)} Expiring Lease records")

    # Build mapping: deal_id -> list of EL records
    el_by_deal = {}
    for el in leases:
        deal_ref = el.get("Deal")
        if isinstance(deal_ref, dict) and deal_ref.get("id"):
            el_by_deal.setdefault(deal_ref["id"], []).append(el)

    # Step 3: Apply audit rules
    no_active_el = []       # Deals with no active EL
    eligibility_issues = [] # Wrong eligibility for membership tier
    moveout_issues = []     # Move-out date inconsistencies
    declined_no_mo = []     # Declined but no move-out date

    for deal in deals:
        deal_id = deal["id"]
        deal_name = deal.get("Deal_Name", "Unknown")
        membership = deal.get("Membership_Tier", "")
        move_out = deal.get("New_Move_out_Date2")  # Actual Move Out
        lease_to = deal.get("Move_out_date")        # Lease To date
        room = deal.get("Room", {})
        room_name = room.get("name", "Unknown") if isinstance(room, dict) else "Unknown"
        renewal_fee = deal.get("Renewal_Fee")
        is_basic = membership == "Basic"
        # Legacy with $0 renewal fee = not eligible (special case)
        is_legacy_zero = membership == "Legacy" and renewal_fee is not None and float(renewal_fee) == 0

        # Find active EL: not closed
        # "Renewed" = always closed (new cycle started)
        # "Declined" = only closed if Current_Lease_To is in the past (tenant moved out)
        deal_els = el_by_deal.get(deal_id, [])
        active_el = None

        def is_el_closed(el):
            decision = el.get("Decision") or ""
            if decision == "Renewed":
                return True
            if decision == "Declined":
                el_lease_to = el.get("Current_Lease_To") or ""
                return el_lease_to < today  # Only closed if tenant already left
            return False

        # First pass: find active EL that matches Lease To date
        for el in deal_els:
            el_lease_to = el.get("Current_Lease_To")
            if not is_el_closed(el) and el_lease_to == lease_to:
                active_el = el
                break

        # Second pass: find any active EL (even if date doesn't match)
        if not active_el:
            for el in deal_els:
                if not is_el_closed(el):
                    active_el = el
                    break

        # Rule 1: Must have an active EL
        if not active_el:
            # Check if all ELs are closed
            closed_els = [el for el in deal_els if is_el_closed(el)]
            no_active_el.append({
                "deal": deal_name,
                "room": room_name,
                "membership": membership,
                "lease_to": lease_to,
                "el_count": len(deal_els),
                "all_closed": len(closed_els) == len(deal_els) and len(deal_els) > 0,
            })
            continue

        el_eligibility = active_el.get("Eligibility") or ""
        el_decision = active_el.get("Decision") or ""

        # Rule 2: Eligibility check
        # Basic → Not eligible for renewal
        # Legacy with $0 renewal fee → Not eligible (don't flag)
        # Everything else → Eligible for renewal
        should_be_not_eligible = is_basic or is_legacy_zero
        if should_be_not_eligible:
            if el_eligibility != "Not eligible for renewal":
                reason = "Basic" if is_basic else "Legacy ($0 renewal fee)"
                eligibility_issues.append({
                    "deal": deal_name,
                    "room": room_name,
                    "membership": membership,
                    "eligibility": el_eligibility,
                    "expected": f"Not eligible for renewal ({reason})",
                })
        else:
            if el_eligibility != "Eligible for renewal":
                eligibility_issues.append({
                    "deal": deal_name,
                    "room": room_name,
                    "membership": membership,
                    "eligibility": el_eligibility,
                    "expected": "Eligible for renewal",
                })

        # Rule 3: Declined (any tier) -> Move-Out must be filled
        is_declined = "declined" in el_decision.lower() if el_decision else False
        if is_declined and not move_out:
            declined_no_mo.append({
                "deal": deal_name,
                "room": room_name,
                "membership": membership,
                "decision": el_decision,
            })

        # Rule 4: Non-basic + not declined -> Move-Out should be empty
        if not is_basic and not is_declined and move_out:
            # Exception: Lease Break, Transfer also justify move-out
            if el_decision not in ("Lease Break", "Transfer Request", "Transfered"):
                moveout_issues.append({
                    "deal": deal_name,
                    "room": room_name,
                    "membership": membership,
                    "decision": el_decision,
                    "move_out": move_out,
                })

    return deals, no_active_el, eligibility_issues, moveout_issues, declined_no_mo


# ── Report Formatting ───────────────────────────────────────────────

def format_report(deals, no_active_el, eligibility_issues, moveout_issues, declined_no_mo):
    today_str = date.today().strftime("%B %d, %Y")
    total = len(no_active_el) + len(eligibility_issues) + len(moveout_issues) + len(declined_no_mo)

    lines = [
        f"*Deals vs. Expiring Leases Audit -- {today_str}*",
        f"Deals audited: {len(deals)} | Issues found: {total}",
        "",
    ]

    if total == 0:
        lines.append("All clear -- no inconsistencies found.")
        return "\n".join(lines)

    # Missing active EL
    lines.append(f"*Missing Active Expiring Lease ({len(no_active_el)}):*")
    if no_active_el:
        for r in no_active_el:
            if r.get('all_closed'):
                el_note = f" (has {r['el_count']} closed EL records — needs new active EL)"
            elif r['el_count'] > 0:
                el_note = f" ({r['el_count']} EL records, none active)"
            else:
                el_note = " (no EL records at all)"
            lines.append(f"  - {r['deal']} -- {r['room']} | {r['membership']} | "
                         f"Lease To: {r['lease_to']}{el_note}")
    else:
        lines.append("  No issues")
    lines.append("")

    # Eligibility mismatches
    lines.append(f"*Eligibility Mismatches ({len(eligibility_issues)}):*")
    if eligibility_issues:
        for r in eligibility_issues:
            lines.append(f"  - {r['deal']} -- {r['room']} | {r['membership']} | "
                         f"Eligibility: \"{r['eligibility']}\" (expected \"{r['expected']}\")")
    else:
        lines.append("  No issues")
    lines.append("")

    # Move-out should be empty
    lines.append(f"*Move-Out Filled but Not Declined ({len(moveout_issues)}):*")
    if moveout_issues:
        for r in moveout_issues:
            lines.append(f"  - {r['deal']} -- {r['room']} | {r['membership']} | "
                         f"Decision: \"{r['decision']}\" | Move-Out: {r['move_out']}")
    else:
        lines.append("  No issues")
    lines.append("")

    # Declined without move-out
    lines.append(f"*Declined but No Move-Out Date ({len(declined_no_mo)}):*")
    if declined_no_mo:
        for r in declined_no_mo:
            lines.append(f"  - {r['deal']} -- {r['room']} | {r['membership']} | "
                         f"Decision: \"{r['decision']}\"")
    else:
        lines.append("  No issues")

    return "\n".join(lines)


# ── Delivery ────────────────────────────────────────────────────────

def send_slack(report):
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set -- skipping Slack")
        return
    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": report})
    if resp.status_code == 200:
        print("Slack message sent to #sale-team")
    else:
        print(f"Slack send failed: {resp.status_code} {resp.text}")


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("CRM Audit -- Deals vs. Expiring Leases")
    print("=" * 60)

    deals, no_active_el, eligibility_issues, moveout_issues, declined_no_mo = run_audit()
    report = format_report(deals, no_active_el, eligibility_issues, moveout_issues, declined_no_mo)

    print("\n" + report + "\n")

    # Save report to file
    today_str = date.today().strftime("%Y-%m-%d")
    report_path = f"report_deals_lease_{today_str}.txt"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"Report saved to {report_path}")

    # Send to Slack
    send_slack(report)

    total = len(no_active_el) + len(eligibility_issues) + len(moveout_issues) + len(declined_no_mo)
    print(f"\nDone. {total} issues flagged.")
    sys.exit(0)
