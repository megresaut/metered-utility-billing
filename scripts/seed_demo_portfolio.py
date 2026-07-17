#!/usr/bin/env python3
"""Seed a realistic demo portfolio for BillFlow.

Creates (via API where the real pipeline matters, SQL for historical backfill):
  - 2 additional properties (4 total)
  - ~14 utility accounts (auto-scrape with encrypted creds + manual-only)
  - ~6 months of bills (Jan-Jul 2026) with real generated PDFs in the store
  - a plausible scrape-job history

Run:  .venv/bin/python seed_portfolio.py  (from anywhere; paths are absolute)
"""
import asyncio, hashlib, json, os, random, sys, urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path("/Users/megkrish/Desktop/utility-billing-platform")
STORE = ROOT / "data" / "store"
API = "http://localhost:8090"
TODAY = date(2026, 7, 17)

random.seed(42)

def api(path, method="GET", body=None, token=None):
    req = urllib.request.Request(API + path, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data) as r:
        return json.loads(r.read() or "{}")

TOKEN = api("/api/login", "POST", {"email": "demo@harborview.example", "password": "demo1234"})["token"]

# ---------------- properties ----------------
props = {p["name"]: p["id"] for p in api("/api/properties", token=TOKEN)}
for name, addr in [
    ("The Maplewood", "310 Maplewood Avenue, Bridgeport, CT 06605"),
    ("Riverside Commons", "77 Riverside Drive, Westport, CT 06880"),
]:
    if name not in props:
        p = api("/api/properties", "POST", {"name": name, "address": addr}, TOKEN)
        props[name] = p["id"]
print("properties:", props)

# ---------------- utility accounts ----------------
# (property, provider_code, vendor, acct#, auto?, stmt_day, category, base_cents, curve)
# curve: flat | winter (gas) | summer (electric)
ACCOUNTS = [
    ("12 Harbor Lane", "aquarion", "Aquarion Water Company", "200198822-105", True, 28, "water", 8400, "flat"),
    ("12 Harbor Lane", "cng", "Connecticut Natural Gas", "57-330911-2", True, 15, "gas", 15800, "winter"),
    ("12 Harbor Lane", "eversource", "Eversource Energy", "51-777203-081", True, 20, "electric", 15500, "summer"),
    ("12 Harbor Lane", "optimum", "Optimum Business", "07701-441982", False, 10, "internet", 10999, "flat"),
    ("48 Beacon Street", "rwa", "Regional Water Authority", "AP0117898", True, 27, "water", 7100, "flat"),
    ("48 Beacon Street", "uinet", "United Illuminating", "0441-2290-114", True, 22, "electric", 13200, "summer"),
    ("48 Beacon Street", "winwaste", "WinWaste Innovations", "WW-20441", False, 4, "waste", 14800, "flat"),
    ("The Maplewood", "snew", "South Norwalk Electric & Water", "SN-88112", True, 26, "water", 9600, "flat"),
    ("The Maplewood", "scg", "Southern Connecticut Gas", "883-201-4471", True, 16, "gas", 18900, "winter"),
    ("The Maplewood", "frontier", "Frontier Communications", "FR-9982031", False, 9, "internet", 9499, "flat"),
    ("The Maplewood", "santaguida", "Santaguida Sanitation", "SG-3341", False, 5, "waste", 16200, "flat"),
    ("Riverside Commons", "wpca", "Norwalk WPCA", "WP-55618", True, 25, "water", 6800, "flat"),
    ("Riverside Commons", "ttd", "Third Taxing District Electric", "TT-104-88", True, 18, "electric", 11900, "summer"),
    ("Riverside Commons", "fios", "Verizon Fios Business", "882-114-9901", False, 11, "internet", 11499, "flat"),
]

providers = {p["code"]: p["id"] for p in api("/api/providers", token=TOKEN)}
existing = {(a["property_id"], a["provider_code"]): a for a in api("/api/utility-accounts", token=TOKEN)}
acct_ids = {}
for prop, code, vendor, num, auto, day, cat, base, curve in ACCOUNTS:
    key = (props[prop], code)
    if key in existing:
        acct_ids[(prop, code)] = existing[key]["id"]
        continue
    body = {
        "property_id": props[prop], "provider_id": providers[code],
        "account_number": num, "username": f"billing@harborviewpm.com",
    }
    if auto:
        body["password"] = "demo-seed-password"
    r = api("/api/utility-accounts", "POST", body, TOKEN)
    acct_ids[(prop, code)] = r["id"]
print("accounts:", len(acct_ids))

