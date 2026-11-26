import os
import json
import time
import asyncio
import aiosqlite
from urllib.parse import urlencode
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    WebAppInfo,
    LabeledPrice,
    PreCheckoutQuery,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DB_PATH = "bot.db"

# Ссылка на твоё веб-приложение (Mini App) магазина (обязательно https://)
# Например: "https://egor499.github.io/cointag-shop/"
SHOP_WEBAPP_URL = os.environ.get("SHOP_WEBAPP_URL", "https://example.com/cointag-shop")

# Курс: 1 звезда = 100 коинов
COINS_PER_STAR = 100

# Тарифы премиума (используются и для покупки себе, и для подарка другу за звёзды)
PREMIUM_TARIFFS = {
    "1d": {"title": "Премиум на 1 день", "stars": 15, "seconds": 86400},
    "7d": {"title": "Премиум на 7 дней", "stars": 69, "seconds": 7 * 86400},
    "30d": {"title": "Премиум на 30 дней", "stars": 149, "seconds": 30 * 86400},
    "forever": {"title": "Премиум НАВСЕГДА (УЛЬТРА СКИДКА)", "stars": 219, "seconds": None},
}

# ТГ-подарки магазина вывода:
# 1 зв = 100 коинов -> мишка (15 зв = 1500), сердечко (25 зв = 2500), колечко (100 зв = 10000)
TG_GIFTS = {
    "bear": {
        "name": "Мишка",
        "emoji": "🧸",
        "stars": 15,
        "coins": 15 * COINS_PER_STAR,
    },
    "heart": {
        "name": "Сердечко",
        "emoji": "💖",
        "stars": 25,
        "coins": 25 * COINS_PER_STAR,
    },
    "ring": {
        "name": "Колечко",
        "emoji": "💍",
        "stars": 100,
        "coins": 100 * COINS_PER_STAR,
    },
}

GIFT_COIN_AMOUNTS = [100, 500, 1500, 5000]
BONUS_COINS = 25
BONUS_COOLDOWN = 86400
REF_BONUS_COINS = 15

# Временное состояние для выбора подарка / суммы
pending_gift_state: dict[int, dict] = {}


