"""Кнопки бота для сотрудников. У каждой роли своё меню: начальник, агент, цех."""
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command as CommandFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from asgiref.sync import sync_to_async
from django.core.exceptions import ValidationError
from django.utils import timezone

from . import botlogic as logic
from .models import Expense, Payment, RawMaterial, Return, Shipment, Worker
from .templatetags.fmt import money

router = Router()

SHIP, RET, PAY, MYSTOCK = "Отгрузка в магазин", "Возврат", "Оплата долга", "Мой остаток"
INCOME, MAKE, GIVE, STOCK = "Приход сырья", "Производство", "Передача агенту", "Остаток в цеху"
EXPENSE, NEWSHOP, WORKERS, SUMMARY = "Расход", "Новый магазин", "Сотрудники", "Сводка"
SKIP = "Пропустить"

MENUS = {
    "boss": [[SHIP, RET], [GIVE, MAKE], [INCOME, EXPENSE], [PAY, NEWSHOP], [STOCK, WORKERS], [SUMMARY]],
    "seller": [[SHIP, RET], [MYSTOCK, PAY], [EXPENSE, NEWSHOP]],
    "workshop": [[INCOME, MAKE], [GIVE, STOCK], [EXPENSE]],
}
ALLOWED = {role: {b for row in rows for b in row} for role, rows in MENUS.items()}
ALL_BUTTONS = set().union(*ALLOWED.values())


def menu_for(role):
    rows = MENUS.get(role, [])
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
                               resize_keyboard=True)


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
    category = State()
    pick = State()
    material_name = State()
    material_unit = State()
    unit_name = State()
    qty = State()
    price = State()
    paid = State()


class Goods(StatesGroup):
    """Отгрузка, возврат, передача агенту, оплата долга: магазин/агент → товар → количество."""
    agent = State()
    district = State()
    shop = State()
    order = State()
    pick = State()
    qty = State()
    reason = State()
    method = State()
    paid = State()
    amount = State()
    pay_method = State()


class Make(StatesGroup):
    product = State()
    qty = State()


class Spend(StatesGroup):
    category = State()
    amount = State()
    note = State()


class NewWorker(StatesGroup):
    name = State()
    role = State()


db = sync_to_async(lambda fn, *a, **kw: fn(*a, **kw), thread_sensitive=True)


async def who(uid):
    """(роль, сотрудник). Роль None — не наш человек."""
    return await db(logic.role_of, uid)


async def notify_bosses(bot: Bot, uid, text):
    for boss in await db(logic.boss_ids) - {uid}:
        try:
            await bot.send_message(boss, text)
        except Exception:
            logging.exception("Не удалось написать начальнику %s", boss)


async def tell(bot: Bot, telegram_id, text):
    if not telegram_id:
        return
    try:
        await bot.send_message(telegram_id, text)
    except Exception:
        logging.exception("Не удалось написать %s", telegram_id)


def _cart(data):
    return {int(k): v for k, v in data.get("cart", {}).items()}


def _msg(event):
    return event.message if isinstance(event, CallbackQuery) else event


async def _done(event):
    if isinstance(event, CallbackQuery):
        await event.answer()


# ---------- Общие ----------

@router.message(CommandFilter("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    role, _ = await who(message.from_user.id)
    if role:
        await message.answer("Отменено.", reply_markup=menu_for(role))


@router.message(F.text.in_(ALL_BUTTONS))
async def menu(message: Message, state: FSMContext):
    """Кнопки меню срабатывают на любом шаге и начинают новое действие."""
    role, worker = await who(message.from_user.id)
    if not role:
        return
    if message.text not in ALLOWED[role]:
        await message.answer("Эта кнопка не для вашей роли.", reply_markup=menu_for(role))
        return
    await state.clear()
    handler = {
        SHIP: goods_start, RET: goods_start, PAY: goods_start, GIVE: goods_start,
        INCOME: income_start, MAKE: make_start, EXPENSE: spend_start, NEWSHOP: shop_start,
        MYSTOCK: my_stock, STOCK: stock_view, WORKERS: workers_list, SUMMARY: summary,
    }[message.text]
    await handler(message, state, role, worker)


@router.callback_query(F.data == "x")
async def cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


# ---------- Новый магазин ----------

async def shop_start(message, state, role, worker):
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
    await state.update_data(firm="" if isinstance(event, CallbackQuery) else event.text.strip())
    _, worker = await who(event.from_user.id)
    names = await db(logic.districts, worker)
    await state.update_data(districts=names)
    await state.set_state(NewShop.district)
    rows = [[(name, f"d:{i}")] for i, name in enumerate(names)]
    text = "Район? Выберите или напишите новый." if names else "Район? Напишите название."
    await _msg(event).answer(text, reply_markup=kb(rows))
    await _done(event)


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
    await state.update_data(address="" if isinstance(event, CallbackQuery) else event.text.strip())
    await state.set_state(NewShop.location)
    keyboard = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📍 Отправить локацию", request_location=True)],
        [KeyboardButton(text=SKIP)],
    ], resize_keyboard=True, one_time_keyboard=True)
    await _msg(event).answer(
        "Локация магазина.\n"
        "• С телефона: нажмите «📍 Отправить локацию» (если вы в магазине) или 📎 → Геопозиция.\n"
        "• С компьютера: найдите магазин в Google Картах, нажмите «Поделиться», скопируйте ссылку "
        "и вставьте сюда.\n"
        "• Или нажмите «Пропустить».", reply_markup=keyboard)
    await _done(event)


