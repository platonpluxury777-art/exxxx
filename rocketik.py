"""
Обменник Crypto Bot <-> xRocket (aiogram 3 + aiohttp + sqlite)
С капчей при первом входе.

pip install aiogram aiohttp
Запуск: python exchange_bot.py
"""
import asyncio
import logging
import random
import sqlite3
import time
from decimal import Decimal, ROUND_DOWN

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ============ НАСТРОЙКИ ============
BOT_TOKEN = "8616587313:AAHtdd21SFqLvUt0neOX5rncoAYy1I7-w9c"
CRYPTOBOT_TOKEN = "670065:AAFoyRI9DSSKBzzvEU7qFuNBfGlHoXQzB3Y"       # @CryptoBot -> Crypto Pay -> Create App
XROCKET_TOKEN = "ТОКЕН_XROCKET_PAY"        # @xRocket -> Rocket Pay -> Create App
CRYPTOBOT_URL = "https://pay.crypt.bot/api"  # для теста: https://testnet-pay.crypt.bot/api
XROCKET_URL = "https://pay.xrocket.tg"       # для теста: https://dev.pay.xrocket.tg

ASSET = "USDT"
FEE = Decimal("0.3")
MIN_AMOUNT = Decimal("5")
INVOICE_TTL = 15 * 60  # секунд на оплату
CAPTCHA_ATTEMPTS = 3   # попыток решить капчу
CAPTCHA_BAN = 5 * 60   # секунд блокировки после неудачи
ADMIN_IDS = [8856069753]  # кому слать уведомления о проблемах
# ===================================

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

db = sqlite3.connect("orders.db")
db.execute(
    """CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, direction TEXT, amount TEXT, payout TEXT,
        invoice_id TEXT, status TEXT, created INTEGER)"""
)
db.execute("CREATE TABLE IF NOT EXISTS verified(user_id INTEGER PRIMARY KEY)")
db.commit()


class Form(StatesGroup):
    amount = State()


def d(x) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


# ---------- Капча ----------
captcha_answers: dict[int, int] = {}   # user_id -> правильный ответ
captcha_fails: dict[int, int] = {}     # user_id -> число ошибок
captcha_ban: dict[int, float] = {}     # user_id -> время разблокировки


def is_verified(user_id: int) -> bool:
    return db.execute("SELECT 1 FROM verified WHERE user_id=?", (user_id,)).fetchone() is not None


def make_captcha(user_id: int):
    a, b = random.randint(2, 9), random.randint(2, 9)
    op = random.choice(["+", "-", "×"])
    answer = a + b if op == "+" else a - b if op == "-" else a * b
    captcha_answers[user_id] = answer

    options = {answer}
    while len(options) < 4:
        options.add(answer + random.choice([-3, -2, -1, 1, 2, 3, 4]))
    options = random.sample(sorted(options), 4)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=str(o), callback_data=f"cap:{o}") for o in options]
        ]
    )
    return f"🤖 Подтвердите, что вы не бот.\nСколько будет <b>{a} {op} {b}</b>?", kb


async def send_captcha(target: Message, user_id: int):
    text, kb = make_captcha(user_id)
    await target.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("cap:"))
async def captcha_check(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id

    if captcha_ban.get(uid, 0) > time.time():
        left = int(captcha_ban[uid] - time.time())
        return await c.answer(f"Слишком много ошибок. Подождите {left} сек.", show_alert=True)

    if uid not in captcha_answers:
        await c.message.delete()
        return await send_captcha(c.message, uid)

    if int(c.data.split(":")[1]) == captcha_answers.pop(uid):
        db.execute("INSERT OR IGNORE INTO verified(user_id) VALUES(?)", (uid,))
        db.commit()
        captcha_fails.pop(uid, None)
        await c.message.delete()
        await c.answer("Проверка пройдена ✅")
        return await show_menu(c.message, state)

    # неверно
    captcha_fails[uid] = captcha_fails.get(uid, 0) + 1
    if captcha_fails[uid] >= CAPTCHA_ATTEMPTS:
        captcha_fails.pop(uid, None)
        captcha_ban[uid] = time.time() + CAPTCHA_BAN
        await c.message.edit_text(f"❌ Слишком много ошибок. Попробуйте через {CAPTCHA_BAN // 60} мин.")
        return await c.answer()

    text, kb = make_captcha(uid)
    await c.message.edit_text("❌ Неверно, попробуйте ещё раз.\n\n" + text, reply_markup=kb, parse_mode="HTML")
    await c.answer()


# ---------- Crypto Bot API ----------
async def cb_call(method: str, params: dict | None = None):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{CRYPTOBOT_URL}/{method}",
            json=params or {},
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
        ) as r:
            data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(f"CryptoBot {method}: {data}")
    return data["result"]


