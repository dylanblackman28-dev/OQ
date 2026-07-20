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

# Cold brew variants, measured in LITRES (not kg).
# SKU -> (qty db field, litres per unit)
CB_VARIANTS = {
    "OQ-CLD-BR-1LT":   ("cb_1lt_qty",        1.0),
    "OQ-CLD-BR-5LT":   ("cb_5lt_qty",        5.0),
    "OQ-CLD-BR-330ML": ("cb_330ml_qty",      0.33),
    "OQ-CLD-BR-20LT":  ("cb_nitro_20lt_qty", 20.0),  # Nitro — OQ Ballina summer
    "OQ-CLD-BR-10LT":  ("cb_nitro_10lt_qty", 10.0),  # Nitro — OQ Ballina winter
}

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

def ordering_week_range(weeks_ago=0):
    """
    Returns an ordering week window.
    Week runs: Tuesday 12:00 noon AEST -> following Tuesday 11:59 AM AEST.
    Late orders (Tue 00:00-11:59) are counted INTO the week that is closing,
    since fulfilment packs and ships them same-day.

    weeks_ago=0 is the CURRENT (open, possibly incomplete) ordering week —
    synced so the tracker shows live data mid-week. weeks_ago=1 is the most
    recently completed week, re-synced each run to pick up late orders.
    """
    now_aest = datetime.now(AEST)
    days_since_tuesday = (now_aest.weekday() - 1) % 7
    this_tuesday_noon = (now_aest - timedelta(days=days_since_tuesday)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    # Before this Tuesday's noon cutover, the open week started last Tuesday
    if now_aest < this_tuesday_noon:
        this_tuesday_noon -= timedelta(days=7)
    week_start = this_tuesday_noon - timedelta(weeks=weeks_ago)
    week_end = (week_start + timedelta(days=7)).replace(
        hour=11, minute=59, second=59)  # cutover Tue 11:59am
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
    # Mae Chedi single origin (cold brew beans, incl. retail bags) is roasted
    # in batches outside this tracker — deliberately not classified
    if "mae chedi" in n or "cold brew release" in n: return None
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

def sync_week(token, sb, weeks_ago):
    start_utc, end_utc, week_start_date = ordering_week_range(weeks_ago)
    label = "current (open)" if weeks_ago == 0 else "previous (late orders)"
    print(f"\n[{label}] Pulling orders: {week_start_date} → {week_start_date + timedelta(days=6)}")
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

    print(f"  Processing line items...")
    totals = defaultdict(float)
    cb_qty = defaultdict(float)
    cold_brew_litres = 0.0
    cb_rows = []   # per-customer cold brew lines for the tally dashboard
    order_count = 0
    rising_sun_dates = []
    # Bean/equipment SKU prefixes that legitimately mention "cold brew" but
    # are never brewed litres — not flagged as unknown variants
    CB_IGNORE_PREFIXES = ("OQ-COF", "TOD-", "HR-", "OQ-MISC")

    for order in all_orders:
        if order.get("cancelled"): continue
        order_count += 1
        detail = om_get(f"https://app.ordermentum.com/v1/orders/{order['id']}", token)
        order_has_rising_sun = False
        retailer_name = order.get("retailerName", "") or ""
        for item in detail.get("lineItems", []):
            sku = (item.get("SKU", "") or "").upper()
            item_name = item.get("name", "") or ""
            if sku in CB_VARIANTS:
                qty_field, litres_per_unit = CB_VARIANTS[sku]
                q = float(item.get("quantity", 0) or 0)
                # Venue orders of bottle/bucket variants tracked separately
                # (nitro is already its own field — always Ballina)
                if "nitro" in qty_field:
                    category = "nitro"
                elif is_venue_order(retailer_name):
                    category = "venue"
                    qty_field = qty_field.replace("cb_", "cb_venue_")
                else:
                    category = "wholesale"
                cb_qty[qty_field] += q
                cold_brew_litres += q * litres_per_unit
                cb_rows.append({
                    "retailer_name": retailer_name, "sku": sku,
                    "product_name": item_name, "qty": q,
                    "litres": round(q * litres_per_unit, 2),
                    "category": category,
                })
                continue
            # Unknown cold brew variant (renamed product / new size) — flag it
            if ("cold brew" in item_name.lower()
                    and not sku.startswith(CB_IGNORE_PREFIXES)):
                cb_rows.append({
                    "retailer_name": retailer_name, "sku": sku,
                    "product_name": item_name,
                    "qty": float(item.get("quantity", 0) or 0),
                    "litres": 0, "category": "unknown",
                })
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

    print(f"  Writing to Supabase...")
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
        "cb_1lt_qty":        round(cb_qty.get("cb_1lt_qty", 0), 2),
        "cb_5lt_qty":        round(cb_qty.get("cb_5lt_qty", 0), 2),
        "cb_330ml_qty":      round(cb_qty.get("cb_330ml_qty", 0), 2),
        "cb_venue_1lt_qty":   round(cb_qty.get("cb_venue_1lt_qty", 0), 2),
        "cb_venue_5lt_qty":   round(cb_qty.get("cb_venue_5lt_qty", 0), 2),
        "cb_venue_330ml_qty": round(cb_qty.get("cb_venue_330ml_qty", 0), 2),
        "cb_nitro_10lt_qty": round(cb_qty.get("cb_nitro_10lt_qty", 0), 2),
        "cb_nitro_20lt_qty": round(cb_qty.get("cb_nitro_20lt_qty", 0), 2),
        "cold_brew_litres":  round(cold_brew_litres, 2),
        "updated_at":       datetime.now(timezone.utc).isoformat(),
    }, on_conflict="week_start").execute()

    # Per-customer cold brew lines for the tally dashboard: replace the week
    sb.table("cold_brew_orders").delete().eq(
        "week_start", str(week_start_date)).execute()
    if cb_rows:
        for r in cb_rows:
            r["week_start"] = str(week_start_date)
        sb.table("cold_brew_orders").insert(cb_rows).execute()

    print(f"  Week {week_start_date} written ✓ ({len(cb_rows)} cold brew lines)")


def main():
    backfill_weeks = int(os.environ.get("BACKFILL_WEEKS", "0") or 0)
    backfill_offset = int(os.environ.get("BACKFILL_OFFSET", "0") or 0)

    print("=" * 55)
    print(f"OQ Roast Sync — {datetime.now(AEST).strftime('%A %d %B %Y %I:%M %p AEST')}")
    if backfill_weeks or backfill_offset:
        print(f"BACKFILL MODE: weeks {backfill_offset} to "
              f"{backfill_offset + backfill_weeks} ago")
    print("=" * 55)

    print("\n[1/3] Authenticating...")
    token = om_auth()
    print("  OK")

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("\n[2/3] Syncing week(s)...")
    if backfill_weeks or backfill_offset:
        week_offsets = range(backfill_offset + backfill_weeks,
                             backfill_offset - 1, -1)
    else:
        # Normal run: previous week (late orders) + current open week
        week_offsets = (1, 0)
    for weeks_ago in week_offsets:
        sync_week(token, sb, weeks_ago)

    # Write sync timestamp so dashboard can show "last updated by workflow"
    sb.table("sync_log").upsert({
        "id": "roast",
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "synced_by": "github_actions",
    }, on_conflict="id").execute()

    print("\n[3/3] Done ✓")
    print("=" * 55)

if __name__ == "__main__":
    main()
