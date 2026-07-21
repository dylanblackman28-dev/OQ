"""
Cold Brew probe — READ ONLY, writes nothing to Supabase.
Scans recent ordering weeks for OQ-CLD-BR* SKUs and prints qty + litres
per variant so the tracking rules can be approved before wiring them
into roast_sync.py.
"""

import os, json, time, urllib.request
from datetime import datetime, timedelta, timezone
from collections import defaultdict

OM_USERNAME = os.environ["OM_USERNAME"]
OM_PASSWORD = os.environ["OM_PASSWORD"]
SUPPLIER_ID = "71bf79dc-4e3d-41b2-b232-6ebe51a297ab"
PROBE_WEEKS = int(os.environ.get("PROBE_WEEKS", "4") or 4)

AEST = timezone(timedelta(hours=10))

# variant SKU -> (label, litres per unit, is_nitro)
VARIANTS = {
    "OQ-CLD-BR-1LT":   ("1L Glass Bottle",          1.0,   False),
    "OQ-CLD-BR-5LT":   ("5L Swap & Refill Bucket",  5.0,   False),
    "OQ-CLD-BR-330ML": ("330ml Glass Bottle",       0.33,  False),
    "OQ-CLD-BR-20LT":  ("20L Nitro Bucket (Summer)", 20.0, True),
    "OQ-CLD-BR-10LT":  ("10L Nitro Bucket (Winter)", 10.0, True),
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
    except Exception:
        return {}


def ordering_week_range(weeks_ago=0):
    now_aest = datetime.now(AEST)
    days_since_tuesday = (now_aest.weekday() - 1) % 7
    this_tuesday_noon = (now_aest - timedelta(days=days_since_tuesday)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    if now_aest < this_tuesday_noon:
        this_tuesday_noon -= timedelta(days=7)
    week_start = this_tuesday_noon - timedelta(weeks=weeks_ago)
    week_end = (week_start + timedelta(days=7)).replace(hour=11, minute=59, second=59)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (week_start.astimezone(timezone.utc).strftime(fmt),
            week_end.astimezone(timezone.utc).strftime(fmt),
            week_start.date())


def probe_week(token, weeks_ago):
    start_utc, end_utc, week_start = ordering_week_range(weeks_ago)
    orders, page = [], 1
    while True:
        data = om_get(f"https://app.ordermentum.com/v2/orders?supplierId={SUPPLIER_ID}"
                      f"&createdAt[gte]={start_utc}&createdAt[lte]={end_utc}"
                      f"&pageSize=50&pageNo={page}", token)
        orders.extend(data.get("data", []))
        if page >= data.get("meta", {}).get("totalPages", 1):
            break
        page += 1
        time.sleep(0.2)

    OQ_VENUES = ["old quarter coffee merchants", "oq ballina", "oq murwillumbah",
                 "oq southport", "oq coolangatta", "oq murbah", "oqc"]
    qty = defaultdict(float)
    unknown = defaultdict(float)   # OQ-CLD-BR* SKUs not in VARIANTS
    lines = []                     # every cold-brew-ish line item, for audit
    for order in orders:
        if order.get("cancelled"):
            continue
        retailer = (order.get("retailerName") or order.get("retailer", {}).get("name") or "?")
        is_venue = any(v in retailer.lower() for v in OQ_VENUES)
        detail = om_get(f"https://app.ordermentum.com/v1/orders/{order['id']}", token)
        for item in detail.get("lineItems", []):
            sku = (item.get("SKU", "") or "").upper()
            name = item.get("name", "") or ""
            q = float(item.get("quantity", 0) or 0)
            # catch ANYTHING cold-brew-ish, by SKU or name
            if "CLD" not in sku and "cold brew" not in name.lower():
                continue
            lines.append((retailer, is_venue, sku, name, q))
            if sku in VARIANTS:
                qty[sku] += q
            elif sku.startswith("OQ-CLD-BR"):
                unknown[f"{sku} | {name}"] += q
        time.sleep(0.1)

    print(f"\nWeek {week_start} → {week_start + timedelta(days=6)}  ({len(orders)} orders scanned)")
    litres_nonnitro = litres_nitro = 0.0
    for sku, (label, lpu, nitro) in VARIANTS.items():
        q = qty.get(sku, 0)
        litres = q * lpu
        if nitro:
            litres_nitro += litres
        else:
            litres_nonnitro += litres
        tag = " [NITRO]" if nitro else ""
        print(f"  {label:28s} ({sku}){tag}: qty {q:g} = {litres:.1f}L")
    print(f"  {'—'*52}")
    print(f"  Non-nitro litres: {litres_nonnitro:.1f}L")
    print(f"  Nitro litres:     {litres_nitro:.1f}L")
    print(f"  TOTAL TO BREW:    {litres_nonnitro + litres_nitro:.1f}L")
    for k, q in unknown.items():
        print(f"  ⚠ UNRECOGNISED cold brew SKU: {k} (qty {q:g})")
    print(f"  --- every cold-brew line item this week ---")
    for retailer, is_venue, sku, name, q in sorted(lines, key=lambda l: (not l[1], l[0])):
        tag = "VENUE " if is_venue else "      "
        print(f"  {tag}{retailer[:30]:30s} | {sku:22s} | {name[:44]:44s} | qty {q:g}")


def main():
    print("=" * 60)
    print(f"Cold Brew Probe (read-only) — last {PROBE_WEEKS} weeks incl. current")
    print("=" * 60)
    token = om_auth()
    for weeks_ago in range(PROBE_WEEKS - 1, -1, -1):
        probe_week(token, weeks_ago)


if __name__ == "__main__":
    main()
