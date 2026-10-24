"""Цифры для дашборда за выбранный период."""
from collections import defaultdict
from datetime import timedelta

from django.db.models import DecimalField, ExpressionWrapper, F, Sum
from django.utils import timezone

from . import models as m

PERIODS = {
    "today": "Сегодня",
    "week": "7 дней",
    "month": "30 дней",
    "all": "Всё время",
}


def period_start(period):
    today = timezone.localdate()
    if period == "today":
        return today
    if period == "week":
        return today - timedelta(days=6)
    if period == "month":
        return today - timedelta(days=29)
    return None


def _money(expr):
    return Sum(ExpressionWrapper(expr, output_field=DecimalField(**m.MONEY)))


def _since(qs, field, start):
    return qs.filter(**{f"{field}__gte": start}) if start else qs


def build(period):
    start = period_start(period)
    items = _since(m.ShipmentItem.objects.all(), "shipment__date", start)
    returns = _since(m.Return.objects.all(), "date", start)
    expenses = _since(m.Expense.objects.all(), "date", start)

    revenue = items.aggregate(s=_money(F("qty") * F("price")))["s"] or m.ZERO
    cogs = items.aggregate(s=_money(F("qty") * F("unit_cost")))["s"] or m.ZERO
    returned = returns.aggregate(s=_money(F("qty") * F("price")))["s"] or m.ZERO
    expense_total = expenses.aggregate(s=Sum("amount"))["s"] or m.ZERO
    sold_qty = items.aggregate(s=Sum("qty"))["s"] or 0
    returned_qty = returns.aggregate(s=Sum("qty"))["s"] or 0
    # Возврат: деньги не получили, а себестоимость уже потратили.
    profit = revenue - returned - cogs - expense_total

    # Продажи по дням.
    by_day = defaultdict(lambda: m.ZERO)
    for row in items.values("shipment__date").annotate(s=_money(F("qty") * F("price"))):
        by_day[row["shipment__date"]] += row["s"]
    days = sorted(by_day)
    if start and period != "today":
        days = [start + timedelta(days=i) for i in range((timezone.localdate() - start).days + 1)]

    # По магазинам и районам.
    shop_sales = {
        r["shipment__shop"]: (r["s"], r["q"])
        for r in items.values("shipment__shop").annotate(s=_money(F("qty") * F("price")), q=Sum("qty"))
    }
    shop_returns = {r["shop"]: r["q"] for r in returns.values("shop").annotate(q=Sum("qty"))}
    shops = []
    districts = defaultdict(lambda: m.ZERO)
    for shop in m.Shop.objects.all():
        sales, qty = shop_sales.get(shop.pk, (m.ZERO, 0))
        ret = shop_returns.get(shop.pk, 0)
        debt = shop.debt()
        if not (sales or ret or debt):
            continue
        districts[shop.district] += sales
        shops.append({
            "shop": shop,
            "sales": sales,
            "qty": qty,
            "returns": ret,
            "return_pct": round(ret / qty * 100) if qty else 0,
            "debt": debt,
        })
    shops.sort(key=lambda s: s["sales"], reverse=True)

    # По продуктам.
    product_sales = {
        r["product"]: (r["s"], r["q"])
        for r in items.values("product").annotate(s=_money(F("qty") * F("price")), q=Sum("qty"))
    }
    product_returns = {r["product"]: r["q"] for r in returns.values("product").annotate(q=Sum("qty"))}
    # Машины: что в кузове сейчас и что продали за период.
    vehicles, in_vans = [], defaultdict(int)
    vehicle_sales = {
        r["shipment__vehicle"]: r["s"]
        for r in items.exclude(shipment__vehicle=None).values("shipment__vehicle").annotate(
            s=_money(F("qty") * F("price")))
    }
    shipments = _since(m.Shipment.objects.exclude(vehicle=None), "date", start)
    vehicle_cash = {r["vehicle"]: r["s"] for r in shipments.values("vehicle").annotate(s=Sum("paid"))}
    for vehicle in m.Vehicle.objects.filter(active=True):
        stock = vehicle.stock()
        for product, n in stock.items():
            in_vans[product.pk] += n
        vehicles.append({
            "vehicle": vehicle,
            "stock": ", ".join(f"{p.name} {n}" for p, n in sorted(stock.items(), key=lambda kv: kv[0].name)),
            "sales": vehicle_sales.get(vehicle.pk, m.ZERO),
            "cash": vehicle_cash.get(vehicle.pk) or m.ZERO,
        })

    products = []
    for product in m.Product.objects.filter(active=True):
        sales, qty = product_sales.get(product.pk, (m.ZERO, 0))
        ret = product_returns.get(product.pk, 0)
        cost = product.unit_cost()
        products.append({
            "product": product,
            "sales": sales,
            "qty": qty,
            "returns": ret,
            "return_pct": round(ret / qty * 100) if qty else 0,
            "stock": product.stock(),
            "in_vans": in_vans.get(product.pk, 0),
            "unit_cost": cost,
            "unit_profit": product.price - cost,
        })

    materials = [
        {"material": x, "stock": x.stock(), "low": x.is_low()}
        for x in m.RawMaterial.objects.all()
    ]
    materials.sort(key=lambda x: (not x["low"], x["material"].name))

    suppliers = [(s, s.debt()) for s in m.Supplier.objects.all()]
    suppliers = [(s, d) for s, d in suppliers if d]

    return {
        "revenue": revenue,
        "returned": returned,
        "cogs": cogs,
        "expenses": expense_total,
        "profit": profit,
        "margin": round(profit / revenue * 100) if revenue else 0,
        "sold_qty": sold_qty,
        "returned_qty": returned_qty,
        "return_pct": round(returned_qty / sold_qty * 100) if sold_qty else 0,
        "receivable": sum((s["debt"] for s in shops if s["debt"] > 0), m.ZERO),
        "debtor_count": sum(1 for s in shops if s["debt"] > 0),
        "payable": sum((d for _, d in suppliers if d > 0), m.ZERO),
        "suppliers": suppliers,
        "chart_days": [d.strftime("%d.%m") for d in days],
        "chart_sales": [float(by_day.get(d, 0)) for d in days],
        "chart_districts": [k for k, _ in sorted(districts.items(), key=lambda kv: -kv[1])],
        "chart_district_sales": [float(v) for _, v in sorted(districts.items(), key=lambda kv: -kv[1])],
        "shops": shops,
        "products": products,
        "vehicles": vehicles,
        "materials": materials,
        "low_materials": [x for x in materials if x["low"]],
    }