@router.message(NewShop.location)
async def shop_location(message: Message, state: FSMContext):
    found = None
    if message.location:
        found = (message.location.latitude, message.location.longitude)
    elif message.text and message.text != SKIP:
        found = await db(logic.resolve_location, message.text)
    if found:
        await state.update_data(lat=round(found[0], 6), lon=round(found[1], 6))
        note = "Локацию записал."
    elif message.text == SKIP:
        note = "Без локации. Её можно добавить потом на сайте."
    else:
        note = "В этом сообщении не нашёл координат, сохраняю без локации. Добавить можно потом на сайте."
    role, _ = await who(message.from_user.id)
    await state.set_state(NewShop.phone)
    await message.answer(note, reply_markup=menu_for(role))
    await message.answer("Телефон магазина?", reply_markup=kb([[(SKIP, "skip")]]))


@router.message(NewShop.phone, F.text)
@router.callback_query(NewShop.phone, F.data == "skip")
async def shop_phone(event, state: FSMContext, bot: Bot):
    data = await state.get_data()
    data["phone"] = "" if isinstance(event, CallbackQuery) else event.text.strip()
    _, worker = await who(event.from_user.id)
    shop = await db(logic.create_shop, data, worker)
    await state.clear()
    card = await db(logic.shop_card, shop)
    link = logic.invite_link(shop.bot_code)
    await _msg(event).answer(f"Готово!\n{card}\n\nСсылка для магазина, отправьте её им:\n{link}")
    await notify_bosses(bot, event.from_user.id, f"Добавлен магазин ({worker or 'начальник'}).\n{card}")
    await _done(event)


# ---------- Приход сырья ----------

async def income_start(message, state, role, worker):
    rows = [[(s.name, f"s:{s.pk}")] for s in await db(logic.suppliers)]
    rows.append([("➕ Новый поставщик", "s:new")])
    await state.set_state(Income.supplier)
    await message.answer("Приход сырья. От какого поставщика?", reply_markup=kb(rows))


@router.callback_query(Income.supplier, F.data == "s:new")
async def income_supplier_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Income.supplier_name)
    await callback.message.answer("Название поставщика (фирмы)?", reply_markup=kb([]))
    await callback.answer()


async def _category_picker(message, state):
    cats = await db(logic.categories)
    if not cats:
        await _material_picker(message, state, 0)
        return
    rows = column((c.name, f"cat:{c.pk}") for c in cats)
    rows.append([("Без категории", "cat:0")])
    await state.set_state(Income.category)
    await message.answer("Какая категория?", reply_markup=kb(rows))


async def _material_picker(message, state, category_id):
    await state.update_data(category=category_id)
    rows = [[(f"{x.name} ({x.unit})", f"m:{x.pk}")]
            for x in await db(logic.materials, category_id or None)]
    rows.append([("➕ Новое сырьё", "m:new")])
    await state.set_state(Income.pick)
    await message.answer("Какое сырьё пришло?", reply_markup=kb(rows))


@router.message(Income.supplier_name, F.text)
async def income_supplier_name(message: Message, state: FSMContext):
    supplier = await db(logic.get_or_create_supplier, message.text)
    await state.update_data(supplier=supplier.pk, lines=[])
    await _category_picker(message, state)


