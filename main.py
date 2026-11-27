import os
import json
import time
import asyncio
import aiosqlite
from urllib.parse import urlencode
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    KeyboardButtonRequestUsers,
    WebAppInfo,
    LabeledPrice,
    PreCheckoutQuery,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DB_PATH = os.environ.get("DB_PATH", "bot.db")

# Ссылка на твоё веб-приложение (Mini App) магазина (обязательно https://)
SHOP_WEBAPP_URL = os.environ.get("SHOP_WEBAPP_URL", "https://egor499.github.io/cointag-shop/")

# Курс: 1 звезда = 100 коинов
COINS_PER_STAR = 25

# Тарифы премиума (используются и для покупки себе, и для подарка другу за звёзды)
PREMIUM_TARIFFS = {
    "1d": {"title": "Премиум на 1 день", "stars": 3, "seconds": 86400},
    "7d": {"title": "Премиум на 7 дней", "stars": 18, "seconds": 7 * 86400},
    "30d": {"title": "Премиум на 30 дней", "stars": 55, "seconds": 30 * 86400},
    "forever": {"title": "Премиум НАВСЕГДА (УЛЬТРА СКИДКА)", "stars": 149, "seconds": None},
}

# ТГ-подарки магазина вывода:
# мишка = 300 коинов (15 зв), сердечко = 500 коинов (25 зв), колечко = 1500 коинов (100 зв)
TG_GIFTS = {
    "bear": {
        "name": "Мишка",
        "emoji": "🧸",
        "stars": 15,
        "coins": 300,
    },
    "heart": {
        "name": "Сердечко",
        "emoji": "💖",
        "stars": 25,
        "coins": 500,
    },
    "ring": {
        "name": "Колечко",
        "emoji": "💍",
        "stars": 100,
        "coins": 1500,
    },
}

GIFT_COIN_AMOUNTS = [100, 500, 1500, 5000]
BONUS_COINS = 5
BONUS_COOLDOWN = 86400
REF_BONUS_COINS = 15
AUTO_DELETE_SECONDS = 60

# Временное состояние для выбора подарка / суммы / действий админа
pending_gift_state: dict[int, dict] = {}
pending_admin_action: dict[int, str] = {}

# Кэш активного экрана и активные таймеры автоудаления на пользователя
active_screen_cache: dict[int, dict] = {}
autodel_tasks: dict[int, asyncio.Task] = {}


def make_btn(text: str, style: str | None = None, **kwargs) -> InlineKeyboardButton:
    """Создаёт InlineKeyboardButton с цветом ('success', 'primary', 'danger' или None)."""
    if style:
        try:
            return InlineKeyboardButton(text=text, style=style, **kwargs)
        except TypeError:
            pass
    return InlineKeyboardButton(text=text, **kwargs)


def is_admin(user_id: int | None) -> bool:
    return bool(user_id is not None and int(user_id) == ADMIN_ID)


