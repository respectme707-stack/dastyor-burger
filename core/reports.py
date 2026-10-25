"""Цифры для дашборда за выбранный период."""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum
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


def _between(qs, field, start, end):
    qs = _since(qs, field, start)
    return qs.filter(**{f"{field}__lte": end}) if end else qs


def totals(start=None, end=None):
    """Главные цифры за период: деньги и штуки. Для сводки в боте."""
    items = _between(m.ShipmentItem.objects.all(), "shipment__date", start, end)
    returns = _between(m.Return.objects.all(), "date", start, end)
    revenue = items.aggregate(s=_money(F("qty") * F("price")))["s"] or m.ZERO
    cogs = items.aggregate(s=_money(F("qty") * F("unit_cost")))["s"] or m.ZERO
    returned = returns.aggregate(s=_money(F("qty") * F("price")))["s"] or m.ZERO
    expenses = _between(m.Expense.objects.all(), "date", start, end).aggregate(s=Sum("amount"))["s"] or m.ZERO
    purchased = _between(m.PurchaseItem.objects.all(), "purchase__date", start, end).aggregate(
        s=_money(F("qty") * F("price")))["s"] or m.ZERO
    cash_in = (_between(m.Shipment.objects.all(), "date", start, end).aggregate(s=Sum("paid"))["s"] or m.ZERO) + (
        _between(m.Payment.objects.exclude(shop=None), "date", start, end).aggregate(s=Sum("amount"))["s"] or m.ZERO)
    products = list(m.Product.objects.filter(active=True))
    agents = [w for w in m.Worker.objects.filter(active=True) if w.is_seller()]
    receivable = sum((d for d in (s.debt() for s in m.Shop.objects.all()) if d > 0), m.ZERO)
    payable = sum((d for d in (s.debt() for s in m.Supplier.objects.all()) if d > 0), m.ZERO)
    return {
        "revenue": revenue,
        "cash_in": cash_in,
        "purchased": purchased,
        "expenses": expenses,
        "returned": returned,
        "cogs": cogs,
        # Как на сайте: выручка минус возвраты, себестоимость проданного и прочие расходы.
        "profit": revenue - returned - cogs - expenses,
        "receivable": receivable,
        "payable": payable,
        "produced_qty": _between(m.Production.objects.all(), "date", start, end).aggregate(s=Sum("qty"))["s"] or 0,
        "sold_qty": items.aggregate(s=Sum("qty"))["s"] or 0,
        "returned_qty": returns.aggregate(s=Sum("qty"))["s"] or 0,
        "workshop_qty": sum(p.stock() for p in products),
        "agents_qty": sum(sum(w.stock().values()) for w in agents),
    }


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
    # Посещения: сколько магазинов получили товар в каждый день.
    visits_by_day = defaultdict(int)
    for row in _since(m.Shipment.objects.all(), "date", start).values("date").annotate(
            n=Count("shop", distinct=True)):
        visits_by_day[row["date"]] = row["n"]
    visit_count = {r["shop"]: r["n"] for r in _since(m.Shipment.objects.all(), "date", start)
                   .values("shop").annotate(n=Count("id"))}

    # Свежесть товара: когда последний раз возили и до какого числа годен.
    freshness = []
    today = timezone.localdate()
    for shop in m.Shop.objects.filter(active=True):
        info = shop.freshness(today)
        if not info:
            continue
        info.update(shop=shop, visits=visit_count.get(shop.pk, 0),
                    level="bad" if info["days_left"] <= 1 else "warn" if info["days_left"] <= 2 else "")
        freshness.append(info)
    freshness.sort(key=lambda x: (x["shop"].archived, x["days_left"]))

    # Убытки от возвратов: себестоимость того, что вернули.
    costs = {}
    losses = defaultdict(lambda: [0, m.ZERO, m.ZERO])  # shop -> [шт, сумма, убыток]
    product_loss = defaultdict(lambda: m.ZERO)
    for row in returns.values("shop", "product").annotate(q=Sum("qty"), s=_money(F("qty") * F("price"))):
        if row["product"] not in costs:
            costs[row["product"]] = m.Product.objects.get(pk=row["product"]).unit_cost()
        loss = row["q"] * costs[row["product"]]
        entry = losses[row["shop"]]
        entry[0] += row["q"]
        entry[1] += row["s"]
        entry[2] += loss
        product_loss[row["product"]] += loss
    shop_names = m.Shop.objects.in_bulk(list(losses))
    returners = sorted(
        ({"shop": shop_names[k], "qty": v[0], "amount": v[1], "loss": v[2]} for k, v in losses.items()),
        key=lambda x: -x["loss"])[:10]

    # Агенты: что на руках сейчас, сколько продали и сколько денег собрали.
    agents, in_vans = [], defaultdict(int)
    agent_sales = {
        r["shipment__worker"]: r["s"]
        for r in items.exclude(shipment__worker=None).values("shipment__worker").annotate(
            s=_money(F("qty") * F("price")))
    }
    shipments = _since(m.Shipment.objects.exclude(worker=None), "date", start)
    agent_cash = defaultdict(lambda: m.ZERO)
    for r in shipments.values("worker").annotate(s=Sum("paid")):
        agent_cash[r["worker"]] += r["s"] or m.ZERO
    for r in _since(m.Payment.objects.exclude(worker=None).exclude(shop=None), "date", start) \
            .values("worker").annotate(s=Sum("amount")):
        agent_cash[r["worker"]] += r["s"] or m.ZERO
    agent_expenses = {r["worker"]: r["s"] for r in _since(m.Expense.objects.exclude(worker=None), "date", start)
                      .values("worker").annotate(s=Sum("amount"))}
    for worker in m.Worker.objects.filter(active=True):
        if not worker.is_seller():
            continue
        stock = worker.stock()
        for product, n in stock.items():
            in_vans[product.pk] += n
        agents.append({
            "worker": worker,
            "stock": ", ".join(f"{p.name} {n}" for p, n in sorted(stock.items(), key=lambda kv: kv[0].name)),
            "sales": agent_sales.get(worker.pk, m.ZERO),
            "cash": agent_cash.get(worker.pk, m.ZERO),
            "expenses": agent_expenses.get(worker.pk) or m.ZERO,
        })
    agents.sort(key=lambda a: -a["sales"])

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
            "loss": product_loss.get(product.pk, m.ZERO),
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
        "agents": agents,
        "freshness": freshness,
        "expiring": [x for x in freshness if x["level"] == "bad" and not x["shop"].archived],
        "chart_visits": [visits_by_day.get(d, 0) for d in days],
        "top_shops": shops[:5],
        "top_products": sorted((p for p in products if p["sales"]), key=lambda p: -p["sales"])[:5],
        "returners": returners,
        "materials": materials,
        "low_materials": [x for x in materials if x["low"]],
    }
