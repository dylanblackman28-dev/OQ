"""
OQ Wholesale Weekly Sync
Runs every Thursday 6:30am AEST via GitHub Actions.
Pulls the previous ordering week's data from Ordermentum
and writes to Supabase for the analytics dashboard.

Ordering week: Tuesday 00:00 AEST -> Monday 23:59 AEST
Late order window: Tuesday 00:01 AEST -> Wednesday 23:59 AEST
(orders placed Tue/Wed after the Monday 11:59pm deadline)
"""

import os
import re
import time
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from supabase import create_client

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OM_USERNAME  = os.environ["OM_USERNAME"]
OM_PASSWORD  = os.environ["OM_PASSWORD"]
SUPPLIER_ID  = "71bf79dc-4e3d-41b2-b232-6ebe51a297ab"

AEST = timezone(timedelta(hours=10))

# OQ venue names to exclude from wholesale analytics
OQ_VENUES = [
    "old quarter coffee merchants",
    "oq ballina", "oq murwillumbah", "oq southport",
    "oq coolangatta", "oq murbah",
]

# ── Ordermentum helpers ───────────────────────────────────────────────────────
def om_auth():
    data = json.dumps({"username": OM_USERNAME, "password": OM_PASSWORD}).encode()
    req = urllib.request.Request(
        "https://app.ordermentum.com/v1/auth", data=data,
        headers={"Content-Type": "application/json"}
    )
    return json.loads(urllib.request.urlopen(req).read().decode())["access_token"]


def om_get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        return json.loads(urllib.request.urlopen(req).read().decode())
    except Exception as e:
        print(f"  Warning: GET failed for {url}: {e}")
        return {}


def is_venue(name):
    return any(v in name.lower() for v in OQ_VENUES)


def is_coffee_sku(sku):
    s = (sku or "").upper()
    # Brewing coffee only: WHS bags/tins, not retail (OQ-COF-RT)
    return s.startswith("OQ-COF") and not s.startswith("OQ-COF-RT")


def extract_kg(name, qty):
    n = name.lower()
    if "5kg" in n and ("drum" in n or "tin" in n or "swap" in n):
        return qty * 5
    if "cold brew" in n and ("5 litre" in n or "bucket" in n):
        return qty * 0.5
    if "sample" in n and "75g" in n:
        return qty * 0.075
    m = re.search(r'(\d+)\s*(kg|g|gram)', n)
    if m:
        num = int(m.group(1))
        unit = m.group(2)
        return qty * num if unit == "kg" else qty * (num / 1000)
    return 0


# ── Date range helpers ────────────────────────────────────────────────────────
def ordering_week_range():
    """
    Returns the most recently completed ordering week.
    Week runs Tuesday 00:00 AEST -> Monday 23:59 AEST.
    Script runs Thursday morning, so last week = Tue 8 days ago -> Mon 1 day ago.
    """
    now_aest = datetime.now(AEST)
    # Find last Monday (1 day ago from Thursday = index 0)
    days_since_monday = (now_aest.weekday() - 0) % 7
    last_monday = now_aest - timedelta(days=days_since_monday)
    week_end = last_monday.replace(hour=23, minute=59, second=59, microsecond=0)
    week_start = (last_monday - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        week_start.astimezone(timezone.utc).strftime(fmt),
        week_end.astimezone(timezone.utc).strftime(fmt),
        week_start.date(),  # Used as the week_start key in Supabase
    )


def is_late_order(created_at_str):
    """
    An order is late if placed Tuesday 00:01 -> Wednesday 23:59 AEST.
    These are orders that missed the Monday 11:59pm deadline.
    """
    try:
        dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00")).astimezone(AEST)
        # Tuesday = weekday 1, Wednesday = weekday 2
        return dt.weekday() in (1, 2)
    except Exception:
        return False


# ── Pull all orders for the week ──────────────────────────────────────────────
def pull_orders(token, start_utc, end_utc):
    all_orders = []
    page = 1
    while True:
        url = (
            f"https://app.ordermentum.com/v2/orders"
            f"?supplierId={SUPPLIER_ID}"
            f"&createdAt[gte]={start_utc}"
            f"&createdAt[lte]={end_utc}"
            f"&pageSize=50&pageNo={page}"
        )
        data = om_get(url, token)
        orders = data.get("data", [])
        all_orders.extend(orders)
        total_pages = data.get("meta", {}).get("totalPages", 1)
        print(f"  Orders page {page}/{total_pages} ({len(all_orders)} so far)")
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.2)
    return all_orders


# ── Process line items ────────────────────────────────────────────────────────
def process_orders(all_orders, token):
    """
    Returns dict keyed by retailer_id with aggregated weekly stats.
    """
    partner_data = defaultdict(lambda: {
        "name": "",
        "retailer_id": "",
        "kg": 0.0,
        "order_count": 0,
        "late_count": 0,
        "revenue": 0.0,
        "first_order_date": None,
    })

    for i, order in enumerate(all_orders):
        if order.get("cancelled"):
            continue

        retailer_name = order.get("retailerName", "")
        if is_venue(retailer_name):
            continue

        retailer_id = order.get("retailerId", "")
        if not retailer_id:
            continue

        p = partner_data[retailer_id]
        p["name"] = retailer_name
        p["retailer_id"] = retailer_id
        p["order_count"] += 1
        p["revenue"] += float(order.get("total", 0) or 0)

        if is_late_order(order.get("createdAt", "")):
            p["late_count"] += 1

        # Pull line items for kg
        detail = om_get(f"https://app.ordermentum.com/v1/orders/{order['id']}", token)
        for item in detail.get("lineItems", []):
            sku = item.get("SKU", "") or ""
            if not is_coffee_sku(sku):
                continue
            name = item.get("name", "") or ""
            qty = item.get("quantity", 0) or 0
            p["kg"] += extract_kg(name, qty)

        if (i + 1) % 10 == 0:
            print(f"  Processed {i+1}/{len(all_orders)} orders...")
        time.sleep(0.15)

    return partner_data


