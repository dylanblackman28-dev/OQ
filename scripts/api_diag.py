"""
Ordermentum API diagnostic — READ ONLY, writes nothing.
Checks that the endpoints the syncs depend on still return what we expect,
in particular that order DETAIL responses carry line items. Silent detail
failures show up as correct order counts but badly understated kilos.
"""

import os, json, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

OM_API_KEY = os.environ["OM_API_KEY"]
SUPPLIER_ID = "71bf79dc-4e3d-41b2-b232-6ebe51a297ab"
AEST = timezone(timedelta(hours=10))


def fetch(url):
    """Return (status, parsed_or_text). Never raises."""
    req = urllib.request.Request(url, headers={"x-api-key": OM_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()[:300]
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        return "ERR", str(e)[:200]


def week_window(weeks_ago=0):
    now = datetime.now(AEST)
    days = (now.weekday() - 1) % 7
    tue = (now - timedelta(days=days)).replace(hour=12, minute=0, second=0, microsecond=0)
    if now < tue:
        tue -= timedelta(days=7)
    start = tue - timedelta(weeks=weeks_ago)
    end = (start + timedelta(days=7)).replace(hour=11, minute=59, second=59)
    f = "%Y-%m-%dT%H:%M:%SZ"
    return start.astimezone(timezone.utc).strftime(f), end.astimezone(timezone.utc).strftime(f), start.date()


def main():
    weeks_ago = int(os.environ.get("WEEKS_AGO", "1") or 1)
    start, end, wk = week_window(weeks_ago)
    print("=" * 66)
    print(f"Ordermentum API diagnostic — ordering week starting {wk}")
    print("=" * 66)

    # 1. LIST endpoint
    list_url = (f"https://api.ordermentum.com/v2/orders?supplierId={SUPPLIER_ID}"
                f"&createdAt[gte]={start}&createdAt[lte]={end}&pageSize=50&pageNo=1")
    st, data = fetch(list_url)
    print(f"\n[1] LIST v2/orders -> HTTP {st}")
    if not isinstance(data, dict):
        print(f"    body: {data}")
        return
    orders = data.get("data", [])
    print(f"    meta: {json.dumps(data.get('meta'))}")
    print(f"    orders returned: {len(orders)}")
    if not orders:
        print("    no orders — nothing further to test")
        return
    print(f"    order numbers + createdAt (AEST):")
    for o in sorted(orders, key=lambda x: x.get("createdAt") or ""):
        ca = o.get("createdAt") or ""
        try:
            ca = datetime.fromisoformat(ca.replace("Z", "+00:00")).astimezone(AEST).strftime("%a %d %b %H:%M")
        except Exception:
            pass
        print(f"      {o.get('orderNumber') or o.get('number'):10s} created {ca:18s} "
              f"lineCount={o.get('lineCount')} {(o.get('retailerName') or '')[:28]}")
    print(f"    order object keys: {sorted(orders[0].keys())}")
    # does the list already carry line items?
    for key in ("lineItems", "items", "orderItems", "products"):
        if key in orders[0]:
            v = orders[0][key]
            print(f"    LIST already contains '{key}' ({len(v) if isinstance(v, list) else type(v).__name__})")

    # 2. DETAIL endpoints — test a few orders, both API versions
    print(f"\n[2] DETAIL endpoints (testing up to 3 orders)")
    for o in orders[:3]:
        oid = o.get("id")
        num = o.get("orderNumber") or o.get("number") or "?"
        print(f"\n  --- {num} (id {oid}) ---")
        for label, url in [
            ("v1/orders/{id}", f"https://api.ordermentum.com/v1/orders/{oid}"),
            ("v2/orders/{id}", f"https://api.ordermentum.com/v2/orders/{oid}"),
        ]:
            st, d = fetch(url)
            if isinstance(d, dict):
                # unwrap a possible {data: {...}} envelope
                inner = d.get("data") if isinstance(d.get("data"), dict) else d
                li = inner.get("lineItems") or inner.get("items") or []
                print(f"    {label:16s} HTTP {st}  keys={sorted(inner.keys())[:9]}")
                print(f"    {'':16s} lineItems={len(li) if isinstance(li, list) else 'n/a'}")
                if isinstance(li, list) and li:
                    print(f"    {'':16s} item keys: {sorted(li[0].keys())}")
                    print(f"    {'':16s} sample: SKU={li[0].get('SKU') or li[0].get('sku')!r} "
                          f"name={(li[0].get('name') or '')[:40]!r} qty={li[0].get('quantity')}")
            else:
                print(f"    {label:16s} HTTP {st}  body={str(d)[:160]}")

    # 3. How many orders in the week actually return line items via v1?
    print(f"\n[3] v1 detail coverage across all {len(orders)} listed orders")
    ok = empty = fail = 0
    for o in orders:
        st, d = fetch(f"https://api.ordermentum.com/v1/orders/{o.get('id')}")
        if not isinstance(d, dict):
            fail += 1
            continue
        inner = d.get("data") if isinstance(d.get("data"), dict) else d
        li = inner.get("lineItems") or inner.get("items") or []
        if li:
            ok += 1
        else:
            empty += 1
    print(f"    with line items : {ok}")
    print(f"    empty           : {empty}")
    print(f"    request failed  : {fail}")


if __name__ == "__main__":
    main()