async def cb_create_invoice(amount: Decimal, order_id: int):
    res = await cb_call(
        "createInvoice",
        {
            "asset": ASSET,
            "amount": str(amount),
            "description": "Обмен USDT",
            "payload": str(order_id),
            "expires_in": INVOICE_TTL,
        },
    )
    return str(res["invoice_id"]), res.get("bot_invoice_url") or res.get("pay_url")


async def cb_invoice_paid(invoice_id: str) -> Decimal | None:
    res = await cb_call("getInvoices", {"invoice_ids": invoice_id})
    items = res["items"] if isinstance(res, dict) else res
    if items and items[0]["status"] == "paid":
        return Decimal(items[0]["amount"])
    return None


async def cb_transfer(user_id: int, amount: Decimal, order_id: int):
    await cb_call(
        "transfer",
        {
            "user_id": user_id,
            "asset": ASSET,
            "amount": str(amount),
            "spend_id": f"order{order_id}",  # защита от двойной выплаты
            "comment": "Обмен USDT",
        },
    )


# ---------- xRocket API ----------
async def xr_call(method: str, path: str, body: dict | None = None):
    async with aiohttp.ClientSession() as s:
        async with s.request(
            method,
            f"{XROCKET_URL}{path}",
            json=body,
            headers={"Rocket-Pay-Key": XROCKET_TOKEN},
        ) as r:
            data = await r.json()
    if not data.get("success"):
        raise RuntimeError(f"xRocket {path}: {data}")
    return data["data"]


async def xr_create_invoice(amount: Decimal, order_id: int):
    res = await xr_call(
        "POST",
        "/tg-invoices",
        {
            "amount": float(amount),
            "currency": ASSET,
            "numPayments": 1,
            "description": "Обмен USDT",
            "payload": str(order_id),
            "expiredIn": INVOICE_TTL,
        },
    )
    return str(res["id"]), res["link"]


async def xr_invoice_paid(invoice_id: str) -> Decimal | None:
    res = await xr_call("GET", f"/tg-invoices/{invoice_id}")
    if res.get("status") == "paid":
        return Decimal(str(res["amount"]))
    return None


async def xr_transfer(user_id: int, amount: Decimal, order_id: int):
    await xr_call(
        "POST",
        "/app/transfer",
        {
            "tgUserId": user_id,
            "currency": ASSET,
            "amount": float(amount),
            "transferId": f"order{order_id}",  # защита от двойной выплаты
            "description": "Обмен USDT",
        },
    )


# ---------- Направления ----------
DIRECTIONS = {
    "cb_xr": {
        "title": "Crypto Bot → xRocket",
        "create": cb_create_invoice, "paid": cb_invoice_paid, "payout": xr_transfer,
    },
    "xr_cb": {
        "title": "xRocket → Crypto Bot",
        "create": xr_create_invoice, "paid": xr_invoice_paid, "payout": cb_transfer,
    },
}


# ---------- Хэндлеры ----------
def main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Crypto Bot → xRocket", callback_data="dir:cb_xr")],
            [InlineKeyboardButton(text="✨ xRocket → Crypto Bot", callback_data="dir:xr_cb")],
        ]
    )


async def show_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "💎 <b>EXCHANGE</b>\nОбмен USDT между Crypto Bot и xRocket.\n"
        f"Комиссия: <b>{FEE}$</b> • Минимум: <b>{MIN_AMOUNT}$</b>\n\nКуда переводим USDT?",
        reply_markup=main_kb(),
        parse_mode="HTML",
    )


@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    uid = m.from_user.id
    if captcha_ban.get(uid, 0) > time.time():
        left = int(captcha_ban[uid] - time.time())
        return await m.answer(f"❌ Слишком много ошибок. Подождите {left} сек.")
    if not is_verified(uid):
        return await send_captcha(m, uid)
    await show_menu(m, state)


