"""
OQ Roast Plan Weekly Sync
Runs every Tuesday 6:30am AEST via GitHub Actions.
"""

import os, re, json, time, random, urllib.request
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OM_API_KEY   = os.environ["OM_API_KEY"]
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

class OMFetchError(RuntimeError):
    """A fetch we cannot proceed without (silently skipping it corrupts totals)."""


def om_get(url, required=False, attempts=6):
    """
    GET with retry + backoff.

    Ordermentum rate-limits (HTTP 429). A swallowed 429 makes an order look like
    it has no line items, so order counts stay correct while kilos silently
    collapse — exactly the failure that understated the 25 Aug week. Anything we
    cannot do without is fetched with required=True, which raises rather than
    returning empty, so the run fails loudly instead of writing wrong numbers.
    """
    delay = 2.0
    last = "unknown"
    for _ in range(attempts):
        req = urllib.request.Request(url, headers={"x-api-key": OM_API_KEY})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (401, 403):
                raise SystemExit(
                    f"Ordermentum auth failed ({e.code}) for {url} — check OM_API_KEY")
            if e.code == 429 or e.code >= 500:
                retry_after = (e.headers or {}).get("Retry-After")
                try:
                    wait = float(retry_after)
                except (TypeError, ValueError):
                    wait = delay
                time.sleep(min(wait, 60) + random.uniform(0, 0.4))
                delay = min(delay * 2, 60)
                continue
            break
        except Exception as e:
            last = str(e)[:120]
            time.sleep(delay)
            delay = min(delay * 2, 60)
    if required:
        raise OMFetchError(f"Ordermentum fetch failed after {attempts} attempts "
                           f"({last}): {url}")
    print(f"  Warning: GET failed for {url}: {last}")
    return {}


