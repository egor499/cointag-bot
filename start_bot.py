import os
import time
import asyncio
import aiosqlite
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DB_PATH = "bot.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

PLANS = {
    "1d": ("Премиум на 1 день", 2, 1),
    "7d": ("Премиум на 7 дней (скидка)", 10, 7),
    "30d": ("Премиум на 30 дней (скидка)", 49, 30),
    "365d": ("Премиум на 365 дней", 200, 365),
    "forever": ("Премиум НАВСЕГДА (УЛЬТРА СКИДКА)", 219, 0),
}

DAILY_BASE = 10
REF_BASE = 15
DAY_SEC = 86400


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                cointag INTEGER DEFAULT 0,
                referrals INTEGER DEFAULT 0,
                referred_by INTEGER DEFAULT NULL,
                last_bonus INTEGER DEFAULT 0,
                premium_until INTEGER DEFAULT 0,
                is_forever INTEGER DEFAULT 0
            )
            """
        )
        await db.commit()


async def get_or_create_user(user_id: int, ref_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            return row, False

        valid_ref = ref_id if (ref_id and ref_id != user_id) else None
        if valid_ref:
            cur_ref = await db.execute(
                "SELECT premium_until, is_forever FROM users WHERE user_id = ?",
                (valid_ref,),
            )
            ref_row = await cur_ref.fetchone()
            if ref_row:
                now = int(time.time())
                ref_is_prem = bool(ref_row[1]) or (ref_row[0] > now)
                reward = REF_BASE * 2 if ref_is_prem else REF_BASE
                await db.execute(
                    "UPDATE users SET referrals = referrals + 1, cointag = cointag + ? WHERE user_id = ?",
                    (reward, valid_ref),
                )
            else:
                valid_ref = None

        await db.execute(
            "INSERT INTO users (user_id, referred_by) VALUES (?, ?)",
            (user_id, valid_ref),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone(), True


def check_premium(row) -> tuple[bool, str]:
    now = int(time.time())
    if row[6]:
        return True, "👑 PREMIUM (Навсегда)"
    if row[5] > now:
        dt = datetime.fromtimestamp(row[5], tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        return True, f"👑 PREMIUM (до {dt})"
    return False, "Базовый"


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
                InlineKeyboardButton(text="🎁 Бонус", callback_data="bonus"),
            ],
            [
                InlineKeyboardButton(text="👥 Рефералка", callback_data="ref"),
                InlineKeyboardButton(text="🛒 Магазин", callback_data="shop"),
            ],
            [
                InlineKeyboardButton(text="👑 Премиум", callback_data="premium"),
                InlineKeyboardButton(text="⚡ VIP-раздел", callback_data="vip"),
            ],
        ]
    )


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]]
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    ref_id = None
    if command.args and command.args.isdigit():
        ref_id = int(command.args)

    _, is_new = await get_or_create_user(message.from_user.id, ref_id)
    greeting = "Добро пожаловать!" if is_new else "С возвращением!"
    await message.answer(
        f"{greeting} Выбирай нужный раздел кнопками ниже:",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "Главное меню — выбирай раздел:",
        reply_markup=main_menu(),
    )
    await callback.answer()


@dp.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    row, _ = await get_or_create_user(callback.from_user.id)
    _, status_text = check_premium(row)
    text = (
        f"👤 <b>Твой профиль</b>\n\n"
        f"🆔 ID: <code>{row[0]}</code>\n"
        f"🎖 Статус: <b>{status_text}</b>\n"
        f"🪙 Баланс: <b>{row[1]} КоинТэг</b>\n"
        f"👥 Приглашено друзей: <b>{row[2]}</b>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_menu())
    await callback.answer()


@dp.callback_query(F.data == "bonus")
async def cb_bonus(callback: CallbackQuery):
    row, _ = await get_or_create_user(callback.from_user.id)
    now = int(time.time())
    diff = now - row[4]

    if diff < DAY_SEC:
        left = DAY_SEC - diff
        hours = left // 3600
        mins = (left % 3600) // 60
        await callback.answer(
            f"Бонус уже получен! Приходи через {hours} ч. {mins} мин.",
            show_alert=True,
        )
        return

    is_prem, _ = check_premium(row)
    reward = DAILY_BASE * 2 if is_prem else DAILY_BASE

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET cointag = cointag + ?, last_bonus = ? WHERE user_id = ?",
            (reward, now, callback.from_user.id),
        )
        await db.commit()

    mult_note = " (x2 за 👑 PREMIUM)" if is_prem else ""
    await callback.answer(
        f"+{reward} КоинТэг начислено{mult_note}!",
        show_alert=True,
    )


@dp.callback_query(F.data == "ref")
async def cb_ref(callback: CallbackQuery):
    row, _ = await get_or_create_user(callback.from_user.id)
    is_prem, _ = check_premium(row)
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start={callback.from_user.id}"
    per_friend = REF_BASE * 2 if is_prem else REF_BASE

    text = (
        f"👥 <b>Реферальная система</b>\n\n"
        f"За каждого нового друга ты получаешь <b>+{per_friend} КоинТэг</b>"
        f"{' (x2 с PREMIUM)' if is_prem else ' (с PREMIUM — x2)'}.\n"
        f"Приглашено друзей: <b>{row[2]}</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_menu())
    await callback.answer()


@dp.callback_query(F.data == "shop")
async def cb_shop(callback: CallbackQuery):
    await callback.answer("🛒 Магазин за КоинТэг: Coming soon!", show_alert=True)


@dp.callback_query(F.data == "premium")
async def cb_premium(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1 день — 2 ⭐", callback_data="buy_1d")],
            [InlineKeyboardButton(text="7 дней — 10 ⭐ (вместо 14)", callback_data="buy_7d")],
            [InlineKeyboardButton(text="30 дней — 49 ⭐ (вместо 60)", callback_data="buy_30d")],
            [InlineKeyboardButton(text="365 дней — 200 ⭐", callback_data="buy_365d")],
            [InlineKeyboardButton(text="🔥 Навсегда — 219 ⭐ (УЛЬТРА СКИДКА)", callback_data="buy_forever")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
        ]
    )
    text = (
        "👑 <b>PREMIUM-версия бота</b>\n\n"
        "Что даёт подписка:\n"
        "• x2 КоинТэг за ежедневный бонус и друзей\n"
        "• Доступ к закрытому ⚡ VIP-разделу\n"
        "• Статус 👑 PREMIUM в профиле\n\n"
        "Выбери тариф за Telegram Stars:"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("buy_"))
async def cb_buy(callback: CallbackQuery):
    plan_key = callback.data.removeprefix("buy_")
    if plan_key not in PLANS:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    title, stars, _ = PLANS[plan_key]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=title,
        description="Активация внутреннего PREMIUM-статуса в боте",
        payload=f"prem:{plan_key}",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)],
    )
    await callback.answer()


@dp.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    if not payload.startswith("prem:"):
        return

    plan_key = payload.removeprefix("prem:")
    if plan_key not in PLANS:
        return

    title, stars, days = PLANS[plan_key]
    user_id = message.from_user.id
    row, _ = await get_or_create_user(user_id)
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        if days == 0:
            await db.execute(
                "UPDATE users SET is_forever = 1 WHERE user_id = ?",
                (user_id,),
            )
        else:
            base_time = max(row[5], now)
            new_until = base_time + days * DAY_SEC
            await db.execute(
                "UPDATE users SET premium_until = ? WHERE user_id = ?",
                (new_until, user_id),
            )
        await db.commit()

    await message.answer(
        f"✅ Оплата прошла! Активирован тариф: <b>{title}</b>.",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )
    await bot.send_message(
        ADMIN_ID,
        f"💰 Новая покупка: юзер {user_id} купил «{title}» за {stars} ⭐",
    )


@dp.callback_query(F.data == "vip")
async def cb_vip(callback: CallbackQuery):
    row, _ = await get_or_create_user(callback.from_user.id)
    is_prem, _ = check_premium(row)
    if not is_prem:
        await callback.answer(
            "🔒 Этот раздел доступен только владельцам 👑 PREMIUM!",
            show_alert=True,
        )
        return

    text = (
        "⚡ <b>Закрытый VIP-раздел</b>\n\n"
        "Добро пожаловать в приватную зону для владельцев 👑 PREMIUM!\n"
        "Здесь будут появляться закрытые бонусы, промокоды и ранний доступ к новым фишкам."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_menu())
    await callback.answer()


async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())