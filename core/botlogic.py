"""Логика Telegram-бота без самого Telegram: всё здесь синхронно и покрыто тестами."""
import os
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from . import models as m
from .models import ZERO, _sum
from .templatetags.fmt import money, qty


def owner_ids():
    """Telegram ID владельца и тех, кто подтверждает заказы. Через запятую в OWNER_TELEGRAM_IDS."""
    # Берём только числа: значение могли вставить как «YourID:409928788».
    return {int(x) for x in re.findall(r"\d+", os.environ.get("OWNER_TELEGRAM_IDS", ""))}


def is_owner(telegram_id):
    return telegram_id in owner_ids()


def shop_for(telegram_id):
    return m.Shop.objects.filter(telegram_id=telegram_id, active=True).first()


def link_shop(code, telegram_id):
    """Привязывает Telegram-аккаунт к магазину по коду. Возвращает магазин или None."""
    shop = m.Shop.objects.filter(bot_code=code, active=True).first()
    if not shop:
        return None
    m.Shop.objects.filter(telegram_id=telegram_id).exclude(pk=shop.pk).update(telegram_id=None)
    shop.telegram_id = telegram_id
    shop.save(update_fields=["telegram_id"])
    return shop


def products():
    return list(m.Product.objects.filter(active=True))


def get_product(product_id):
    return m.Product.objects.filter(pk=product_id, active=True).first()


def cart_text(cart):
    """cart: {product_id: qty}."""
    if not cart:
        return "Корзина пуста."
    lines, total = [], 0
    for product in m.Product.objects.filter(pk__in=cart):
        n = cart[product.pk]
        total += product.price * n
        lines.append(f"{product.name} × {n} = {money(product.price * n)}")
    lines.append(f"Итого: {money(total)} сум")
    return "\n".join(lines)


@transaction.atomic
def create_order(shop, cart, note=""):
    order = m.Order.objects.create(shop=shop, note=note)
    for product in m.Product.objects.filter(pk__in=cart, active=True):
        if cart[product.pk] > 0:
            m.OrderItem.objects.create(order=order, product=product, qty=cart[product.pk],
                                       price=product.price)
    return order


def order_text(order):
    lines = [f"Заказ №{order.pk} · {order.shop}",
             timezone.localtime(order.created_at).strftime("%d.%m.%Y %H:%M")]
    for item in order.items.select_related("product"):
        lines.append(f"{item.product.name} × {item.qty} = {money(item.qty * item.price)}")
    lines.append(f"Итого: {money(order.total())} сум")
    if order.note:
        lines.append(f"Комментарий: {order.note}")
    lines.append(f"Статус: {order.get_status_display()}")
    return "\n".join(lines)


def set_status(order_id, status):
    """Подтвердить или отклонить новый заказ. Возвращает заказ или None, если он уже обработан."""
    order = m.Order.objects.select_related("shop").filter(pk=order_id).first()
    if not order or order.status != m.Order.Status.NEW:
        return None
    order.status = status
    order.save(update_fields=["status"])
    return order


def debt_text(shop):
    debt = shop.debt()
    if debt > 0:
        return f"Ваш долг: {money(debt)} сум."
    if debt < 0:
        return f"У вас переплата: {money(-debt)} сум."
    return "Долгов нет. Спасибо!"


def history_text(shop, limit=5):
    orders = list(shop.orders.prefetch_related("items__product")[:limit])
    if not orders:
        return "Заказов пока нет."
    return "\n\n".join(order_text(o) for o in orders)


def _period(days=None):
    """(начало, конец) периода: сегодня или последние N дней."""
    today = timezone.localdate()
    return (today if not days else today - timedelta(days=days - 1)), today


