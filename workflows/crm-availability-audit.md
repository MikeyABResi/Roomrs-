---
name: crm-availability-audit
description: Daily audit of Roomrs CRM room availability statuses — flags inconsistencies and sends report to Slack #sale-team and email.
---

You are running a daily CRM availability audit for the Roomrs property management system. Your working directory is /Users/mikelazougui/Desktop/Roomrs.

## Objective
Query Zoho CRM for all rooms with Operation_Status = "Available", cross-reference with active deals, and flag rooms whose Sales_Status or Occupancy Status is inconsistent with their deal data. Send the audit report to Slack and email.

## Step 1 — Fetch all Available rooms (paginated)

Use ZohoCRM_executeCOQLQuery to fetch rooms. COQL has a 200-record limit per page, so paginate:

Page 1: `select id, Name, Sales_Status, Status, Next_Vacancy_date, Building, Unit from Rooms where Operation_Status = 'Available' order by Name asc limit 200`
Page 2: same query + ` offset 200`
Page 3: same query + ` offset 400`
Page 4: same query + ` offset 600`
Continue until a page returns fewer than 200 records.

Collect all rooms into a single list.

## Step 2 — Fetch all Moved-In deals (paginated)

`select Deal_Name, Stage, Room, New_Move_out_Date2, Move_in_date, Move_out_date, id from Deals where Stage = 'Moved In' and Room is not null order by Deal_Name asc limit 200`

Paginate with offset 200, 400, etc. until fewer than 200 results.

Build a mapping: room_id → list of Moved-In deals (with their New_Move_out_Date2).

## Step 3 — Fetch early-stage open deals

`select Deal_Name, Stage, Room, id from Deals where Stage in ('Application', 'Qualified', 'Lease Sent') and Room is not null order by Deal_Name asc limit 200`

Paginate if needed. Build a mapping: room_id → list of early-stage deals.

## Step 4 — Apply audit rules

For each room from Step 1, apply these rules using today's date:

**Rule 1 — Should be Available**: Room has a Moved-In deal whose `New_Move_out_Date2` field is not null and is >= today. If the room's Sales_Status is NOT "Available", flag it. Include the deal name and move-out date.

**Rule 2 — Should be Not Available**: Room has a Moved-In deal whose `New_Move_out_Date2` field is null (no confirmed move-out). If the room's Sales_Status IS "Available", flag it. The tenant has no move-out planned so the room should not be listed as available. Include the deal name.

**Rule 3 — Should be In Process**: Room has NO Moved-In deal but DOES have a deal at stage Application, Qualified, or Lease Sent. If the room's Sales_Status is NOT "In Process", flag it. Include the deal name and stage.

**Rule 4 — Occupied but no Moved-In deal**: Room's Status (occupancy) is "Occupied" but there is NO Moved-In deal for this room. Flag as a data inconsistency.

CRITICAL FIELD DISTINCTION:
- `Move_out_date` = "Lease To" — this is just the lease end date and does NOT indicate the tenant is leaving. IGNORE this field for move-out logic.
- `New_Move_out_Date2` = "Move Out" — this is the actual confirmed move-out date and DOES indicate the tenant is leaving. USE this field for all move-out logic.

## Step 5 — Format the report

Create a report with:
- Header: "Room Availability Audit — [today's date]"
- Summary: total rooms audited, number of issues found per rule
- Grouped sections for each rule with issues:
  - Rule 1: "Should be Available" — room name, current Sales_Status, deal name, move-out date
  - Rule 2: "Should be Not Available" — room name, current Sales_Status, deal name
  - Rule 3: "Should be In Process" — room name, current Sales_Status, deal name, deal stage
  - Rule 4: "Occupied but no deal" — room name, occupancy status
- If no issues found for a rule, note "No issues found"
- If no issues found at all, send a short "All clear" message

## Step 6 — Send to Slack

Use the slack_send_message MCP tool to send the report to the #sale-team channel.

## Step 7 — Send via email

Use the ZohoCRM_Send_Mail MCP tool to email the report to mikey@roorms.com with subject "Daily Room Availability Audit — [today's date]".
If the email tool fails (it requires a record context), skip email and note in the Slack message that the email could not be sent.