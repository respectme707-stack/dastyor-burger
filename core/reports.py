"""Цифры для дашборда за выбранный период."""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import models as m

PERIODS = {
    "today": _("Сегодня"),
    "week": _("7 дней"),
    "month": _("30 дней"),
    "all": _("Всё время"),
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
    shipments = _between(m.Shipment.objects.all(), "date", start, end)
    payments = _between(m.Payment.objects.exclude(shop=None), "date", start, end)
    cash_in = (shipments.aggregate(s=Sum("paid"))["s"] or m.ZERO) + (
        payments.aggregate(s=Sum("amount"))["s"] or m.ZERO)
    card_in = (shipments.filter(method=m.Shipment.Method.CARD).aggregate(s=Sum("paid"))["s"] or m.ZERO) + (
        payments.filter(method=m.Payment.Method.CARD).aggregate(s=Sum("amount"))["s"] or m.ZERO)
    consignment = sum((s.debt() for s in shipments.filter(method=m.Shipment.Method.CONSIGNMENT)), m.ZERO)
    products = list(m.Product.objects.filter(active=True))
    agents = [w for w in m.Worker.objects.filter(active=True) if w.is_seller()]
    receivable = sum((d for d in (s.debt() for s in m.Shop.objects.all()) if d > 0), m.ZERO)
    payable = sum((d for d in (s.debt() for s in m.Supplier.objects.all()) if d > 0), m.ZERO)
    return {
        "revenue": revenue,
        "cash_in": cash_in,
        "card_in": card_in,
        "consignment": consignment,
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

    materials = []
    for x in m.RawMaterial.objects.select_related("unit", "category"):
        left, price = x.stock(), x.avg_price()
        materials.append({"material": x, "stock": left, "low": left < x.min_stock, "price": price,
                          "value": left * price if left > 0 else m.ZERO})
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
        "materials_value": sum((x["value"] for x in materials), m.ZERO),
    }


def _day_range(start, items_by_day):
    """Дни для графика: весь период подряд, а для «всё время» только дни с данными."""
    if start:
        return [start + timedelta(days=i) for i in range((timezone.localdate() - start).days + 1)]
    return sorted(items_by_day)


def _sales_by_day(items, start):
    by_day = defaultdict(lambda: m.ZERO)
    for row in items.values("shipment__date").annotate(s=_money(F("qty") * F("price"))):
        by_day[row["shipment__date"]] += row["s"]
    days = _day_range(start, by_day)
    return [d.strftime("%d.%m") for d in days], [float(by_day.get(d, 0)) for d in days]


def money(period):
    """Деньги за период: что пришло, куда ушло, кто кому должен."""
    start = period_start(period)
    t = totals(start)
    expenses = _since(m.Expense.objects.all(), "date", start)
    labels = dict(m.Expense.Category.choices)
    by_category = [
        {"label": labels.get(r["category"], r["category"]), "amount": r["s"]}
        for r in expenses.values("category").annotate(s=Sum("amount")).order_by("-s")
    ]
    debtors = sorted(((s, s.debt()) for s in m.Shop.objects.all()), key=lambda x: -x[1])
    suppliers = sorted(((s, s.debt()) for s in m.Supplier.objects.all()), key=lambda x: -x[1])
    shipments = _since(m.Shipment.objects.all(), "date", start)
    method_labels = dict(m.Shipment.Method.choices)
    by_method = []
    for r in shipments.values("method").annotate(n=Count("id"), paid=Sum("paid")).order_by("method"):
        total = _money(F("qty") * F("price"))
        amount = m.ShipmentItem.objects.filter(shipment__in=shipments.filter(method=r["method"])).aggregate(
            s=total)["s"] or m.ZERO
        by_method.append({"label": method_labels.get(r["method"], r["method"]), "count": r["n"],
                          "amount": amount, "paid": r["paid"] or m.ZERO})
    return {
        **t,
        "cash_only": t["cash_in"] - t["card_in"],
        "expense_rows": by_category,
        "chart_exp_labels": [x["label"] for x in by_category],
        "chart_exp_values": [float(x["amount"]) for x in by_category],
        "debtors": [(s, d) for s, d in debtors if d > 0],
        "supplier_debts": [(s, d) for s, d in suppliers if d > 0],
        "by_method": by_method,
        "payments": _since(m.Payment.objects.select_related("shop", "supplier", "worker"), "date", start)[:30],
        "expense_list": expenses.select_related("worker")[:30],
    }


def staff(period, query="", role="", show_inactive=False):
    """Сводка по каждому сотруднику за период."""
    start = period_start(period)
    items = _since(m.ShipmentItem.objects.exclude(shipment__worker=None), "shipment__date", start)
    sales = {r["shipment__worker"]: (r["s"], r["q"]) for r in items.values("shipment__worker").annotate(
        s=_money(F("qty") * F("price")), q=Sum("qty"))}
    shipments = _since(m.Shipment.objects.exclude(worker=None), "date", start)
    visits = {r["worker"]: r["n"] for r in shipments.values("worker").annotate(n=Count("shop", distinct=True))}
    cash = defaultdict(lambda: m.ZERO)
    for r in shipments.values("worker").annotate(s=Sum("paid")):
        cash[r["worker"]] += r["s"] or m.ZERO
    for r in _since(m.Payment.objects.exclude(worker=None).exclude(shop=None), "date", start) \
            .values("worker").annotate(s=Sum("amount")):
        cash[r["worker"]] += r["s"] or m.ZERO
    expenses = {r["worker"]: r["s"] for r in _since(m.Expense.objects.exclude(worker=None), "date", start)
                .values("worker").annotate(s=Sum("amount"))}
    produced = {r["worker"]: r["q"] for r in _since(m.Production.objects.exclude(worker=None), "date", start)
                .values("worker").annotate(q=Sum("qty"))}
    loads = _since(m.VanLoadItem.objects.filter(load__kind=m.VanLoad.Kind.LOAD), "load__date", start)
    received = {r["load__worker"]: r["q"] for r in loads.values("load__worker").annotate(q=Sum("qty"))}
    given = {r["load__given_by"]: r["q"] for r in loads.exclude(load__given_by=None)
             .values("load__given_by").annotate(q=Sum("qty"))}
    # Возвраты, которые принял сотрудник, и наш убыток по себестоимости.
    costs, returns = {}, defaultdict(lambda: [0, m.ZERO])
    for r in _since(m.Return.objects.exclude(worker=None), "date", start).values("worker", "product").annotate(
            q=Sum("qty")):
        if r["product"] not in costs:
            costs[r["product"]] = m.Product.objects.get(pk=r["product"]).unit_cost()
        returns[r["worker"]][0] += r["q"]
        returns[r["worker"]][1] += r["q"] * costs[r["product"]]
    # Дни работы: отметки в табеле плюс дни, когда сотрудник что-то делал.
    days = defaultdict(set)
    for model, field in [(m.Shipment, "worker"), (m.Production, "worker"), (m.MaterialBatch, "worker"),
                         (m.VanLoad, "worker"), (m.VanLoad, "given_by"), (m.Return, "worker")]:
        for r in _since(model.objects.exclude(**{field: None}), "date", start).values(field, "date").distinct():
            days[r[field]].add(r["date"])
    absent = defaultdict(int)
    for d in _since(m.WorkDay.objects.all(), "date", start).values("worker", "date", "status"):
        if d["status"] == m.WorkDay.Status.WORKED:
            days[d["worker"]].add(d["date"])
        else:
            days[d["worker"]].discard(d["date"])
            if d["status"] == m.WorkDay.Status.ABSENT:
                absent[d["worker"]] += 1
    workers = m.Worker.objects.all().order_by("-active", "role", "name")
    if role:
        workers = workers.filter(role=role)
    if not show_inactive:
        workers = workers.filter(active=True)
    if query:
        # В Python, а не в базе: SQLite не умеет без учёта регистра для кириллицы.
        q = query.casefold()
        workers = [w for w in workers if q in w.name.casefold() or q in w.phone]
    rows = []
    for w in workers:
        s, q = sales.get(w.pk, (m.ZERO, 0))
        rows.append({
            "worker": w,
            "sales": s,
            "qty": q,
            "shops": visits.get(w.pk, 0),
            "cash": cash.get(w.pk, m.ZERO),
            "expenses": expenses.get(w.pk) or m.ZERO,
            "produced": produced.get(w.pk, 0),
            "received": received.get(w.pk, 0),
            "given": given.get(w.pk, 0),
            "on_hand": sum(w.stock().values()) if w.is_seller() else 0,
            "returns": returns[w.pk][0] if w.pk in returns else 0,
            "loss": returns[w.pk][1] if w.pk in returns else m.ZERO,
            "days": len(days.get(w.pk, ())),
            "absent": absent.get(w.pk, 0),
        })
    return {"rows": rows, "ratings": _ratings(rows)}


def _ratings(rows):
    """Топ-5 сотрудников по разным показателям для карточки рейтинга."""
    def top(key, only=None):
        picked = [r for r in rows if r[key] and (only is None or only(r))]
        return sorted(picked, key=lambda r: -r[key])[:5]
    return {
        "sales": top("sales"),
        "loss": top("loss"),
        "shops": top("shops"),
        "days": top("days"),
    }


def staff_detail(worker, period):
    start = period_start(period)
    items = _since(m.ShipmentItem.objects.filter(shipment__worker=worker), "shipment__date", start)
    days, values = _sales_by_day(items, start)
    shipments = _since(worker.shipments.select_related("shop"), "date", start)
    payments = _since(worker.payments.exclude(shop=None), "date", start)
    expenses = _since(worker.expenses.all(), "date", start)
    productions = _since(worker.productions.select_related("product"), "date", start)
    loads = _since(worker.loads.prefetch_related("items__product"), "date", start)
    by_product = items.values("product__name").annotate(q=Sum("qty"), s=_money(F("qty") * F("price"))).order_by("-s")
    return {
        "worker": worker,
        "sales": items.aggregate(s=_money(F("qty") * F("price")))["s"] or m.ZERO,
        "qty": items.aggregate(s=Sum("qty"))["s"] or 0,
        "cash": (shipments.aggregate(s=Sum("paid"))["s"] or m.ZERO) + (payments.aggregate(s=Sum("amount"))["s"]
                                                                       or m.ZERO),
        "expense_total": expenses.aggregate(s=Sum("amount"))["s"] or m.ZERO,
        "produced": productions.aggregate(s=Sum("qty"))["s"] or 0,
        "stock": sorted(worker.stock().items(), key=lambda kv: kv[0].name) if worker.is_seller() else [],
        "by_product": by_product,
        "shipments": shipments[:50],
        "expenses": expenses[:50],
        "productions": productions[:50],
        "loads": loads[:30],
        "shop_list": worker.shops.all(),
        "returns": _since(worker.returns.select_related("shop", "product"), "date", start)[:50],
        "workdays": _since(worker.workdays.all(), "date", start)[:62],
        "chart_days": days,
        "chart_sales": values,
    }


def shop_detail(shop, period):
    start = period_start(period)
    items = _since(m.ShipmentItem.objects.filter(shipment__shop=shop), "shipment__date", start)
    returns = _since(shop.returns.select_related("product"), "date", start)
    days, values = _sales_by_day(items, start)
    sales = items.aggregate(s=_money(F("qty") * F("price")))["s"] or m.ZERO
    qty = items.aggregate(s=Sum("qty"))["s"] or 0
    returned_qty = returns.aggregate(s=Sum("qty"))["s"] or 0
    by_product = []
    ret_by_product = {r["product"]: r["q"] for r in returns.values("product").annotate(q=Sum("qty"))}
    for r in items.values("product", "product__name").annotate(q=Sum("qty"), s=_money(F("qty") * F("price"))) \
            .order_by("-s"):
        back = ret_by_product.get(r["product"], 0)
        by_product.append({"name": r["product__name"], "qty": r["q"], "sales": r["s"], "returns": back,
                           "return_pct": round(back / r["q"] * 100) if r["q"] else 0})
    return {
        "shop": shop,
        "sales": sales,
        "qty": qty,
        "returned": returns.aggregate(s=_money(F("qty") * F("price")))["s"] or m.ZERO,
        "returned_qty": returned_qty,
        "return_pct": round(returned_qty / qty * 100) if qty else 0,
        "debt": shop.debt(),
        "fresh": shop.freshness(),
        "by_product": by_product,
        "shipments": _since(shop.shipments.select_related("worker").prefetch_related("items__product"),
                            "date", start)[:50],
        "returns": returns[:50],
        "payments": _since(shop.payments.select_related("worker"), "date", start)[:50],
        "orders": shop.orders.all()[:10],
        "agents": shop.agents.all(),
        "chart_days": days,
        "chart_sales": values,
    }


def product_detail(product, period):
    start = period_start(period)
    items = _since(product.shipment_items.all(), "shipment__date", start)
    returns = _since(product.returns.select_related("shop"), "date", start)
    days, values = _sales_by_day(items, start)
    qty = items.aggregate(s=Sum("qty"))["s"] or 0
    returned_qty = returns.aggregate(s=Sum("qty"))["s"] or 0
    recipe = [{"item": r, "price": r.material.avg_price(), "cost": r.qty * r.material.avg_price()}
              for r in product.recipe.select_related("material__unit")]
    in_agents = sum(w.stock().get(product, 0) for w in m.Worker.objects.filter(active=True) if w.is_seller())
    unit_cost = product.unit_cost()
    return {
        "product": product,
        "sales": items.aggregate(s=_money(F("qty") * F("price")))["s"] or m.ZERO,
        "qty": qty,
        "returned_qty": returned_qty,
        "return_pct": round(returned_qty / qty * 100) if qty else 0,
        "produced": _since(product.productions.all(), "date", start).aggregate(s=Sum("qty"))["s"] or 0,
        "stock": product.stock(),
        "in_agents": in_agents,
        "unit_cost": unit_cost,
        "unit_profit": product.price - unit_cost,
        "recipe": recipe,
        "top_shops": items.values("shipment__shop__name").annotate(
            q=Sum("qty"), s=_money(F("qty") * F("price"))).order_by("-q")[:15],
        "returns": returns[:30],
        "productions": _since(product.productions.select_related("worker"), "date", start)[:30],
        "chart_days": days,
        "chart_sales": values,
    }


def recent(limit=15):
    """Последние записи всех видов: что и когда вносили."""
    rows = []

    def add(qs, kind, text, amount, url_name):
        for obj in qs[:limit]:
            rows.append({"date": obj.date, "id": obj.pk, "kind": kind, "text": text(obj),
                         "amount": amount(obj), "url": f"admin:core_{url_name}_change"})

    add(m.Purchase.objects.select_related("supplier"), _("Приход сырья"), lambda o: o.supplier.name,
        lambda o: o.total(), "purchase")
    add(m.Production.objects.select_related("product"), _("Производство"), lambda o: f"{o.product.name} × {o.qty}",
        lambda o: None, "production")
    add(m.MaterialBatch.objects.select_related("material"), _("Выпечка / заготовка"),
        lambda o: f"{o.material.name} × {o.qty:g}", lambda o: o.total_cost(), "materialbatch")
    add(m.VanLoad.objects.select_related("worker"), _("Передача агенту"),
        lambda o: f"{o.worker or ''}" + (_(" сдал остаток в цех") if o.kind == m.VanLoad.Kind.UNLOAD else ""),
        lambda o: None, "vanload")
    add(m.Shipment.objects.select_related("shop"), _("Отгрузка"), lambda o: o.shop.name, lambda o: o.total(), "shipment")
    add(m.Return.objects.select_related("shop", "product"), _("Возврат"),
        lambda o: f"{o.shop.name}: {o.product.name} × {o.qty}", lambda o: o.total(), "return")
    add(m.Payment.objects.select_related("shop", "supplier"), _("Оплата"),
        lambda o: _("от %(name)s") % {"name": o.shop.name} if o.shop else _("поставщику %(name)s") % {"name": o.supplier.name}, lambda o: o.amount, "payment")
    add(m.Expense.objects.all(), _("Расход"), lambda o: o.get_category_display() + (f" · {o.note}" if o.note else ""),
        lambda o: o.amount, "expense")
    add(m.MaterialWriteOff.objects.select_related("material"), _("Списание сырья"),
        lambda o: f"{o.material.name}: {o.qty:g}", lambda o: None, "materialwriteoff")
    rows.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
    return rows[:limit]


def _match(query, *texts):
    q = query.casefold()
    return any(q in (t or "").casefold() for t in texts)


def search(query):
    """Общий поиск по справочникам. Фильтр в Python: SQLite не умеет без регистра по-русски."""
    query = query.strip()
    if not query:
        return []
    groups = [
        (_("Магазины"), [(s.name, " · ".join(x for x in (s.district, s.address, s.phone) if x),
                       reverse("shop_detail", args=[s.pk]))
                      for s in m.Shop.objects.all() if _match(query, s.name, s.firm, s.address, s.district,
                                                               s.phone, s.contact)]),
        (_("Товары"), [(p.name, _("%(amount)s сум") % {"amount": f"{p.price:,.0f}".replace(",", " ")}, reverse("product_detail", args=[p.pk]))
                    for p in m.Product.objects.all() if _match(query, p.name)]),
        (_("Сырьё"), [(r.name, r.pack_label(), reverse("admin:core_rawmaterial_change", args=[r.pk]))
                   for r in m.RawMaterial.objects.select_related("unit") if _match(query, r.name)]),
        (_("Сотрудники"), [(w.name, " · ".join(x for x in (w.get_role_display(), w.phone) if x),
                         reverse("staff_detail", args=[w.pk]))
                        for w in m.Worker.objects.all() if _match(query, w.name, w.phone)]),
        (_("Поставщики"), [(s.name, s.phone, reverse("admin:core_supplier_change", args=[s.pk]))
                        for s in m.Supplier.objects.all() if _match(query, s.name, s.phone)]),
    ]
    return [(title, rows) for title, rows in groups if rows]


def recipes():
    """Все рецепты: товары и свои полуфабрикаты, с себестоимостью по текущим ценам."""
    products = []
    for p in m.Product.objects.prefetch_related("recipe__material__unit"):
        rows = [{"material": r.material, "qty": r.qty, "cost": r.qty * r.material.avg_price()} for r in p.recipe.all()]
        products.append({"obj": p, "lines": rows, "cost": sum((x["cost"] for x in rows), m.ZERO)})
    homemade = []
    for mat in m.RawMaterial.objects.filter(homemade=True).prefetch_related("ingredients__ingredient__unit"):
        rows = [{"material": r.ingredient, "qty": r.qty, "cost": r.qty * r.ingredient.avg_price()}
                for r in mat.ingredients.all()]
        homemade.append({"obj": mat, "lines": rows, "cost": sum((x["cost"] for x in rows), m.ZERO)})
    return {"products": products, "homemade": homemade}


def purchases(limit=15):
    """Последние купленные позиции: что, сколько упаковок, по какой цене."""
    rows = []
    items = (m.PurchaseItem.objects.select_related("purchase__supplier", "material__unit")
             .order_by("-purchase__date", "-purchase_id", "id")[:limit])
    for it in items:
        old = it.before_packs()
        size = 1 if old else it.material.pack_size or 1
        rows.append({"item": it, "purchase": it.purchase, "material": it.material, "old": old,
                     "packs": (it.qty / size).quantize(Decimal("0.001")), "pack_price": it.price * size,
                     "sum": it.qty * it.price})
    return rows