def summary_text(start=None, end=None, title=None):
    """Сводка для начальника: сначала деньги, потом штуки."""
    from . import reports

    today = timezone.localdate()
    start = start or today.replace(day=1)
    end = end or today
    d = reports.totals(start, end)
    period = f"за {start:%d.%m}" if start == end else f"{start:%d.%m}–{end:%d.%m}"
    lines = [title or f"Сводка {period}", ""]
    lines += [
        f"💰 Продажи: {money(d['revenue'])} сум",
        f"💵 Получено денег: {money(d['cash_in'])}"
        + (f" (из них картой {money(d['card_in'])})" if d["card_in"] else ""),
        f"📦 Приход сырья: {money(d['purchased'])}",
        f"🧾 Прочие расходы: {money(d['expenses'])}",
        f"↩️ Возвраты: {money(d['returned'])}",
        f"📈 Прибыль: {money(d['profit'])}",
        f"Нам должны: {money(d['receivable'])} · Мы должны: {money(d['payable'])}",
    ]
    if d["consignment"]:
        lines.append(f"На консигнации за период: {money(d['consignment'])}")
    lines += [
        "",
        "Изделия, шт:",
        f"сделано {d['produced_qty']} · продано {d['sold_qty']} · возврат {d['returned_qty']}",
        f"в цеху {d['workshop_qty']} · у агентов {d['agents_qty']}",
    ]
    new = m.Order.objects.filter(status=m.Order.Status.NEW).count()
    confirmed = m.Order.objects.filter(status=m.Order.Status.CONFIRMED).count()
    if new or confirmed:
        lines.append(f"\nЗаказы из бота: новых {new}, ждут отгрузки {confirmed}")
    low = [x for x in m.RawMaterial.objects.all() if x.is_low()]
    if low:
        lines.append("Заканчивается: " + ", ".join(
            f"{x.name} ({qty(x.stock())} {x.unit})" for x in low))
    return "\n".join(lines)


def daily_report_text(day=None):
    day = day or timezone.localdate()
    text = summary_text(day, day, f"Итоги дня {day:%d.%m.%Y}")
    agents = [w for w in m.Worker.objects.filter(active=True) if w.is_seller()]
    rows = []
    for w in agents:
        sold = _sum(m.ShipmentItem.objects.filter(shipment__worker=w, shipment__date=day), F("qty") * F("price"))
        if sold or w.cash_on(day):
            rows.append(f"• {w.name}: продал на {money(sold)}, получил {money(w.cash_on(day))}")
    if rows:
        text += "\n\nАгенты:\n" + "\n".join(rows)
    return text


# ---------- Сотрудники ----------

def worker_for(telegram_id):
    return m.Worker.objects.filter(telegram_id=telegram_id, active=True).select_related("vehicle").first()


def link_worker(code, telegram_id):
    worker = m.Worker.objects.filter(bot_code=code, active=True).first()
    if not worker:
        return None
    m.Worker.objects.filter(telegram_id=telegram_id).exclude(pk=worker.pk).update(telegram_id=None)
    worker.telegram_id = telegram_id
    worker.save(update_fields=["telegram_id"])
    return worker


def role_of(telegram_id):
    """'boss', 'seller', 'workshop' или None. Вместе с сотрудником (или None для владельца из настроек)."""
    worker = worker_for(telegram_id)
    if is_owner(telegram_id) or (worker and worker.role == m.Worker.Role.BOSS):
        return "boss", worker
    if worker and worker.is_seller():
        return "seller", worker
    if worker:
        return "workshop", worker
    return None, None


def is_staff(telegram_id):
    return role_of(telegram_id)[0] is not None


def boss_ids():
    ids = set(owner_ids())
    ids |= set(m.Worker.objects.filter(active=True, role=m.Worker.Role.BOSS).exclude(telegram_id=None)
               .values_list("telegram_id", flat=True))
    return ids


def create_worker(name, role):
    return m.Worker.objects.create(name=name.strip()[:200], role=role)


def workers():
    return list(m.Worker.objects.filter(active=True))


def sellers():
    return [w for w in m.Worker.objects.filter(active=True).select_related("vehicle") if w.is_seller()]


def get_worker(worker_id):
    return m.Worker.objects.filter(pk=worker_id).select_related("vehicle").first()


