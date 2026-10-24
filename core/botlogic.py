"""Логика Telegram-бота без самого Telegram: всё здесь синхронно и покрыто тестами."""
import os
import re

from django.db import transaction
from django.utils import timezone

from . import models as m
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