def retry_db(op, attempts=5):
    """
    Run a Supabase/PostgREST call with retry on transient gateway errors.

    PostgREST intermittently returns 502/503/504 (a Gateway Timeout on a trivial
    select aborted a whole wholesale sync on 14 Sep, leaving that week written
    for only 3 of 24 partners). Every call here is an idempotent read or upsert,
    so retrying is safe, and a partial write is far worse than a slow one.
    """
    delay = 2.0
    for attempt in range(attempts):
        try:
            return op()
        except Exception as e:
            msg = str(e)
            transient = any(s in msg for s in (
                "502", "503", "504", "Gateway Timeout", "timeout",
                "timed out", "Connection", "Server disconnected"))
            if not transient or attempt == attempts - 1:
                raise
            print(f"  Supabase transient error ({msg[:70]}) — "
                  f"retry {attempt + 1}/{attempts - 1} in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 30)

def om_pages(meta, page_size, got):
    """
    Last page number, tolerant of either meta shape.
    app.ordermentum.com/v2 returned meta.totalPages; api.ordermentum.com/v2 is
    documented as meta.totalResults/pageSize/pageNo. If neither is present we
    fall back to "keep going while the page came back full", so a missing field
    can never silently truncate a sync to one page.
    """
    if not isinstance(meta, dict):
        return None
    if meta.get("totalPages"):
        return int(meta["totalPages"])
    total = meta.get("totalResults")
    size = meta.get("pageSize") or page_size
    if total and size:
        return -(-int(total) // int(size))   # ceil
    return None

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

    # Blends are identified by their SKU token (4th segment), NOT the product
    # name. Names are unreliable: "Laos - PDK Village Natural | Filter Roast"
    # (OQ-COF-WHS-FLT-*) is a single origin whose farm name contains "Village"
    # and was being counted as Village Blend.
    #   VG = Village Blend, HS = Cloud Nine, EPH = Euphoria, DC = Decaf
    parts = s.split("-")
    token = parts[3] if len(parts) > 3 else ""
    BLEND_TOKENS = {
        "VG": "village_blend_kg", "HS": "cloud_nine_kg",
        "EPH": "euphoria_kg", "DC": "decaf_kg",
    }
    if token in BLEND_TOKENS:
        return BLEND_TOKENS[token]
    if "k'ho" in n or "kho" in n or "vietnam" in n: return "vietnam_kho_kg"
    # Single origin / filter roasts are never blends, whatever the name says
    if token == "FLT" or "filter roast" in n:
        return None
    # Legacy fallback for older SKUs that predate the token scheme
    if "village blend" in n:  return "village_blend_kg"
    if "cloud nine" in n:     return "cloud_nine_kg"
    if "euphoria" in n:       return "euphoria_kg"
    if "decaf" in n:          return "decaf_kg"
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

def sync_week(sb, weeks_ago):
    start_utc, end_utc, week_start_date = ordering_week_range(weeks_ago)
    label = "current (open)" if weeks_ago == 0 else "previous (late orders)"
    print(f"\n[{label}] Pulling orders: {week_start_date} → {week_start_date + timedelta(days=6)}")
    print(f"  (UTC: {start_utc} → {end_utc})")

    all_orders = []
    page = 1
    while True:
        url = (f"https://api.ordermentum.com/v2/orders"
               f"?supplierId={SUPPLIER_ID}"
               f"&createdAt[gte]={start_utc}"
               f"&createdAt[lte]={end_utc}"
               f"&pageSize=50&pageNo={page}")
        data = om_get(url, required=True)
        batch = data.get("data", [])
        all_orders.extend(batch)
        last = om_pages(data.get("meta"), 50, len(batch))
        if (last is not None and page >= last) or (last is None and len(batch) < 50): break
        page += 1
        time.sleep(0.2)
    print(f"  {len(all_orders)} orders pulled")

    print(f"  Processing line items...")
    totals = defaultdict(float)
    cb_qty = defaultdict(float)
    cold_brew_litres = 0.0
    cb_rows = []   # per-customer cold brew lines for the tally dashboard
    blend_rows = []  # per-customer blend lines for the dashboard order modals
    order_count = 0
    rising_sun_dates = []
    # Fields that get per-customer order detail on the dashboard.
    # K'Ho and Rising Sun deliberately excluded — no order modal for those.
    BLEND_DETAIL_FIELDS = {
        "village_blend_kg", "cloud_nine_kg", "euphoria_kg", "decaf_kg",
        "venue_milk_kg", "venue_black_kg",
    }
    # Bean/equipment SKU prefixes that legitimately mention "cold brew" but
    # are never brewed litres — not flagged as unknown variants
    CB_IGNORE_PREFIXES = ("OQ-COF", "TOD-", "HR-", "OQ-MISC")

    for order in all_orders:
        if order.get("cancelled"): continue
        order_count += 1
        detail = om_get(f"https://api.ordermentum.com/v1/orders/{order['id']}", required=True)
        order_has_rising_sun = False
        retailer_name = order.get("retailerName", "") or ""
        order_number = order.get("orderNumber") or order.get("number") or None
        placed_at = order.get("createdAt") or None
        delivery_date = order.get("deliveryDate") or None
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
                    "order_number": order_number,
                    "placed_at": placed_at,
                    "delivery_date": delivery_date,
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
                    "order_number": order_number,
                    "placed_at": placed_at,
                    "delivery_date": delivery_date,
                })
            field = classify(item.get("name", ""), item.get("SKU", ""))
            if not field: continue
            qty = item.get("quantity", 0) or 0
            kg = extract_kg(item.get("name", ""), qty)
            totals[field] += kg
            if field in BLEND_DETAIL_FIELDS:
                blend_rows.append({
                    "retailer_name": retailer_name, "field": field,
                    "sku": sku, "product_name": item_name,
                    "qty": float(qty), "kg": round(kg, 2),
                    "order_number": order_number,
                    "placed_at": placed_at,
                    "delivery_date": delivery_date,
                })
            if field == "rising_sun_kg":
                order_has_rising_sun = True
        if order_has_rising_sun:
            created_at = order.get("createdAt", "")
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(AEST)
                rising_sun_dates.append(dt.strftime("%d/%m/%y"))
            except Exception:
                pass
        time.sleep(0.5)

    print(f"  {order_count} orders processed")
    for field, kg in sorted(totals.items()):
        print(f"  {field}: {kg:.1f}kg")
    if rising_sun_dates:
        print(f"  Rising Sun orders on: {', '.join(rising_sun_dates)}")

    print(f"  Writing to Supabase...")
    retry_db(lambda: sb.table("roast_weekly").upsert({
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
    }, on_conflict="week_start"))

    # Per-customer cold brew lines for the tally dashboard: replace the week
    retry_db(lambda: sb.table("cold_brew_orders").delete().eq(
        "week_start", str(week_start_date)))
    if cb_rows:
        for r in cb_rows:
            r["week_start"] = str(week_start_date)
        retry_db(lambda: sb.table("cold_brew_orders").insert(cb_rows))

    # Per-customer blend lines for the dashboard order modals: replace the week
    retry_db(lambda: sb.table("blend_orders").delete().eq(
        "week_start", str(week_start_date)))
    if blend_rows:
        for r in blend_rows:
            r["week_start"] = str(week_start_date)
        retry_db(lambda: sb.table("blend_orders").insert(blend_rows))

    print(f"  Week {week_start_date} written ✓ "
          f"({len(cb_rows)} cold brew lines, {len(blend_rows)} blend lines)")


def main():
    backfill_weeks = int(os.environ.get("BACKFILL_WEEKS", "0") or 0)
    backfill_offset = int(os.environ.get("BACKFILL_OFFSET", "0") or 0)

    print("=" * 55)
    print(f"OQ Roast Sync — {datetime.now(AEST).strftime('%A %d %B %Y %I:%M %p AEST')}")
    if backfill_weeks or backfill_offset:
        print(f"BACKFILL MODE: weeks {backfill_offset} to "
              f"{backfill_offset + backfill_weeks} ago")
    print("=" * 55)

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("\n[2/3] Syncing week(s)...")
    if backfill_weeks or backfill_offset:
        week_offsets = range(backfill_offset + backfill_weeks,
                             backfill_offset - 1, -1)
    else:
        # Normal run: previous week (late orders) + current open week
        week_offsets = (1, 0)
    for weeks_ago in week_offsets:
        sync_week(sb, weeks_ago)

    # Write sync timestamp so dashboard can show "last updated by workflow"
    retry_db(lambda: sb.table("sync_log").upsert({
        "id": "roast",
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "synced_by": "github_actions",
    }, on_conflict="id"))

    print("\n[3/3] Done ✓")
    print("=" * 55)

if __name__ == "__main__":
    main()