def build_shop_webapp_url(user_id: int, coins: int, bot_username: str = "LarplBot") -> str:
    sep = "&" if "?" in SHOP_WEBAPP_URL else "?"
    params = urlencode(
        {
            "user_id": user_id,
            "coins": coins,
            "rate": COINS_PER_STAR,
            "bot": bot_username,
            "v": int(time.time()),
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
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
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


async def cleanup_expired_premiums_db():
    now_ts = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE users
            SET premium_until = 0
            WHERE premium_forever = 0 AND premium_until > 0 AND premium_until <= ?
            """,
            (now_ts,),
        )
        await db.commit()


async def add_coins(target_user_id: int, delta: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT coins FROM users WHERE user_id = ?", (target_user_id,))
        row = await cur.fetchone()
        if not row:
            initial = max(0, 10 + delta)
            await db.execute(
                "INSERT INTO users (user_id, coins) VALUES (?, ?)",
                (target_user_id, initial),
            )
        else:
            new_balance = max(0, (row[0] or 0) + delta)
            await db.execute(
                "UPDATE users SET coins = ? WHERE user_id = ?",
                (new_balance, target_user_id),
            )
        await db.commit()


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


def main_menu_kb(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            make_btn("👤 Профиль", style="primary", callback_data="menu:profile"),
            make_btn("🎁 Бонус", style="success", callback_data="menu:bonus"),
        ],
        [
            make_btn("👥 Рефералка", style="primary", callback_data="menu:ref"),
            make_btn("🛒 Магазин", style="success", callback_data="menu:shop"),
        ],
        [
            make_btn("👑 Премиум", style="primary", callback_data="menu:premium"),
            make_btn("⚡ VIP-раздел", style="success", callback_data="menu:vip"),
        ],
        [
            make_btn("🎁 Подарок", style="danger", callback_data="menu:gift"),
        ],
    ]
    if is_admin(user_id):
        rows.append([make_btn("🛠 Админ-панель", style ="danger", callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                make_btn("📊 Статистика", style="primary", callback_data="adm:stats"),
                make_btn("📣 Рассылка всем", callback_data="adm:broadcast"),
            ],
            [
                make_btn("🪙 Выдать/снять коины", style="success", callback_data="adm:coins"),
                make_btn("👑 Выдать Премиум 30д", style="success", callback_data="adm:prem30"),
            ],
            [make_btn("⬅️ В меню", style="danger", callback_data="menu:main")],
        ]
    )


def back_menu_kb(user_id: int | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_btn("⬅️ В меню", style="danger", callback_data="menu:main")]
        ]
    )


def premium_menu_kb(prefix: str = "buy_prem") -> InlineKeyboardMarkup:
    rows = []
    for key, t in PREMIUM_TARIFFS.items():
        btn_style = "success" if key == "forever" else "primary"
        rows.append(
            [
                make_btn(
                    f"{t['title']} — {t['stars']} ★",
                    style=btn_style,
                    callback_data=f"{prefix}:{key}",
                )
            ]
        )
    rows.append([make_btn("⬅️ В меню", style="danger", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def shop_webapp_kb(user_id: int, coins: int, bot_username: str = "LarplBot") -> InlineKeyboardMarkup:
    url = build_shop_webapp_url(user_id, coins, bot_username)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                make_btn(
                    "🛍 Открыть магазин подарков",
                    style="success",
                    web_app=WebAppInfo(url=url),
                )
            ],
            [make_btn("⬅️ В меню", style="danger", callback_data="menu:main")],
        ]
    )


def gift_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [make_btn("🪙 Подарить КоинТэг", style="primary", callback_data="gift_mode:coins")],
            [make_btn("👑 Подарить Премиум (★)", style="success", callback_data="gift_mode:premium")],
            [make_btn("⬅️ В меню", style="danger", callback_data="menu:main")],
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
                make_btn(
                    f"Подарить {amt} КоинТэг",
                    style="success",
                    callback_data=f"gift_coins_amt:{amt}",
                )
            ]
        )
    rows.append([make_btn("⬅️ В меню", style="danger", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def clear_user_cache_and_timer(user_id: int, cancel_task: bool = False):
    active_screen_cache.pop(user_id, None)
    task = autodel_tasks.pop(user_id, None)
    if cancel_task and task and not task.done():
        task.cancel()


async def build_screen_payload(bot: Bot, user_id: int, screen: str) -> tuple[str, InlineKeyboardMarkup] | None:
    await cleanup_expired_premiums_db()
    user = await get_user(user_id)
    if not user:
        return None

    if screen == "profile":
        text = (
            "👤 <b>Твой профиль</b>\n\n"
            f"🆔 ID: <code>{user['user_id']}</code>\n"
            f"🏅 Статус: <b>{format_premium_status(user)}</b>\n"
            f"🪙 Баланс: <b>{user['coins']} КоинТэг</b>\n"
            f"👥 Приглашено друзей: <b>{user['referrals_count']}</b>"
        )
        return text, back_menu_kb(user_id)

    if screen == "ref":
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        text = (
            "👥 <b>Реферальная система</b>\n\n"
            f"За каждого нового друга ты получаешь <b>+{REF_BONUS_COINS} КоинТэг</b> (с PREMIUM — x2).\n"
            f"Приглашено друзей: <b>{user['referrals_count']}</b>\n\n"
            f"Твоя ссылка:\n{ref_link}"
        )
        return text, back_menu_kb(user_id)

    if screen == "shop":
        bot_info = await bot.get_me()
        lines = [
            "🛒 <b>Магазин ТГ-подарков</b>",
            f"Твой баланс: <b>{user['coins']} КоинТэг</b>",
            f"Курс вывода: <b>1 ★ = {COINS_PER_STAR} КоинТэг</b>\n",
            "Доступные подарки к выводу:",
        ]
        for item in TG_GIFTS.values():
            lines.append(f"• {item['emoji']} <b>{item['name']}</b> — <b>{item['coins']} КоинТэг</b> ({item['stars']} ★)")
        return "\n".join(lines), shop_webapp_kb(user_id, user["coins"], bot_info.username or "LarplBot")

    if screen == "premium":
        text = (
            "👑 <b>Премиум-статус</b>\n"
            f"Текущий статус: <b>{format_premium_status(user)}</b>\n\n"
            "Выбери тариф:"
        )
        return text, premium_menu_kb("buy_prem")

    if screen == "vip":
        if not is_premium_active(user):
            return (
                "Добро пожаловать! Выбирай нужный раздел кнопками ниже:",
                main_menu_kb(user_id),
            )
        text = (
            "⚡ <b>VIP-раздел</b>\n\n"
            "Добро пожаловать в VIP! Здесь доступны эксклюзивные бонусы и привилегии для владельцев PREMIUM."
        )
        return text, back_menu_kb(user_id)

    if screen == "gift":
        text = (
            "🎁 <b>Раздел «Подарок»</b>\n"
            f"Твой текущий баланс: <b>{user['coins']} КоинТэг</b>\n\n"
            "Что хочешь подарить пользователю из контактов?"
        )
        return text, gift_type_kb()

    if screen == "admin":
        if not is_admin(user_id):
            return (
                "Добро пожаловать! Выбирай нужный раздел кнопками ниже:",
                main_menu_kb(user_id),
            )
        return "🛠 <b>Админ-панель</b>\n\nВыбери действие:", admin_panel_kb()

    return (
        "Добро пожаловать! Выбирай нужный раздел кнопками ниже:",
        main_menu_kb(user_id),
    )


async def _autodel_worker(bot: Bot, user_id: int, chat_id: int, message_id: int, screen: str):
    try:
        await asyncio.sleep(AUTO_DELETE_SECONDS)
    except asyncio.CancelledError:
        return

    cached = active_screen_cache.get(user_id)
    if cached and cached.get("message_id") == message_id:
        active_screen_cache.pop(user_id, None)
    if autodel_tasks.get(user_id) is asyncio.current_task():
        autodel_tasks.pop(user_id, None)

    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"[autodel] delete_message failed for user={user_id} msg={message_id}: {e}")


async def send_screen_with_autodel(
    bot: Bot,
    user_id: int,
    chat_id: int,
    screen: str,
    text: str,
    kb: InlineKeyboardMarkup,
) -> Message:
    clear_user_cache_and_timer(user_id, cancel_task=True)

    msg = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=kb,
        parse_mode="HTML",
    )
    active_screen_cache[user_id] = {
        "chat_id": msg.chat.id,
        "message_id": msg.message_id,
        "screen": screen,
    }
    autodel_tasks[user_id] = asyncio.create_task(
        _autodel_worker(bot, user_id, msg.chat.id, msg.message_id, screen)
    )
    return msg


async def replace_screen_on_callback(callback: CallbackQuery, screen: str, text: str, kb: InlineKeyboardMarkup):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    old_msg_id = callback.message.message_id

    clear_user_cache_and_timer(user_id, cancel_task=True)
    try:
        await bot_msg_delete_safe(callback.bot, chat_id, old_msg_id)
    except Exception:
        pass

    await send_screen_with_autodel(callback.bot, user_id, chat_id, screen, text, kb)
    await callback.answer()


async def bot_msg_delete_safe(bot: Bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"[autodel] instant delete failed chat={chat_id} msg={message_id}: {e}")


async def push_update_to_all(bot: Bot) -> tuple[int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()

    sent = 0
    failed = 0
    for (uid,) in rows:
        try:
            await send_screen_with_autodel(
                bot,
                uid,
                uid,
                "main",
                "🔄 <b>Бот и магазин обновлены!</b>\nЗабирай бонус и заглядывай в <b>Магазин</b> 🛒",
                main_menu_kb(uid),
            )
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    return sent, failed


async def process_gift_order(message: Message, item_key: str, bot: Bot):
    item = TG_GIFTS.get(item_key)
    if not item:
        await message.answer("❌ Выбран неизвестный ТГ-подарок.", reply_markup=main_menu_kb(message.from_user.id))
        return

    user_id = message.from_user.id
    await ensure_user(user_id, message.from_user.username, message.from_user.full_name)
    user = await get_user(user_id)

    if not user or user["coins"] < item["coins"]:
        current = user["coins"] if user else 0
        await message.answer(
            f"❌ Недостаточно КоинТэг для вывода <b>{item['emoji']} {item['name']}</b>!\n"
            f"Нужно: <b>{item['coins']} КоинТэг</b> ({item['stars']} ★), у тебя: <b>{current} КоинТэг</b>.",
            reply_markup=main_menu_kb(user_id),
            parse_mode="HTML",
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE users SET coins = coins - ? WHERE user_id = ? AND coins >= ?",
            (item["coins"], user_id, item["coins"]),
        )
        if cur.rowcount == 0:
            await message.answer("❌ Недостаточно КоинТэг!", reply_markup=main_menu_kb(user_id))
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
        reply_markup=main_menu_kb(user_id),
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


async def show_profile(target: Message | CallbackQuery):
    user_id = target.from_user.id
    await ensure_user(user_id, target.from_user.username, target.from_user.full_name)
    payload = await build_screen_payload(target.bot, user_id, "profile")
    if not payload:
        return
    text, kb = payload
    if isinstance(target, CallbackQuery):
        await replace_screen_on_callback(target, "profile", text, kb)
    else:
        await send_screen_with_autodel(target.bot, user_id, target.chat.id, "profile", text, kb)


async def open_shop_app(callback: CallbackQuery):
    user_id = callback.from_user.id
    await ensure_user(user_id, callback.from_user.username, callback.from_user.full_name)
    payload = await build_screen_payload(callback.bot, user_id, "shop")
    if not payload:
        await callback.answer()
        return
    text, kb = payload
    await replace_screen_on_callback(callback, "shop", text, kb)


async def show_gift_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    await ensure_user(user_id, callback.from_user.username, callback.from_user.full_name)
    payload = await build_screen_payload(callback.bot, user_id, "gift")
    if not payload:
        await callback.answer()
        return
    text, kb = payload
    await replace_screen_on_callback(callback, "gift", text, kb)


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

    if command.args and command.args.startswith("shop_"):
        item_key = command.args.removeprefix("shop_")
        await process_gift_order(message, item_key, bot)
        return

    if command.args and command.args.startswith("gift_"):
        parts = command.args.split("_")
        if len(parts) >= 4 and parts[1] == "coins" and parts[2].isdigit() and parts[3].isdigit():
            recipient_id = int(parts[2])
            amount = int(parts[3])
            ok, msg = await send_coins_gift(message.from_user.id, recipient_id, amount, bot)
            await message.answer(msg, reply_markup=main_menu_kb(message.from_user.id), parse_mode="HTML")
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

    await send_screen_with_autodel(
        bot,
        message.from_user.id,
        message.chat.id,
        "main",
        "Добро пожаловать! Выбирай нужный раздел кнопками ниже:",
        main_menu_kb(message.from_user.id),
    )


async def handle_push_update_cmd(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    sent, failed = await push_update_to_all(bot)
    await message.answer(
        f"✅ Рассылка обновления завершена. Доставлено: <b>{sent}</b>, ошибок: <b>{failed}</b>",
        reply_markup=admin_panel_kb(),
        parse_mode="HTML",
    )


async def handle_menu_callbacks(callback: CallbackQuery):
    action = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    await ensure_user(user_id, callback.from_user.username, callback.from_user.full_name)

    if action == "main":
        pending_admin_action.pop(user_id, None)
        await replace_screen_on_callback(
            callback,
            "main",
            "Добро пожаловать! Выбирай нужный раздел кнопками ниже:",
            main_menu_kb(user_id),
        )
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
        payload = await build_screen_payload(callback.bot, user_id, "ref")
        if payload:
            await replace_screen_on_callback(callback, "ref", payload[0], payload[1])
        else:
            await callback.answer()
        return

    if action == "shop":
        await open_shop_app(callback)
        return

    if action == "premium":
        payload = await build_screen_payload(callback.bot, user_id, "premium")
        if payload:
            await replace_screen_on_callback(callback, "premium", payload[0], payload[1])
        else:
            await callback.answer()
        return

    if action == "vip":
        user = await get_user(user_id)
        if not is_premium_active(user):
            await callback.answer("🔒 Этот раздел доступен только владельцам 👑 PREMIUM!", show_alert=True)
            return
        payload = await build_screen_payload(callback.bot, user_id, "vip")
        if payload:
            await replace_screen_on_callback(callback, "vip", payload[0], payload[1])
        else:
            await callback.answer()
        return

    if action == "gift":
        await show_gift_menu(callback)
        return

    if action == "admin":
        if not is_admin(user_id):
            await callback.answer("Доступ запрещён.", show_alert=True)
            return
        pending_admin_action.pop(user_id, None)
        await replace_screen_on_callback(
            callback,
            "admin",
            "🛠 <b>Админ-панель</b>\n\nВыбери действие:",
            admin_panel_kb(),
        )
        return


async def handle_admin_callbacks(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    sub = callback.data.split(":", 1)[1]
    uid = callback.from_user.id

    if sub == "stats":
        async with aiosqlite.connect(DB_PATH) as db:
            cur_u = await db.execute("SELECT COUNT(*), COALESCE(SUM(coins), 0) FROM users")
            users_cnt, total_coins = await cur_u.fetchone()
            cur_o = await db.execute("SELECT COUNT(*) FROM shop_orders WHERE status = 'pending'")
            (pending_orders,) = await cur_o.fetchone()

        text = (
            "📊 <b>Статистика бота</b>\n\n"
            f"👥 Пользователей: <b>{users_cnt}</b>\n"
            f"🪙 Всего КоинТэг на балансах: <b>{total_coins}</b>\n"
            f"🛍 Заявок на вывод в ожидании: <b>{pending_orders}</b>"
        )
        await callback.message.edit_text(text, reply_markup=admin_panel_kb(), parse_mode="HTML")
        await callback.answer()
        return

    if sub == "broadcast":
        await callback.answer("Запускаю рассылку...", show_alert=False)
        sent, failed = await push_update_to_all(bot)
        await callback.message.answer(
            f"✅ Рассылка завершена. Доставлено: <b>{sent}</b>, ошибок: <b>{failed}</b>",
            reply_markup=admin_panel_kb(),
            parse_mode="HTML",
        )
        return

    if sub == "coins":
        pending_admin_action[uid] = "coins"
        await callback.message.edit_text(
            "🪙 <b>Начисление / списание КоинТэг</b>\n\n"
            "Отправь сообщение в формате:\n<code>USER_ID СУММА</code>\n"
            "Например: <code>123456789 500</code> или <code>123456789 -200</code>",
            reply_markup=admin_panel_kb(),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if sub == "prem30":
        pending_admin_action[uid] = "prem30"
        await callback.message.edit_text(
            "👑 <b>Выдача Премиума на 30 дней</b>\n\n"
            "Отправь <code>USER_ID</code> пользователя, которому нужно выдать тариф на 30 дней:",
            reply_markup=admin_panel_kb(),
            parse_mode="HTML",
        )
        await callback.answer()
        return


async def handle_buy_prem(callback: CallbackQuery):
    tariff_key = callback.data.split(":", 1)[1]
    tariff = PREMIUM_TARIFFS.get(tariff_key)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await callback.message.answer_invoice(
        title=tariff["title"],
        description=f"Активация статуса «{tariff['title']}» в CoinTag",
        payload=f"buy_prem:{tariff_key}",
        currency="XTR",
        prices=[LabeledPrice(label=tariff["title"], amount=tariff["stars"])],
    )
    await callback.answer()


async def handle_gift_prem_tariff(callback: CallbackQuery):
    tariff_key = callback.data.split(":", 1)[1]
    tariff = PREMIUM_TARIFFS.get(tariff_key)
    state = pending_gift_state.get(callback.from_user.id)
    recipient_id = state.get("recipient_id") if state else None

    if not tariff or not recipient_id:
        await callback.answer("Сначала выбери получателя подарка.", show_alert=True)
        return

    await callback.message.answer_invoice(
        title=f"Подарок: {tariff['title']}",
        description=f"Подарок «{tariff['title']}» для пользователя ID {recipient_id}",
        payload=f"gift_prem:{recipient_id}:{tariff_key}",
        currency="XTR",
        prices=[LabeledPrice(label=tariff["title"], amount=tariff["stars"])],
    )
    await callback.answer()


async def handle_gift_coins_amount(callback: CallbackQuery, bot: Bot):
    amt_str = callback.data.split(":", 1)[1]
    if not amt_str.isdigit():
        await callback.answer("Некорректная сумма.", show_alert=True)
        return

    amount = int(amt_str)
    sender_id = callback.from_user.id
    state = pending_gift_state.pop(sender_id, None)
    recipient_id = state.get("recipient_id") if state else None

    if not recipient_id:
        await callback.answer("Получатель не выбран. Начни заново через «Подарок».", show_alert=True)
        return

    ok, msg = await send_coins_gift(sender_id, recipient_id, amount, bot)
    await replace_screen_on_callback(callback, "main", msg, main_menu_kb(sender_id))


async def handle_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


async def handle_successful_payment(message: Message, bot: Bot):
    sp = message.successful_payment
    if not sp:
        return

    payload = sp.invoice_payload or ""
    parts = payload.split(":")

    if len(parts) == 2 and parts[0] == "buy_prem" and parts[1] in PREMIUM_TARIFFS:
        tariff_key = parts[1]
        await grant_premium(message.from_user.id, tariff_key)
        await message.answer(
            f"👑 Оплата прошла! Тебе активирован <b>{PREMIUM_TARIFFS[tariff_key]['title']}</b>.",
            reply_markup=main_menu_kb(message.from_user.id),
            parse_mode="HTML",
        )
        return

    if len(parts) == 3 and parts[0] == "gift_prem" and parts[1].isdigit() and parts[2] in PREMIUM_TARIFFS:
        recipient_id = int(parts[1])
        tariff_key = parts[2]
        await grant_premium(recipient_id, tariff_key)
        await message.answer(
            f"🎁 Подарок <b>{PREMIUM_TARIFFS[tariff_key]['title']}</b> отправлен пользователю <code>{recipient_id}</code>!",
            reply_markup=main_menu_kb(message.from_user.id),
            parse_mode="HTML",
        )
        try:
            await bot.send_message(
                recipient_id,
                f"👑 Тебе подарили <b>{PREMIUM_TARIFFS[tariff_key]['title']}</b> от пользователя <code>{message.from_user.id}</code>!",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def handle_admin_text_input(message: Message, bot: Bot) -> bool:
    if not is_admin(message.from_user.id):
        return False

    action = pending_admin_action.get(message.from_user.id)
    if not action or not message.text:
        return False

    raw = message.text.strip()
    pending_admin_action.pop(message.from_user.id, None)

    if action == "coins":
        parts = raw.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit() or not parts[0].isdigit():
            await message.answer(
                "❌ Неверный формат. Нужно: <code>USER_ID СУММА</code>",
                reply_markup=admin_panel_kb(),
                parse_mode="HTML",
            )
            return True
        target_id = int(parts[0])
        delta = int(parts[1])
        await add_coins(target_id, delta)
        updated = await get_user(target_id)
        await message.answer(
            f"✅ Баланс <code>{target_id}</code> изменён на <b>{delta:+d}</b>. Текущий баланс: <b>{updated['coins']} КоинТэг</b>.",
            reply_markup=admin_panel_kb(),
            parse_mode="HTML",
        )
        try:
            await bot.send_message(
                target_id,
                f"🪙 Администратор обновил твой баланс на <b>{delta:+d} КоинТэг</b>!",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return True

    if action == "prem30":
        if not raw.isdigit():
            await message.answer(
                "❌ Нужно указать числовой <code>USER_ID</code>.",
                reply_markup=admin_panel_kb(),
                parse_mode="HTML",
            )
            return True
        target_id = int(raw)
        await grant_premium(target_id, "30d")
        await message.answer(
            f"✅ Пользователю <code>{target_id}</code> выдан <b>Премиум на 30 дней</b>.",
            reply_markup=admin_panel_kb(),
            parse_mode="HTML",
        )
        try:
            await bot.send_message(
                target_id,
                "👑 Тебе выдан <b>Премиум на 30 дней</b>!",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return True

    return False


async def handle_webapp_data(message: Message, bot: Bot):
    if not message.web_app_data:
        return
    try:
        payload = json.loads(message.web_app_data.data)
    except Exception:
        await message.answer("❌ Некорректные данные из приложения магазина.", reply_markup=main_menu_kb(message.from_user.id))
        return

    item_key = payload.get("item_id") or payload.get("item")
    await process_gift_order(message, item_key, bot)


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


async def handle_users_shared(message: Message):
    if not message.users_shared or not message.users_shared.users:
        return

    shared = message.users_shared.users[0]
    recipient_id = shared.user_id
    sender_id = message.from_user.id
    state = pending_gift_state.get(sender_id) or {"mode": "coins"}
    state["recipient_id"] = recipient_id
    pending_gift_state[sender_id] = state

    await message.answer(
        f"✅ Выбран пользователь ID <code>{recipient_id}</code>.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML",
    )

    if state.get("mode") == "premium":
        await message.answer(
            f"👑 Выбери тариф Премиума для отправки пользователю <code>{recipient_id}</code>:",
            reply_markup=premium_menu_kb("gift_prem_tariff"),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"🪙 Выбери сумму КоинТэг для подарка пользователю <code>{recipient_id}</code>:",
            reply_markup=gift_coins_amounts_kb(),
            parse_mode="HTML",
        )


async def handle_text_messages(message: Message, bot: Bot):
    if message.text and message.text.strip() == "❌ Отмена":
        pending_gift_state.pop(message.from_user.id, None)
        pending_admin_action.pop(message.from_user.id, None)
        await message.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())
        await send_screen_with_autodel(
            bot,
            message.from_user.id,
            message.chat.id,
            "main",
            "Добро пожаловать! Выбирай нужный раздел кнопками ниже:",
            main_menu_kb(message.from_user.id),
        )
        return

    if await handle_admin_text_input(message, bot):
        return


async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_push_update_cmd, Command("push_update"))
    dp.message.register(handle_webapp_data, F.web_app_data)
    dp.message.register(handle_users_shared, F.users_shared)
    dp.message.register(handle_successful_payment, F.successful_payment)

    dp.callback_query.register(handle_menu_callbacks, F.data.startswith("menu:"))
    dp.callback_query.register(handle_admin_callbacks, F.data.startswith("adm:"))
    dp.callback_query.register(handle_buy_prem, F.data.startswith("buy_prem:"))
    dp.callback_query.register(handle_gift_mode, F.data.startswith("gift_mode:"))
    dp.callback_query.register(handle_gift_prem_tariff, F.data.startswith("gift_prem_tariff:"))
    dp.callback_query.register(handle_gift_coins_amount, F.data.startswith("gift_coins_amt:"))

    dp.pre_checkout_query.register(handle_pre_checkout)
    dp.message.register(handle_text_messages, F.text)

    try:
        await dp.start_polling(bot)
    finally:
        for task in list(autodel_tasks.values()):
            if not task.done():
                task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
