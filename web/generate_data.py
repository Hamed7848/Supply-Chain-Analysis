"""Pre-compute the dashboard aggregates that mirror the Gold-layer SQL views.

Reads the raw CSVs under ../Datasets and emits web/dashboard_data.js so the
portfolio page works fully offline (no DB, no server) when opened directly.
"""
import csv
import json
import os
from collections import defaultdict
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIMS = os.path.join(ROOT, "Datasets", "dimensions_data")
FACTS = os.path.join(ROOT, "Datasets", "Facts data")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_data.js")


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def parse_month(d):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d.strip(), fmt).strftime("%Y-%m")
        except (ValueError, AttributeError):
            continue
    return None


def as_float(row, col, default=0.0):
    try:
        return float(row[col])
    except (TypeError, ValueError):
        return default


def main():
    customers = {r["Customer_ID"]: r for r in read_csv(os.path.join(DIMS, "customer_master.csv"))}
    products = {r["Product_ID"]: r for r in read_csv(os.path.join(DIMS, "product_master.csv"))}
    suppliers = {r["Supplier_ID"]: r for r in read_csv(os.path.join(DIMS, "supplier_master.csv"))}
    sales_rows = read_csv(os.path.join(FACTS, "sales_orders.csv"))
    proc_rows = read_csv(os.path.join(FACTS, "procurement_orders.csv"))

    # ------------------------------------------------------------------ SALES
    monthly = defaultdict(lambda: {"orders": 0, "revenue": 0.0, "profit": 0.0})
    categories = defaultdict(lambda: {"revenue": 0.0, "qty": 0, "orders": 0, "profit": 0.0})
    industries = defaultdict(lambda: {"revenue": 0.0, "orders": 0})
    segments = defaultdict(lambda: {"revenue": 0.0, "orders": 0})
    countries = defaultdict(lambda: {"revenue": 0.0, "orders": 0})
    statuses = defaultdict(int)
    delivery_status = defaultdict(int)
    carriers = defaultdict(lambda: {"revenue": 0.0, "orders": 0})
    product_profit = defaultdict(lambda: {"profit": 0.0, "orders": 0, "qty": 0, "name": ""})
    product_cat = {}
    shipping_modes = defaultdict(int)

    total_orders = 0
    total_revenue = 0.0
    total_profit = 0.0
    total_units = 0
    order_total_mismatch = 0
    delivery_status_mismatch = 0
    negative_profit = 0

    for r in sales_rows:
        month = parse_month(r["Order_Date"])
        cust = customers.get(r["Customer_ID"], {})
        prod = products.get(r["Product_ID"], {})
        qty = int(as_float(r, "Order_Quantity"))
        revenue = as_float(r, "Order_Total")
        profit = as_float(r, "Profit_Per_Order")
        order_status = r["Order_Status"]
        delivery = r["Delivery_Status"]
        cat = prod.get("Category", "Unknown")
        product_id = r["Product_ID"]

        total_orders += 1
        total_revenue += revenue
        total_profit += profit
        total_units += qty
        statuses[order_status] += 1
        delivery_status[delivery] += 1
        shipping_modes[r["Shipping_Mode"]] += 1

        if month:
            m = monthly[month]
            m["orders"] += 1
            m["revenue"] += revenue
            m["profit"] += profit

        categories[cat]["revenue"] += revenue
        categories[cat]["qty"] += qty
        categories[cat]["orders"] += 1
        categories[cat]["profit"] += profit
        if prod:
            categories[cat]["name"] = cat

        ind = cust.get("Industry", "Unknown")
        seg = cust.get("Market_Segment", "Unknown")
        cty = cust.get("Country", "Unknown")
        industries[ind]["revenue"] += revenue
        industries[ind]["orders"] += 1
        segments[seg]["revenue"] += revenue
        segments[seg]["orders"] += 1
        countries[cty]["revenue"] += revenue
        countries[cty]["orders"] += 1

        carr = r["Shipping_Carrier"]
        carriers[carr]["revenue"] += revenue
        carriers[carr]["orders"] += 1

        pp = product_profit[product_id]
        pp["profit"] += profit
        pp["orders"] += 1
        pp["qty"] += qty
        pp["name"] = prod.get("Product_Name", product_id)
        product_cat[product_id] = cat

        # recalculate order_total and lateness exactly like silver layer
        unit_price = as_float(r, "Unit_Price")
        disc = as_float(r, "Discount")
        vat = as_float(r, "VAT_Rate")
        calc_total = round(qty * unit_price * (1 - disc) * (1 + vat), 2)
        if abs(calc_total - revenue) > 0.01:
            order_total_mismatch += 1

        try:
            sched = datetime.strptime(r["Shipping_Date_Scheduled"].strip(), "%m/%d/%Y")
            actual = datetime.strptime(r["Shipping_Date_Actual"].strip(), "%m/%d/%Y")
            calc_late = 1 if actual > sched else 0
            src_late = int(as_float(r, "Late_Delivery_Risk_Flag"))
            src_late_label = 1 if delivery == "Late" else 0
            if src_late_label != calc_late:
                delivery_status_mismatch += 1
        except (ValueError, AttributeError):
            pass

        if profit < 0:
            negative_profit += 1

    # ------------------------------------------------------------------- PROC
    proc_monthly = defaultdict(lambda: {"spend": 0.0, "orders": 0})
    supplier_stats = defaultdict(lambda: {
        "name": "", "region": "", "country": "", "stated_otr": 0.0,
        "spend": 0.0, "orders": 0, "late": 0, "delay_total": 0, "delay_n": 0,
        "cert": "None", "preferred": 0,
    })
    spend_by_region = defaultdict(float)
    spend_by_country = defaultdict(float)
    spend_by_cert = defaultdict(float)
    top_products_proc = defaultdict(lambda: {"spend": 0.0, "name": ""})

    total_spend = 0.0
    total_pos = 0
    late_pos = 0
    cost_mismatch = 0
    delay_sum = 0.0
    delay_n = 0

    for r in proc_rows:
        sup = suppliers.get(r["Supplier_ID"], {})
        qty = int(as_float(r, "Order_Quantity"))
        unit_cost = as_float(r, "Unit_Cost")
        total_cost = as_float(r, "Total_Cost")
        month = parse_month(r["Order_Date"])
        try:
            planned = datetime.strptime(r["Delivery_Date_Planned"].strip(), "%Y-%m-%d")
            actual = datetime.strptime(r["Delivery_Date_Actual"].strip(), "%Y-%m-%d")
            delay = (actual - planned).days
            is_late = 1 if delay > 0 else 0
        except (ValueError, AttributeError):
            delay, is_late = 0, 0

        total_spend += total_cost
        total_pos += 1
        if is_late:
            late_pos += 1
        delay_sum += delay
        delay_n += 1

        calc_total = round(qty * unit_cost, 4)
        if abs(calc_total - total_cost) > 0.01:
            cost_mismatch += 1

        if month:
            pm = proc_monthly[month]
            pm["spend"] += total_cost
            pm["orders"] += 1

        sid = r["Supplier_ID"]
        ss = supplier_stats[sid]
        ss["name"] = sup.get("Supplier_Name", sid)
        ss["region"] = sup.get("Region", "Unknown")
        ss["country"] = sup.get("Country", "Unknown")
        ss["stated_otr"] = as_float(sup, "On_Time_Delivery_Rate")
        ss["spend"] += total_cost
        ss["orders"] += 1
        ss["delay_total"] += delay
        ss["delay_n"] += 1
        ss["cert"] = sup.get("Certification_Level", "None")
        ss["preferred"] = int(as_float(sup, "Preferred_Supplier_Flag"))
        if is_late:
            ss["late"] += 1

        spend_by_region[sup.get("Region", "Unknown")] += total_cost
        spend_by_country[sup.get("Country", "Unknown")] += total_cost
        spend_by_cert[sup.get("Certification_Level", "None")] += total_cost
        raw_id = r["Raw_Material_ID"]
        pp = top_products_proc[raw_id]
        pp["spend"] += total_cost
        pp["name"] = products.get(raw_id, {}).get("Product_Name", raw_id)

    # supplier performance summary derivation
    supplier_perf = []
    for sid, ss in supplier_stats.items():
        if ss["orders"] == 0:
            continue
        actual_otr = round((ss["orders"] - ss["late"]) / ss["orders"] * 100, 2)
        supplier_perf.append({
            "id": sid,
            "name": ss["name"],
            "region": ss["region"],
            "country": ss["country"],
            "stated": round(ss["stated_otr"] * 100, 2),
            "actual": actual_otr,
            "gap": round(ss["stated_otr"] * 100 - actual_otr, 2),
            "spend": round(ss["spend"], 2),
            "orders": ss["orders"],
            "late": ss["late"],
            "avg_delay": round(ss["delay_total"] / ss["delay_n"], 2),
            "cert": ss["cert"],
            "preferred": ss["preferred"],
        })

    # --------------------------------------------------------------- PRODUCTS
    loss_making = 0
    product_margins = []
    for pid, p in products.items():
        cost = as_float(p, "Unit_Cost")
        price = as_float(p, "Standard_Price")
        if cost > price:
            loss_making += 1
        product_margins.append({
            "name": p.get("Product_Name", pid),
            "category": p.get("Category", "Unknown"),
            "margin": round(price - cost, 2),
            "active": 1 if not p.get("Discontinuation_Date", "").strip() else 0,
        })
    active_products = sum(1 for m in product_margins if m["active"])

    cat_dist = defaultdict(int)
    for p in products.values():
        cat_dist[p.get("Category", "Unknown")] += 1

    cert_dist = defaultdict(int)
    for s in suppliers.values():
        cert_dist[s.get("Certification_Level", "None")] += 1
    preferred_count = sum(1 for s in suppliers.values() if int(as_float(s, "Preferred_Supplier_Flag")))

    def top(series, n=10):
        return sorted(series.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def top_dict(series, n=10, sort_by="revenue"):
        return sorted(series.items(), key=lambda kv: kv[1][sort_by], reverse=True)[:n]

    # customer geo quality check (lat/lon out of range)
    geo_bad = 0
    for c in customers.values():
        lat = as_float(c, "Latitude", 999)
        lon = as_float(c, "Longitude", 999)
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            geo_bad += 1

    tot_customers = len(customers)
    to_country = top_dict(countries)
    to_industry = top_dict(industries)
    top_prod_profit = sorted(
        [{"name": v["name"], "profit": v["profit"], "orders": v["orders"],
          "qty": v["qty"], "category": product_cat.get(k, "")}
         for k, v in product_profit.items() if v["orders"] > 0],
        key=lambda d: d["profit"], reverse=True)[:10]

    data = {
        "generated": datetime.now().strftime("%Y-%m-%d"),
        "meta": {
            "sales_orders": len(sales_rows),
            "procurement_orders": len(proc_rows),
            "customers": tot_customers,
            "products": len(products),
            "suppliers": len(suppliers),
        },
        # ---- Sales KPIs
        "sales": {
            "total_orders": total_orders,
            "total_revenue": round(total_revenue, 2),
            "total_profit": round(total_profit, 2),
            "total_units": total_units,
            "avg_revenue_per_order": round(total_revenue / total_orders, 2),
            "avg_profit_per_order": round(total_profit / total_orders, 2),
            "order_total_mismatch": order_total_mismatch,
            "delivery_status_mismatch": delivery_status_mismatch,
            "negative_profit": negative_profit,
        },
        "monthly_sales": [
            {"month": k, "revenue": round(v["revenue"], 2), "profit": round(v["profit"], 2), "orders": v["orders"]}
            for k, v in sorted(monthly.items())
        ],
        "sales_by_category": [
            {"category": k, "revenue": round(v["revenue"], 2), "profit": round(v["profit"], 2),
             "orders": v["orders"], "qty": v["qty"]}
            for k, v in sorted(categories.items(), key=lambda kv: kv[1]["revenue"], reverse=True)
        ],
        "sales_by_industry": [
            {"industry": k, "revenue": round(v["revenue"], 2), "orders": v["orders"]}
            for k, v in sorted(industries.items(), key=lambda kv: kv[1]["revenue"], reverse=True)
        ],
        "sales_by_segment": [
            {"segment": k, "revenue": round(v["revenue"], 2), "orders": v["orders"]}
            for k, v in sorted(segments.items(), key=lambda kv: kv[1]["revenue"], reverse=True)
        ],
        "sales_by_country": [{"country": k, "revenue": v} for k, v in to_country],
        "sales_by_status": [{"status": k, "count": v} for k, v in statuses.items()],
        "delivery_status": [{"status": k, "count": v} for k, v in delivery_status.items()],
        "sales_by_carrier": [
            {"carrier": k, "revenue": round(v["revenue"], 2), "orders": v["orders"]}
            for k, v in sorted(carriers.items(), key=lambda kv: kv[1]["revenue"], reverse=True)
        ],
        "shipping_modes": [{"mode": k, "count": v} for k, v in shipping_modes.items()],
        "top_products": top_prod_profit,
        # ---- Procurement KPIs
        "procurement": {
            "total_spend": round(total_spend, 2),
            "total_pos": total_pos,
            "late_pos": late_pos,
            "late_rate": round(late_pos / total_pos * 100, 2),
            "avg_delay_days": round(delay_sum / delay_n, 2) if delay_n else 0,
            "cost_mismatch": cost_mismatch,
            "actual_otr": round((total_pos - late_pos) / total_pos * 100, 2),
        },
        "monthly_procurement": [
            {"month": k, "spend": round(v["spend"], 2), "orders": v["orders"]}
            for k, v in sorted(proc_monthly.items())
        ],
        "supplier_performance": sorted(supplier_perf, key=lambda s: s["spend"], reverse=True),
        "spend_by_region": [{"region": k, "spend": round(v, 2)} for k, v in spend_by_region.items()],
        "spend_by_country": [{"country": k, "spend": round(v, 2)} for k, v in
                             sorted(spend_by_country.items(), key=lambda kv: kv[1], reverse=True)[:8]],
        "spend_by_cert": [{"cert": k, "spend": round(v, 2)} for k, v in spend_by_cert.items()],
        "top_procurement_products": [{"name": v["name"], "spend": round(v["spend"], 2)}
                                     for v in sorted(top_products_proc.values(), key=lambda d: d["spend"], reverse=True)[:8]],
        # ---- Stock / dimensions
        "products": {
            "total": len(products),
            "active": active_products,
            "loss_making": loss_making,
            "by_category": [{"category": k, "count": v} for k, v in
                            sorted(cat_dist.items(), key=lambda kv: kv[1], reverse=True)],
            "top_margin": sorted(product_margins, key=lambda d: d["margin"], reverse=True)[:10],
        },
        "suppliers": {
            "total": len(suppliers),
            "certified": sum(1 for s in suppliers.values() if s.get("Certification_Level", "None") not in ("None", "")),
            "preferred": preferred_count,
            "by_cert": [{"cert": k, "count": v} for k, v in cert_dist.items()],
        },
        "customers": {
            "total": tot_customers,
            "geo_out_of_range": geo_bad,
        },
    }

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("window.DASHBOARD_DATA = " + json.dumps(data, indent=0) + ";\n")
    print(f"wrote {OUT} ({os.path.getsize(OUT):,} bytes)")


if __name__ == "__main__":
    main()