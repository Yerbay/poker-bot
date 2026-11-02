"""
Покерный калькулятор вероятности (Texas Hold'em) — Telegram-бот + Mini App.

Карты отмечаются одним нажатием в сетке из 52 кнопок, число игроков за столом
и в раздаче меняется кнопками, диапазон соперников — переключателем.
Можно прислать сообщением банк и ставку — бот скажет, выгоден ли колл.

Запуск:
    pip install -r requirements.txt
    python build_preflop.py          # один раз, ~15 минут
    BOT_TOKEN=... WEBAPP_URL=https://.../poker/ python poker_bot.py
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import signal
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp,
                      Update, WebAppInfo)
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PicklePersistence, filters)

import engine
from engine import RANKS, SUITS

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("poker_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе токен бота попадает в логи
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
WEB_PORT = int(os.getenv("WEB_PORT", "8085"))
BASE = Path(__file__).parent

MIN_PLAYERS, MAX_PLAYERS, DEFAULT_PLAYERS = 2, 10, 6
DEFAULT_RANGE = "mid"

SUIT_SYM = {"s": "♠️", "h": "♥️", "d": "♦️", "c": "♣️"}
RANK_LBL = {"T": "10"}
RANGE_NAMES = {"any": "Любые", "mid": "Средние", "tight": "Крепкие"}
RANGE_HINT = {
    "any": "любые две карты",
    "mid": "играют ~40% лучших рук",
    "tight": "играют ~20% лучших рук",
}
TYPE_RU = {
    "High Card": "Старшая карта", "Pair": "Пара", "Two Pair": "Две пары",
    "Trips": "Сет / тройка", "Straight": "Стрит", "Flush": "Флеш",
    "Full House": "Фулл-хаус", "Quads": "Каре", "Straight Flush": "Стрит-флеш",
}
STAGES = {0: "префлоп", 3: "флоп", 4: "тёрн", 5: "ривер"}

POOL: ProcessPoolExecutor | None = None
_cache: OrderedDict = OrderedDict()


def card_label(c: str) -> str:
    return RANK_LBL.get(c[0], c[0]) + SUIT_SYM[c[1]]


def fmt_num(x: float) -> str:
    return f"{x:g}"


async def analyze(hand, board, opp, rng) -> dict:
    """engine.analyze в пуле процессов с кэшем — одинаковая ситуация не пересчитывается
    (и цифры не «прыгают» от нажатия к нажатию)."""
    key = (tuple(sorted(hand)), tuple(sorted(board)), opp, rng)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    global POOL
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(POOL, engine.analyze, list(hand), list(board), opp, rng)
    except BrokenProcessPool:
        log.warning("Пул вычислений сломан — пересоздаю")
        POOL.shutdown(wait=False, cancel_futures=True)
        POOL = ProcessPoolExecutor(max_workers=2)
        res = await loop.run_in_executor(POOL, engine.analyze, list(hand), list(board), opp, rng)
    _cache[key] = res
    if len(_cache) > 5000:
        _cache.popitem(last=False)
    return res


# ---------- состояние ----------

def get_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    st = context.chat_data
    st.setdefault("hand", [])
    st.setdefault("board", [])
    st.setdefault("history", [])  # порядок добавления карт — для «Отмены»
    st.setdefault("players", DEFAULT_PLAYERS)  # за столом
    st.setdefault("active", st["players"])  # в раздаче (включая вас)
    st.setdefault("mode", "hand")
    st.setdefault("range", DEFAULT_RANGE)
    st.setdefault("pot", None)
    st.setdefault("call", None)
    if st["range"] not in engine.available_ranges():
        st["range"] = "any"
    return st


def new_hand(st: dict) -> None:
    st["hand"], st["board"], st["history"] = [], [], []
    st["mode"] = "hand"
    st["active"] = st["players"]
    st["pot"] = st["call"] = None


def toggle_card(st: dict, c: str):
    """Добавляет/убирает карту. Возвращает текст ошибки или None."""
    for key in ("hand", "board"):
        if c in st[key]:
            st[key].remove(c)
            st["history"].remove(c)
            if len(st["hand"]) < 2:
                st["mode"] = "hand"
            return None
    target = st["mode"]
    if target == "hand" and len(st["hand"]) >= 2:
        target = "board"  # рука заполнена — карта уходит на стол
    if target == "board" and len(st["board"]) >= 5:
        return "На столе уже 5 карт"
    st[target].append(c)
    st["history"].append(c)
    if st["mode"] == "hand" and len(st["hand"]) == 2:
        st["mode"] = "board"
    return None


def undo(st: dict) -> None:
    if not st["history"]:
        return
    c = st["history"].pop()
    for key in ("hand", "board"):
        if c in st[key]:
            st[key].remove(c)
    if len(st["hand"]) < 2:
        st["mode"] = "hand"


# ---------- отображение ----------

def build_keyboard(st: dict) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(("✅ " if st["mode"] == "hand" else "") + "🖐 Мои карты", callback_data="m:hand"),
        InlineKeyboardButton(("✅ " if st["mode"] == "board" else "") + "🃏 Стол", callback_data="m:board"),
    ]]
    for r in RANKS:
        row = []
        for s in SUITS:
            c = r + s
            mark = "🟢" if c in st["hand"] else "🔵" if c in st["board"] else ""
            row.append(InlineKeyboardButton(mark + card_label(c), callback_data="c:" + c))
        rows.append(row)
    rows.append([
        InlineKeyboardButton("➖", callback_data="p:-"),
        InlineKeyboardButton(f"👥 За столом: {st['players']}", callback_data="noop:p"),
        InlineKeyboardButton("➕", callback_data="p:+"),
    ])
    rows.append([
        InlineKeyboardButton("➖", callback_data="a:-"),
        InlineKeyboardButton(f"🎯 В раздаче: {st['active']}", callback_data="noop:a"),
        InlineKeyboardButton("➕", callback_data="a:+"),
    ])
    rows.append([InlineKeyboardButton(("✅ " if st["range"] == r else "") + RANGE_NAMES[r], callback_data="g:" + r)
                 for r in engine.available_ranges()])
    rows.append([
        InlineKeyboardButton("💰 Банк/ставка", callback_data="pot"),
        InlineKeyboardButton("↩️ Отменить", callback_data="undo"),
        InlineKeyboardButton("🔄 Новая", callback_data="new"),
    ])
    last = [InlineKeyboardButton("📖 Комбинации", callback_data="combos")]
    if WEBAPP_URL:
        last.append(InlineKeyboardButton("🃏 Стол (Mini App)", web_app=WebAppInfo(WEBAPP_URL)))
    rows.append(last)
    return InlineKeyboardMarkup(rows)


COMBOS_TEXT = (
    "📖 <b>Покерные комбинации</b> — от старшей к младшей\n\n"
    "1. <b>Роял-флеш</b> — A K Q J 10 одной масти\n     A♠️ K♠️ Q♠️ J♠️ 10♠️\n"
    "2. <b>Стрит-флеш</b> — 5 карт подряд одной масти\n     9♥️ 8♥️ 7♥️ 6♥️ 5♥️\n"
    "3. <b>Каре</b> — 4 карты одного достоинства\n     K♠️ K♣️ K♦️ K♥️\n"
    "4. <b>Фулл-хаус</b> — тройка + пара\n     Q♠️ Q♣️ Q♥️ 7♦️ 7♠️\n"
    "5. <b>Флеш</b> — 5 карт одной масти, не подряд\n     A♦️ J♦️ 8♦️ 6♦️ 2♦️\n"
    "6. <b>Стрит</b> — 5 карт подряд, масти любые\n     10♠️ 9♥️ 8♣️ 7♦️ 6♠️\n"
    "7. <b>Сет / тройка</b> — 3 карты одного достоинства\n     8♠️ 8♥️ 8♦️\n"
    "8. <b>Две пары</b>\n     J♠️ J♦️ 4♣️ 4♥️\n"
    "9. <b>Пара</b>\n     10♠️ 10♥️\n"
    "10. <b>Старшая карта</b> — ничего не собрано, решает старшая карта\n\n"
    "<i>Из 7 карт (2 ваши + 5 на столе) берутся 5 лучших. При равной комбинации побеждает "
    "старшая карта (кикер). Туз может быть и младшей картой в стрите: A-2-3-4-5.</i>"
)


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


async def build_text(st: dict) -> str:
    hand, board = st["hand"], st["board"]
    opp = st["active"] - 1
    stage = STAGES.get(len(board), "…")
    lines = [
        f"👥 За столом: <b>{st['players']}</b> · в раздаче: <b>{st['active']}</b> (соперников: {opp})",
        f"🎭 Соперники: {RANGE_NAMES[st['range']].lower()} — {RANGE_HINT[st['range']]}",
        f"🖐 Мои карты: {' '.join(map(card_label, hand)) or '—'}",
        f"🃏 Стол ({stage}): {' '.join(map(card_label, board)) or '—'}",
    ]
    res = await analyze(hand, board, opp, st["range"])
    if "type" in res:
        lines.append(f"Сейчас у вас: <b>{TYPE_RU[res['type']]}</b>")
    lines.append("")

    if len(hand) < 2:
        lines.append("👉 Отметьте свои 2 карты (🟢), затем карты стола (🔵).")
    elif "eq" not in res:
        lines.append("👉 Добавьте карты флопа (всего 3).")
    else:
        eq = res["eq"]
        lines.append(f"📊 Шанс выиграть банк: <b>{pct(eq)}</b>")
        lines.append(f"     победа {pct(res['win'])} · делёж {pct(res['tie'])}")

        o = res.get("outs")
        if o and o["count"]:
            parts = " · ".join(f"{TYPE_RU[t]} {n}" for t, n in sorted(o["by"].items(), key=lambda x: -x[1]))
            when = "к риверу" if len(board) == 3 else "на ривере"
            lines.append(f"🎯 Ауты: <b>{o['count']}</b> ({parts}) — собрать {when}: <b>{pct(o['hit'])}</b>")
        dist = res.get("dist") or {}
        if len(board) < 5 and dist:
            cur = engine.TYPE_IDX.get(res.get("type"), 0)
            better = [(t, p) for t, p in dist.items() if engine.TYPE_IDX[t] > cur and p >= 0.01]
            if better:
                better.sort(key=lambda x: -engine.TYPE_IDX[x[0]])
                lines.append("🔮 К риверу соберёте: " + " · ".join(f"{TYPE_RU[t]} {p * 100:.0f}%" for t, p in better))

        if st["pot"] and st["call"]:
            need = st["call"] / (st["pot"] + st["call"])
            lines.append(f"\n💰 Банк {fmt_num(st['pot'])}, доплатить {fmt_num(st['call'])} → "
                         f"нужно ≥ {pct(need)} шанса")
            if eq >= need:
                lines.append(f"✅ <b>КОЛЛ выгоден</b> (запас +{(eq - need) * 100:.1f}%)")
            else:
                lines.append(f"❌ <b>ФОЛД</b> — не хватает {(need - eq) * 100:.1f}%")
        elif eq < 1:
            lines.append(f"\n💰 Колл выгоден, если доплата ≤ <b>{eq / (1 - eq) * 100:.0f}%</b> от банка "
                         f"(банк вместе со ставкой соперника). Пришлите «банк ставка», например <code>300 100</code>, "
                         f"— скажу точно.")
        lines.append("<i>Расчёт по математике карт, без учёта стиля игры и будущих ставок.</i>")

    target = "мои карты" if st["mode"] == "hand" and len(hand) < 2 else "стол"
    lines.append(f"\nКарта добавится в: <b>{target}</b>. Нажатие на выбранную карту — убрать.")
    return "\n".join(lines)


async def show_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, st: dict) -> None:
    text, kb = await build_text(st), build_keyboard(st)
    q = update.callback_query
    if q:
        st["panel"] = q.message.message_id
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        return
    if st.get("panel") and update.message and not update.message.text.startswith("/"):
        try:  # ответ на текст — обновляем старую панель, а не плодим новые
            await context.bot.edit_message_text(text, chat_id=update.effective_chat.id, message_id=st["panel"],
                                                reply_markup=kb, parse_mode="HTML")
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    msg = await update.effective_message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    st["panel"] = msg.message_id


# ---------- обработчики ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главный вход — Mini App. Панель в чате доступна по /calc."""
    if not WEBAPP_URL:
        await cmd_calc(update, context)
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🃏 Открыть стол", web_app=WebAppInfo(WEBAPP_URL))]])
    await update.effective_message.reply_text(
        "♠️♥️ <b>Покерный калькулятор</b>\n\n"
        "Нажмите кнопку — откроется стол: отметьте свои карты и карты на столе, "
        "и я покажу шанс выиграть, ауты и совет «колл или фолд».\n\n"
        "<i>Стол также открывается кнопкой «Стол» слева от поля ввода. "
        "Справка по комбинациям — /combos, калькулятор прямо в чате — /calc.</i>",
        reply_markup=kb, parse_mode="HTML")


