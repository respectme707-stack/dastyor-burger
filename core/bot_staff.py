"""Кнопки бота для владельца и сотрудников: новый магазин, приход сырья, машины."""
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command as CommandFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError

from . import botlogic as logic
from .models import RawMaterial, Return
from .templatetags.fmt import money

router = Router()

SHIP, RET, LOAD, VAN = "Отгрузка в магазин", "Возврат", "Загрузить машину", "Остаток в машине"
INCOME, NEWSHOP, NEWWORKER, SUMMARY = "Приход сырья", "Новый магазин", "Новый сотрудник", "Сводка"
SKIP = "Пропустить"


def staff_menu(owner):
    rows = [
        [KeyboardButton(text=SHIP), KeyboardButton(text=RET)],
        [KeyboardButton(text=LOAD), KeyboardButton(text=VAN)],
        [KeyboardButton(text=INCOME), KeyboardButton(text=NEWSHOP)],
    ]
    if owner:
        rows.append([KeyboardButton(text=SUMMARY), KeyboardButton(text=NEWWORKER)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb(rows, cancel=True):
    """rows: [[(текст, callback_data), ...], ...]."""
    keyboard = [[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    if cancel:
        keyboard.append([InlineKeyboardButton(text="Отмена", callback_data="x")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def column(items):
    return [[item] for item in items]


class NewShop(StatesGroup):
    name = State()
    firm = State()
    district = State()
    address = State()
    location = State()
    phone = State()


class Income(StatesGroup):
    supplier = State()
    supplier_name = State()
    pick = State()
    material_name = State()
    material_unit = State()
    qty = State()
    price = State()
    paid = State()


class Van(StatesGroup):
    vehicle = State()
    vehicle_name = State()
    district = State()
    shop = State()
    order = State()
    pick = State()
    qty = State()
    reason = State()
    paid = State()


class NewWorker(StatesGroup):
    name = State()


def _call(fn):
    return sync_to_async(fn, thread_sensitive=True)


worker_for = _call(logic.worker_for)
db = _call(lambda fn, *a, **kw: fn(*a, **kw))


async def who(uid):
    """(владелец?, сотрудник или None), либо None, если это не наш человек."""
    owner = logic.is_owner(uid)
    worker = await worker_for(uid)
    if not owner and not worker:
        return None
    return owner, worker


async def notify_owners(bot: Bot, uid, text):
    for owner in logic.owner_ids() - {uid}:
        try:
            await bot.send_message(owner, text)
        except Exception:
            logging.exception("Не удалось написать владельцу %s", owner)


def _cart(data):
    return {int(k): v for k, v in data.get("cart", {}).items()}


# ---------- Общие ----------

@router.message(CommandFilter("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    me = await who(message.from_user.id)
    if me:
        await message.answer("Отменено.", reply_markup=staff_menu(me[0]))


@router.message(F.text.in_({SHIP, RET, LOAD, VAN, INCOME, NEWSHOP, NEWWORKER}))
async def menu(message: Message, state: FSMContext):
    """Кнопки меню срабатывают на любом шаге и начинают новое действие."""
    if message.text == NEWSHOP:
        await shop_start(message, state)
    elif message.text == INCOME:
        await income_start(message, state)
    elif message.text == NEWWORKER:
        await worker_start(message, state)
    else:
        await van_start(message, state)


@router.callback_query(F.data == "x")
async def cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


# ---------- Новый магазин ----------

async def shop_start(message: Message, state: FSMContext):
    if not await who(message.from_user.id):
        return
    await state.clear()
    await state.set_state(NewShop.name)
    await message.answer("Новый магазин. Как называется магазин?", reply_markup=kb([]))


@router.message(NewShop.name, F.text)
async def shop_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(NewShop.firm)
    await message.answer("Фирма (юридическое название)?", reply_markup=kb([[(SKIP, "skip")]]))


@router.message(NewShop.firm, F.text)
@router.callback_query(NewShop.firm, F.data == "skip")
async def shop_firm(event, state: FSMContext):
    message = event.message if isinstance(event, CallbackQuery) else event
    await state.update_data(firm="" if isinstance(event, CallbackQuery) else event.text.strip())
    names = await db(logic.districts)
    await state.update_data(districts=names)
    await state.set_state(NewShop.district)
    rows = [[(name, f"d:{i}")] for i, name in enumerate(names)]
    text = "Район? Выберите или напишите новый." if names else "Район? Напишите название."
    await message.answer(text, reply_markup=kb(rows))
    if isinstance(event, CallbackQuery):
        await event.answer()


async def _ask_address(message, state, district):
    await state.update_data(district=district)
    await state.set_state(NewShop.address)
    await message.answer("Адрес или ориентир?", reply_markup=kb([[(SKIP, "skip")]]))


@router.message(NewShop.district, F.text)
async def shop_district_text(message: Message, state: FSMContext):
    await _ask_address(message, state, message.text.strip())


@router.callback_query(NewShop.district, F.data.startswith("d:"))
async def shop_district_pick(callback: CallbackQuery, state: FSMContext):
    names = (await state.get_data())["districts"]
    await _ask_address(callback.message, state, names[int(callback.data[2:])])
    await callback.answer()


@router.message(NewShop.address, F.text)
@router.callback_query(NewShop.address, F.data == "skip")
async def shop_address(event, state: FSMContext):
    message = event.message if isinstance(event, CallbackQuery) else event
    await state.update_data(address="" if isinstance(event, CallbackQuery) else event.text.strip())
    await state.set_state(NewShop.location)
    keyboard = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📍 Отправить локацию", request_location=True)],
        [KeyboardButton(text=SKIP)],
    ], resize_keyboard=True, one_time_keyboard=True)
    await message.answer("Локация магазина. Если вы сейчас в магазине, нажмите «📍 Отправить локацию». "
                         "Или прикрепите точку на карте через 📎 → Геопозиция.", reply_markup=keyboard)
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.message(NewShop.location, F.location)
@router.message(NewShop.location, F.text == SKIP)
async def shop_location(message: Message, state: FSMContext):
    if message.location:
        await state.update_data(lat=round(message.location.latitude, 6), lon=round(message.location.longitude, 6))
    me = await who(message.from_user.id)
    await state.set_state(NewShop.phone)
    await message.answer("Записал.", reply_markup=staff_menu(bool(me and me[0])))
    await message.answer("Телефон магазина?", reply_markup=kb([[(SKIP, "skip")]]))


@router.message(NewShop.phone, F.text)
@router.callback_query(NewShop.phone, F.data == "skip")
async def shop_phone(event, state: FSMContext, bot: Bot):
    message = event.message if isinstance(event, CallbackQuery) else event
    data = await state.get_data()
    data["phone"] = "" if isinstance(event, CallbackQuery) else event.text.strip()
    shop = await db(logic.create_shop, data)
    await state.clear()
    card = await db(logic.shop_card, shop)
    link = logic.invite_link(shop.bot_code)
    await message.answer(f"Готово!\n{card}\n\nСсылка для магазина, отправьте её им:\n{link}")
    await notify_owners(bot, event.from_user.id, f"Добавлен магазин.\n{card}")
    if isinstance(event, CallbackQuery):
        await event.answer()


# ---------- Приход сырья ----------

async def income_start(message: Message, state: FSMContext):
    if not await who(message.from_user.id):
        return
    await state.clear()
    rows = [[(s.name, f"s:{s.pk}")] for s in await db(logic.suppliers)]
    rows.append([("➕ Новый поставщик", "s:new")])
    await state.set_state(Income.supplier)
    await message.answer("Приход сырья. От какого поставщика?", reply_markup=kb(rows))


@router.callback_query(Income.supplier, F.data == "s:new")
async def income_supplier_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Income.supplier_name)
    await callback.message.answer("Название поставщика (фирмы)?", reply_markup=kb([]))
    await callback.answer()


async def _material_picker(message, state):
    rows = [[(f"{x.name} ({x.get_unit_display()})", f"m:{x.pk}")] for x in await db(logic.materials)]
    rows.append([("➕ Новое сырьё", "m:new")])
    await state.set_state(Income.pick)
    await message.answer("Какое сырьё пришло?", reply_markup=kb(rows))


@router.message(Income.supplier_name, F.text)
async def income_supplier_name(message: Message, state: FSMContext):
    supplier = await db(logic.get_or_create_supplier, message.text)
    await state.update_data(supplier=supplier.pk, supplier_name=supplier.name, lines=[])
    await _material_picker(message, state)


@router.callback_query(Income.supplier, F.data.startswith("s:"))
async def income_supplier(callback: CallbackQuery, state: FSMContext):
    await state.update_data(supplier=int(callback.data[2:]), lines=[])
    await _material_picker(callback.message, state)
    await callback.answer()


@router.callback_query(Income.pick, F.data == "m:new")
async def income_material_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Income.material_name)
    await callback.message.answer("Название сырья?", reply_markup=kb([]))
    await callback.answer()


@router.message(Income.material_name, F.text)
async def income_material_name(message: Message, state: FSMContext):
    await state.update_data(material_name=message.text.strip())
    await state.set_state(Income.material_unit)
    await message.answer("В чём считаем?", reply_markup=kb(
        [[(label, f"u:{value}") for value, label in RawMaterial.Unit.choices]]))


async def _ask_qty(message, state, material):
    await state.update_data(material=material.pk, unit=material.get_unit_display())
    await state.set_state(Income.qty)
    await message.answer(f"{material.name}: сколько {material.get_unit_display()}? Напишите число.")


@router.callback_query(Income.material_unit, F.data.startswith("u:"))
async def income_material_unit(callback: CallbackQuery, state: FSMContext):
    name = (await state.get_data())["material_name"]
    material = await db(logic.get_or_create_material, name, callback.data[2:])
    await _ask_qty(callback.message, state, material)
    await callback.answer()


@router.callback_query(Income.pick, F.data.startswith("m:"))
async def income_material(callback: CallbackQuery, state: FSMContext):
    material = await db(lambda pk: RawMaterial.objects.filter(pk=pk).first(), int(callback.data[2:]))
    if material:
        await _ask_qty(callback.message, state, material)
    await callback.answer()


@router.message(Income.qty, F.text)
async def income_qty(message: Message, state: FSMContext):
    value = logic.parse_number(message.text)
    if value is None:
        await message.answer("Напишите количество числом, например 50 или 2,5.")
        return
    data = await state.update_data(qty=str(value))
    await state.set_state(Income.price)
    await message.answer(f"Цена за 1 {data['unit']}, сум?")


async def _show_lines(message, state):
    data = await state.get_data()
    text, _ = await db(logic.purchase_lines_text, data["lines"])
    await state.set_state(Income.pick)
    await message.answer("Приход:\n" + text, reply_markup=kb([[("Добавить ещё", "i:more"), ("Готово", "i:done")]]))


@router.message(Income.price, F.text)
async def income_price(message: Message, state: FSMContext):
    value = logic.parse_number(message.text)
    if value is None:
        await message.answer("Напишите цену числом, например 3500.")
        return
    data = await state.get_data()
    lines = data["lines"] + [[data["material"], data["qty"], str(value)]]
    await state.update_data(lines=lines)
    await _show_lines(message, state)


@router.callback_query(Income.pick, F.data == "i:more")
async def income_more(callback: CallbackQuery, state: FSMContext):
    await _material_picker(callback.message, state)
    await callback.answer()


@router.callback_query(Income.pick, F.data == "i:done")
async def income_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    _, total = await db(logic.purchase_lines_text, data["lines"])
    await state.update_data(total=str(total))
    await state.set_state(Income.paid)
    await callback.message.answer(f"Итого {money(total)} сум. Сколько оплатили сразу? Напишите сумму.",
                                  reply_markup=kb([[("Всё оплачено", "pay:all"), ("Всё в долг", "pay:0")]]))
    await callback.answer()


async def _save_purchase(event, state, bot, paid):
    message = event.message if isinstance(event, CallbackQuery) else event
    data = await state.get_data()
    me = await who(event.from_user.id)
    note = f"Внёс в боте: {me[1]}" if me and me[1] else "Внесено в боте"
    purchase = await db(logic.create_purchase, data["supplier"], data["lines"], paid, note)
    await state.clear()
    text = await db(logic.purchase_text, purchase)
    await message.answer("Сохранено.\n" + text)
    await notify_owners(bot, event.from_user.id, "Новый приход сырья.\n" + text)


@router.callback_query(Income.paid, F.data.startswith("pay:"))
async def income_paid_button(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    paid = logic.Decimal(data["total"]) if callback.data == "pay:all" else logic.Decimal(0)
    await callback.answer()
    await _save_purchase(callback, state, bot, paid)


@router.message(Income.paid, F.text)
async def income_paid(message: Message, state: FSMContext, bot: Bot):
    value = logic.parse_number(message.text, allow_zero=True)
    if value is None:
        await message.answer("Напишите сумму числом, например 500000, или 0, если всё в долг.")
        return
    await _save_purchase(message, state, bot, value)


# ---------- Машины: загрузка, отгрузка, возврат, остаток ----------

async def _choose_vehicle(message, state, flow, uid):
    me = await who(uid)
    if not me:
        return
    owner, worker = me
    await state.clear()
    await state.update_data(flow=flow)
    if flow == "ret":
        await _shop_picker(message, state)
        return
    if worker and worker.vehicle_id and worker.vehicle.active:
        await _vehicle_chosen(message, state, worker.vehicle)
        return
    rows = [[(v.name, f"v:{v.pk}")] for v in await db(logic.vehicles)]
    if owner:
        rows.append([("➕ Новая машина", "v:new")])
    if not rows:
        await message.answer("Машин пока нет. Попросите владельца добавить машину.")
        return
    await state.set_state(Van.vehicle)
    await message.answer("Какая машина?", reply_markup=kb(rows))


async def van_start(message: Message, state: FSMContext):
    flow = {SHIP: "ship", RET: "ret", LOAD: "load", VAN: "view"}[message.text]
    await _choose_vehicle(message, state, flow, message.from_user.id)


@router.callback_query(Van.vehicle, F.data == "v:new")
async def vehicle_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Van.vehicle_name)
    await callback.message.answer("Название машины, например «Damas 01 A 123 BC»?", reply_markup=kb([]))
    await callback.answer()


@router.message(Van.vehicle_name, F.text)
async def vehicle_name(message: Message, state: FSMContext):
    vehicle = await db(logic.create_vehicle, message.text)
    await _vehicle_chosen(message, state, vehicle)


@router.callback_query(Van.vehicle, F.data.startswith("v:"))
async def vehicle_pick(callback: CallbackQuery, state: FSMContext):
    vehicle = await db(logic.get_vehicle, int(callback.data[2:]))
    if vehicle:
        await _vehicle_chosen(callback.message, state, vehicle)
    await callback.answer()


async def _vehicle_chosen(message, state, vehicle):
    data = await state.update_data(vehicle=vehicle.pk, cart={})
    if data["flow"] == "view":
        await state.clear()
        await message.answer(await db(logic.van_text, vehicle), reply_markup=kb(
            [[("Сдать остаток в цех", f"un:{vehicle.pk}")]], cancel=False))
    elif data["flow"] == "load":
        await _product_picker(message, state)
    else:
        await _shop_picker(message, state)


async def _shop_picker(message, state):
    names = await db(logic.districts)
    if not names:
        await state.clear()
        await message.answer("Магазинов пока нет. Добавьте через «Новый магазин».")
        return
    await state.update_data(districts=names)
    await state.set_state(Van.district)
    await message.answer("Район?", reply_markup=kb(column((n, f"dd:{i}") for i, n in enumerate(names))))


@router.callback_query(Van.district, F.data.startswith("dd:"))
async def van_district(callback: CallbackQuery, state: FSMContext):
    district = (await state.get_data())["districts"][int(callback.data[3:])]
    shops = await db(logic.shops_in, district)
    await state.set_state(Van.shop)
    await callback.message.answer("Магазин?", reply_markup=kb(column((s.name, f"sh:{s.pk}") for s in shops)))
    await callback.answer()


@router.callback_query(Van.shop, F.data.startswith("sh:"))
async def van_shop(callback: CallbackQuery, state: FSMContext):
    shop = await db(logic.get_shop, int(callback.data[3:]))
    await callback.answer()
    if not shop:
        return
    data = await state.update_data(shop=shop.pk)
    await callback.message.answer(await db(logic.shop_card, shop))
    if shop.latitude is not None and data["flow"] == "ship":
        await callback.message.answer_location(float(shop.latitude), float(shop.longitude))
    order = await db(logic.open_order, shop) if data["flow"] == "ship" else None
    if order:
        text = await db(logic.order_text, order)
        await state.set_state(Van.order)
        await callback.message.answer(f"У магазина есть заказ:\n{text}", reply_markup=kb(
            [[(f"Отгрузить по заказу №{order.pk}", f"o:{order.pk}")], [("Без заказа", "o:0")]]))
        return
    await _product_picker(callback.message, state)


@router.callback_query(Van.order, F.data.startswith("o:"))
async def van_order(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data[2:])
    await callback.answer()
    if not order_id:
        await _product_picker(callback.message, state)
        return
    items = await db(lambda: {i.product_id: i.qty for i in logic.m.OrderItem.objects.filter(order_id=order_id)})
    await state.update_data(order=order_id, cart={str(k): v for k, v in items.items()})
    await _show_cart(callback.message, state)


async def _product_picker(message, state):
    data = await state.get_data()
    if data["flow"] == "load":
        stock = await db(logic.workshop_stock)
        rows = [[(f"{p.name} · в цеху {n}", f"g:{p.pk}")] for p, n in stock.items()]
        title = "Что грузим в машину?"
    elif data["flow"] == "ship":
        vehicle = await db(logic.get_vehicle, data["vehicle"])
        stock = await db(vehicle.stock)
        rows = [[(f"{p.name} · в машине {n}", f"g:{p.pk}")] for p, n in stock.items() if n > 0]
        title = "Что отдаём магазину?"
        if not rows:
            await state.clear()
            await message.answer("Машина пустая. Сначала «Загрузить машину».")
            return
    else:
        rows = [[(p.name, f"g:{p.pk}")] for p in await db(logic.products)]
        title = "Что вернули?"
    await state.set_state(Van.pick)
    await message.answer(title, reply_markup=kb(rows))


@router.callback_query(Van.pick, F.data.startswith("g:"))
async def van_product(callback: CallbackQuery, state: FSMContext):
    product = await db(logic.get_product, int(callback.data[2:]))
    await callback.answer()
    if not product:
        return
    await state.update_data(product=product.pk)
    await state.set_state(Van.qty)
    await callback.message.answer(f"{product.name}: сколько штук?")


@router.message(Van.qty, F.text)
async def van_qty(message: Message, state: FSMContext):
    n = logic.parse_count(message.text)
    if n is None:
        await message.answer("Напишите количество целым числом, например 40.")
        return
    data = await state.get_data()
    if data["flow"] == "ret":
        await state.update_data(qty=n)
        await state.set_state(Van.reason)
        await message.answer("Причина возврата?", reply_markup=kb(
            column((label, f"r:{value}") for value, label in Return.Reason.choices)))
        return
    cart = _cart(data)
    cart[data["product"]] = cart.get(data["product"], 0) + n
    await state.update_data(cart={str(k): v for k, v in cart.items()})
    await _show_cart(message, state)


async def _show_cart(message, state):
    data = await state.get_data()
    cart = _cart(data)
    text = await db(logic.items_text, cart)
    if data["flow"] == "ship":
        text = await db(logic.cart_text, cart)
    await state.set_state(Van.pick)
    await message.answer(text, reply_markup=kb([[("Добавить ещё", "c:more"), ("Готово", "c:done")]]))


@router.callback_query(Van.pick, F.data == "c:more")
async def van_more(callback: CallbackQuery, state: FSMContext):
    await _product_picker(callback.message, state)
    await callback.answer()


@router.callback_query(Van.pick, F.data == "c:done")
async def van_done(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    cart = _cart(data)
    await callback.answer()
    if not cart:
        return
    if data["flow"] == "ship":
        total = await db(logic.cart_total, cart)
        await state.update_data(total=str(total))
        await state.set_state(Van.paid)
        await callback.message.answer(f"Сумма {money(total)} сум. Сколько магазин заплатил сейчас?",
                                      reply_markup=kb([[("Всё оплачено", "pay:all"), ("Всё в долг", "pay:0")]]))
        return
    me = await who(callback.from_user.id)
    vehicle = await db(logic.get_vehicle, data["vehicle"])
    try:
        await db(logic.create_van_load, vehicle, me[1] if me else None, cart)
    except ValidationError as e:
        await callback.message.answer(e.messages[0] + "\nИсправьте количество или нажмите «Отмена».",
                                      reply_markup=kb([[("Добавить ещё", "c:more"), ("Готово", "c:done")]]))
        return
    await state.clear()
    text = await db(logic.van_text, vehicle)
    await callback.message.answer("Загружено.\n" + text)
    await notify_owners(bot, callback.from_user.id,
                        f"Загрузка машины {vehicle}:\n" + await db(logic.items_text, cart))


async def _save_shipment(event, state, bot, paid):
    message = event.message if isinstance(event, CallbackQuery) else event
    data = await state.get_data()
    me = await who(event.from_user.id)
    vehicle = await db(logic.get_vehicle, data["vehicle"])
    shop = await db(logic.get_shop, data["shop"])
    try:
        shipment = await db(logic.create_van_shipment, vehicle, shop, _cart(data), paid,
                            me[1] if me else None, data.get("order"))
    except ValidationError as e:
        await state.set_state(Van.pick)
        await message.answer(e.messages[0], reply_markup=kb([[("Добавить ещё", "c:more"), ("Готово", "c:done")]]))
        return
    await state.clear()
    text = await db(logic.shipment_text, shipment)
    await message.answer("Сохранено.\n" + text)
    await notify_owners(bot, event.from_user.id, f"Отгрузка с машины {vehicle}.\n" + text)
    if shop.telegram_id:
        try:
            await bot.send_message(shop.telegram_id, "Вы получили товар. Спасибо!\n" + text)
        except Exception:
            logging.exception("Не удалось написать магазину %s", shop.pk)


@router.callback_query(Van.paid, F.data.startswith("pay:"))
async def van_paid_button(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    paid = logic.Decimal(data["total"]) if callback.data == "pay:all" else logic.Decimal(0)
    await callback.answer()
    await _save_shipment(callback, state, bot, paid)


@router.message(Van.paid, F.text)
async def van_paid(message: Message, state: FSMContext, bot: Bot):
    value = logic.parse_number(message.text, allow_zero=True)
    if value is None:
        await message.answer("Напишите сумму числом, например 200000, или 0, если всё в долг.")
        return
    await _save_shipment(message, state, bot, value)


@router.callback_query(Van.reason, F.data.startswith("r:"))
async def van_reason(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    shop = await db(logic.get_shop, data["shop"])
    item = await db(logic.create_return, shop, data["product"], data["qty"], callback.data[2:])
    await state.set_state(Van.pick)
    await state.update_data(flow="ret")
    debt = await db(shop.debt)
    text = (f"Возврат записан: {await db(lambda: item.product.name)} × {item.qty} = {money(item.total())} сум "
            f"({item.get_reason_display()}).\nДолг магазина теперь: {money(debt)} сум")
    await callback.message.answer(text, reply_markup=kb([[("Ещё возврат от этого магазина", "c:more")]]))
    await notify_owners(bot, callback.from_user.id, f"Возврат от «{shop.name}».\n{text}")
    await callback.answer()


@router.callback_query(F.data.startswith("un:"))
async def van_unload(callback: CallbackQuery, bot: Bot):
    me = await who(callback.from_user.id)
    await callback.answer()
    if not me:
        return
    vehicle = await db(logic.get_vehicle, int(callback.data[3:]))
    load = await db(logic.unload_all, vehicle, me[1])
    if not load:
        await callback.message.answer("Машина уже пустая.")
        return
    items = await db(lambda: "\n".join(f"{i.product.name} × {i.qty}" for i in load.items.select_related("product")))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Сдано в цех:\n{items}")
    await notify_owners(bot, callback.from_user.id, f"Машина {vehicle} сдала остаток в цех:\n{items}")


# ---------- Новый сотрудник (только владелец) ----------

async def worker_start(message: Message, state: FSMContext):
    if not logic.is_owner(message.from_user.id):
        return
    await state.clear()
    await state.set_state(NewWorker.name)
    await message.answer("Имя сотрудника?", reply_markup=kb([]))


@router.message(NewWorker.name, F.text)
async def worker_name(message: Message, state: FSMContext):
    worker = await db(logic.create_worker, message.text)
    await state.clear()
    await message.answer(
        f"Сотрудник «{worker.name}» добавлен. Отправьте ему эту ссылку, пусть откроет её в Telegram:\n"
        f"{logic.invite_link(worker.bot_code)}\n\n"
        "На сайте в разделе «Сотрудники» можно указать, на какой машине он обычно ездит.")
