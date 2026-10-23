import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command as CommandFilter, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand, CommandError

from core import botlogic as logic
from core.models import Order

router = Router()

ORDER, DEBT, HISTORY, SUMMARY = "Сделать заказ", "Мой долг", "Мои заказы", "Сводка"

shop_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=ORDER)], [KeyboardButton(text=DEBT), KeyboardButton(text=HISTORY)]],
    resize_keyboard=True,
)
owner_menu = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=SUMMARY)]], resize_keyboard=True)

cart_buttons = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Добавить ещё", callback_data="add")],
    [InlineKeyboardButton(text="Отправить заказ", callback_data="send"),
     InlineKeyboardButton(text="Очистить", callback_data="clear")],
])


class Ordering(StatesGroup):
    qty = State()


def _call(fn):
    return sync_to_async(fn, thread_sensitive=True)


shop_for = _call(logic.shop_for)
link_shop = _call(logic.link_shop)
products = _call(logic.products)
get_product = _call(logic.get_product)
cart_text = _call(logic.cart_text)
create_order = _call(logic.create_order)
order_text = _call(logic.order_text)
set_status = _call(logic.set_status)
debt_text = _call(logic.debt_text)
history_text = _call(logic.history_text)
summary_text = _call(logic.summary_text)


def _cart(data):
    # Ключи в FSM хранятся строками.
    return {int(k): v for k, v in data.get("cart", {}).items()}


async def product_keyboard():
    rows = [[InlineKeyboardButton(text=f"{p.name} · {int(p.price):,} сум".replace(",", " "),
                                  callback_data=f"p:{p.pk}")] for p in await products()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    if command.args:
        shop = await link_shop(command.args.strip(), uid)
        if shop:
            await message.answer(f"Готово! Этот аккаунт подключён к магазину «{shop.name}». "
                                 "Теперь можно заказывать.", reply_markup=shop_menu)
        else:
            await message.answer("Код не подошёл. Попросите у поставщика новую ссылку.")
        return
    if logic.is_owner(uid):
        await message.answer("Здравствуйте! Сюда будут приходить новые заказы. "
                             "Кнопка «Сводка» покажет заказы, остатки и долги.", reply_markup=owner_menu)
        return
    shop = await shop_for(uid)
    if shop:
        await message.answer(f"Здравствуйте, «{shop.name}»! Что хотите заказать?", reply_markup=shop_menu)
    else:
        await message.answer("Этот бот для магазинов-партнёров Dastyor Burger. "
                             "Попросите у поставщика ссылку для подключения.")


@router.message(CommandFilter("svodka"))
@router.message(F.text == SUMMARY)
async def summary(message: Message):
    if logic.is_owner(message.from_user.id):
        await message.answer(await summary_text())


@router.message(F.text == ORDER)
async def order_start(message: Message, state: FSMContext):
    if not await shop_for(message.from_user.id):
        return
    await state.set_state(None)
    await message.answer("Выберите продукт:", reply_markup=await product_keyboard())


@router.callback_query(F.data == "add")
async def order_add(callback: CallbackQuery):
    await callback.message.answer("Выберите продукт:", reply_markup=await product_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("p:"))
async def order_pick(callback: CallbackQuery, state: FSMContext):
    product = await get_product(int(callback.data[2:]))
    if not product:
        await callback.answer("Этот продукт больше не продаётся.", show_alert=True)
        return
    await state.set_state(Ordering.qty)
    await state.update_data(product=product.pk)
    await callback.message.answer(f"{product.name}: сколько штук? Напишите число.")
    await callback.answer()


@router.message(Ordering.qty)
async def order_qty(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or not 0 < int(text) <= 10000:
        await message.answer("Напишите количество числом, например 40.")
        return
    data = await state.get_data()
    cart = _cart(data)
    cart[data["product"]] = cart.get(data["product"], 0) + int(text)
    await state.set_state(None)
    await state.update_data(cart={str(k): v for k, v in cart.items()})
    await message.answer("Ваш заказ:\n" + await cart_text(cart), reply_markup=cart_buttons)


@router.callback_query(F.data == "clear")
async def order_clear(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Корзина очищена.")
    await callback.answer()


@router.callback_query(F.data == "send")
async def order_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    shop = await shop_for(callback.from_user.id)
    cart = _cart(await state.get_data())
    if not shop or not cart:
        await callback.answer("Корзина пуста.", show_alert=True)
        return
    order = await create_order(shop, cart)
    await state.clear()
    text = await order_text(order)
    await callback.message.edit_text(f"Заказ №{order.pk} отправлен. Мы сообщим, когда его подтвердят.")
    await callback.answer()
    buttons = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Подтвердить", callback_data=f"ok:{order.pk}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"no:{order.pk}"),
    ]])
    for owner in logic.owner_ids():
        try:
            await bot.send_message(owner, "Новый заказ!\n" + text, reply_markup=buttons)
        except Exception:
            logging.exception("Не удалось отправить заказ владельцу %s", owner)


@router.callback_query(F.data.regexp(r"^(ok|no):\d+$"))
async def order_decide(callback: CallbackQuery, bot: Bot):
    if not logic.is_owner(callback.from_user.id):
        await callback.answer()
        return
    action, order_id = callback.data.split(":")
    status = Order.Status.CONFIRMED if action == "ok" else Order.Status.REJECTED
    order = await set_status(int(order_id), status)
    if not order:
        await callback.answer("Этот заказ уже обработан.", show_alert=True)
        return
    await callback.message.edit_text(await order_text(order))
    await callback.answer()
    if order.shop.telegram_id:
        verdict = "подтверждён" if action == "ok" else "отклонён, свяжитесь с нами"
        await bot.send_message(order.shop.telegram_id, f"Ваш заказ №{order.pk} {verdict}.")


@router.message(F.text == DEBT)
async def debt(message: Message):
    shop = await shop_for(message.from_user.id)
    if shop:
        await message.answer(await debt_text(shop))


@router.message(F.text == HISTORY)
async def history(message: Message):
    shop = await shop_for(message.from_user.id)
    if shop:
        await message.answer(await history_text(shop))


class Command(BaseCommand):
    help = "Запускает Telegram-бота. Нужна переменная TELEGRAM_BOT_TOKEN."

    def handle(self, *args, **options):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise CommandError("Задайте TELEGRAM_BOT_TOKEN (токен от @BotFather).")
        logging.basicConfig(level=logging.INFO)
        asyncio.run(self.run(token))

    async def run(self, token):
        bot = Bot(token)
        dp = Dispatcher()
        dp.include_router(router)
        await dp.start_polling(bot)