def worker_card(worker, start=None, end=None):
    """Табель сотрудника за период (по умолчанию с начала месяца)."""
    today = timezone.localdate()
    start = start or today.replace(day=1)
    end = end or today
    lines = [f"{worker.name} · {worker.get_role_display()}"]
    if worker.phone:
        lines.append(f"Телефон: {worker.phone}")
    if worker.districts:
        lines.append(f"Районы: {worker.districts}")
    if worker.vehicle_id:
        lines.append(f"Машина: {worker.vehicle}")
    lines.append(f"Бот: {'подключён' if worker.telegram_id else 'не подключён'}")
    lines.append(f"\nС {start:%d.%m} по {end:%d.%m}:")
    period = dict(date__gte=start, date__lte=end)
    ship = m.ShipmentItem.objects.filter(shipment__worker=worker, shipment__date__gte=start,
                                         shipment__date__lte=end)
    sold = _sum(ship, F("qty") * F("price"))
    if worker.is_seller() or sold:
        shipments = worker.shipments.filter(**period)
        paid = _sum(shipments, F("paid")) + _sum(worker.payments.filter(**period), F("amount"))
        returns = m.Return.objects.filter(shop__in=worker.shipments.values("shop"), **period)
        got = m.VanLoadItem.objects.filter(load__worker=worker, load__kind=m.VanLoad.Kind.LOAD,
                                           load__date__gte=start, load__date__lte=end)
        lines += [
            f"Получил из цеха: {got.aggregate(s=Sum('qty'))['s'] or 0} шт",
            f"Продал: {ship.aggregate(s=Sum('qty'))['s'] or 0} шт на {money(sold)} сум",
            f"Отгрузок: {shipments.count()}, магазинов: {shipments.values('shop').distinct().count()}",
            f"Получил денег: {money(paid)}",
            f"Отдал в долг: {money(sold - _sum(shipments, F('paid')))}",
            f"Возвраты в его магазинах: {returns.aggregate(s=Sum('qty'))['s'] or 0} шт "
            f"на {money(_sum(returns, F('qty') * F('price')))}",
        ]
        stock = worker.stock()
        lines.append("Сейчас на руках: " + (", ".join(f"{p.name} {n}" for p, n in stock.items()) or "ничего"))
    made = worker.productions.filter(**period).aggregate(s=Sum("qty"))["s"]
    if made:
        lines.append(f"Сделал: {made} шт")
    gave = m.VanLoadItem.objects.filter(load__given_by=worker, load__kind=m.VanLoad.Kind.LOAD,
                                        load__date__gte=start, load__date__lte=end).aggregate(s=Sum("qty"))["s"]
    if gave:
        lines.append(f"Передал агентам: {gave} шт")
    lines.append(f"Расходы: {money(_sum(worker.expenses.filter(**period), F('amount')))} сум")
    return "\n".join(lines)


