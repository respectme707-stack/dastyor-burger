import asyncio
import logging
import os
import re
from datetime import timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)
from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from core import botlogic as logic
from core import bot_staff
from core.backup import make_backup
from core.bot_staff import is_button, tell
from core.models import Order
from core.templatetags.fmt import money

router = Router()

ORDER, DEBT, HISTORY = gettext_noop("Сделать заказ"), gettext_noop("Мой долг"), gettext_noop("Мои заказы")
bot_staff.BUTTON_KEYS.update({ORDER, DEBT, HISTORY})


def shop_menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=_(ORDER))],
        [KeyboardButton(text=_(DEBT)), KeyboardButton(text=_(HISTORY))],
        [KeyboardButton(text=_(bot_staff.LANG))],
    ], resize_keyboard=True)


def cart_buttons():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_("Добавить ещё"), callback_data="add")],
        [InlineKeyboardButton(text=_("Отправить заказ"), callback_data="send"),
         InlineKeyboardButton(text=_("Очистить"), callback_data="clear")],
    ])


LANGUAGE_BUTTONS = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
    InlineKeyboardButton(text="🇺🇿 Oʻzbekcha", callback_data="lang:uz"),
]])


class LanguageMiddleware(BaseMiddleware):
    """Каждое сообщение и нажатие обрабатываем на языке, который человек выбрал."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        language = await sync_to_async(logic.language_of)(user.id) if user else None
        with translation.override(language or "ru"):
            return await handler(event, data)


class Ordering(StatesGroup):
    qty = State()


def _call(fn):
    return sync_to_async(fn, thread_sensitive=True)


shop_for = _call(logic.shop_for)
link_shop = _call(logic.link_shop)
link_worker = _call(logic.link_worker)
role_of = _call(logic.role_of)
boss_ids = _call(logic.boss_ids)
products = _call(logic.sale_products)
get_product = _call(logic.get_product)
cart_text = _call(logic.cart_text)
create_order = _call(logic.create_order)
order_text = _call(logic.order_text)
set_status = _call(logic.set_status)
debt_text = _call(logic.debt_text)
history_text = _call(logic.history_text)


def _cart(data):
    # Ключи в FSM хранятся строками.
    return {int(k): v for k, v in data.get("cart", {}).items()}


async def product_keyboard():
    rows = [[InlineKeyboardButton(text=_("%(name)s · %(price)s сум") % {"name": p.name, "price": money(p.price)},
                                  callback_data=f"p:{p.pk}")] for p in await products()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    code = (command.args or "").strip()
    if not await sync_to_async(logic.language_of)(message.from_user.id):
        # Новый человек: сначала язык, потом подключение по ссылке.
        await state.update_data(start_code=code)
        await message.answer("Tilni tanlang / Выберите язык:", reply_markup=LANGUAGE_BUTTONS)
        return
    await greet(message, message.from_user.id, code)


@router.message(Command("lang"))
@router.message(is_button(bot_staff.LANG))
async def language_ask(message: Message):
    await message.answer("Tilni tanlang / Выберите язык:", reply_markup=LANGUAGE_BUTTONS)


@router.callback_query(F.data.in_({"lang:ru", "lang:uz"}))
async def language_set(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    await sync_to_async(logic.set_language)(uid, callback.data[5:])
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    code = (await state.get_data()).get("start_code", "")
    await state.clear()
    with translation.override(callback.data[5:]):
        await greet(callback.message, uid, code)


async def greet(message: Message, uid, code=""):
    """Подключение по ссылке (код сотрудника или магазина) и приветствие с меню нужной роли."""
    if code.startswith("w"):
        worker = await link_worker(code, uid)
        if not worker:
            await message.answer(_("Код не подошёл. Попросите у начальника новую ссылку."))
            return
    elif code:
        shop = await link_shop(code, uid)
        if shop:
            await message.answer(_("Готово! Этот аккаунт подключён к магазину «%(name)s». Теперь можно заказывать.") % {"name": shop.name}, reply_markup=shop_menu())
        else:
            await message.answer(_("Код не подошёл. Попросите у поставщика новую ссылку."))
        return
    role, worker = await role_of(uid)
    if role:
        name = f", {worker.name}" if worker else ""
        hint = {
            "boss": _("Вам доступны все кнопки. «Сводка» — деньги и остатки, «Сотрудники» — табель каждого. "
                    "Каждый вечер пришлю итоги дня."),
            "seller": _("Ваши кнопки: отгрузка в магазин, возврат, оплата долга, ваш остаток и расходы."),
            "workshop": _("Ваши кнопки: приход сырья, производство, передача товара агентам и расходы."),
        }[role]
        await message.answer(_("Здравствуйте%(name)s! %(hint)s") % {"name": name, "hint": hint}, reply_markup=bot_staff.menu_for(role))
        return
    shop = await shop_for(uid)
    if shop:
        await message.answer(_("Здравствуйте, «%(name)s»! Что хотите заказать?") % {"name": shop.name}, reply_markup=shop_menu())
    else:
        await message.answer(_("Этот бот для магазинов-партнёров DASTYOR. "
                             "Попросите у поставщика ссылку для подключения."))


@router.message(Command("backup"))
async def backup_now(message: Message, bot: Bot):
    """Владелец пишет /backup — бот сразу присылает копию базы."""
    if not logic.is_owner(message.from_user.id):
        return
    name, data = await sync_to_async(make_backup)()
    await bot.send_document(message.chat.id, BufferedInputFile(data, filename=name),
                            caption=backup_caption())


@router.message(is_button(ORDER))
async def order_start(message: Message, state: FSMContext):
    if not await shop_for(message.from_user.id):
        return
    await state.set_state(None)
    await message.answer(_("Выберите продукт:"), reply_markup=await product_keyboard())


@router.callback_query(F.data == "add")
async def order_add(callback: CallbackQuery):
    await callback.message.answer(_("Выберите продукт:"), reply_markup=await product_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("p:"))
async def order_pick(callback: CallbackQuery, state: FSMContext):
    product = await get_product(int(callback.data[2:]))
    if not product:
        await callback.answer(_("Этот продукт больше не продаётся."), show_alert=True)
        return
    await state.set_state(Ordering.qty)
    await state.update_data(product=product.pk)
    await callback.message.answer(_("%(name)s: сколько штук? Напишите число.") % {"name": product.name})
    await callback.answer()


@router.message(Ordering.qty)
async def order_qty(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit() or not 0 < int(text) <= 10000:
        await message.answer(_("Напишите количество числом, например 40."))
        return
    data = await state.get_data()
    cart = _cart(data)
    cart[data["product"]] = cart.get(data["product"], 0) + int(text)
    await state.set_state(None)
    await state.update_data(cart={str(k): v for k, v in cart.items()})
    await message.answer(_("Ваш заказ:\n") + await cart_text(cart), reply_markup=cart_buttons())


@router.callback_query(F.data == "clear")
async def order_clear(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(_("Корзина очищена."))
    await callback.answer()


@router.callback_query(F.data == "send")
async def order_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    shop = await shop_for(callback.from_user.id)
    cart = _cart(await state.get_data())
    if not shop or not cart:
        await callback.answer(_("Корзина пуста."), show_alert=True)
        return
    order = await create_order(shop, cart)
    await state.clear()
    await callback.message.edit_text(_("Заказ №%(pk)s отправлен. Мы сообщим, когда его подтвердят.") % {"pk": order.pk})
    await callback.answer()

    def buttons():
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=_("Подтвердить"), callback_data=f"ok:{order.pk}"),
            InlineKeyboardButton(text=_("Отклонить"), callback_data=f"no:{order.pk}"),
        ]])

    for owner in await boss_ids():
        markup = await sync_to_async(logic.in_language)(owner, buttons)
        await tell(bot, owner, lambda: _("Новый заказ!\n") + logic.order_text(order), reply_markup=markup)


@router.callback_query(F.data.regexp(r"^(ok|no):\d+$"))
async def order_decide(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id not in await boss_ids():
        await callback.answer()
        return
    action, order_id = callback.data.split(":")
    status = Order.Status.CONFIRMED if action == "ok" else Order.Status.REJECTED
    order = await set_status(int(order_id), status)
    if not order:
        await callback.answer(_("Этот заказ уже обработан."), show_alert=True)
        return
    await callback.message.edit_text(await order_text(order))
    await callback.answer()
    if action == "ok":
        await tell(bot, order.shop.telegram_id, lambda: _("Ваш заказ №%(pk)s подтверждён.") % {"pk": order.pk})
    else:
        await tell(bot, order.shop.telegram_id,
                   lambda: _("Ваш заказ №%(pk)s отклонён, свяжитесь с нами.") % {"pk": order.pk})


@router.message(is_button(DEBT))
async def debt(message: Message):
    shop = await shop_for(message.from_user.id)
    if shop:
        await message.answer(await debt_text(shop))


@router.message(is_button(HISTORY))
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
        dp.update.outer_middleware(LanguageMiddleware())
        dp.include_router(router)
        dp.include_router(bot_staff.router)
        tasks = [asyncio.create_task(reminder_loop(bot)), asyncio.create_task(daily_report_loop(bot)),
                 asyncio.create_task(backup_loop(bot))]
        try:
            await dp.start_polling(bot)
        finally:
            for task in tasks:
                task.cancel()


async def reminder_loop(bot):
    """Каждый день в REMINDER_HOUR (по умолчанию 9:00) напоминает о магазинах без заказа."""
    hour = int(os.environ.get("REMINDER_HOUR", "9"))
    while True:
        now = timezone.localtime()
        run_at = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now >= run_at:
            # Уже позже: отправляем сразу (повторно в тот же день магазин не попадёт).
            try:
                await bot_staff.send_reminders(bot)
            except Exception:
                logging.exception("Ошибка при отправке напоминаний")
            run_at += timedelta(days=1)
        await asyncio.sleep((run_at - timezone.localtime()).total_seconds())


async def daily_report_loop(bot):
    """Каждый вечер в DAILY_REPORT_HOUR (по умолчанию 20:00) присылает начальнику итоги дня."""
    hour = int(os.environ.get("DAILY_REPORT_HOUR", "20"))
    while True:
        now = timezone.localtime()
        run_at = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now >= run_at:
            run_at += timedelta(days=1)
        await asyncio.sleep((run_at - timezone.localtime()).total_seconds())
        try:
            await bot_staff.send_daily_report(bot)
        except Exception:
            logging.exception("Ошибка при отправке итогов дня")


async def backup_loop(bot):
    """Каждую ночь в BACKUP_HOUR (по умолчанию 3:00) присылает владельцам файл с копией базы.

    Кому: OWNER_TELEGRAM_IDS и, если задано, BACKUP_TELEGRAM_IDS (через запятую).
    """
    hour = int(os.environ.get("BACKUP_HOUR", "3"))
    while True:
        now = timezone.localtime()
        run_at = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now >= run_at:
            run_at += timedelta(days=1)
        await asyncio.sleep((run_at - timezone.localtime()).total_seconds())
        try:
            await send_backup(bot)
        except Exception:
            logging.exception("Ошибка при отправке резервной копии")


async def send_backup(bot):
    name, data = await sync_to_async(make_backup)()
    extra = {int(x) for x in re.findall(r"\d+", os.environ.get("BACKUP_TELEGRAM_IDS", ""))}
    for chat_id in logic.owner_ids() | extra:
        try:
            caption = await sync_to_async(logic.in_language)(chat_id, backup_caption)
            await bot.send_document(chat_id, BufferedInputFile(data, filename=name), caption=caption)
        except Exception:
            logging.exception("Не удалось отправить копию %s", chat_id)


def backup_caption():
    return _("Резервная копия базы DASTYOR. Сохраните этот файл.")