# ── Supabase upserts ──────────────────────────────────────────────────────────
def upsert_partner(sb, retailer_id, name, first_order_date=None):
    """
    Upsert partner record. Returns the Supabase partner UUID.
    """
    existing = sb.table("partners").select("id, first_order_date").eq(
        "ordermentum_retailer_id", retailer_id
    ).execute()

    if existing.data:
        partner_id = existing.data[0]["id"]
        # Update first_order_date if we have one and it's not set
        if first_order_date and not existing.data[0].get("first_order_date"):
            sb.table("partners").update({
                "first_order_date": str(first_order_date),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", partner_id).execute()
        return partner_id
    else:
        result = sb.table("partners").insert({
            "ordermentum_retailer_id": retailer_id,
            "name": name,
            "first_order_date": str(first_order_date) if first_order_date else None,
        }).execute()
        return result.data[0]["id"]


def upsert_weekly_order(sb, partner_id, week_start, kg, order_count, late_count, revenue):
    sb.table("weekly_orders").upsert({
        "partner_id": partner_id,
        "week_start": str(week_start),
        "kg_ordered": round(kg, 2),
        "order_count": order_count,
        "late_order_count": late_count,
        "total_revenue": round(revenue, 2),
    }, on_conflict="partner_id,week_start").execute()


def refresh_order_summary(sb, partner_id):
    """
    Recalculates LTV, total orders, late rate, avg annual value,
    avg kg/week, and years as customer from all historical weekly_orders.
    """
    rows = sb.table("weekly_orders").select("*").eq("partner_id", partner_id).execute()
    if not rows.data:
        return

    total_revenue = sum(float(r["total_revenue"] or 0) for r in rows.data)
    total_orders  = sum(int(r["order_count"] or 0) for r in rows.data)
    total_late    = sum(int(r["late_order_count"] or 0) for r in rows.data)
    total_kg      = sum(float(r["kg_ordered"] or 0) for r in rows.data)
    late_rate     = round((total_late / total_orders * 100), 2) if total_orders > 0 else 0

    # Years as customer from first_order_date
    partner = sb.table("partners").select("first_order_date").eq("id", partner_id).execute()
    first_date = partner.data[0].get("first_order_date") if partner.data else None
    years = 0.0
    if first_date:
        delta = datetime.now(timezone.utc).date() - datetime.fromisoformat(str(first_date)).date()
        years = round(delta.days / 365.25, 2)

    avg_annual = round(total_revenue / years, 2) if years > 0 else total_revenue
    weeks = len(rows.data)
    avg_kg = round(total_kg / weeks, 2) if weeks > 0 else 0

    sb.table("order_summary").upsert({
        "partner_id": partner_id,
        "ltv": round(total_revenue, 2),
        "total_orders": total_orders,
        "late_rate": late_rate,
        "avg_annual_value": avg_annual,
        "avg_kg_per_week": avg_kg,
        "years_as_customer": years,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="partner_id").execute()


# ── First order date lookup ───────────────────────────────────────────────────
def get_first_order_date(token, retailer_id):
    """
    Pull the earliest order for this retailer to determine first order date.
    """
    url = (
        f"https://app.ordermentum.com/v2/orders"
        f"?supplierId={SUPPLIER_ID}"
        f"&retailerId={retailer_id}"
        f"&pageSize=1&pageNo=1"
        f"&sort=createdAt&order=asc"
    )
    data = om_get(url, token)
    orders = data.get("data", [])
    if orders:
        created = orders[0].get("createdAt", "")
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).date()
        except Exception:
            return None
    return None


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"OQ Wholesale Sync — {datetime.now(AEST).strftime('%A %d %B %Y %I:%M %p AEST')}")
    print("=" * 60)

    # Authenticate
    print("\n[1/5] Authenticating with Ordermentum...")
    token = om_auth()
    print("  Authenticated ✓")

    # Get week range
    start_utc, end_utc, week_start_date = ordering_week_range()
    print(f"\n[2/5] Pulling orders for week: {week_start_date} -> {week_start_date + timedelta(days=6)}")
    print(f"  UTC window: {start_utc} -> {end_utc}")

    # Pull orders
    all_orders = pull_orders(token, start_utc, end_utc)
    print(f"  {len(all_orders)} orders pulled ✓")

    # Process line items
    print(f"\n[3/5] Processing line items...")
    partner_data = process_orders(all_orders, token)
    print(f"  {len(partner_data)} partners found ✓")

    # Connect to Supabase
    print(f"\n[4/5] Writing to Supabase...")
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    for retailer_id, data in partner_data.items():
        print(f"  → {data['name']}: {data['kg']:.1f}kg, {data['order_count']} orders, {data['late_count']} late")

        # Get first order date (for new partners)
        first_date = get_first_order_date(token, retailer_id)
        time.sleep(0.1)

        # Upsert partner
        partner_id = upsert_partner(sb, retailer_id, data["name"], first_date)

        # Write this week's row
        upsert_weekly_order(
            sb, partner_id, week_start_date,
            data["kg"], data["order_count"], data["late_count"], data["revenue"]
        )

        # Refresh summary stats
        refresh_order_summary(sb, partner_id)

    print(f"\n[5/5] Done ✓")
    print(f"  {len(partner_data)} partners synced to Supabase")
    print(f"  Week: {week_start_date}")
    print("=" * 60)


if __name__ == "__main__":
    main()