@router.callback_query(Income.supplier, F.data.startswith("s:"))
async def income_supplier(callback: CallbackQuery, state: FSMContext):
    await state.update_data(supplier=int(callback.data[2:]), lines=[])
    await _category_picker(callback.message, state)
    await callback.answer()


@router.callback_query(Income.category, F.data.startswith("cat:"))
async def income_category(callback: CallbackQuery, state: FSMContext):
    await _material_picker(callback.message, state, int(callback.data[4:]))
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
    units = await db(logic.units)
    rows = [[(u.name, f"u:{u.pk}") for u in units[i:i + 4]] for i in range(0, len(units), 4)]
    rows.append([("➕ Другая единица", "u:new")])
    await message.answer("В чём считаем?", reply_markup=kb(rows))


async def _ask_qty(message, state, material):
    unit = material.pack_word() if material.pack_size else str(material.unit)
    await state.update_data(material=material.pk, unit=unit)
    await state.set_state(Income.qty)
    await message.answer(f"{material.name} ({material.pack_label()}): сколько {unit}? Напишите число.")


async def _new_material(message, state, unit_name):
    data = await state.get_data()
    material = await db(logic.get_or_create_material, data["material_name"], unit_name,
                        data.get("category") or None)
    await _ask_qty(message, state, material)


@router.callback_query(Income.material_unit, F.data == "u:new")
async def income_unit_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Income.unit_name)
    await callback.message.answer("Напишите единицу, например «гр», «пачка», «ящик».", reply_markup=kb([]))
    await callback.answer()


@router.message(Income.unit_name, F.text)
async def income_unit_name(message: Message, state: FSMContext):
    await _new_material(message, state, message.text)


@router.callback_query(Income.material_unit, F.data.startswith("u:"))
async def income_material_unit(callback: CallbackQuery, state: FSMContext):
    unit = await db(lambda pk: logic.m.Unit.objects.filter(pk=pk).first(), int(callback.data[2:]))
    await callback.answer()
    if unit:
        await _new_material(callback.message, state, unit.name)


@router.callback_query(Income.pick, F.data.startswith("m:"))
async def income_material(callback: CallbackQuery, state: FSMContext):
    material = await db(lambda pk: RawMaterial.objects.select_related("unit").filter(pk=pk).first(),
                        int(callback.data[2:]))
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


@router.message(Income.price, F.text)
async def income_price(message: Message, state: FSMContext):
    value = logic.parse_number(message.text)
    if value is None:
        await message.answer("Напишите цену числом, например 3500.")
        return
    data = await state.get_data()
    lines = data["lines"] + [[data["material"], data["qty"], str(value)]]
    await state.update_data(lines=lines)
    text, _ = await db(logic.purchase_lines_text, lines)
    await state.set_state(Income.pick)
    await message.answer("Приход:\n" + text, reply_markup=kb([[("Добавить ещё", "i:more"), ("Готово", "i:done")]]))


@router.callback_query(Income.pick, F.data == "i:more")
async def income_more(callback: CallbackQuery, state: FSMContext):
    await _category_picker(callback.message, state)
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
    data = await state.get_data()
    _, worker = await who(event.from_user.id)
    note = f"Внёс в боте: {worker}" if worker else "Внесено в боте"
    purchase = await db(logic.create_purchase, data["supplier"], data["lines"], paid, note)
    await state.clear()
    text = await db(logic.purchase_text, purchase)
    await _msg(event).answer("Сохранено.\n" + text)
    await notify_bosses(bot, event.from_user.id, f"Новый приход сырья ({worker or 'начальник'}).\n" + text)


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


# ---------- Производство ----------

NO_PRODUCTS = "Продуктов пока нет. Добавьте их на сайте: «Продукты» → «Добавить», с ценой и рецептом."


async def make_start(message, state, role, worker):
    products = await db(logic.products)
    if not products:
        await message.answer(NO_PRODUCTS)
        return
    await state.set_state(Make.product)
    await message.answer("Что сделали?", reply_markup=kb(column((p.name, f"pr:{p.pk}") for p in products)))


@router.callback_query(Make.product, F.data.startswith("pr:"))
async def make_product(callback: CallbackQuery, state: FSMContext):
    product = await db(logic.get_product, int(callback.data[3:]))
    await callback.answer()
    if not product:
        return
    await state.update_data(product=product.pk)
    await state.set_state(Make.qty)
    await callback.message.answer(f"{product.name}: сколько штук сделали?")


