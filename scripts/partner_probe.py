"""
Partner order probe — READ ONLY, writes nothing.

Lists a partner's recent Ordermentum orders with every line item, and shows how
the wholesale sync currently classifies each line. Use it before changing any
classification rule, so the change is based on what a partner actually buys
rather than an assumption.

Env: OM_API_KEY, RETAILER_IDS (comma-separated), ORDER_LIMIT (default 6),
     DAYS_BACK (default 400)
"""

import os, sys, json, importlib.util, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

OM_API_KEY = os.environ["OM_API_KEY"]
SUPPLIER_ID = "71bf79dc-4e3d-41b2-b232-6ebe51a297ab"
AEST = timezone(timedelta(hours=10))

# Load the real sync so we report its actual behaviour, not a copy of it
os.environ.setdefault("SUPABASE_URL", "x")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "x")
sys.modules["supabase"] = type(sys)("supabase")
sys.modules["supabase"].create_client = lambda *a, **k: None
_spec = importlib.util.spec_from_file_location(
    "whs", os.path.join(os.path.dirname(__file__), "wholesale_sync.py"))
whs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(whs)


def fetch(url, attempts=5):
    """GET with backoff — the API rate-limits, and a backfill may be running."""
    import time, random
    delay = 2.0
    for _ in range(attempts):
        req = urllib.request.Request(url, headers={"x-api-key": OM_API_KEY})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(delay + random.uniform(0, 0.4))
                delay = min(delay * 2, 60)
                continue
            return {"_error": f"HTTP {e.code}"}
        except Exception as e:
            return {"_error": str(e)[:120]}
    return {"_error": "rate limited"}


def orders_in_window(days_back):
    """
    Ordermentum's order list does not accept a retailerId filter (HTTP 400), so
    page the supplier's whole window and filter client-side.
    """
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    out, page = [], 1
    while True:
        d = fetch(f"https://api.ordermentum.com/v2/orders?supplierId={SUPPLIER_ID}"
                  f"&createdAt[gte]={start}&createdAt[lte]={end}"
                  f"&pageSize=50&pageNo={page}")
        batch = d.get("data", []) if isinstance(d, dict) else []
        out.extend(batch)
        if len(batch) < 50:
            break
        page += 1
    return out


def main():
    ids = [i.strip() for i in os.environ.get("RETAILER_IDS", "").split(",") if i.strip()]
    limit = int(os.environ.get("ORDER_LIMIT", "6") or 6)
    if not ids:
        print("Set RETAILER_IDS")
        return

    days_back = int(os.environ.get("DAYS_BACK", "400") or 400)
    print(f"Scanning supplier orders for the last {days_back} days…")
    everything = orders_in_window(days_back)
    print(f"  {len(everything)} orders in window\n")

    for rid in ids:
        name = whs.TRACKED_PARTNERS.get(rid, "(untracked)")
        print("=" * 78)
        print(f"{name}   retailerId={rid}")
        print("=" * 78)
        orders = sorted([o for o in everything if o.get("retailerId") == rid],
                        key=lambda o: o.get("createdAt") or "", reverse=True)
        if not orders:
            print(f"  no orders in the last {days_back} days\n")
            continue
        print(f"  {len(orders)} orders in window; showing most recent {min(limit, len(orders))}")

        for o in orders[:limit]:
            created = o.get("createdAt") or ""
            try:
                created = datetime.fromisoformat(created.replace("Z", "+00:00")) \
                    .astimezone(AEST).strftime("%a %d %b %Y %H:%M")
            except Exception:
                pass
            num = o.get("orderNumber") or o.get("number")
            print(f"\n  {num}  placed {created}  total ${float(o.get('total') or 0):,.2f}")
            detail = fetch(f"https://api.ordermentum.com/v1/orders/{o.get('id')}")
            items = detail.get("lineItems", []) if isinstance(detail, dict) else []
            any_whs = False
            blend_kg = 0.0
            for it in items:
                sku = (it.get("SKU") or "").upper()
                nm = it.get("name") or ""
                qty = float(it.get("quantity") or 0)
                is_whs = whs.is_whs_coffee_sku(sku)
                is_blend = is_whs and whs.is_tracked_blend(nm, sku)
                if is_whs:
                    any_whs = True
                if is_blend:
                    blend_kg += whs.extract_kg(nm, qty)
                tag = "BLEND" if is_blend else ("whs  " if is_whs else "     ")
                print(f"    [{tag}] qty {qty:6g}  {sku:30s} {nm[:52]}")
            print(f"    -> counted by wholesale sync? {'YES' if any_whs else 'NO — whole order skipped'}"
                  f"   blend kg: {blend_kg:.2f}")
        print()


if __name__ == "__main__":
    main()
