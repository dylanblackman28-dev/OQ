"""
OQ Roast Plan Weekly Sync
Runs every Tuesday 6:30am AEST via GitHub Actions.
"""

import os, re, json, time, urllib.request
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OM_USERNAME  = os.environ["OM_USERNAME"]
OM_PASSWORD  = os.environ["OM_PASSWORD"]
SUPPLIER_ID  = "71bf79dc-4e3d-41b2-b232-6ebe51a297ab"

AEST = timezone(timedelta(hours=10))

OQ_VENUES = [
    "old quarter coffee merchants", "oq ballina", "oq murwillumbah",
    "oq southport", "oq coolangatta", "oq murbah"
]

def om_auth():
    data = json.dumps({"username": OM_USERNAME, "password": OM_PASSWORD}).encode()
    req = urllib.request.Request("https://app.ordermentum.com/v1/auth", data=data,
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())["access_token"]

def om_get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
    except:
        return {}

def ordering_week_range():
    """
    Returns the most recently COMPLETED ordering week (Tue -> Mon).
    Works correctly regardless of what day the script is triggered.

    Logic:
      1. Find most recent Tuesday = start of the CURRENT open week
         (if today IS Tuesday, days_since_tuesday = 0, so current week = today)
      2. Subtract 7 days = start of the last COMPLETED week

    Example running on Monday 22 June 2026:
      - Most recent Tuesday = 17 June (current open week)
      - Last completed week = Tue 10 June -> Mon 16 June  ✓

    Example running on Tuesday 17 June 2026 (scheduled run):
      - Most recent Tuesday = 17 June (today = current week just opened)
      - Last completed week = Tue 10 June -> Mon 16 June  ✓
    """
    now_aest = datetime.now(AEST)
    # weekday(): Mon=0, Tue=1 ... Sun=6
    days_since_tuesday = (now_aest.weekday() - 1) % 7
    # Start of current open week (most recent Tuesday at midnight)
    current_week_start = (now_aest - timedelta(days=days_since_tuesday)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    # Last completed week starts one full week before that
    week_start = current_week_start - timedelta(days=7)
    week_end = (week_start + timedelta(days=6)).replace(hour=23, minute=59, second=59)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        week_start.astimezone(timezone.utc).strftime(fmt),
        week_end.astimezone(timezone.utc).strftime(fmt),
        week_start.date()
    )

def classify(name, sku):
    n = (name or "").lower()
    s = (sku or "").upper()
    if not s.startswith("OQ-COF"):
        return None
    if "oq cafe coffee" in n or "oq-cof-cafe" in s or "oq-cof-ven" in s:
        if "milk" in n:  return "venue_milk_kg"
        if "black" in n: return "venue_black_kg"
        return None
    if "rising sun" in n or "rsr" in s: return "rising_sun_kg"
    if "village" in n:    return "village_blend_kg"
    if "cloud nine" in n or "c9" in s: return "cloud_nine_kg"
    if "euphoria" in n:   return "euphoria_kg"
    if "decaf" in n:      return "decaf_kg"
    if "k'ho" in n or "kho" in n or "vietnam" in n: return "vietnam_kho_kg"
    return None

def extract_kg(name, qty):
    n = (name or "").lower()
    if "5kg" in n and ("drum" in n or "tin" in n or "swap" in n): return qty * 5
    if "cold brew" in n and ("5 litre" in n or "bucket" in n): return qty * 0.5
    if "sample" in n and "75g" in n: return qty * 0.075
    m = re.search(r'(\d+)\s*(kg|g|gram)', n)
    if m:
        num = int(m.group(1))
        return qty * num if m.group(2) == "kg" else qty * (num / 1000)
    return qty * 1

def is_venue_order(retailer_name):
    return any(v in (retailer_name or "").lower() for v in OQ_VENUES)

def main():
    print("=" * 55)
    print(f"OQ Roast Sync — {datetime.now(AEST).strftime('%A %d %B %Y %I:%M %p AEST')}")
    print("=" * 55)

    print("\n[1/4] Authenticating...")
    token = om_auth()
    print("  OK")

    start_utc, end_utc, week_start_date = ordering_week_range()
    print(f"\n[2/4] Pulling orders: {week_start_date} → {week_start_date + timedelta(days=6)}")
    print(f"  (UTC: {start_utc} → {end_utc})")

    all_orders = []
    page = 1
    while True:
        url = (f"https://app.ordermentum.com/v2/orders"
               f"?supplierId={SUPPLIER_ID}"
               f"&createdAt[gte]={start_utc}"
               f"&createdAt[lte]={end_utc}"
               f"&pageSize=50&pageNo={page}")
        data = om_get(url, token)
        all_orders.extend(data.get("data", []))
        total = data.get("meta", {}).get("totalPages", 1)
        if page >= total: break
        page += 1
        time.sleep(0.2)
    print(f"  {len(all_orders)} orders pulled")

    print(f"\n[3/4] Processing line items...")
    totals = defaultdict(float)
    order_count = 0

    for order in all_orders:
        if order.get("cancelled"): continue
        order_count += 1
        detail = om_get(f"https://app.ordermentum.com/v1/orders/{order['id']}", token)
        for item in detail.get("lineItems", []):
            field = classify(item.get("name", ""), item.get("SKU", ""))
            if not field: continue
            kg = extract_kg(item.get("name", ""), item.get("quantity", 0) or 0)
            totals[field] += kg
        time.sleep(0.1)

    print(f"  {order_count} orders processed")
    for field, kg in sorted(totals.items()):
        print(f"  {field}: {kg:.1f}kg")

    print(f"\n[4/4] Writing to Supabase...")
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    sb.table("roast_weekly").upsert({
        "week_start": str(week_start_date),
        "village_blend_kg": round(totals.get("village_blend_kg", 0), 2),
        "cloud_nine_kg":    round(totals.get("cloud_nine_kg", 0), 2),
        "euphoria_kg":      round(totals.get("euphoria_kg", 0), 2),
        "decaf_kg":         round(totals.get("decaf_kg", 0), 2),
        "vietnam_kho_kg":   round(totals.get("vietnam_kho_kg", 0), 2),
        "rising_sun_kg":    round(totals.get("rising_sun_kg", 0), 2),
        "venue_milk_kg":    round(totals.get("venue_milk_kg", 0), 2),
        "venue_black_kg":   round(totals.get("venue_black_kg", 0), 2),
        "total_orders":     order_count,
        "updated_at":       datetime.now(timezone.utc).isoformat(),
    }, on_conflict="week_start").execute()

    print(f"  Week {week_start_date} written ✓")
    print("=" * 55)

if __name__ == "__main__":
    main()