# ---------------- bill schedule ----------------
VENDOR_META = {
    "aquarion": ("200 Monroe Turnpike, Bridgeport, CT 06611", "#00529b"),
    "cng": ("PO Box 1085, Meriden, CT 06450", "#c8102e"),
    "eversource": ("PO Box 56002, Boston, MA 02205", "#00529b"),
    "optimum": ("1111 Stewart Avenue, Bethpage, NY 11714", "#00468b"),
    "rwa": ("90 Sargent Drive, New Haven, CT 06511", "#0f766e"),
    "uinet": ("PO Box 9230, Chelsea, MA 02150", "#b45309"),
    "winwaste": ("100 Commerce Drive, Shelton, CT 06484", "#374151"),
    "snew": ("1 State Street, South Norwalk, CT 06856", "#155e75"),
    "scg": ("60 Marsh Hill Road, Orange, CT 06477", "#9d174d"),
    "frontier": ("401 Merritt 7, Norwalk, CT 06851", "#b91c1c"),
    "santaguida": ("18 Lunar Drive, Woodbridge, CT 06525", "#4d7c0f"),
    "wpca": ("South Smith Street, Norwalk, CT 06855", "#1d4ed8"),
    "ttd": ("2 Second Street, East Norwalk, CT 06855", "#a16207"),
    "fios": ("PO Box 16800, Newark, NJ 07101", "#dc2626"),
}

def seasonal(curve, month, base):
    if curve == "winter":
        f = {1: 1.55, 2: 1.45, 3: 1.15, 4: 0.72, 5: 0.42, 6: 0.30, 7: 0.28}[month]
    elif curve == "summer":
        f = {1: 0.95, 2: 0.92, 3: 0.88, 4: 0.90, 5: 1.02, 6: 1.28, 7: 1.42}[month]
    else:
        f = 1.0
    jitter = 1.0 if curve == "flat" and base in (9499, 10999, 11499) else random.uniform(0.93, 1.07)
    return int(round(base * f * jitter))

# Existing June/May uploads (bills 1-6) — skip those (property, code, month) slots
SKIP = {("12 Harbor Lane", "aquarion", 6), ("12 Harbor Lane", "cng", 6), ("12 Harbor Lane", "eversource", 6),
        ("48 Beacon Street", "rwa", 5), ("48 Beacon Street", "uinet", 6), ("48 Beacon Street", "winwaste", 5)}

bills = []
for prop, code, vendor, num, auto, day, cat, base, curve in ACCOUNTS:
    for month in range(1, 8):
        if (prop, code, month) in SKIP:
            continue
        stmt = date(2026, month, day)
        if stmt > TODAY - timedelta(days=2):
            continue
        p_end = stmt - timedelta(days=2)
        p_start = p_end - timedelta(days=29)
        due = stmt + timedelta(days=random.choice([21, 24, 25, 28]))
        cents = seasonal(curve, month, base)
        if due < date(2026, 6, 25):
            status = "paid"
        elif due < TODAY:
            status = "paid" if random.random() < 0.55 else "outstanding"  # unpaid+past due renders as overdue
        else:
            status = "paid" if random.random() < 0.15 else "outstanding"
        bills.append({
            "prop": prop, "code": code, "vendor": vendor, "acct": num, "cat": cat,
            "stmt": stmt, "due": due, "p_start": p_start, "p_end": p_end,
            "cents": cents, "status": status, "source": "scrape" if auto else "upload",
            "conf": None if auto else random.randint(88, 97),
        })
print("bills to create:", len(bills))

# ---------------- generate PDFs ----------------
TPL = """<html><body style="font-family: Arial; padding: 40px; width: 700px">
<h1 style="color:{color}; margin:0">{vendor}</h1><p>{addr}</p><hr>
<p><b>Statement Date:</b> {stmt}<br><b>Account Number:</b> {acct}<br>
<b>Service Address:</b> {svc}<br><b>Bill To:</b> Harborview Property Management LLC</p>
<h3>Service Period: {ps} to {pe}</h3>
<table style="width:100%; border-collapse:collapse; font-size:14px" border="1" cellpadding="6">
<tr style="background:#eee"><th align="left">Description</th><th align="right">Amount</th></tr>
<tr><td>{desc}</td><td align="right">${amt}</td></tr>
<tr><td><b>Total Amount Due</b></td><td align="right"><b>${amt}</b></td></tr></table>
<p style="font-size:16px"><b>Payment Due Date: {due}</b></p>
<p style="font-size:12px; color:#555">Invoice #{inv}</p></body></html>"""

DESC = {"water": "Water service and usage", "gas": "Natural gas delivery and supply",
        "electric": "Electric supply, delivery and transmission", "internet": "Business internet service",
        "waste": "Waste collection and disposal"}

PROP_ADDR = {
    "12 Harbor Lane": "12 Harbor Lane, Norwalk, CT 06854",
    "48 Beacon Street": "48 Beacon Street, Stamford, CT 06902",
    "The Maplewood": "310 Maplewood Avenue, Bridgeport, CT 06605",
    "Riverside Commons": "77 Riverside Drive, Westport, CT 06880",
}