@router.message(Make.qty, F.text)
async def make_qty(message: Message, state: FSMContext, bot: Bot):
    n = logic.parse_count(message.text)
    if n is None:
        await message.answer("Напишите количество целым числом, например 200.")
        return
    data = await state.get_data()
    _, worker = await who(message.from_user.id)
    await state.clear()
    try:
        production = await db(logic.create_production, data["product"], n, worker)
    except ValidationError as e:
        await message.answer(e.messages[0] + "\nРецепт и сырьё можно поправить на сайте.")
        return
    text = await db(logic.production_text, production)
    await message.answer(text)
    await notify_bosses(bot, message.from_user.id, f"Производство ({worker or 'начальник'}).\n{text}")


# ---------- Отгрузка, возврат, передача агенту, оплата долга ----------

FLOWS = {SHIP: "ship", RET: "ret", PAY: "pay", GIVE: "give"}


async def goods_start(message, state, role, worker):
    flow = FLOWS[message.text]
    seller = worker if role == "seller" else None
    await state.update_data(flow=flow, agent=seller.pk if seller else None, cart={})
    if flow == "give":
        agents = await db(logic.sellers)
        if not agents:
            await state.clear()
            await message.answer("Агентов пока нет. Добавьте их на сайте или через «Сотрудники».")
            return
        await state.set_state(Goods.agent)
        await message.answer("Кому передаём товар?", reply_markup=kb(column(
            (f"{a.name} · {a.get_role_display()}", f"ag:{a.pk}") for a in agents)))
        return
    if flow == "ship" and not await db(logic.products):
        await state.clear()
        await message.answer(NO_PRODUCTS)
        return
    await _shop_picker(message, state, seller)


@router.callback_query(Goods.agent, F.data.startswith("ag:"))
async def goods_agent(callback: CallbackQuery, state: FSMContext):
    await state.update_data(agent=int(callback.data[3:]))
    await _product_picker(callback.message, state)
    await callback.answer()


async def _shop_picker(message, state, seller):
    names = await db(logic.districts, seller)
    if not names:
        await state.clear()
        await message.answer("Магазинов пока нет. Добавьте через «Новый магазин».")
        return
    await state.update_data(districts=names)
    if len(names) == 1:
        await _shops_of(message, state, names[0])
        return
    await state.set_state(Goods.district)
    await message.answer("Район?", reply_markup=kb(column((n, f"dd:{i}") for i, n in enumerate(names))))


async def _shops_of(message, state, district):
    data = await state.get_data()
    seller = await db(logic.get_worker, data["agent"]) if data.get("agent") else None
    shops = await db(logic.shops_in, district, seller)
    if data["flow"] == "pay":
        debts = await db(lambda: {s.pk: s.debt() for s in shops})
        rows = column((f"{s.name} · долг {money(debts[s.pk])}", f"sh:{s.pk}") for s in shops)
    else:
        rows = column((s.name, f"sh:{s.pk}") for s in shops)
    await state.set_state(Goods.shop)
    await message.answer(f"Магазин ({district})?", reply_markup=kb(rows))


@router.callback_query(Goods.district, F.data.startswith("dd:"))
async def goods_district(callback: CallbackQuery, state: FSMContext):
    district = (await state.get_data())["districts"][int(callback.data[3:])]
    await _shops_of(callback.message, state, district)
    await callback.answer()


@router.callback_query(Goods.shop, F.data.startswith("sh:"))
async def goods_shop(callback: CallbackQuery, state: FSMContext):
    shop = await db(logic.get_shop, int(callback.data[3:]))
    await callback.answer()
    if not shop:
        return
    data = await state.update_data(shop=shop.pk)
    await callback.message.answer(await db(logic.shop_card, shop))
    if data["flow"] == "pay":
        debt = await db(shop.debt)
        await state.set_state(Goods.amount)
        rows = [[(f"Весь долг {money(debt)}", "debt:all")]] if debt > 0 else []
        await callback.message.answer("Сколько магазин заплатил? Напишите сумму.", reply_markup=kb(rows))
        return
    if data["flow"] == "ship":
        if shop.latitude is not None:
            await callback.message.answer_location(float(shop.latitude), float(shop.longitude))
        order = await db(logic.open_order, shop)
        if order:
            text = await db(logic.order_text, order)
            await state.set_state(Goods.order)
            await callback.message.answer(f"У магазина есть заказ:\n{text}", reply_markup=kb(
                [[(f"Отгрузить по заказу №{order.pk}", f"o:{order.pk}")], [("Без заказа", "o:0")]]))
            return
    await _product_picker(callback.message, state)