async def cmd_calc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = get_state(context)
    new_hand(st)
    st.pop("panel", None)
    await show_panel(update, context, st)


async def cmd_combos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(COMBOS_TEXT, parse_mode="HTML")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    st = get_state(context)
    data = q.data
    alert = None

    if data.startswith("c:"):
        alert = toggle_card(st, data[2:])
    elif data in ("m:hand", "m:board"):
        st["mode"] = data[2:]
    elif data in ("p:-", "p:+"):
        n = st["players"] + (1 if data == "p:+" else -1)
        if not MIN_PLAYERS <= n <= MAX_PLAYERS:
            alert = f"За столом от {MIN_PLAYERS} до {MAX_PLAYERS} игроков"
        else:
            # пока все в игре — счётчик раздачи идёт вместе со столом
            if st["active"] == st["players"] or st["active"] > n:
                st["active"] = n
            st["players"] = n
    elif data in ("a:-", "a:+"):
        n = st["active"] + (1 if data == "a:+" else -1)
        if n < 2:
            alert = "В раздаче минимум 2 игрока: вы и соперник"
        elif n > st["players"]:
            alert = "В раздаче не может быть больше игроков, чем за столом"
        else:
            st["active"] = n
    elif data.startswith("g:"):
        st["range"] = data[2:]
    elif data == "undo":
        undo(st)
    elif data == "new":
        new_hand(st)
    elif data == "combos":
        await q.answer()
        await q.message.reply_text(COMBOS_TEXT, parse_mode="HTML")
        return
    elif data == "pot":
        await q.answer()
        await q.message.reply_text(
            "💰 Пришлите два числа: <b>банк</b> (всё, что на столе, вместе со ставкой соперника) "
            "и <b>сколько вам доплатить</b>.\nНапример: <code>300 100</code>\n"
            "Чтобы сбросить — пришлите <code>0</code>.", parse_mode="HTML")
        return
    elif data.startswith("noop"):
        await q.answer("Игроков за столом" if data == "noop:p" else
                       "Сколько игроков ещё в раздаче (вместе с вами). Сбросил соперник — нажмите ➖")
        return

    await q.answer(alert or "", show_alert=bool(alert))
    if not alert:
        await show_panel(update, context, st)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = get_state(context)
    nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", update.message.text)]
    if len(nums) == 1 and nums[0] == 0:
        st["pot"] = st["call"] = None
    elif len(nums) >= 2 and nums[0] > 0 and nums[1] > 0:
        st["pot"], st["call"] = nums[0], nums[1]
    else:
        await update.message.reply_text(
            "Пришлите банк и доплату двумя числами, например <code>300 100</code>, "
            "или нажмите /calc для новой раздачи в чате, /start — открыть стол.", parse_mode="HTML")
        return
    try:
        await update.message.delete()
    except BadRequest:
        pass
    await show_panel(update, context, st)


