"""Логика Telegram-бота без самого Telegram: всё здесь синхронно и покрыто тестами."""
import os
import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from . import models as m
from .models import ZERO
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


def summary_text():
    """Сводка для владельца: новые заказы, что заканчивается, кто должен."""
    new = m.Order.objects.filter(status=m.Order.Status.NEW).count()
    confirmed = m.Order.objects.filter(status=m.Order.Status.CONFIRMED).count()
    lines = [f"Новых заказов: {new}, подтверждено и ждут отгрузки: {confirmed}."]
    low = [x for x in m.RawMaterial.objects.all() if x.is_low()]
    if low:
        lines.append("Заканчивается: " + ", ".join(
            f"{x.name} ({qty(x.stock())} {x.get_unit_display()})" for x in low))
    debtors = sorted(((s, s.debt()) for s in m.Shop.objects.all()), key=lambda p: -p[1])
    debtors = [(s, d) for s, d in debtors if d > 0]
    if debtors:
        lines.append(f"Нам должны всего: {money(sum(d for _, d in debtors))} сум")
        lines += [f"• {s.name}: {money(d)}" for s, d in debtors[:10]]
    return "\n".join(lines)


# ---------- Сотрудники: владелец и работники вносят данные из бота ----------

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


def is_staff(telegram_id):
    return is_owner(telegram_id) or worker_for(telegram_id) is not None


def create_worker(name):
    return m.Worker.objects.create(name=name.strip()[:200])


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


# Магазины

def districts():
    return list(m.Shop.objects.filter(active=True).order_by("district")
                .values_list("district", flat=True).distinct())


def shops_in(district):
    return list(m.Shop.objects.filter(active=True, district=district))


def get_shop(shop_id):
    return m.Shop.objects.filter(pk=shop_id, active=True).first()


def _coord(value):
    return None if value is None else Decimal(str(value)).quantize(Decimal("0.000001"))


def create_shop(data):
    return m.Shop.objects.create(
        name=data["name"][:200], firm=data.get("firm", "")[:200], district=data["district"][:100],
        address=data.get("address", "")[:300], phone=data.get("phone", "")[:50],
        latitude=_coord(data.get("lat")), longitude=_coord(data.get("lon")),
    )


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


def materials():
    return list(m.RawMaterial.objects.all())


def get_or_create_supplier(name):
    return m.Supplier.objects.get_or_create(name=name.strip()[:200])[0]


def get_or_create_material(name, unit):
    return m.RawMaterial.objects.get_or_create(name=name.strip()[:200], defaults={"unit": unit})[0]


def purchase_lines_text(lines):
    """lines: [[material_id, "qty", "price"], ...]."""
    found = m.RawMaterial.objects.in_bulk([x[0] for x in lines])
    out, total = [], ZERO
    for material_id, q, p in lines:
        material, q, p = found[material_id], Decimal(q), Decimal(p)
        total += q * p
        out.append(f"{material.name}: {qty(q)} {material.get_unit_display()} × {money(p)} = {money(q * p)}")
    out.append(f"Итого: {money(total)} сум")
    return "\n".join(out), total


@transaction.atomic
def create_purchase(supplier_id, lines, paid, note=""):
    purchase = m.Purchase.objects.create(supplier_id=supplier_id, paid=paid, note=note[:300])
    for material_id, q, p in lines:
        m.PurchaseItem.objects.create(purchase=purchase, material_id=material_id, qty=Decimal(q),
                                      price=Decimal(p))
    return purchase


def purchase_text(purchase):
    total = purchase.total()
    lines = [f"Приход №{purchase.pk} · {purchase.supplier}"]
    for item in purchase.items.select_related("material"):
        lines.append(f"{item.material.name}: {qty(item.qty)} {item.material.get_unit_display()} × "
                     f"{money(item.price)}")
    lines.append(f"Сумма: {money(total)} сум, оплачено {money(purchase.paid)}, "
                 f"в долг {money(total - purchase.paid)}")
    lines.append(f"Всего должны поставщику: {money(purchase.supplier.debt())} сум")
    return "\n".join(lines)


# Машины

def vehicles():
    return list(m.Vehicle.objects.filter(active=True))


