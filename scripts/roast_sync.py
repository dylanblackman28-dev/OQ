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
    Returns the most recently COMPLETED ordering week.
    Week runs: Tuesday 12:00 noon AEST -> following Tuesday 11:59 AM AEST.
    Late orders (Tue 00:00-11:59) are counted INTO the week that is closing,
    since fulfilment packs and ships them same-day.

    Example running Tuesday 30 June 2026 (any time):
      - This Tuesday noon = 30 June 12:00
      - If now < this Tuesday noon -> still in last week's late window,
        so the week closing is Tue 23 June 12:00 -> Tue 30 June 11:59
      - If now >= this Tuesday noon -> that week just closed,
        so the week closing is also Tue 23 June 12:00 -> Tue 30 June 11:59
        (the new week, Tue 30 June 12:00 onwards, has not closed yet)
    """
    now_aest = datetime.now(AEST)
    days_since_tuesday = (now_aest.weekday() - 1) % 7
    this_tuesday_noon = (now_aest - timedelta(days=days_since_tuesday)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    # If we haven't reached this Tuesday's noon cutover yet, the week that's
    # closing is the one before it
    if now_aest < this_tuesday_noon:
        this_tuesday_noon -= timedelta(days=7)
    week_start = this_tuesday_noon - timedelta(days=7)   # Tue noon, 1 week before cutover
    week_end = this_tuesday_noon.replace(hour=11, minute=59, second=59)  # cutover Tue 11:59am
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
    rising_sun_dates = []

    for order in all_orders:
        if order.get("cancelled"): continue
        order_count += 1
        detail = om_get(f"https://app.ordermentum.com/v1/orders/{order['id']}", token)
        order_has_rising_sun = False
        for item in detail.get("lineItems", []):
            field = classify(item.get("name", ""), item.get("SKU", ""))
            if not field: continue
            kg = extract_kg(item.get("name", ""), item.get("quantity", 0) or 0)
            totals[field] += kg
            if field == "rising_sun_kg":
                order_has_rising_sun = True
        if order_has_rising_sun:
            created_at = order.get("createdAt", "")
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(AEST)
                rising_sun_dates.append(dt.strftime("%d/%m/%y"))
            except Exception:
                pass
        time.sleep(0.1)

    print(f"  {order_count} orders processed")
    for field, kg in sorted(totals.items()):
        print(f"  {field}: {kg:.1f}kg")
    if rising_sun_dates:
        print(f"  Rising Sun orders on: {', '.join(rising_sun_dates)}")

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
        "rising_sun_order_count": len(rising_sun_dates),
        "rising_sun_order_dates": ", ".join(rising_sun_dates),
        "updated_at":       datetime.now(timezone.utc).isoformat(),
    }, on_conflict="week_start").execute()

    # Write sync timestamp so dashboard can show "last updated by workflow"
    sb.table("sync_log").upsert({
        "id": "roast",
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "synced_by": "github_actions",
    }, on_conflict="id").execute()

    print(f"  Week {week_start_date} written ✓")
    print("=" * 55)

if __name__ == "__main__":
    main()
