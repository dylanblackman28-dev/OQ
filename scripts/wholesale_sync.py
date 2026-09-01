"""
OQ Wholesale Weekly Sync
Runs every Thursday 6:30am AEST via GitHub Actions.

Ordering week: Tuesday 00:00 AEST -> Monday 23:59 AEST
Late order: placed Tuesday 00:01 -> Wednesday 23:59 AEST AND contains OQ-COF-WHS SKU.
Only tracked partners (TRACKED_PARTNERS) are processed. All others ignored.
"""

import os, random
import re
import time
import json
import urllib.request, urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from supabase import create_client

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OM_API_KEY   = os.environ["OM_API_KEY"]
SUPPLIER_ID  = "71bf79dc-4e3d-41b2-b232-6ebe51a297ab"

AEST = timezone(timedelta(hours=10))

# ── Tracked partners whitelist ────────────────────────────────────────────────
# Only these retailer IDs are processed. Add new partners here when onboarded.
# Format: "ordermentum_retailer_id": "Display Name"
TRACKED_PARTNERS = {
    "3ef953b2-e261-4367-8d90-ff33901c9825": "Rising Sun Roasters",
    "97d05e3e-ff04-4e75-a8ef-4df59ffd4b5e": "Steam Espresso",
    "c446588c-2712-4df9-8204-ed6616c3a6da": "Hitched",
    "3c451bf8-5ac4-458c-bdb9-636352c79bda": "Crafty Monk Brewing",
    "e12b57cc-6eb7-44b1-aff2-33d9b6aa354a": "Muse Yamba",
    "7302e01e-ca4d-4268-8848-a2c3b9bc2f44": "Tel Aviv Yafo Express",
    "085a631b-353f-4da2-9620-93b2fbb0525d": "5 Church Street",
    "46e02c42-5582-4c54-afab-65bcb06c57a3": "Miquette",
    "7f308671-4caa-494e-a43a-c15ee3bfb2f6": "Jetty Beach House Coffs Harbour",
    "ab47b25c-c5e1-43a7-be32-4e24c35b5eea": "HAP Melbourne",
    "c2f42d7f-f3fd-405e-9aba-24746e35daed": "Roxy Lane Cafe",
    "df56d4e1-39f8-4d84-a4e2-fb471500ca2d": "Kefi Cafe",
    "31d5d05a-7419-430a-8d5c-d673578b6822": "Golden Hash",
    "fab2d4f8-e0fd-466b-ad50-a04a8e4171d3": "Rara Van Bar",
    "63b6d5f9-c23f-454d-af22-429531855c8f": "Bowradise",
    "1714ab15-545e-4f6e-82dc-e14a71723555": "Makers Grain Bakehouse",
    "c1ff495f-cf3a-4e4c-b155-207f3f1f676f": "Ballandean General Store",
    "201ac1b8-1be9-40e8-91bc-1198b9afc475": "Deepwater Bakery",
    "046533de-4a89-4a8e-a4d6-afd9ab0e1e02": "M|Arts Cafe & Bar",
    "10cb371c-5a32-4ac3-8074-d15d7c5db2aa": "Tayzies Coffee + Kitchen",
    "b7cf496e-a8b8-496e-8ca7-b7a6623d7545": "Bar Henry",
    "9daccd46-58d7-491d-8589-d90af3fca25b": "Pour Good",
    "c3af10cb-5b3f-43a5-b0fa-548394c38fef": "The Quick Brown Fox Cafe",
    "b6b8cc4d-fad1-4cf6-bc2a-c28fdfd23b3a": "Bep Coffee",
    "9790cf30-0a9d-45ac-a990-1f84eef1a227": "Taco Love Bros",
    "ab801b2c-09a6-474b-a56a-d7a06ae5a07b": "Capiche",
    "bec061dc-0668-40a2-ba30-fe033072b6e6": "Capiche Kiosk",
    "236041d5-50ef-4b89-918e-efef35576a20": "The Dove",
    "2ac031e6-eb44-47d0-bb63-84c672242e54": "Tel Aviv Yafo",
    "a024b93a-d3d7-40e8-8786-fe347ce20096": "Dent Coffee",
    "f2e095d9-e178-4ebd-89db-1dad27bcb36d": "Bangalow Bread Co",
    "702d98d0-663e-42e2-bb4b-1a5ccbaf8c67": "The Treehouse Byron Bay",
    "fa44602b-a7cd-4b36-ab68-22a634df62a3": "Bang Bang Byron Bay",
    "627e716c-d0f0-4e37-9df4-ce7a2c555cd3": "The Little Byronian",
    "72f013b4-1da9-46fb-a04d-b2597a806825": "Williams Street Kitchen And Bar",
    "ac0a7ef1-1cc5-4074-84e0-7a72c753566f": "The Olive Norfolk Island",
    "d8660838-5877-41b5-9af5-1c42d10b00d9": "Alstonville Country Cottages",
    "03b08341-ee46-4560-954c-865e58c15a1b": "North Coast Community College",
    "16d0ded8-95aa-4d65-b350-442384dd6173": "Bob's Tacos",
    "711e1512-494c-4970-ac75-7a93861394ae": "Raised Cold Brew",
}