@router.callback_query(Goods.order, F.data.startswith("o:"))
async def goods_order(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data[2:])
    await callback.answer()
    if not order_id:
        await _product_picker(callback.message, state)
        return
    items = await db(lambda: {i.product_id: i.qty for i in logic.m.OrderItem.objects.filter(order_id=order_id)})
    await state.update_data(order=order_id, cart={str(k): v for k, v in items.items()})
    await _show_cart(callback.message, state)


async def _stock_for(data):
    """Откуда берём товар: у агента (его отгрузка) или в цеху (передача агенту, отгрузка начальника)."""
    if data["flow"] == "ship" and data.get("agent"):
        agent = await db(logic.get_worker, data["agent"])
        return await db(agent.stock), "у вас"
    return await db(logic.workshop_stock), "в цеху"


async def _product_picker(message, state):
    data = await state.get_data()
    if data["flow"] == "ret":
        rows = [[(p.name, f"g:{p.pk}")] for p in await db(logic.sale_products)]
        title = "Что вернули?"
    else:
        stock, where = await _stock_for(data)
        rows = [[(f"{p.name} · {where} {n}", f"g:{p.pk}")] for p, n in stock.items() if n > 0]
        title = "Что отдаём магазину?" if data["flow"] == "ship" else "Что передаём агенту?"
        if not rows:
            await state.clear()
            empty = ("У вас нет товара. Попросите цех сделать «Передача агенту»."
                     if where == "у вас" else "В цеху нет готового товара. Сначала «Производство».")
            await message.answer(empty)
            return
    await state.set_state(Goods.pick)
    await message.answer(title, reply_markup=kb(rows))


@router.callback_query(Goods.pick, F.data.startswith("g:"))
async def goods_product(callback: CallbackQuery, state: FSMContext):
    product = await db(logic.get_product, int(callback.data[2:]))
    await callback.answer()
    if not product:
        return
    await state.update_data(product=product.pk)
    await state.set_state(Goods.qty)
    await callback.message.answer(f"{product.name}: сколько штук?")


@router.message(Goods.qty, F.text)
async def goods_qty(message: Message, state: FSMContext):
    n = logic.parse_count(message.text)
    if n is None:
        await message.answer("Напишите количество целым числом, например 40.")
        return
    data = await state.get_data()
    if data["flow"] == "ret":
        await state.update_data(qty=n)
        await state.set_state(Goods.reason)
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
    text = await db(logic.cart_text if data["flow"] == "ship" else logic.items_text, cart)
    await state.set_state(Goods.pick)
    await message.answer(text, reply_markup=kb([[("Добавить ещё", "c:more"), ("Готово", "c:done")]]))


@router.callback_query(Goods.pick, F.data == "c:more")
async def goods_more(callback: CallbackQuery, state: FSMContext):
    await _product_picker(callback.message, state)
    await callback.answer()