async def gen_pdfs():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        pg = await b.new_page()
        for i, bill in enumerate(bills):
            addr, color = VENDOR_META[bill["code"]]
            amt = f"{bill['cents'] // 100}.{bill['cents'] % 100:02d}"
            html = TPL.format(color=color, vendor=bill["vendor"], addr=addr,
                              stmt=bill["stmt"].strftime("%B %d, %Y"), acct=bill["acct"],
                              svc=PROP_ADDR[bill["prop"]], ps=bill["p_start"].strftime("%m/%d/%Y"),
                              pe=bill["p_end"].strftime("%m/%d/%Y"), desc=DESC[bill["cat"]], amt=amt,
                              due=bill["due"].strftime("%B %d, %Y"),
                              inv=f"{bill['code'].upper()}-{bill['stmt']:%Y%m}-{1000 + i}")
            key = f"seed/{bill['code']}/{bill['stmt']:%Y/%m}/{1000 + i}.pdf"
            dst = STORE / key
            dst.parent.mkdir(parents=True, exist_ok=True)
            await pg.set_content(html)
            await pg.pdf(path=str(dst), format="Letter")
            bill["key"] = key
            bill["sha"] = hashlib.sha256(dst.read_bytes()).hexdigest()
        await b.close()

asyncio.run(gen_pdfs())
print("pdfs generated")

# ---------------- SQL: bills + job history + touch-ups ----------------
def q(v):
    return "NULL" if v is None else f"'{v}'"

lines = ["BEGIN;"]
for bill in bills:
    aid = acct_ids[(bill["prop"], bill["code"])]
    lines.append(
        "INSERT INTO bills (org_id, utility_account_id, provider_id, property_id, vendor_name,"
        " amount_cents, statement_date, due_date, service_start, service_end, status, source,"
        " pdf_object_key, sha256_pdf, parse_confidence, created_at) VALUES "
        f"(1, {aid}, {providers[bill['code']]}, {props[bill['prop']]}, '{bill['vendor']}',"
        f" {bill['cents']}, '{bill['stmt']}', '{bill['due']}', '{bill['p_start']}', '{bill['p_end']}',"
        f" '{bill['status']}', '{bill['source']}', '{bill['key']}', '{bill['sha']}', {q(bill['conf'])},"
        f" '{bill['stmt']}T09:{random.randint(10,55)}:00-04:00');"
    )

# Link the 6 original uploaded bills to their accounts (vendor/property match)
LINK = [(1, "12 Harbor Lane", "aquarion"), (2, "12 Harbor Lane", "cng"), (3, "12 Harbor Lane", "eversource"),
        (4, "48 Beacon Street", "rwa"), (5, "48 Beacon Street", "uinet"), (6, "48 Beacon Street", "winwaste")]
for bid, prop, code in LINK:
    lines.append(f"UPDATE bills SET utility_account_id = {acct_ids[(prop, code)]},"
                 f" provider_id = {providers[code]} WHERE id = {bid} AND org_id = 1;")

# Scrape-job history: recent successes + a couple of realistic failures
jobs = []
for prop, code in [("12 Harbor Lane", "aquarion"), ("12 Harbor Lane", "eversource"),
                   ("48 Beacon Street", "rwa"), ("48 Beacon Street", "uinet"),
                   ("The Maplewood", "snew"), ("The Maplewood", "scg"),
                   ("Riverside Commons", "wpca"), ("Riverside Commons", "ttd")]:
    days_ago = random.randint(1, 24)
    dur = random.randint(65, 240)
    jobs.append((acct_ids[(prop, code)], "succeeded", days_ago, dur, None))
jobs.append((acct_ids[("12 Harbor Lane", "eversource")], "failed", 3, 1500,
             "eversource timeout: portal did not finish loading the billing page after 25m"))
jobs.append((acct_ids[("48 Beacon Street", "rwa")], "failed", 9, 42,
             "This bill has already been imported for this account and billing cycle. Please try again next cycle."))
for aid, status, days_ago, dur, err in jobs:
    lines.append(
        "INSERT INTO scrape_jobs (org_id, utility_account_id, status, attempt, max_attempts,"
        " requested_by, requested_at, started_at, finished_at, error_message) VALUES "
        f"(1, {aid}, '{status}', 1, 3, 'scheduler-cron',"
        f" now() - interval '{days_ago} days', now() - interval '{days_ago} days' + interval '4 seconds',"
        f" now() - interval '{days_ago} days' + interval '{dur} seconds', {q(err)});"
    )

# Auto accounts: push next scrape into the future (staggered) so the hourly
# scheduler doesn't fire seed credentials at real portals.
for (prop, code), aid in acct_ids.items():
    days = random.randint(6, 30)
    lines.append(f"UPDATE utility_accounts SET next_scheduled_scrape_at = now() + interval '{days} days',"
                 f" last_scheduled_run_at = now() - interval '{random.randint(2, 20)} days'"
                 f" WHERE id = {aid} AND credential_ciphertext IS NOT NULL;")
lines.append("COMMIT;")

sql_path = Path("/tmp/ubp-test/seed_portfolio.sql")
sql_path.write_text("\n".join(lines))
print("sql written:", sql_path, f"({len(lines)} statements)")