def parse_number(text, allow_zero=False):
    """«1 500,5» → Decimal('1500.5'). None, если это не число или оно не больше нуля."""
    raw = (text or "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        return None
    return value


def parse_count(text):
    value = parse_number(text)
    if value is None or value != value.to_integral_value() or value > 100000:
        return None
    return int(value)


COORDS = re.compile(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")


def parse_location(text):
    """Координаты из ссылки Google/Яндекс карт или из «41.31, 69.24». None, если не нашли."""
    text = text or ""
    found = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", text) or COORDS.search(text.replace("%2C", ","))
    if not found:
        return None
    lat, lon = float(found.group(1)), float(found.group(2))
    if "yandex" in text and "ll=" in text:
        lat, lon = lon, lat  # у Яндекса сначала долгота
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


URL = re.compile(r"https?://\S+")


def resolve_location(text):
    """Как parse_location, но короткую ссылку (maps.app.goo.gl, yandex …/-/…) сначала открывает."""
    found = parse_location(text)
    link = URL.search(text or "")
    if found or not link:
        return found
    import urllib.request
    from urllib.parse import unquote
    try:
        request = urllib.request.Request(link.group(0), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=8) as response:
            final = unquote(unquote(response.geturl()))
    except Exception:
        return None
    return parse_location(final)


# Магазины

def districts(worker=None):
    return sorted({s.district for s in shops_for_worker(worker)})


def shops_for_worker(worker=None):
    """Магазины агента: прикреплённые, иначе из его районов, иначе все."""
    shops = m.Shop.objects.filter(active=True)
    if worker and worker.is_seller():
        attached = worker.shops.filter(active=True)
        if attached.exists():
            return list(attached)
        names = worker.district_list()
        if names:
            return [s for s in shops if s.district.strip().lower() in names]
    return list(shops)


def shops_in(district, worker=None):
    return [s for s in shops_for_worker(worker) if s.district == district]


def get_shop(shop_id):
    return m.Shop.objects.filter(pk=shop_id, active=True).first()


def _coord(value):
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.000001"))


def create_shop(data, worker=None):
    shop = m.Shop.objects.create(
        name=data["name"][:200], firm=data.get("firm", "")[:200], district=data["district"][:100],
        address=data.get("address", "")[:300], phone=data.get("phone", "")[:50],
        latitude=_coord(data.get("lat")), longitude=_coord(data.get("lon")),
    )
    if worker and worker.is_seller() and worker.shops.exists():
        worker.shops.add(shop)
    return shop


def invite_link(code):
    username = os.environ.get("TELEGRAM_BOT_USERNAME", "")
    return f"https://t.me/{username}?start={code}" if username else f"код {code}"


def shop_card(shop):
    lines = [f"Магазин «{shop.name}»"]
    if shop.firm:
        lines.append(f"Фирма: {shop.firm}")
    lines.append(f"Район: {shop.district}")
    if shop.address:
        lines.append(f"Адрес: {shop.address}")
    if shop.phone:
        lines.append(f"Телефон: {shop.phone}")
    if shop.map_url():
        lines.append(f"Карта: {shop.map_url()}")
    lines.append(f"Долг: {money(shop.debt())} сум")
    return "\n".join(lines)


# Приход сырья

def suppliers():
    return list(m.Supplier.objects.all())


def categories():
    return list(m.MaterialCategory.objects.all())


def materials(category_id=None):
    qs = m.RawMaterial.objects.select_related("unit")
    return list(qs.filter(category_id=category_id) if category_id else qs.filter(category=None))


def get_or_create_supplier(name):
    return m.Supplier.objects.get_or_create(name=name.strip()[:200])[0]


def get_or_create_category(name):
    return m.MaterialCategory.objects.get_or_create(name=name.strip()[:100])[0]


def units():
    m.default_unit()  # «шт» есть всегда
    return list(m.Unit.objects.all())


def get_or_create_unit(name):
    return m.Unit.objects.get_or_create(name=name.strip()[:20])[0]


def get_or_create_material(name, unit_name, category_id=None):
    material, created = m.RawMaterial.objects.get_or_create(
        name=name.strip()[:200], defaults={"unit": get_or_create_unit(unit_name), "category_id": category_id})
    if not created and category_id and not material.category_id:
        material.category_id = category_id
        material.save(update_fields=["category"])
    return m.RawMaterial.objects.select_related("unit").get(pk=material.pk)


def purchase_lines_text(lines):
    """lines: [[material_id, "qty", "price"], ...]. Если у сырья есть упаковка, qty и price за упаковку."""
    found = m.RawMaterial.objects.select_related("unit").in_bulk([x[0] for x in lines])
    out, total = [], ZERO
    for material_id, q, p in lines:
        material, q, p = found[material_id], Decimal(q), Decimal(p)
        total += q * p
        unit = f"{material.pack_word()} ({material.pack_label()})" if material.pack_size else material.unit
        out.append(f"{material.name}: {qty(q)} {unit} × {money(p)} = {money(q * p)}")
    out.append(f"Итого: {money(total)} сум")
    return "\n".join(out), total


@transaction.atomic
def create_purchase(supplier_id, lines, paid, note=""):
    purchase = m.Purchase.objects.create(supplier_id=supplier_id, paid=paid, note=note[:300])
    found = m.RawMaterial.objects.in_bulk([x[0] for x in lines])
    for material_id, q, p in lines:
        q, p = found[material_id].from_packs(Decimal(q), Decimal(p))
        m.PurchaseItem.objects.create(purchase=purchase, material_id=material_id, qty=q, price=p)
    return purchase


def purchase_text(purchase):
    total = purchase.total()
    lines = [f"Приход №{purchase.pk} · {purchase.supplier}"]
    for item in purchase.items.select_related("material"):
        lines.append(f"{item.material.name}: {qty(item.qty)} {item.material.unit} × "
                     f"{money(item.price)}")
    lines.append(f"Сумма: {money(total)} сум, оплачено {money(purchase.paid)}, "
                 f"в долг {money(total - purchase.paid)}")
    lines.append(f"Всего должны поставщику: {money(purchase.supplier.debt())} сум")
    return "\n".join(lines)


# Производство

def create_production(product_id, n, worker=None):
    """ValidationError, если не хватает сырья или нет рецепта."""
    production = m.Production(product_id=product_id, qty=n, worker=worker)
    production.full_clean()
    production.save()
    return production


def production_text(production):
    lines = [f"Записано: {production.product.name} × {production.qty}",
             f"Себестоимость 1 шт: {money(production.unit_cost)} сум"]
    for c in production.consumptions.select_related("material"):
        lines.append(f"− {c.material.name}: {qty(c.qty)} {c.material.unit}")
    lines.append(f"В цеху теперь: {production.product.stock()} шт")
    return "\n".join(lines)


# Товар: цех и агенты

def workshop_stock():
    return {p: p.stock() for p in m.Product.objects.filter(active=True)}


def items_text(cart):
    """cart: {product_id: qty} → строки без цен."""
    found = m.Product.objects.in_bulk(list(cart))
    return "\n".join(f"{found[pid].name} × {n}" for pid, n in cart.items() if pid in found)


def _check(cart, have, where):
    found = m.Product.objects.in_bulk(list(cart))
    short = [f"{found[pid].name}: {where} {have.get(pid, 0)}" for pid, n in cart.items()
             if n > have.get(pid, 0)]
    if short:
        raise ValidationError("Не хватает. " + "; ".join(short))


@transaction.atomic
def create_handover(agent, given_by, cart, kind=m.VanLoad.Kind.LOAD):
    """Цех передаёт товар агенту (или агент сдаёт остаток в цех). ValidationError, если не хватает."""
    if kind == m.VanLoad.Kind.LOAD:
        _check(cart, {p.pk: n for p, n in workshop_stock().items()}, "в цеху")
    else:
        _check(cart, {p.pk: n for p, n in agent.stock().items()}, "у агента")
    load = m.VanLoad.objects.create(worker=agent, given_by=given_by, vehicle=agent.vehicle, kind=kind)
    for pid, n in cart.items():
        if n > 0:
            m.VanLoadItem.objects.create(load=load, product_id=pid, qty=n)
    return load


def unload_all(agent, given_by=None):
    cart = {p.pk: n for p, n in agent.stock().items() if n > 0}
    if not cart:
        return None
    return create_handover(agent, given_by, cart, m.VanLoad.Kind.UNLOAD)


def agent_stock_text(agent):
    stock = agent.stock()
    lines = [f"У {agent.name} на руках:"]
    if stock:
        lines += [f"{p.name}: {n} шт" for p, n in sorted(stock.items(), key=lambda kv: kv[0].name)]
    else:
        lines.append("ничего.")
    lines.append(f"Получено денег сегодня: {money(agent.cash_on(timezone.localdate()))} сум")
    return "\n".join(lines)


def open_order(shop):
    """Последний неотгруженный заказ магазина из бота."""
    return (shop.orders.filter(status__in=[m.Order.Status.NEW, m.Order.Status.CONFIRMED], shipment=None)
            .prefetch_related("items").first())


def cart_total(cart):
    found = m.Product.objects.in_bulk(list(cart))
    return sum((found[pid].price * n for pid, n in cart.items() if pid in found), ZERO)


@transaction.atomic
def create_shipment(shop, cart, paid, agent=None, order_id=None, note="", method=m.Shipment.Method.CASH):
    """Отгрузка магазину: с остатка агента или (agent=None) прямо из цеха."""
    if agent:
        _check(cart, {p.pk: n for p, n in agent.stock().items()}, "у вас")
    else:
        _check(cart, {p.pk: n for p, n in workshop_stock().items()}, "в цеху")
    order = None
    if order_id:
        order = m.Order.objects.filter(pk=order_id, shop=shop, shipment=None).first()
    if order:
        note = f"По заказу №{order.pk} из бота. {note}".strip()
    shipment = m.Shipment.objects.create(shop=shop, worker=agent, vehicle=agent.vehicle if agent else None,
                                         paid=paid, method=method, note=note[:300])
    found = m.Product.objects.in_bulk(list(cart))
    for pid, n in cart.items():
        if n > 0:
            m.ShipmentItem.objects.create(shipment=shipment, product=found[pid], qty=n)
    if order:
        order.shipment = shipment
        order.status = m.Order.Status.SHIPPED
        order.save(update_fields=["shipment", "status"])
    return shipment


def shipment_text(shipment):
    lines = [f"Отгрузка №{shipment.pk} · {shipment.shop}"]
    for item in shipment.items.select_related("product"):
        lines.append(f"{item.product.name} × {item.qty} = {money(item.qty * item.price)}")
    total = shipment.total()
    lines.append(f"Оплата: {shipment.get_method_display().lower()}. Сумма: {money(total)} сум, "
                 f"оплачено {money(shipment.paid)}, в долг {money(total - shipment.paid)}")
    lines.append(f"Общий долг магазина: {money(shipment.shop.debt())} сум")
    return "\n".join(lines)


def create_return(shop, product_id, n, reason, worker=None):
    return m.Return.objects.create(shop=shop, product_id=product_id, qty=n, reason=reason, worker=worker)


def create_payment(shop, amount, worker=None, method=m.Payment.Method.CASH):
    return m.Payment.objects.create(shop=shop, amount=amount, worker=worker, method=method,
                                    note=f"Принял: {worker}" if worker else "")


def create_expense(category, amount, note="", worker=None):
    return m.Expense.objects.create(category=category, amount=amount, note=note[:300], worker=worker)


# ---------- Срок годности: напоминания о магазинах без заказа ----------

REMIND_DAYS_BEFORE = 2  # начинаем напоминать за 2 дня до конца срока (при сроке 5 дней — на 3-й день)


def _has_new_order(shop, since):
    return shop.orders.filter(status__in=[m.Order.Status.NEW, m.Order.Status.CONFIRMED],
                              created_at__date__gte=since).exists()


def reminder_text(shop, info):
    head = f"Магазин «{shop.name}» ({shop.district})"
    delivered = f"товар привезли {info['date']:%d.%m}"
    left = info["days_left"]
    if left > 0:
        text = f"⏰ {head}: {delivered}, годен до {info['expires']:%d.%m}, осталось {left} дн. Заказа пока нет."
    elif left == 0:
        text = f"⏰ {head}: {delivered}, сегодня последний день срока. Заказа пока нет."
    else:
        text = (f"⚠️ {head}: срок товара от {info['date']:%d.%m} истёк {info['expires']:%d.%m} "
                f"({-left} дн. назад). Проверьте, чтобы просрочку не продали покупателям. Заказа нет.")
    if shop.phone:
        text += f"\nТелефон: {shop.phone}"
    return text


def due_reminders(today=None):
    """Магазины, о которых сегодня надо напомнить: [(магазин, текст, срок_истёк)]."""
    today = today or timezone.localdate()
    result = []
    for shop in m.Shop.objects.filter(active=True, archived=False).exclude(reminded_on=today):
        info = shop.freshness(today)
        if not info or info["days_left"] > REMIND_DAYS_BEFORE:
            continue
        if shop.snoozed_shipment == info["shipment"].pk or _has_new_order(shop, info["date"]):
            continue
        result.append((shop, reminder_text(shop, info), info["days_left"] < 0))
    return result


def reminder_recipients(shop):
    """Начальники и агенты, за которыми этот магазин (прикреплён или в их районе)."""
    ids = boss_ids()
    district = shop.district.strip().lower()
    for worker in m.Worker.objects.filter(active=True).exclude(telegram_id=None).prefetch_related("shops"):
        if worker.pk in {a.pk for a in shop.agents.all()} or district in worker.district_list():
            ids.add(worker.telegram_id)
    return ids


def mark_reminded(shop, today=None):
    m.Shop.objects.filter(pk=shop.pk).update(reminded_on=today or timezone.localdate())


def archive_shop(shop_id):
    shop = m.Shop.objects.filter(pk=shop_id).first()
    if shop:
        m.Shop.objects.filter(pk=shop_id).update(archived=True)
    return shop


def snooze_shop(shop_id):
    """Больше не напоминать о последней поставке этого магазина."""
    shop = m.Shop.objects.filter(pk=shop_id).first()
    last = shop and shop.last_shipment()
    if last:
        m.Shop.objects.filter(pk=shop_id).update(snoozed_shipment=last.pk)
    return shop