def build_shop_webapp_url(user_id: int, coins: int) -> str:
    sep = "&" if "?" in SHOP_WEBAPP_URL else "?"
    params = urlencode(
        {
            "user_id": user_id,
            "coins": coins,
            "rate": COINS_PER_STAR,
            "items": json.dumps(
                [
                    {
                        "id": k,
                        "emoji": v["emoji"],
                        "name": v["name"],
                        "stars": v["stars"],
                        "coins": v["coins"],
                    }
                    for k, v in TG_GIFTS.items()
                ],
                ensure_ascii=False,
            ),
        }
    )
    return f"{SHOP_WEBAPP_URL}{sep}{params}"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                coins INTEGER DEFAULT 10,
                premium_until INTEGER DEFAULT 0,
                premium_forever INTEGER DEFAULT 0,
                last_bonus INTEGER DEFAULT 0,
                referrer_id INTEGER,
                referrals_count INTEGER DEFAULT 0
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS shop_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                item_key TEXT,
                coins_spent INTEGER,
                status TEXT DEFAULT 'pending',
                created_at INTEGER
            )
            """
        )
        await db.commit()


async def user_exists(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row is not None


async def ensure_user(
    user_id: int,
    username: str | None = None,
    full_name: str | None = None,
    referrer_id: int | None = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id, referrer_id FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()

        if row is None:
            valid_ref = referrer_id if (referrer_id and referrer_id != user_id) else None
            await db.execute(
                """
                INSERT INTO users (user_id, username, full_name, coins, referrer_id)
                VALUES (?, ?, ?, 10, ?)
                """,
                (user_id, username, full_name, valid_ref),
            )
            if valid_ref:
                ref_cur = await db.execute(
                    "SELECT premium_until, premium_forever FROM users WHERE user_id = ?",
                    (valid_ref,),
                )
                ref_row = await ref_cur.fetchone()
                if ref_row:
                    now_ts = int(time.time())
                    is_ref_prem = bool(ref_row[1]) or (ref_row[0] and ref_row[0] > now_ts)
                    reward = REF_BONUS_COINS * (2 if is_ref_prem else 1)
                    await db.execute(
                        """
                        UPDATE users
                        SET coins = coins + ?, referrals_count = referrals_count + 1
                        WHERE user_id = ?
                        """,
                        (reward, valid_ref),
                    )
            await db.commit()
        else:
            await db.execute(
                """
                UPDATE users
                SET username = COALESCE(?, username),
                    full_name = COALESCE(?, full_name)
                WHERE user_id = ?
                """,
                (username, full_name, user_id),
            )
            await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        cols = [col[0] for col in cursor.description]
        return dict(zip(cols, row))


def is_premium_active(user: dict) -> bool:
    if not user:
        return False
    if user.get("premium_forever"):
        return True
    now_ts = int(time.time())
    return bool(user.get("premium_until") and user["premium_until"] > now_ts)


def format_premium_status(user: dict) -> str:
    if user.get("premium_forever"):
        return "PREMIUM НАВСЕГДА 👑"
    now_ts = int(time.time())
    if user.get("premium_until") and user["premium_until"] > now_ts:
        dt = datetime.fromtimestamp(user["premium_until"], tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        return f"PREMIUM до {dt} 👑"
    return "Базовый"


async def grant_premium(target_user_id: int, tariff_key: str):
    tariff = PREMIUM_TARIFFS[tariff_key]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT premium_until, premium_forever FROM users WHERE user_id = ?",
            (target_user_id,),
        )
        row = await cur.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO users (user_id, coins) VALUES (?, 10)",
                (target_user_id,),
            )
            current_until = 0
            current_forever = 0
        else:
            current_until = row[0] or 0
            current_forever = row[1] or 0

        if tariff["seconds"] is None:
            await db.execute(
                "UPDATE users SET premium_forever = 1 WHERE user_id = ?",
                (target_user_id,),
            )
        else:
            if not current_forever:
                now_ts = int(time.time())
                base_ts = max(now_ts, current_until)
                new_until = base_ts + tariff["seconds"]
                await db.execute(
                    "UPDATE users SET premium_until = ? WHERE user_id = ?",
                    (new_until, target_user_id),
                )
        await db.commit()


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 Профиль", callback_data="menu:profile"),
                InlineKeyboardButton(text="🎁 Бонус", callback_data="menu:bonus"),
            ],
            [
                InlineKeyboardButton(text="👥 Рефералка", callback_data="menu:ref"),
                InlineKeyboardButton(text="🛒 Магазин", callback_data="menu:shop"),
            ],
            [
                InlineKeyboardButton(text="👑 Премиум", callback_data="menu:premium"),
                InlineKeyboardButton(text="⚡ VIP-раздел", callback_data="menu:vip"),
            ],
            [
                InlineKeyboardButton(text="🎁 Подарок", callback_data="menu:gift"),
            ],
        ]
    )


def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")]
        ]
    )


def premium_menu_kb(prefix: str = "buy_prem") -> InlineKeyboardMarkup:
    rows = []
    for key, t in PREMIUM_TARIFFS.items():
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{t['title']} — {t['stars']} ★",
                    callback_data=f"{prefix}:{key}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_webapp_kb(user_id: int, coins: int) -> InlineKeyboardMarkup:
    url = build_shop_webapp_url(user_id, coins)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛍 Открыть магазин подарков",
                    web_app=WebAppInfo(url=url),
                )
            ],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ]
    )


def gift_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🪙 Подарить КоинТэг", callback_data="gift_mode:coins")],
            [InlineKeyboardButton(text="👑 Подарить Премиум (★)", callback_data="gift_mode:premium")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ]
    )


def pick_contact_reply_kb(button_label: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=button_label,
                    request_users=KeyboardButtonRequestUsers(
                        request_id=101,
                        user_is_bot=False,
                        max_quantity=1,
                    ),
                )
            ],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def gift_coins_amounts_kb() -> InlineKeyboardMarkup:
    rows = []
    for amt in GIFT_COIN_AMOUNTS:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Подарить {amt} КоинТэг",
                    callback_data=f"gift_coins_amt:{amt}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_profile(target: Message | CallbackQuery):
    user_id = target.from_user.id
    await ensure_user(user_id, target.from_user.username, target.from_user.full_name)
    user = await get_user(user_id)
    text = (
        "👤 <b>Твой профиль</b>\n\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"🏅 Статус: <b>{format_premium_status(user)}</b>\n"
        f"🪙 Баланс: <b>{user['coins']} КоинТэг</b>\n"
        f"👥 Приглашено друзей: <b>{user['referrals_count']}</b>"
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=back_menu_kb(), parse_mode="HTML")
        await target.answer()
    else:
        await target.answer(text, reply_markup=back_menu_kb(), parse_mode="HTML")


async def open_shop_app(callback: CallbackQuery):
    user_id = callback.from_user.id
    await ensure_user(user_id, callback.from_user.username, callback.from_user.full_name)
    user = await get_user(user_id)
    lines = [
        "🛒 <b>Магазин ТГ-подарков</b>",
        f"Твой баланс: <b>{user['coins']} КоинТэг</b>",
        f"Курс вывода: <b>1 ★ = {COINS_PER_STAR} КоинТэг</b>\n",
        "Доступные подарки к выводу:",
    ]
    for item in TG_GIFTS.values():
        lines.append(f"• {item['emoji']} <b>{item['name']}</b> — <b>{item['coins']} КоинТэг</b> ({item['stars']} ★)")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=shop_webapp_kb(user_id, user["coins"]),
        parse_mode="HTML",
    )
    await callback.answer()


async def show_gift_menu(callback: CallbackQuery):
    await ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    user = await get_user(callback.from_user.id)
    text = (
        "🎁 <b>Раздел «Подарок»</b>\n"
        f"Твой текущий баланс: <b>{user['coins']} КоинТэг</b>\n\n"
        "Что хочешь подарить пользователю из контактов?"
    )
    await callback.message.edit_text(text, reply_markup=gift_type_kb(), parse_mode="HTML")
    await callback.answer()


async def send_coins_gift(sender_id: int, recipient_id: int, amount: int, bot: Bot) -> tuple[bool, str]:
    if recipient_id == sender_id:
        return False, "❌ Нельзя подарить КоинТэг самому себе."
    if amount <= 0:
        return False, "❌ Некорректная сумма подарка."
    if not await user_exists(recipient_id):
        return False, f"❌ Ошибка: пользователь с ID <code>{recipient_id}</code> не зарегистрирован в боте."

    sender = await get_user(sender_id)
    if not sender or sender["coins"] < amount:
        current = sender["coins"] if sender else 0
        return False, f"❌ Недостаточно КоинТэг! Нужно <b>{amount}</b>, а у тебя <b>{current}</b>."

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE users SET coins = coins - ? WHERE user_id = ? AND coins >= ?",
            (amount, sender_id, amount),
        )
        if cur.rowcount == 0:
            return False, "❌ Недостаточно КоинТэг для отправки подарка."
        await db.execute(
            "UPDATE users SET coins = coins + ? WHERE user_id = ?",
            (amount, recipient_id),
        )
        await db.commit()

    try:
        await bot.send_message(
            recipient_id,
            f"🎁 Тебе подарили <b>{amount} КоинТэг</b> от пользователя <code>{sender_id}</code>!",
            parse_mode="HTML",
        )
    except Exception:
        pass

    return True, f"🎁 Ты успешно подарил <b>{amount} КоинТэг</b> пользователю <code>{recipient_id}</code>!"


async def handle_start(message: Message, command: CommandObject, bot: Bot):
    ref_id = None
    if command.args and command.args.isdigit():
        ref_id = int(command.args)

    await ensure_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        referrer_id=ref_id,
    )

    if command.args and command.args.startswith("gift_"):
        parts = command.args.split("_")
        if len(parts) >= 4 and parts[1] == "coins" and parts[2].isdigit() and parts[3].isdigit():
            recipient_id = int(parts[2])
            amount = int(parts[3])
            ok, msg = await send_coins_gift(message.from_user.id, recipient_id, amount, bot)
            await message.answer(msg, reply_markup=main_menu_kb(), parse_mode="HTML")
            return

        if len(parts) >= 4 and parts[1] == "prem" and parts[2].isdigit():
            recipient_id = int(parts[2])
            tariff_key = "_".join(parts[3:])
            if tariff_key in PREMIUM_TARIFFS:
                t = PREMIUM_TARIFFS[tariff_key]
                await message.answer_invoice(
                    title=f"Подарок: {t['title']}",
                    description=f"Подарок «{t['title']}» для пользователя ID {recipient_id}",
                    payload=f"gift_prem:{recipient_id}:{tariff_key}",
                    currency="XTR",
                    prices=[LabeledPrice(label=t["title"], amount=t["stars"])],
                )
                return

    await message.answer(
        "Добро пожаловать! Выбирай нужный раздел кнопками ниже:",
        reply_markup=main_menu_kb(),
    )


async def handle_menu_callbacks(callback: CallbackQuery):
    action = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    await ensure_user(user_id, callback.from_user.username, callback.from_user.full_name)

    if action == "main":
        await callback.message.edit_text(
            "Добро пожаловать! Выбирай нужный раздел кнопками ниже:",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return

    if action == "profile":
        await show_profile(callback)
        return

    if action == "bonus":
        user = await get_user(user_id)
        now_ts = int(time.time())
        last_bonus = user.get("last_bonus") or 0
        diff = now_ts - last_bonus
        if diff < BONUS_COOLDOWN:
            rem = BONUS_COOLDOWN - diff
            hours = rem // 3600
            mins = (rem % 3600) // 60
            await callback.answer(
                f"Бонус уже получен! Повтор через {hours} ч {mins} мин.",
                show_alert=True,
            )
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET coins = coins + ?, last_bonus = ? WHERE user_id = ?",
                (BONUS_COINS, now_ts, user_id),
            )
            await db.commit()
        await callback.answer(f"🎁 Получено +{BONUS_COINS} КоинТэг!", show_alert=True)
        await show_profile(callback)
        return

    if action == "ref":
        user = await get_user(user_id)
        bot_info = await callback.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        text = (
            "👥 <b>Реферальная система</b>\n\n"
            f"За каждого нового друга ты получаешь <b>+{REF_BONUS_COINS} КоинТэг</b> (с PREMIUM — x2).\n"
            f"Приглашено друзей: <b>{user['referrals_count']}</b>\n\n"
            f"Твоя ссылка:\n{ref_link}"
        )
        await callback.message.edit_text(text, reply_markup=back_menu_kb(), parse_mode="HTML")
        await callback.answer()
        return

    if action == "shop":
        await open_shop_app(callback)
        return

    if action == "premium":
        user = await get_user(user_id)
        text = (
            "👑 <b>Премиум-статус</b>\n"
            f"Текущий статус: <b>{format_premium_status(user)}</b>\n\n"
            "Выбери тариф:"
        )
        await callback.message.edit_text(text, reply_markup=premium_menu_kb("buy_prem"), parse_mode="HTML")
        await callback.answer()
        return

    if action == "vip":
        user = await get_user(user_id)
        if not is_premium_active(user):
            await callback.answer("🔒 Этот раздел доступен только владельцам 👑 PREMIUM!", show_alert=True)
            return
        text = (
            "⚡ <b>VIP-раздел</b>\n\n"
            "Добро пожаловать в VIP! Здесь доступны эксклюзивные бонусы и привилегии для владельцев PREMIUM."
        )
        await callback.message.edit_text(text, reply_markup=back_menu_kb(), parse_mode="HTML")
        await callback.answer()
        return

    if action == "gift":
        await show_gift_menu(callback)
        return


async def handle_webapp_data(message: Message, bot: Bot):
    if not message.web_app_data:
        return
    try:
        payload = json.loads(message.web_app_data.data)
    except Exception:
        await message.answer("❌ Некорректные данные из приложения магазина.", reply_markup=main_menu_kb())
        return

    item_key = payload.get("item_id") or payload.get("item")
    item = TG_GIFTS.get(item_key)
    if not item:
        await message.answer("❌ Выбран неизвестный ТГ-подарок.", reply_markup=main_menu_kb())
        return

    user_id = message.from_user.id
    await ensure_user(user_id, message.from_user.username, message.from_user.full_name)
    user = await get_user(user_id)

    if user["coins"] < item["coins"]:
        await message.answer(
            f"❌ Недостаточно КоинТэг для вывода <b>{item['emoji']} {item['name']}</b>!\n"
            f"Нужно: <b>{item['coins']} КоинТэг</b> ({item['stars']} ★), у тебя: <b>{user['coins']} КоинТэг</b>.",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE users SET coins = coins - ? WHERE user_id = ? AND coins >= ?",
            (item["coins"], user_id, item["coins"]),
        )
        if cur.rowcount == 0:
            await message.answer("❌ Недостаточно КоинТэг!", reply_markup=main_menu_kb())
            return
        await db.execute(
            """
            INSERT INTO shop_orders (user_id, item_key, coins_spent, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (user_id, item_key, item["coins"], int(time.time())),
        )
        await db.commit()

    await message.answer(
        f"✅ Заявка на вывод ТГ-подарка <b>{item['emoji']} {item['name']}</b> оформлена!\n"
        f"Списано: <b>{item['coins']} КоинТэг</b> ({item['stars']} ★).",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            ADMIN_ID,
            f"🛍 Новый вывод ТГ-подарка через Mini App:\n"
            f"Пользователь: <code>{user_id}</code> (@{message.from_user.username or 'без_юзернейма'})\n"
            f"Подарок: {item['emoji']} {item['name']}\n"
            f"Списано: <b>{item['coins']} КоинТэг</b> ({item['stars']} ★)",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def handle_gift_mode(callback: CallbackQuery):
    mode = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    await ensure_user(user_id, callback.from_user.username, callback.from_user.full_name)
    user = await get_user(user_id)

    pending_gift_state[user_id] = {"mode": mode}

    if mode == "coins":
        label = "📇 Выбрать контакт для подарка КоинТэг"
        info_text = (
            f"🪙 <b>Подарок КоинТэг</b>\n"
            f"Твой баланс: <b>{user['coins']} КоинТэг</b>\n\n"
            "Нажми кнопку внизу, чтобы выбрать пользователя из контактов."
        )
    else:
        label = "📇 Выбрать контакт для подарка Премиум"
        info_text = (
            f"👑 <b>Подарок Премиум (★)</b>\n"
            f"Твой баланс: <b>{user['coins']} КоинТэг</b>\n\n"
            "Нажми кнопку внизу, чтобы выбрать пользователя из контактов."
        )

    await callback.message.answer(info_text, reply_markup=pick_contact_reply_kb(label), parse_mode="HTML")
    await callback.answer()


async def handle_users_shared(message: Message, bot: Bot):
    if not message.users_shared or not message.users_shared.users:
        return

    shared = message.users_shared.users[0]
    recipient_id = shared.user_id
    sender_id = message.from_user.id
    await ensure_user(sender_id, message.from_user.username, message.from_user.full_name)
    sender = await get_user(sender_id)

    state = pending_gift_state.get(sender_id, {"mode": "coins"})
    mode = state.get("mode", "coins")

    if mode == "coins" and not await user_exists(recipient_id):
        pending_gift_state.pop(sender_id, None)
        await message.answer(
            f"❌ Ошибка: выбранный пользователь (ID: <code>{recipient_id}</code>) не зарегистрирован в боте. "
            "Отправить КоинТэг можно только тем, кто уже заходил в бота.",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
        return

    pending_gift_state[sender_id] = {"mode": mode, "recipient_id": recipient_id}
    bot_info = await bot.get_me()

    if mode == "coins":
        gift_link = f"https://t.me/{bot_info.username}?start=gift_coins_{recipient_id}_500"
        await message.answer(
            f"📇 Выбран получатель ID: <code>{recipient_id}</code>\n"
            f"Твой баланс: <b>{sender['coins']} КоинТэг</b>\n\n"
            "Выбери сумму подарка кнопкой ниже:",
            reply_markup=gift_coins_amounts_kb(),
            parse_mode="HTML",
        )
        await message.answer(
            f"Также при необходимости можно отправить ссылку получателю:\n<code>{gift_link}</code>",
            reply_markup=back_menu_kb(),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"📇 Выбран получатель ID: <code>{recipient_id}</code>\n"
            f"Твой баланс: <b>{sender['coins']} КоинТэг</b>\n\n"
            "Выбери тариф Премиум для подарка за звёзды:",
            reply_markup=premium_menu_kb("gift_prem_select"),
            parse_mode="HTML",
        )


async def handle_gift_coins_amount(callback: CallbackQuery, bot: Bot):
    amount = int(callback.data.split(":", 1)[1])
    sender_id = callback.from_user.id
    state = pending_gift_state.get(sender_id)
    recipient_id = state.get("recipient_id") if state else None

    if not recipient_id:
        await callback.answer("Сначала выбери получателя через «🎁 Подарок».", show_alert=True)
        return

    await ensure_user(sender_id, callback.from_user.username, callback.from_user.full_name)
    ok, msg = await send_coins_gift(sender_id, recipient_id, amount, bot)

    if not ok:
        await callback.message.edit_text(msg, reply_markup=back_menu_kb(), parse_mode="HTML")
        await callback.answer("Ошибка отправки подарка", show_alert=True)
        return

    await callback.message.edit_text(msg, reply_markup=back_menu_kb(), parse_mode="HTML")
    await callback.answer("Подарок отправлен!", show_alert=True)


async def handle_premium_or_gift_invoice(callback: CallbackQuery):
    prefix, tariff_key = callback.data.split(":", 1)
    tariff = PREMIUM_TARIFFS.get(tariff_key)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    if prefix == "gift_prem_select":
        state = pending_gift_state.get(callback.from_user.id)
        recipient_id = state.get("recipient_id") if state else None
        if not recipient_id:
            await callback.answer("Сначала выбери получателя через «🎁 Подарок».", show_alert=True)
            return
        await callback.message.answer_invoice(
            title=f"Подарок: {tariff['title']}",
            description=f"Подарок «{tariff['title']}» для пользователя ID {recipient_id}",
            payload=f"gift_prem:{recipient_id}:{tariff_key}",
            currency="XTR",
            prices=[LabeledPrice(label=tariff["title"], amount=tariff["stars"])],
        )
        await callback.answer()
        return

    await callback.message.answer_invoice(
        title=tariff["title"],
        description=f"Активация статуса «{tariff['title']}»",
        payload=f"self_prem:{tariff_key}",
        currency="XTR",
        prices=[LabeledPrice(label=tariff["title"], amount=tariff["stars"])],
    )
    await callback.answer()


async def handle_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


async def handle_successful_payment(message: Message, bot: Bot):
    payload = message.successful_payment.invoice_payload
    parts = payload.split(":")

    if parts[0] == "self_prem" and len(parts) >= 2:
        tariff_key = parts[1]
        if tariff_key in PREMIUM_TARIFFS:
            await grant_premium(message.from_user.id, tariff_key)
            await message.answer(
                f"👑 Оплата прошла! Тариф <b>{PREMIUM_TARIFFS[tariff_key]['title']}</b> активирован!",
                reply_markup=main_menu_kb(),
                parse_mode="HTML",
            )
        return

    if parts[0] == "gift_prem" and len(parts) >= 3:
        recipient_id = int(parts[1])
        tariff_key = parts[2]
        if tariff_key in PREMIUM_TARIFFS:
            await grant_premium(recipient_id, tariff_key)
            await message.answer(
                f"🎁 Оплата прошла! Тариф <b>{PREMIUM_TARIFFS[tariff_key]['title']}</b> подарен пользователю <code>{recipient_id}</code>!",
                reply_markup=main_menu_kb(),
                parse_mode="HTML",
            )
            try:
                await bot.send_message(
                    recipient_id,
                    f"🎁 Тебе подарили <b>{PREMIUM_TARIFFS[tariff_key]['title']}</b> от пользователя <code>{message.from_user.id}</code>!",
                    parse_mode="HTML",
                )
            except Exception:
                pass


async def handle_cancel_reply(message: Message):
    pending_gift_state.pop(message.from_user.id, None)
    await message.answer("Действие отменено.", reply_markup=main_menu_kb())


async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_cancel_reply, F.text == "❌ Отмена")
    dp.message.register(handle_users_shared, F.users_shared)
    dp.message.register(handle_webapp_data, F.web_app_data)
    dp.message.register(handle_successful_payment, F.successful_payment)

    dp.callback_query.register(handle_menu_callbacks, F.data.startswith("menu:"))
    dp.callback_query.register(handle_gift_mode, F.data.startswith("gift_mode:"))
    dp.callback_query.register(handle_gift_coins_amount, F.data.startswith("gift_coins_amt:"))
    dp.callback_query.register(
        handle_premium_or_gift_invoice,
        F.data.startswith("buy_prem:") | F.data.startswith("gift_prem_select:"),
    )

    dp.pre_checkout_query.register(handle_pre_checkout)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
