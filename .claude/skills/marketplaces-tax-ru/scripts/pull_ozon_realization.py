#!/usr/bin/env python3
"""Pull Ozon /v2/finance/realization for each month of 2025 and aggregate."""
import json, os, time, urllib.request, pathlib

ENV = pathlib.Path(__file__).resolve().parents[1] / ".env"
for line in ENV.read_text().splitlines():
    if line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip())

CLIENT_ID = os.environ["OZON_CLIENT_ID"]
API_KEY = os.environ["OZON_API_KEY"]
RAW = pathlib.Path(__file__).resolve().parents[1] / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)


def call(month: int, year: int) -> dict:
    req = urllib.request.Request(
        "https://api-seller.ozon.ru/v2/finance/realization",
        data=json.dumps({"month": month, "year": year}).encode(),
        headers={
            "Client-Id": CLIENT_ID,
            "Api-Key": API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def safe(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return 0
        d = d.get(p)
    return d or 0


months = []
for m in range(1, 13):
    print(f"  pulling {m:02d}/2025 ...", end=" ", flush=True)
    try:
        data = call(m, 2025)
    except Exception as e:
        print(f"ERR {e}")
        continue
    out = RAW / f"ozon_realization_2025-{m:02d}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    rows = data.get("result", {}).get("rows", [])
    sales_seller = sales_delivery_amount = sales_delivery_total = 0.0
    ret_seller = ret_amount = ret_total = 0.0
    for r in rows:
        sp = r.get("seller_price_per_instance", 0) or 0
        dq = safe(r, "delivery_commission", "quantity")
        rq = safe(r, "return_commission", "quantity")
        sales_seller += sp * dq
        ret_seller += sp * rq
        sales_delivery_amount += safe(r, "delivery_commission", "amount")
        sales_delivery_total += safe(r, "delivery_commission", "total")
        ret_amount += safe(r, "return_commission", "amount")
        ret_total += safe(r, "return_commission", "total")

    months.append({
        "month": m,
        "rows": len(rows),
        "sales_at_seller_price": round(sales_seller, 2),
        "returns_at_seller_price": round(ret_seller, 2),
        "net_at_seller_price": round(sales_seller - ret_seller, 2),
        "sales_buyer_paid": round(sales_delivery_amount, 2),
        "returns_buyer_paid": round(ret_amount, 2),
        "net_buyer_paid": round(sales_delivery_amount - ret_amount, 2),
    })
    print(f"rows={len(rows):4d}  net(seller_price)={sales_seller-ret_seller:>14,.2f}")
    time.sleep(0.4)

summary = RAW.parent / "ozon_2025_summary.json"
total_seller = sum(m["net_at_seller_price"] for m in months)
total_buyer = sum(m["net_buyer_paid"] for m in months)
summary.write_text(json.dumps({"months": months,
                               "total_net_at_seller_price": round(total_seller, 2),
                               "total_net_buyer_paid": round(total_buyer, 2)},
                              ensure_ascii=False, indent=2))

print()
print(f"{'Month':<6} {'rows':>5} {'sales(seller_price)':>22} {'returns':>14} {'NET':>16}")
for m in months:
    print(f"{m['month']:02d}/2025 {m['rows']:>5} {m['sales_at_seller_price']:>22,.2f} "
          f"{m['returns_at_seller_price']:>14,.2f} {m['net_at_seller_price']:>16,.2f}")
print(f"\nTOTAL net @ seller_price: {total_seller:>16,.2f} ₽")
print(f"TOTAL net @ buyer_paid:   {total_buyer:>16,.2f} ₽")