def get_vehicle(vehicle_id):
    return m.Vehicle.objects.filter(pk=vehicle_id, active=True).first()


def create_vehicle(name):
    return m.Vehicle.objects.get_or_create(name=name.strip()[:100])[0]


def workshop_stock():
    return {p: p.stock() for p in m.Product.objects.filter(active=True)}


def items_text(cart):
    """cart: {product_id: qty} → строки без цен."""
    found = m.Product.objects.in_bulk(list(cart))
    return "\n".join(f"{found[pid].name} × {n}" for pid, n in cart.items() if pid in found)


@transaction.atomic
def create_van_load(vehicle, worker, cart, kind=m.VanLoad.Kind.LOAD):
    """Загрузить машину из цеха или сдать остаток в цех. ValidationError, если товара не хватает."""
    if kind == m.VanLoad.Kind.LOAD:
        have = {p.pk: n for p, n in workshop_stock().items()}
        where = "в цеху"
    else:
        have = {p.pk: n for p, n in vehicle.stock().items()}
        where = "в машине"
    found = m.Product.objects.in_bulk(list(cart))
    short = [f"{found[pid].name}: {where} {have.get(pid, 0)}" for pid, n in cart.items()
             if n > have.get(pid, 0)]
    if short:
        raise ValidationError("Не хватает. " + "; ".join(short))
    load = m.VanLoad.objects.create(vehicle=vehicle, worker=worker, kind=kind)
    for pid, n in cart.items():
        if n > 0:
            m.VanLoadItem.objects.create(load=load, product_id=pid, qty=n)
    return load


def unload_all(vehicle, worker):
    cart = {p.pk: n for p, n in vehicle.stock().items() if n > 0}
    if not cart:
        return None
    return create_van_load(vehicle, worker, cart, m.VanLoad.Kind.UNLOAD)


def van_text(vehicle):
    stock = vehicle.stock()
    lines = [f"Машина {vehicle}"]
    if stock:
        lines += [f"{p.name}: {n} шт" for p, n in sorted(stock.items(), key=lambda kv: kv[0].name)]
    else:
        lines.append("Машина пустая.")
    lines.append(f"Собрано денег сегодня: {money(vehicle.cash_on(timezone.localdate()))} сум")
    return "\n".join(lines)


def open_order(shop):
    """Последний неотгруженный заказ магазина из бота."""
    return (shop.orders.filter(status__in=[m.Order.Status.NEW, m.Order.Status.CONFIRMED], shipment=None)
            .prefetch_related("items").first())


def cart_total(cart):
    found = m.Product.objects.in_bulk(list(cart))
    return sum((found[pid].price * n for pid, n in cart.items() if pid in found), ZERO)


@transaction.atomic
def create_van_shipment(vehicle, shop, cart, paid, worker=None, order_id=None):
    """Отгрузка магазину с машины. ValidationError, если в машине не хватает товара."""
    have = {p.pk: n for p, n in vehicle.stock().items()}
    found = m.Product.objects.in_bulk(list(cart))
    short = [f"{found[pid].name}: в машине {have.get(pid, 0)}" for pid, n in cart.items()
             if n > have.get(pid, 0)]
    if short:
        raise ValidationError("В машине не хватает. " + "; ".join(short))
    order = None
    if order_id:
        order = m.Order.objects.filter(pk=order_id, shop=shop, shipment=None).first()
    shipment = m.Shipment.objects.create(
        shop=shop, vehicle=vehicle, worker=worker, paid=paid,
        note=f"По заказу №{order.pk} из бота" if order else "")
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
    lines.append(f"Сумма: {money(total)} сум, оплачено {money(shipment.paid)}, "
                 f"в долг {money(total - shipment.paid)}")
    lines.append(f"Общий долг магазина: {money(shipment.shop.debt())} сум")
    return "\n".join(lines)


def create_return(shop, product_id, n, reason):
    return m.Return.objects.create(shop=shop, product_id=product_id, qty=n, reason=reason)


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
    ids = set(owner_ids())
    district = shop.district.strip().lower()
    for worker in m.Worker.objects.filter(active=True).exclude(telegram_id=None):
        if district in worker.district_list():
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