@dp.callback_query(F.data.startswith("dir:"))
async def choose_dir(c: CallbackQuery, state: FSMContext):
    if not is_verified(c.from_user.id):
        await c.answer()
        return await send_captcha(c.message, c.from_user.id)
    direction = c.data.split(":")[1]
    await state.update_data(direction=direction)
    await state.set_state(Form.amount)
    await c.message.answer(
        f"<b>{DIRECTIONS[direction]['title']}</b>\nВведите сумму в USDT (от {MIN_AMOUNT}$):",
        parse_mode="HTML",
    )
    await c.answer()


@dp.message(Form.amount)
async def got_amount(m: Message, state: FSMContext):
    if not is_verified(m.from_user.id):
        return await send_captcha(m, m.from_user.id)
    try:
        amount = d(Decimal(m.text.replace(",", ".").strip()))
    except Exception:
        return await m.answer("Введите число, например 10")

    # ❗ проверка минимальной суммы
    if amount < MIN_AMOUNT:
        return await m.answer(f"❌ Ошибка: обмен доступен от {MIN_AMOUNT}$")

    data = await state.get_data()
    direction = DIRECTIONS[data["direction"]]
    payout = amount - FEE

    cur = db.execute(
        "INSERT INTO orders(user_id,direction,amount,payout,status,created) VALUES(?,?,?,?,?,?)",
        (m.from_user.id, data["direction"], str(amount), str(payout), "new", int(time.time())),
    )
    db.commit()
    order_id = cur.lastrowid

    try:
        invoice_id, link = await direction["create"](amount, order_id)
    except Exception:
        logging.exception("create invoice")
        return await m.answer("⚠️ Ошибка создания счёта, попробуйте позже.")

    db.execute("UPDATE orders SET invoice_id=?, status='wait' WHERE id=?", (invoice_id, order_id))
    db.commit()
    await state.clear()

    await m.answer(
        f"📋 <b>Заявка создана</b>\n{direction['title']}\n"
        f"Оплатите: <b>{amount} USDT</b>\nКомиссия: <b>{FEE} USDT</b>\n"
        f"Получите: <b>{payout} USDT</b>\n\n"
        f"Счёт действует {INVOICE_TTL // 60} мин.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатить", url=link)]]
        ),
        parse_mode="HTML",
    )


# ---------- Фоновая проверка оплат ----------
async def watcher():
    while True:
        try:
            rows = db.execute(
                "SELECT id,user_id,direction,amount,payout,invoice_id,created FROM orders WHERE status='wait'"
            ).fetchall()
            for oid, uid, dkey, amount, payout, inv, created in rows:
                dr = DIRECTIONS[dkey]
                try:
                    paid = await dr["paid"](inv)
                except Exception:
                    logging.exception("check invoice")
                    continue

                if paid is None:
                    if time.time() - created > INVOICE_TTL + 60:
                        db.execute("UPDATE orders SET status='expired' WHERE id=?", (oid,))
                        db.commit()
                        await bot.send_message(uid, "⌛ Заявка истекла.")
                    continue

                # защита от двойной обработки
                db.execute("UPDATE orders SET status='paying' WHERE id=?", (oid,))
                db.commit()

                if paid < MIN_AMOUNT:
                    await bot.send_message(uid, f"❌ Ошибка: обмен доступен от {MIN_AMOUNT}$")
                    db.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
                    db.commit()
                    continue

                try:
                    await dr["payout"](uid, paid - FEE, oid)
                    db.execute("UPDATE orders SET status='done' WHERE id=?", (oid,))
                    db.commit()
                    await bot.send_message(uid, f"✅ Обмен выполнен!\nЗачислено: {paid - FEE} USDT")
                except Exception:
                    logging.exception("payout")
                    db.execute("UPDATE orders SET status='error' WHERE id=?", (oid,))
                    db.commit()
                    await bot.send_message(uid, "⚠️ Ошибка выплаты, оператор свяжется с вами.")
                    for a in ADMIN_IDS:
                        await bot.send_message(a, f"❗ Ошибка выплаты по заявке #{oid} (user {uid}, {paid - FEE} USDT)")
        except Exception:
            logging.exception("watcher")
        await asyncio.sleep(5)


async def main():
    asyncio.create_task(watcher())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
