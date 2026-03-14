# CRM Audit

## Overview
Daily availability audit for Roomrs property management via Zoho CRM MCP integration.

## CRM Data Model

### Modules
- **Rooms** — Individual rooms within units/buildings
- **Deals** — Tenant deals (applications, leases, move-ins)
- **Contacts** — Tenants
- **Accounts (Buildings)** — Properties
- **Units** — Units within buildings

### Key Room Fields
- `Operation_Status` — "Available", etc.
- `Sales_Status` — "Available", "Not Available", "In Process"
- `Status` — Occupancy status ("Occupied", "Vacant")
- `Next_Vacancy_date` — Date room becomes available
- `Building` — Lookup to Accounts
- `Unit` — Lookup to Units

### Key Deal Fields
- `Stage` — Deal pipeline stage
- `Room` — Lookup to Rooms
- `New_Move_out_Date2` — **Move Out** (confirmed move-out date, triggers availability)
- `Move_out_date` — **Lease To** (just lease end date, does NOT trigger availability)
- `Move_in_date` — Lease From date

### Deal Stages (ordered)
Application, Missing Paperwork, Negotiation, Room Switch, Approved, Qualified, Disqualified, Lease Sent, Renters Insurance Purchased, Pending, Pending Portal, Lease Renewal, Closed-Lost to Competition, Portal sent, Partial Payment, Renewal, Move in cost paid, Move-In Ready, Moved In, Past Deal, Closed Lost

### Important Distinctions
- `Move_out_date` = "Lease To" — just end of lease, does NOT mean tenant is leaving
- `New_Move_out_Date2` = "Move Out" — actual confirmed move-out, DOES trigger availability

## Zoho CRM API Notes
- COQL queries: max 200 records per page, use `offset` for pagination
- Lookup fields return `{name, id}` objects