@router.callback_query(Goods.pick, F.data == "c:done")
async def goods_done(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    cart = _cart(data)
    await callback.answer()
    if not cart:
        return
    if data["flow"] == "ship":
        total = await db(logic.cart_total, cart)
        await state.update_data(total=str(total))
        await state.set_state(Goods.method)
        await callback.message.answer(f"Сумма {money(total)} сум. Как магазин платит?", reply_markup=kb(
            [[(label, f"pm:{value}") for value, label in Shipment.Method.choices[:2]],
             [(label, f"pm:{value}") for value, label in Shipment.Method.choices[2:]]]))
        return
    _, me = await who(callback.from_user.id)
    agent = await db(logic.get_worker, data["agent"])
    try:
        await db(logic.create_handover, agent, me, cart)
    except ValidationError as e:
        await callback.message.answer(e.messages[0] + "\nИсправьте количество или нажмите «Отмена».",
                                      reply_markup=kb([[("Добавить ещё", "c:more"), ("Готово", "c:done")]]))
        return
    await state.clear()
    items = await db(logic.items_text, cart)
    await callback.message.answer(f"Передано агенту {agent.name}:\n{items}")
    await tell(bot, agent.telegram_id, f"Вам передали товар из цеха ({me or 'начальник'}):\n{items}\n\n"
               + await db(logic.agent_stock_text, agent))
    await notify_bosses(bot, callback.from_user.id, f"Передача агенту {agent.name} ({me or 'начальник'}):\n{items}")


async def _save_shipment(event, state, bot, paid):
    message = _msg(event)
    data = await state.get_data()
    agent = await db(logic.get_worker, data["agent"]) if data.get("agent") else None
    shop = await db(logic.get_shop, data["shop"])
    try:
        shipment = await db(logic.create_shipment, shop, _cart(data), paid, agent, data.get("order"), "",
                            data.get("method", Shipment.Method.CASH))
    except ValidationError as e:
        await state.set_state(Goods.pick)
        await message.answer(e.messages[0], reply_markup=kb([[("Добавить ещё", "c:more"), ("Готово", "c:done")]]))
        return
    await state.clear()
    text = await db(logic.shipment_text, shipment)
    full = text + ("\n\n" + await db(logic.agent_stock_text, agent) if agent else "")
    await message.answer("Сохранено.\n" + full)
    await notify_bosses(bot, event.from_user.id, f"Отгрузка ({agent or 'из цеха'}).\n" + text)
    await tell(bot, shop.telegram_id, "Вы получили товар. Спасибо!\n" + text)


@router.callback_query(Goods.method, F.data.startswith("pm:"))
async def goods_method(callback: CallbackQuery, state: FSMContext, bot: Bot):
    method = callback.data[3:]
    data = await state.update_data(method=method)
    await callback.answer()
    if method in (Shipment.Method.CONSIGNMENT, Shipment.Method.DEBT):
        await _save_shipment(callback, state, bot, logic.Decimal(0))
        return
    await state.set_state(Goods.paid)
    await callback.message.answer("Сколько заплатили сейчас? Если часть, напишите сумму, остальное уйдёт в долг.",
                                  reply_markup=kb([[(f"Всё, {money(logic.Decimal(data['total']))}", "pay:all")]]))


@router.callback_query(Goods.paid, F.data.startswith("pay:"))
async def goods_paid_button(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    paid = logic.Decimal(data["total"]) if callback.data == "pay:all" else logic.Decimal(0)
    await callback.answer()
    await _save_shipment(callback, state, bot, paid)


@router.message(Goods.paid, F.text)
async def goods_paid(message: Message, state: FSMContext, bot: Bot):
    value = logic.parse_number(message.text, allow_zero=True)
    if value is None:
        await message.answer("Напишите сумму числом, например 200000, или 0, если всё в долг.")
        return
    await _save_shipment(message, state, bot, value)


@router.callback_query(Goods.reason, F.data.startswith("r:"))
async def goods_reason(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    shop = await db(logic.get_shop, data["shop"])
    _, me = await who(callback.from_user.id)
    item = await db(logic.create_return, shop, data["product"], data["qty"], callback.data[2:], me)
    await state.set_state(Goods.pick)
    debt = await db(shop.debt)
    text = (f"Возврат записан: {await db(lambda: item.product.name)} × {item.qty} = {money(item.total())} сум "
            f"({item.get_reason_display()}).\nДолг магазина теперь: {money(debt)} сум")
    await callback.message.answer(text, reply_markup=kb([[("Ещё возврат от этого магазина", "c:more")]]))
    await notify_bosses(bot, callback.from_user.id, f"Возврат от «{shop.name}» ({me or 'начальник'}).\n{text}")
    await callback.answer()


async def _ask_pay_method(message, state, amount):
    await state.update_data(amount=str(amount))
    await state.set_state(Goods.pay_method)
    await message.answer(f"{money(amount)} сум. Как заплатили?", reply_markup=kb(
        [[(label, f"pym:{value}") for value, label in Payment.Method.choices]]))


@router.callback_query(Goods.pay_method, F.data.startswith("pym:"))
async def goods_pay_method(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    shop = await db(logic.get_shop, data["shop"])
    amount = logic.Decimal(data["amount"])
    _, me = await who(callback.from_user.id)
    await callback.answer()
    payment = await db(logic.create_payment, shop, amount, me, callback.data[4:])
    await state.clear()
    text = (f"Оплата от «{shop.name}»: {money(amount)} сум ({payment.get_method_display().lower()}). "
            f"Долг теперь: {money(await db(shop.debt))} сум")
    await callback.message.answer("Сохранено.\n" + text)
    await notify_bosses(bot, callback.from_user.id, f"{text} (принял {me or 'начальник'})")
    await tell(bot, shop.telegram_id, f"Получили вашу оплату {money(amount)} сум. Спасибо!\n"
               + await db(logic.debt_text, shop))


@router.callback_query(Goods.amount, F.data == "debt:all")
async def goods_pay_all(callback: CallbackQuery, state: FSMContext):
    shop = await db(logic.get_shop, (await state.get_data())["shop"])
    await callback.answer()
    await _ask_pay_method(callback.message, state, await db(shop.debt))


@router.message(Goods.amount, F.text)
async def goods_pay(message: Message, state: FSMContext):
    value = logic.parse_number(message.text)
    if value is None:
        await message.answer("Напишите сумму числом, например 150000.")
        return
    await _ask_pay_method(message, state, value)


# ---------- Остатки ----------

async def my_stock(message, state, role, worker):
    await message.answer(await db(logic.agent_stock_text, worker), reply_markup=kb(
        [[("Сдать остаток в цех", f"un:{worker.pk}")]], cancel=False))


async def stock_view(message, state, role, worker):
    stock = await db(logic.workshop_stock)
    lines = ["В цеху готового товара:"] + [f"{p.name}: {n} шт" for p, n in stock.items()]
    if not stock:
        lines.append(NO_PRODUCTS)
    for agent in await db(logic.sellers):
        have = await db(agent.stock)
        if have:
            lines.append(f"\nУ {agent.name}: " + ", ".join(f"{p.name} {n}" for p, n in have.items()))
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("un:"))
async def unload(callback: CallbackQuery, bot: Bot):
    role, me = await who(callback.from_user.id)
    await callback.answer()
    agent = await db(logic.get_worker, int(callback.data[3:]))
    if not agent or not (role == "boss" or me == agent):
        return
    load = await db(logic.unload_all, agent, me)
    if not load:
        await callback.message.answer("У вас уже ничего нет на руках.")
        return
    items = await db(lambda: "\n".join(f"{i.product.name} × {i.qty}" for i in load.items.select_related("product")))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Сдано в цех:\n{items}")
    await notify_bosses(bot, callback.from_user.id, f"{agent.name} сдал остаток в цех:\n{items}")


# ---------- Расход ----------

BOSS_ONLY_EXPENSES = {Expense.Category.SALARY, Expense.Category.RENT, Expense.Category.UTILITIES}


async def spend_start(message, state, role, worker):
    await state.set_state(Spend.category)
    await message.answer("Какой расход?", reply_markup=kb(
        [[(label, f"ex:{value}")] for value, label in Expense.Category.choices
         if role == "boss" or value not in BOSS_ONLY_EXPENSES]))


@router.callback_query(Spend.category, F.data.startswith("ex:"))
async def spend_category(callback: CallbackQuery, state: FSMContext):
    await state.update_data(category=callback.data[3:])
    await state.set_state(Spend.amount)
    await callback.message.answer("Сколько, сум?")
    await callback.answer()


@router.message(Spend.amount, F.text)
async def spend_amount(message: Message, state: FSMContext):
    value = logic.parse_number(message.text)
    if value is None:
        await message.answer("Напишите сумму числом, например 30000.")
        return
    await state.update_data(amount=str(value))
    await state.set_state(Spend.note)
    await message.answer("Комментарий? Например «такси до Чиланзара».", reply_markup=kb([[(SKIP, "skip")]]))


@router.message(Spend.note, F.text)
@router.callback_query(Spend.note, F.data == "skip")
async def spend_note(event, state: FSMContext, bot: Bot):
    data = await state.get_data()
    note = "" if isinstance(event, CallbackQuery) else event.text.strip()
    _, me = await who(event.from_user.id)
    expense = await db(logic.create_expense, data["category"], logic.Decimal(data["amount"]), note, me)
    await state.clear()
    text = f"Расход: {expense.get_category_display()} {money(expense.amount)} сум" + (f" ({note})" if note else "")
    await _msg(event).answer("Сохранено. " + text)
    await notify_bosses(bot, event.from_user.id, f"{text} — {me or 'начальник'}")
    await _done(event)


# ---------- Сотрудники (начальник) ----------

async def workers_list(message, state, role, worker):
    people = await db(logic.workers)
    rows = column((f"{w.name} · {w.get_role_display()}", f"wk:{w.pk}") for w in people)
    rows.append([("➕ Новый сотрудник", "nw")])
    await message.answer("Сотрудники. Нажмите, чтобы увидеть табель за месяц.\n"
                         "Роли, районы, магазины и машины меняются на сайте.",
                         reply_markup=kb(rows, cancel=False))


async def _boss(uid):
    return (await who(uid))[0] == "boss"


@router.callback_query(F.data.regexp(r"^wkp?:\d+$"))
async def worker_card(callback: CallbackQuery):
    await callback.answer()
    if not await _boss(callback.from_user.id):
        return
    prefix, pk = callback.data.split(":")
    worker = await db(logic.get_worker, int(pk))
    if not worker:
        return
    if prefix == "wkp":
        end = timezone.localdate().replace(day=1) - logic.timedelta(days=1)
        text = await db(logic.worker_card, worker, end.replace(day=1), end)
        rows = [[("Этот месяц", f"wk:{pk}")]]
    else:
        text = await db(logic.worker_card, worker)
        rows = [[("Прошлый месяц", f"wkp:{pk}")]]
    if not worker.telegram_id:
        text += f"\nСсылка для бота: {logic.invite_link(worker.bot_code)}"
    await callback.message.answer(text, reply_markup=kb(rows, cancel=False))


@router.callback_query(F.data == "nw")
async def worker_new(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not await _boss(callback.from_user.id):
        return
    await state.clear()
    await state.set_state(NewWorker.name)
    await callback.message.answer("Имя сотрудника?", reply_markup=kb([]))


@router.message(NewWorker.name, F.text)
async def worker_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(NewWorker.role)
    await message.answer("Роль?", reply_markup=kb(column((label, f"role:{value}")
                                                         for value, label in Worker.Role.choices)))


@router.callback_query(NewWorker.role, F.data.startswith("role:"))
async def worker_role(callback: CallbackQuery, state: FSMContext):
    name = (await state.get_data())["name"]
    worker = await db(logic.create_worker, name, callback.data[5:])
    await state.clear()
    extra = "\n\nНа сайте в «Сотрудники» укажите его районы, магазины и машину." if worker.is_seller() else ""
    await callback.message.answer(
        f"Добавлен: {worker.name} · {worker.get_role_display()}.\n"
        f"Отправьте ему эту ссылку, пусть откроет её в Telegram:\n{logic.invite_link(worker.bot_code)}{extra}")
    await callback.answer()


# ---------- Сводка (начальник) ----------

SUMMARY_BUTTONS = [[("Сегодня", "sum:1"), ("7 дней", "sum:7"), ("Месяц", "sum:m")]]


async def summary(message, state, role, worker):
    await message.answer(await db(logic.summary_text), reply_markup=kb(SUMMARY_BUTTONS, cancel=False))


@router.callback_query(F.data.startswith("sum:"))
async def summary_period(callback: CallbackQuery):
    await callback.answer()
    if not await _boss(callback.from_user.id):
        return
    if callback.data == "sum:m":
        text = await db(logic.summary_text)
    else:
        start, end = logic._period(int(callback.data[4:]))
        text = await db(logic.summary_text, start, end)
    await callback.message.answer(text, reply_markup=kb(SUMMARY_BUTTONS, cancel=False))


async def send_daily_report(bot: Bot):
    text = await db(logic.daily_report_text)
    for uid in await db(logic.boss_ids):
        await tell(bot, uid, text)


# ---------- Напоминания о сроке годности ----------

def reminder_buttons(shop, expired):
    if not expired:
        return None
    return kb([[("Пропустить", f"snz:{shop.pk}"), ("В архив", f"arch:{shop.pk}")]], cancel=False)


async def send_reminders(bot: Bot):
    for shop, text, expired in await db(logic.due_reminders):
        for uid in await db(logic.reminder_recipients, shop):
            try:
                await bot.send_message(uid, text, reply_markup=reminder_buttons(shop, expired))
            except Exception:
                logging.exception("Не удалось отправить напоминание %s", uid)
        await db(logic.mark_reminded, shop)


@router.callback_query(F.data.regexp(r"^(snz|arch):\d+$"))
async def reminder_action(callback: CallbackQuery):
    role, me = await who(callback.from_user.id)
    await callback.answer()
    if not role:
        return
    action, shop_id = callback.data.split(":")
    name = me.name if me else "начальник"
    if action == "arch":
        shop = await db(logic.archive_shop, int(shop_id))
        note = f"📦 В архиве ({name}). Напоминаний не будет, пока магазин не закажет или не получит товар."
    else:
        shop = await db(logic.snooze_shop, int(shop_id))
        note = f"Пропущено ({name}). До следующей поставки напоминаний не будет."
    if shop:
        await callback.message.edit_text(f"{callback.message.text}\n\n{note}")