# ── Ordermentum helpers ───────────────────────────────────────────────────────

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


def is_whs_coffee_sku(sku):
    """Only OQ-COF-WHS SKUs count — brewing wholesale coffee only."""
    return (sku or "").upper().startswith("OQ-COF-WHS")


def is_tracked_blend(name, sku):
    """
    This tracker measures BLEND kg only — the core wholesale product Amelia
    uses for pricing / retention / upsell analysis. Rather than trying to
    exclude an ever-growing list of non-blends (decaf, single origins, limited
    releases like Sirinya / Mae Chedi cold brew), we POSITIVELY allow-list the
    three blends. Anything else carrying an OQ-COF-WHS SKU — every current and
    future single origin or limited release — is automatically excluded from kg.

    Non-blend coffees still mark the order as a coffee order, so order_count /
    revenue / late tracking / LTV are unaffected — only kg_ordered changes.

    Tracked blends (confirmed by Dylan, Jul 2026): Village Blend, Cloud Nine,
    Euphoria. Decaf is excluded even when it's a decaf version of a blend.
    """
    n = (name or "").lower()
    s = (sku or "").upper()
    if "decaf" in n or "-DC-" in s or "-DEC" in s:
        return False
    # The SKU token (4th segment) is the authority, not the product name.
    # "Laos - PDK Village Natural | Filter Roast" (OQ-COF-WHS-FLT-*) is a single
    # origin whose farm name contains "Village" — name matching counted it as
    # Village Blend. Any token that isn't a tracked blend is excluded, so new
    # single origins and limited releases stay out automatically.
    parts = s.split("-")
    token = parts[3] if len(parts) > 3 else ""
    if token:
        return token in ("VG", "HS", "EPH")   # Village, Cloud Nine, Euphoria
    # Legacy fallback for older SKUs that predate the token scheme
    if "filter roast" in n:
        return False
    return ("village blend" in n or "cloud nine" in n or "euphoria" in n)


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
def ordering_week_range(weeks_ago=0):
    """
    Returns an ordering week window.
    Week runs: Tuesday 12:00 noon AEST -> following Tuesday 11:59 AM AEST.
    Late orders: Tuesday 00:00 -> Tuesday 11:59 AM (before noon cutover).

    weeks_ago=0 is the CURRENT (open, possibly incomplete) ordering week —
    synced so the dashboard shows live data mid-week. weeks_ago=1 is the
    most recently completed week, re-synced each run for late orders.
    week_start returned is the DATE of the Tuesday, used as the DB key.
    """
    now_aest = datetime.now(AEST)
    # Find the most recent Tuesday noon cutover at or before now
    days_since_tuesday = (now_aest.weekday() - 1) % 7
    this_tuesday = (now_aest - timedelta(days=days_since_tuesday)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    if now_aest < this_tuesday:
        this_tuesday -= timedelta(days=7)
    week_start_dt = this_tuesday - timedelta(weeks=weeks_ago)  # Tue 12:00 noon AEST
    week_end_dt = (week_start_dt + timedelta(days=7)).replace(
        hour=11, minute=59, second=59, microsecond=0)  # Following Tue 11:59 AM
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        week_start_dt.astimezone(timezone.utc).strftime(fmt),
        week_end_dt.astimezone(timezone.utc).strftime(fmt),
        week_start_dt.date(),  # DATE of the Tuesday = DB key
    )


def is_in_late_window(created_at_str):
    """Late = Tuesday 00:00 -> Tuesday 11:59 AM AEST (before the noon week cutover)."""
    try:
        dt = datetime.fromisoformat(
            created_at_str.replace("Z", "+00:00")).astimezone(AEST)
        return dt.weekday() == 1 and dt.hour < 12  # Tuesday before noon
    except Exception:
        return False


