"""Логика Telegram-бота без самого Telegram: всё здесь синхронно и покрыто тестами."""
import os
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from . import models as m
from .models import ZERO, _sum
from .templatetags.fmt import money, qty


def owner_ids():
    """Telegram ID владельца и тех, кто подтверждает заказы. Через запятую в OWNER_TELEGRAM_IDS."""
    # Берём только числа: значение могли вставить как «YourID:409928788».
    return {int(x) for x in re.findall(r"\d+", os.environ.get("OWNER_TELEGRAM_IDS", ""))}


# ---------- Язык ----------

_languages = {}


def language_of(telegram_id):
    """'ru' или 'uz'. None, если человек ещё не выбирал язык."""
    if telegram_id not in _languages:
        _languages[telegram_id] = (m.BotUser.objects.filter(telegram_id=telegram_id)
                                   .values_list("language", flat=True).first())
    return _languages[telegram_id]


def set_language(telegram_id, language):
    m.BotUser.objects.update_or_create(telegram_id=telegram_id, defaults={"language": language})
    _languages[telegram_id] = language


def in_language(telegram_id, build):
    """Текст для человека на его языке. build — функция без аргументов, которая собирает текст."""
    with translation.override(language_of(telegram_id) or "ru"):
        return build()


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
        return _("Корзина пуста.")
    lines, total = [], 0
    for product in m.Product.objects.filter(pk__in=cart):
        n = cart[product.pk]
        total += product.price * n
        lines.append(f"{product.name} × {n} = {money(product.price * n)}")
    lines.append(_("Итого: %(x)s сум") % {"x": money(total)})
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
    lines = [_("Заказ №%(pk)s · %(shop)s") % {"pk": order.pk, "shop": order.shop},
             timezone.localtime(order.created_at).strftime("%d.%m.%Y %H:%M")]
    for item in order.items.select_related("product"):
        lines.append(f"{item.product.name} × {item.qty} = {money(item.qty * item.price)}")
    lines.append(_("Итого: %(x)s сум") % {"x": money(order.total())})
    if order.note:
        lines.append(_("Комментарий: %(note)s") % {"note": order.note})
    lines.append(_("Статус: %(get_status_display)s") % {"get_status_display": order.get_status_display()})
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
        return _("Ваш долг: %(x)s сум.") % {"x": money(debt)}
    if debt < 0:
        return _("У вас переплата: %(x)s сум.") % {"x": money(-debt)}
    return _("Долгов нет. Спасибо!")


def history_text(shop, limit=5):
    orders = list(shop.orders.prefetch_related("items__product")[:limit])
    if not orders:
        return _("Заказов пока нет.")
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
    period = _("за %(start)s") % {"start": format(start, '%d.%m')} if start == end else f"{start:%d.%m}–{end:%d.%m}"
    lines = [title or _("Сводка %(period)s") % {"period": period}, ""]
    lines += [
        _("💰 Продажи: %(x)s сум") % {"x": money(d['revenue'])},
        _("💵 Получено денег: %(x)s") % {"x": money(d['cash_in'])}
        + (_(" (из них картой %(x)s)") % {"x": money(d['card_in'])} if d["card_in"] else ""),
        _("📦 Приход сырья: %(x)s") % {"x": money(d['purchased'])},
        _("🧾 Прочие расходы: %(x)s") % {"x": money(d['expenses'])},
        _("↩️ Возвраты: %(x)s") % {"x": money(d['returned'])},
        _("📈 Прибыль: %(x)s") % {"x": money(d['profit'])},
        _("Нам должны: %(x)s · Мы должны: %(x2)s") % {"x": money(d['receivable']), "x2": money(d['payable'])},
    ]
    if d["consignment"]:
        lines.append(_("На консигнации за период: %(x)s") % {"x": money(d['consignment'])})
    lines += [
        "",
        _("Изделия, шт:"),
        _("сделано %(x)s · продано %(x2)s · возврат %(x3)s") % {"x": d['produced_qty'], "x2": d['sold_qty'], "x3": d['returned_qty']},
        _("в цеху %(x)s · у агентов %(x2)s") % {"x": d['workshop_qty'], "x2": d['agents_qty']},
    ]
    new = m.Order.objects.filter(status=m.Order.Status.NEW).count()
    confirmed = m.Order.objects.filter(status=m.Order.Status.CONFIRMED).count()
    if new or confirmed:
        lines.append(_("\nЗаказы из бота: новых %(new)s, ждут отгрузки %(confirmed)s") % {"new": new, "confirmed": confirmed})
    low = [x for x in m.RawMaterial.objects.all() if x.is_low()]
    if low:
        lines.append(_("Заканчивается: ") + ", ".join(
            f"{x.name} ({qty(x.stock())} {x.unit})" for x in low))
    return "\n".join(lines)