# ---------- Mini App ----------

def check_init_data(init_data: str) -> bool:
    """Проверка подписи Telegram WebApp initData."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    return bool(received) and hmac.compare_digest(
        hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), received)


async def api_calc(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "bad json"}, status=400)
    if not check_init_data(str(body.get("initData", ""))):
        return web.json_response({"error": "Откройте калькулятор из Telegram"}, status=403)
    hand, board = body.get("hand") or [], body.get("board") or []
    cards = hand + board
    if (not all(isinstance(c, str) and c in engine.IDX for c in cards) or len(set(cards)) != len(cards)
            or len(hand) > 2 or len(board) > 5):
        return web.json_response({"error": "bad cards"}, status=400)
    try:
        active = int(body.get("active", 2))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad active"}, status=400)
    if not MIN_PLAYERS <= active <= MAX_PLAYERS:
        return web.json_response({"error": "bad active"}, status=400)
    rng = body.get("range")
    if rng not in engine.available_ranges():
        rng = "any"
    return web.json_response(await analyze(hand, board, active - 1, rng))


async def webapp_page(request: web.Request) -> web.Response:
    return web.FileResponse(BASE / "webapp" / "index.html", headers={"Cache-Control": "no-cache"})


async def api_config(request: web.Request) -> web.Response:
    return web.json_response({"ranges": engine.available_ranges()})


# ---------- запуск ----------

async def main() -> None:
    global POOL
    if not TOKEN:
        raise SystemExit("Укажите токен: export BOT_TOKEN='...'")
    POOL = ProcessPoolExecutor(max_workers=2)

    app = (Application.builder().token(TOKEN)
           .persistence(PicklePersistence(BASE / "state.pickle", update_interval=30)).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["calc", "new"], cmd_calc))
    app.add_handler(CommandHandler("combos", cmd_combos))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    web_app = web.Application(client_max_size=64 * 1024)
    web_app.router.add_get("/poker/", webapp_page)
    web_app.router.add_get("/poker", lambda r: web.HTTPFound("/poker/"))
    web_app.router.add_post("/poker/api/calc", api_calc)
    web_app.router.add_get("/poker/api/config", api_config)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", WEB_PORT).start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async with app:
        await app.bot.set_my_commands([BotCommand("start", "Открыть стол"), BotCommand("combos", "Покерные комбинации"),
                                        BotCommand("calc", "Калькулятор в чате")])
        if WEBAPP_URL:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("Стол", WebAppInfo(WEBAPP_URL)))
        await app.start()
        await app.updater.start_polling()
        log.info("Бот запущен, Mini App на порту %s, диапазоны: %s", WEB_PORT, engine.available_ranges())
        await stop.wait()
        await app.updater.stop()
        await app.stop()
    await runner.cleanup()
    POOL.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