# ── Pull orders ───────────────────────────────────────────────────────────────
def pull_orders(start_utc, end_utc):
    all_orders = []
    page = 1
    while True:
        url = (
            f"https://api.ordermentum.com/v2/orders"
            f"?supplierId={SUPPLIER_ID}"
            f"&createdAt[gte]={start_utc}"
            f"&createdAt[lte]={end_utc}"
            f"&pageSize=50&pageNo={page}"
        )
        data = om_get(url, required=True)
        orders = data.get("data", [])
        all_orders.extend(orders)
        total_pages = om_pages(data.get("meta"), 50, len(orders))
        print(f"  Orders page {page}/{total_pages or '?'} ({len(all_orders)} so far)")
        if (total_pages is not None and page >= total_pages) or \
           (total_pages is None and len(orders) < 50):
            break
        page += 1
        time.sleep(0.2)
    return all_orders


# ── Process line items ────────────────────────────────────────────────────────
def process_orders(all_orders):
    """
    Only processes orders from TRACKED_PARTNERS.
    Only counts OQ-COF-WHS SKUs for kg and late flag.
    Partners with zero WHS coffee kg are excluded entirely.
    """
    partner_data = defaultdict(lambda: {
        "name": "",
        "retailer_id": "",
        "kg": 0.0,
        "order_count": 0,
        "late_count": 0,
        "revenue": 0.0,
        "last_blend_date": None,   # date of most recent order containing BLEND kg
    })

    for i, order in enumerate(all_orders):
        if order.get("cancelled"):
            continue

        retailer_id = order.get("retailerId", "")

        # Skip anyone not on the whitelist
        if retailer_id not in TRACKED_PARTNERS:
            continue

        detail = om_get(
            f"https://api.ordermentum.com/v1/orders/{order['id']}", required=True)

        # Calculate WHS BLEND kg for this order. Only the three tracked blends
        # count toward kg; every other wholesale coffee (single origins, limited
        # releases, decaf) still marks the order as a coffee order — so
        # order_count / revenue / late tracking are unaffected — but adds no kg.
        # See is_tracked_blend().
        whs_kg = 0.0
        has_whs_coffee = False
        for item in detail.get("lineItems", []):
            sku = item.get("SKU", "") or ""
            if not is_whs_coffee_sku(sku):
                continue
            has_whs_coffee = True
            name = item.get("name", "") or ""
            if not is_tracked_blend(name, sku):
                continue
            qty = item.get("quantity", 0) or 0
            whs_kg += extract_kg(name, qty)

        # Only count this order if it has WHS coffee
        if not has_whs_coffee:
            continue

        p = partner_data[retailer_id]
        p["name"] = TRACKED_PARTNERS[retailer_id]
        p["retailer_id"] = retailer_id
        p["kg"] += whs_kg
        p["order_count"] += 1
        p["revenue"] += float(order.get("total", 0) or 0)

        # Track the most recent order that actually contained BLEND kg.
        # Retail-only / single-origin / decaf-only orders must NOT count here —
        # the dashboard alerts on partners who've stopped ordering blends even
        # if they're still ordering other products.
        if whs_kg > 0:
            try:
                d = datetime.fromisoformat(
                    (order.get("createdAt") or "").replace("Z", "+00:00")
                ).astimezone(AEST).date()
                if p["last_blend_date"] is None or d > p["last_blend_date"]:
                    p["last_blend_date"] = d
            except Exception:
                pass

        # Late only if WHS coffee order placed in Tue/Wed window
        if is_in_late_window(order.get("createdAt", "")):
            p["late_count"] += 1

        if (i + 1) % 10 == 0:
            print(f"  Processed {i+1}/{len(all_orders)} orders...")
        time.sleep(0.5)

    return partner_data


# ── Supabase upserts ──────────────────────────────────────────────────────────
def upsert_partner(sb, retailer_id, name, first_order_date=None):
    existing = sb.table("partners").select("id, first_order_date").eq(
        "ordermentum_retailer_id", retailer_id
    ).execute()

    if existing.data:
        partner_id = existing.data[0]["id"]
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


def update_last_blend_order_date(sb, partner_id, blend_date):
    """
    Store the partner's most recent BLEND order date, only ever moving it
    forward. Syncs process one or two weeks at a time (and backfills run
    oldest-first), so an older run must never overwrite a newer date.
    """
    if not blend_date:
        return
    existing = sb.table("partners").select("last_blend_order_date").eq(
        "id", partner_id).execute()
    current = existing.data[0].get("last_blend_order_date") if existing.data else None
    if current and str(current) >= str(blend_date):
        return
    sb.table("partners").update({
        "last_blend_order_date": str(blend_date),
    }).eq("id", partner_id).execute()