def daily_report_text(day=None):
    day = day or timezone.localdate()
    text = summary_text(day, day, _("Итоги дня %(day)s") % {"day": format(day, '%d.%m.%Y')})
    agents = [w for w in m.Worker.objects.filter(active=True) if w.is_seller()]
    rows = []
    for w in agents:
        sold = _sum(m.ShipmentItem.objects.filter(shipment__worker=w, shipment__date=day), F("qty") * F("price"))
        if sold or w.cash_on(day):
            rows.append(_("• %(name)s: продал на %(x)s, получил %(x2)s") % {"name": w.name, "x": money(sold), "x2": money(w.cash_on(day))})
    if rows:
        text += _("\n\nАгенты:\n") + "\n".join(rows)
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
        lines.append(_("Телефон: %(phone)s") % {"phone": worker.phone})
    if worker.districts:
        lines.append(_("Районы: %(districts)s") % {"districts": worker.districts})
    lines.append(_("Бот: %(x)s") % {"x": _('подключён') if worker.telegram_id else _('не подключён')})
    lines.append(_("\nС %(start)s по %(end)s:") % {"start": format(start, '%d.%m'), "end": format(end, '%d.%m')})
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
            _("Получил из цеха: %(x)s шт") % {"x": got.aggregate(s=Sum('qty'))['s'] or 0},
            _("Продал: %(x)s шт на %(x2)s сум") % {"x": ship.aggregate(s=Sum('qty'))['s'] or 0, "x2": money(sold)},
            _("Отгрузок: %(count)s, магазинов: %(x)s") % {"count": shipments.count(), "x": shipments.values('shop').distinct().count()},
            _("Получил денег: %(x)s") % {"x": money(paid)},
            _("Отдал в долг: %(x)s") % {"x": money(sold - _sum(shipments, F('paid')))},
            _("Возвраты в его магазинах: %(x)s шт на %(x2)s") % {"x": returns.aggregate(s=Sum('qty'))['s'] or 0, "x2": money(_sum(returns, F('qty') * F('price')))},
        ]
        stock = worker.stock()
        lines.append(_("Сейчас на руках: ") + (", ".join(f"{p.name} {n}" for p, n in stock.items()) or _("ничего")))
    made = worker.productions.filter(**period).aggregate(s=Sum("qty"))["s"]
    if made:
        lines.append(_("Сделал: %(made)s шт") % {"made": made})
    gave = m.VanLoadItem.objects.filter(load__given_by=worker, load__kind=m.VanLoad.Kind.LOAD,
                                        load__date__gte=start, load__date__lte=end).aggregate(s=Sum("qty"))["s"]
    if gave:
        lines.append(_("Передал агентам: %(gave)s шт") % {"gave": gave})
    lines.append(_("Расходы: %(x)s сум") % {"x": money(_sum(worker.expenses.filter(**period), F('amount')))})
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
    return f"https://t.me/{username}?start={code}" if username else _("код %(code)s") % {"code": code}


def shop_card(shop):
    lines = [_("Магазин «%(name)s»") % {"name": shop.name}]
    if shop.firm:
        lines.append(_("Фирма: %(firm)s") % {"firm": shop.firm})
    lines.append(_("Район: %(district)s") % {"district": shop.district})
    if shop.address:
        lines.append(_("Адрес: %(address)s") % {"address": shop.address})
    if shop.phone:
        lines.append(_("Телефон: %(phone)s") % {"phone": shop.phone})
    if shop.map_url():
        lines.append(_("Карта: %(map_url)s") % {"map_url": shop.map_url()})
    lines.append(_("Долг: %(x)s сум") % {"x": money(shop.debt())})
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
    out.append(_("Итого: %(x)s сум") % {"x": money(total)})
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
    lines = [_("Приход №%(pk)s · %(supplier)s") % {"pk": purchase.pk, "supplier": purchase.supplier}]
    for item in purchase.items.select_related("material"):
        lines.append(f"{item.material.name}: {qty(item.qty)} {item.material.unit} × "
                     f"{money(item.price)}")
    lines.append(_("Сумма: %(x)s сум, оплачено %(x2)s, в долг %(x3)s") % {"x": money(total), "x2": money(purchase.paid), "x3": money(total - purchase.paid)})
    lines.append(_("Всего должны поставщику: %(x)s сум") % {"x": money(purchase.supplier.debt())})
    return "\n".join(lines)


# Производство

def create_production(product_id, n, worker=None):
    """ValidationError, если не хватает сырья или нет рецепта."""
    production = m.Production(product_id=product_id, qty=n, worker=worker)
    production.full_clean()
    production.save()
    return production


def production_text(production):
    lines = [_("Записано: %(name)s × %(qty)s") % {"name": production.product.name, "qty": production.qty},
             _("Себестоимость 1 шт: %(x)s сум") % {"x": money(production.unit_cost)}]
    for c in production.consumptions.select_related("material"):
        lines.append(f"− {c.material.name}: {qty(c.qty)} {c.material.unit}")
    lines.append(_("В цеху теперь: %(stock)s шт") % {"stock": production.product.stock()})
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
        raise ValidationError(_("Не хватает. ") + "; ".join(short))


@transaction.atomic
def create_handover(agent, given_by, cart, kind=m.VanLoad.Kind.LOAD):
    """Цех передаёт товар агенту (или агент сдаёт остаток в цех). ValidationError, если не хватает."""
    if kind == m.VanLoad.Kind.LOAD:
        _check(cart, {p.pk: n for p, n in workshop_stock().items()}, _("в цеху"))
    else:
        _check(cart, {p.pk: n for p, n in agent.stock().items()}, _("у агента"))
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
    lines = [_("У %(name)s на руках:") % {"name": agent.name}]
    if stock:
        lines += [_("%(name)s: %(n)s шт") % {"name": p.name, "n": n} for p, n in sorted(stock.items(), key=lambda kv: kv[0].name)]
    else:
        lines.append(_("ничего."))
    lines.append(_("Получено денег сегодня: %(x)s сум") % {"x": money(agent.cash_on(timezone.localdate()))})
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
        _check(cart, {p.pk: n for p, n in agent.stock().items()}, _("у вас"))
    else:
        _check(cart, {p.pk: n for p, n in workshop_stock().items()}, _("в цеху"))
    order = None
    if order_id:
        order = m.Order.objects.filter(pk=order_id, shop=shop, shipment=None).first()
    if order:
        note = (_("По заказу №%(pk)s из бота. %(note)s") % {"pk": order.pk, "note": note}).strip()
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
    lines = [_("Отгрузка №%(pk)s · %(shop)s") % {"pk": shipment.pk, "shop": shipment.shop}]
    for item in shipment.items.select_related("product"):
        lines.append(f"{item.product.name} × {item.qty} = {money(item.qty * item.price)}")
    total = shipment.total()
    lines.append(_("Оплата: %(x)s. Сумма: %(x2)s сум, оплачено %(x3)s, в долг %(x4)s") % {"x": shipment.get_method_display().lower(), "x2": money(total), "x3": money(shipment.paid), "x4": money(total - shipment.paid)})
    lines.append(_("Общий долг магазина: %(x)s сум") % {"x": money(shipment.shop.debt())})
    return "\n".join(lines)


def create_return(shop, product_id, n, reason, worker=None):
    return m.Return.objects.create(shop=shop, product_id=product_id, qty=n, reason=reason, worker=worker)


def create_payment(shop, amount, worker=None, method=m.Payment.Method.CASH):
    return m.Payment.objects.create(shop=shop, amount=amount, worker=worker, method=method,
                                    note=_("Принял: %(worker)s") % {"worker": worker} if worker else "")


def create_expense(category, amount, note="", worker=None):
    return m.Expense.objects.create(category=category, amount=amount, note=note[:300], worker=worker)


# ---------- Срок годности: напоминания о магазинах без заказа ----------

REMIND_DAYS_BEFORE = 2  # начинаем напоминать за 2 дня до конца срока (при сроке 5 дней — на 3-й день)


def _has_new_order(shop, since):
    return shop.orders.filter(status__in=[m.Order.Status.NEW, m.Order.Status.CONFIRMED],
                              created_at__date__gte=since).exists()


def reminder_text(shop, info):
    head = _("Магазин «%(name)s» (%(district)s)") % {"name": shop.name, "district": shop.district}
    delivered = _("товар привезли %(x)s") % {"x": format(info['date'], '%d.%m')}
    left = info["days_left"]
    if left > 0:
        text = _("⏰ %(head)s: %(delivered)s, годен до %(x)s, осталось %(left)s дн. Заказа пока нет.") % {"head": head, "delivered": delivered, "x": format(info['expires'], '%d.%m'), "left": left}
    elif left == 0:
        text = _("⏰ %(head)s: %(delivered)s, сегодня последний день срока. Заказа пока нет.") % {"head": head, "delivered": delivered}
    else:
        text = (_("⚠️ %(head)s: срок товара от %(x)s истёк %(x2)s (%(x3)s дн. назад). Проверьте, чтобы просрочку не продали покупателям. Заказа нет.") % {"head": head, "x": format(info['date'], '%d.%m'), "x2": format(info['expires'], '%d.%m'), "x3": -left})
    if shop.phone:
        text += _("\nТелефон: %(phone)s") % {"phone": shop.phone}
    return text


def due_reminders(today=None):
    """Магазины, о которых сегодня надо напомнить: [(магазин, свежесть, срок_истёк)].

    Текст собирается reminder_text отдельно для каждого получателя, на его языке.
    """
    today = today or timezone.localdate()
    result = []
    for shop in m.Shop.objects.filter(active=True, archived=False).exclude(reminded_on=today):
        info = shop.freshness(today)
        if not info or info["days_left"] > REMIND_DAYS_BEFORE:
            continue
        if shop.snoozed_shipment == info["shipment"].pk or _has_new_order(shop, info["date"]):
            continue
        result.append((shop, info, info["days_left"] < 0))
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