def upsert_weekly_order(sb, partner_id, week_start, kg, order_count,
                        late_count, revenue):
    sb.table("weekly_orders").upsert({
        "partner_id": partner_id,
        "week_start": str(week_start),
        "kg_ordered": round(kg, 2),
        "order_count": order_count,
        "late_order_count": late_count,
        "total_revenue": round(revenue, 2),
    }, on_conflict="partner_id,week_start").execute()


def refresh_order_summary(sb, partner_id):
    rows = sb.table("weekly_orders").select("*").eq(
        "partner_id", partner_id).execute()
    if not rows.data:
        return

    total_revenue = sum(float(r["total_revenue"] or 0) for r in rows.data)
    total_orders  = sum(int(r["order_count"] or 0) for r in rows.data)
    total_late    = sum(int(r["late_order_count"] or 0) for r in rows.data)
    total_kg      = sum(float(r["kg_ordered"] or 0) for r in rows.data)
    late_rate     = round(
        (total_late / total_orders * 100), 2) if total_orders > 0 else 0

    partner = sb.table("partners").select("first_order_date").eq(
        "id", partner_id).execute()
    first_date = partner.data[0].get(
        "first_order_date") if partner.data else None
    years = 0.0
    if first_date:
        delta = (datetime.now(timezone.utc).date() -
                 datetime.fromisoformat(str(first_date)).date())
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


# ── First order date ──────────────────────────────────────────────────────────
def get_first_order_date(retailer_id):
    url = f"https://api.ordermentum.com/v1/purchasers/{retailer_id}"
    data = om_get(url, required=True)
    if data:
        activated = data.get("activatedAt") or data.get("firstOrderedAt")
        if activated:
            try:
                return datetime.fromisoformat(
                    activated.replace("Z", "+00:00")).date()
            except Exception:
                pass
    return None


# ── Main ──────────────────────────────────────────────────────────────────────
def sync_week(sb, weeks_ago):
    start_utc, end_utc, week_start_date = ordering_week_range(weeks_ago)
    print(f"\n[week] Pulling orders for week: "
          f"{week_start_date} -> {week_start_date + timedelta(days=6)}")
    print(f"  UTC window: {start_utc} -> {end_utc}")

    all_orders = pull_orders(start_utc, end_utc)
    print(f"  {len(all_orders)} orders pulled ✓")

    print(f"  Processing (tracked partners + OQ-COF-WHS SKUs only)...")
    partner_data = process_orders(all_orders)
    print(f"  {len(partner_data)} partners with WHS coffee orders ✓")

    for retailer_id, data in partner_data.items():
        print(f"  → {data['name']}: {data['kg']:.1f}kg, "
              f"{data['order_count']} orders, {data['late_count']} late")
        first_date = get_first_order_date(retailer_id)
        time.sleep(0.1)
        partner_id = upsert_partner(
            sb, retailer_id, data["name"], first_date)
        upsert_weekly_order(
            sb, partner_id, week_start_date,
            data["kg"], data["order_count"], data["late_count"], data["revenue"]
        )
        update_last_blend_order_date(sb, partner_id, data["last_blend_date"])
        refresh_order_summary(sb, partner_id)

    print(f"  Week {week_start_date}: {len(partner_data)} partners synced ✓")
    return week_start_date


def main():
    backfill_weeks = int(os.environ.get("BACKFILL_WEEKS", "0") or 0)
    # Offset lets a long backfill run in chunks, e.g. offset=52 weeks=51
    # rewrites weeks 52-103 ago without touching more recent weeks.
    backfill_offset = int(os.environ.get("BACKFILL_OFFSET", "0") or 0)

    print("=" * 60)
    print(f"OQ Wholesale Sync — "
          f"{datetime.now(AEST).strftime('%A %d %B %Y %I:%M %p AEST')}")
    if backfill_weeks or backfill_offset:
        print(f"BACKFILL MODE: weeks {backfill_offset} to "
              f"{backfill_offset + backfill_weeks} ago")
    print("=" * 60)

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    print(f"\n[2/3] Syncing week(s)...")
    if backfill_weeks or backfill_offset:
        # Oldest first so refresh_order_summary ends on complete data
        week_offsets = range(backfill_offset + backfill_weeks,
                             backfill_offset - 1, -1)
    else:
        # Normal run: previous week (late orders) + current open week
        week_offsets = (1, 0)
    for weeks_ago in week_offsets:
        sync_week(sb, weeks_ago)

    # Write sync timestamp so dashboard can show "last updated by workflow"
    sb.table("sync_log").upsert({
        "id": "wholesale",
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "synced_by": "github_actions",
    }, on_conflict="id").execute()

    print(f"\n[3/3] Done ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()
