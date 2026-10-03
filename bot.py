import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, ChosenInlineResult, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent, Message,
)

TOKEN = os.environ["BOT_TOKEN"]  # токен от @BotFather
MAX_LEN = 4000
ALERT_LIMIT = 190   # лимит всплывающего окна Telegram = 200 символов
PM_TTL = 10         # через сколько секунд исчезает шепот в личке
RECENT_SHOWN = 5    # сколько недавних контактов показывать
RECENT_KEPT = 10    # сколько хранить
TZ = timezone(timedelta(hours=3))  # часовой пояс для времени прочтения (3 = Москва)
WHISPER_TTL = 24 * 3600  # через сколько секунд непрочитанные шепоты удаляются (сутки)

router = Router()
db = sqlite3.connect("whispers.db", check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, ts REAL)")
db.execute("CREATE INDEX IF NOT EXISTS ux ON users(username)")
db.execute("""CREATE TABLE IF NOT EXISTS w2(
    id TEXT PRIMARY KEY, owner INTEGER, targets TEXT, text TEXT,
    secure INTEGER DEFAULT 0, readers TEXT DEFAULT '[]',
    read_ids TEXT DEFAULT '[]', imid TEXT)""")
try:
    db.execute("ALTER TABLE w2 ADD COLUMN ts REAL")
except sqlite3.OperationalError:
    pass
try:
    db.execute("ALTER TABLE w2 ADD COLUMN draft INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
db.execute("""CREATE TABLE IF NOT EXISTS contacts(
    owner INTEGER, key TEXT, uid INTEGER, username TEXT, ts REAL,
    PRIMARY KEY(owner, key))""")
db.commit()

RECIP_RE = re.compile(r"^((?:\s*(?:@\w{3,32}|\d{5,}))+)\s*(.*)$", re.S)
_tasks = set()


# ---------- ЛЮДИ И КОНТАКТЫ ----------
def remember(user):
    """Запоминаем связку ID <-> username у каждого, кто писал боту."""
    db.execute("INSERT OR REPLACE INTO users(id,username,ts) VALUES(?,?,?)",
               (user.id, (user.username or "").lower() or None, time.time()))
    db.commit()


class Remember(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u:
            remember(u)
        return await handler(event, data)


def label(t) -> str:
    if t["id"]:
        r = db.execute("SELECT username FROM users WHERE id=?", (t["id"],)).fetchone()
        if r and r[0]:
            return f"@{r[0]}"
    return f"@{t['u']}" if t["u"] else f"ID {t['id']}"


def add_contact(owner, uid, username=None):
    """Запоминаем человека в истории owner (последние N)."""
    if uid == owner:
        return
    if uid and not username:
        r = db.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        username = r[0] if r else None
    if not uid and not username:
        return
    key = str(uid) if uid else username
    if uid and username:  # заменяем старую запись по юзернейму на запись по ID
        db.execute("DELETE FROM contacts WHERE owner=? AND key=?", (owner, username))
    db.execute("INSERT OR REPLACE INTO contacts(owner,key,uid,username,ts) VALUES(?,?,?,?,?)",
               (owner, key, uid, username, time.time()))
    db.execute("""DELETE FROM contacts WHERE owner=? AND key NOT IN
                  (SELECT key FROM contacts WHERE owner=? ORDER BY ts DESC LIMIT ?)""",
               (owner, owner, RECENT_KEPT))
    db.commit()


def recent(owner, n=RECENT_SHOWN):
    rows = db.execute("SELECT key,uid,username FROM contacts WHERE owner=? ORDER BY ts DESC LIMIT ?",
                      (owner, n)).fetchall()
    out = []
    for key, uid, un in rows:
        t = {"u": un, "id": uid}
        out.append({"key": key, "t": t, "name": label(t)})
    return out


# ---------- ШЕПОТЫ ----------
def parse(raw: str):
    m = RECIP_RE.match(raw.strip())
    if not m:
        return [], raw.strip()
    tokens = [t.lstrip("@").lower() for t in m.group(1).split()]
    return list(dict.fromkeys(tokens)), m.group(2).strip()


def make_targets(tokens):
    """username -> ID, если человек уже известен боту. Иначе привяжется при первом открытии."""
    out = []
    for t in tokens:
        if t.isdigit():
            out.append({"u": None, "id": int(t)})
        else:
            r = db.execute("SELECT id FROM users WHERE username=? ORDER BY ts DESC LIMIT 1", (t,)).fetchone()
            out.append({"u": t, "id": r[0] if r else None})
    return out


def save(owner, targets, text, secure=False, wid=None, draft=False):
    wid = wid or hashlib.sha1(os.urandom(16)).hexdigest()[:10]
    db.execute("INSERT OR IGNORE INTO w2(id,owner,targets,text,secure,ts,draft) VALUES(?,?,?,?,?,?,?)",
               (wid, owner, json.dumps(targets), text, int(secure), time.time(), int(draft)))
    db.commit()
    return wid


def get(wid):
    r = db.execute("SELECT owner,targets,text,secure,readers,read_ids,imid,ts FROM w2 WHERE id=?", (wid,)).fetchone()
    if not r:
        return None
    if not r[7] or r[7] < time.time() - WHISPER_TTL:  # просрочен — считаем удалённым
        db.execute("DELETE FROM w2 WHERE id=?", (wid,))
        db.commit()
        return None
    return {"id": wid, "owner": r[0], "targets": json.loads(r[1]), "text": r[2],
            "secure": bool(r[3]), "readers": json.loads(r[4]),
            "read_ids": json.loads(r[5]), "imid": r[6]}


def update(w):
    db.execute("UPDATE w2 SET targets=?, readers=?, read_ids=?, imid=? WHERE id=?",
               (json.dumps(w["targets"]), json.dumps(w["readers"], ensure_ascii=False),
                json.dumps(w["read_ids"]), w["imid"], w["id"]))
    db.commit()


def check(w, user) -> bool:
    """Доступ по ID. Если ID ещё не известен — привязываем по username при первом открытии."""
    if user.id == w["owner"]:
        return True
    un = (user.username or "").lower()
    for t in w["targets"]:
        if t["id"] == user.id:
            return True
        if t["id"] is None and un and t["u"] == un:
            t["id"] = user.id  # запомнили навсегда — смена юзернейма уже не страшна
            update(w)
            return True
    return False


def readers_text(w) -> str:
    lines = []
    for r in w["readers"]:
        if isinstance(r, dict):
            when = datetime.fromtimestamp(r["t"], TZ).strftime("%H:%M:%S, %d.%m")
            lines.append(f"• {html.escape(r['n'])} — {when}")
        else:  # записи из старых версий без времени
            lines.append(f"• {html.escape(str(r))}")
    return "\n".join(lines)


def chat_text(w) -> str:
    names = ", ".join(html.escape(label(t)) for t in w["targets"])
    who = "Только он может прочитать" if len(w["targets"]) == 1 else "Только они могут прочитать"
    s = f"🔒 <b>Шёпот для:</b> {names}\n<i>{who} этот шёпот.</i>"
    if w["secure"]:
        s += "\n🔐 <i>Защищённый: откроется только в личке и исчезнет.</i>"
    if w["readers"]:
        s += "\n\n👁 <b>Прочитали:</b>\n" + readers_text(w)
    return s


def read_kb(wid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👁 Прочитать", callback_data=f"r:{wid}")]])


NO_KB = InlineKeyboardMarkup(inline_keyboard=[])


# ---------- ЛИЧКА: отправка с самоудалением ----------
async def delete_later(bot, chat_id, msg_id):
    await asyncio.sleep(PM_TTL)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def send_private(bot, uid, text) -> bool:
    try:
        msg = await bot.send_message(
            uid, f"🔒 Шёпот (исчезнет через {PM_TTL} сек):\n\n{text}",
            protect_content=True)  # запрет пересылки/сохранения (+ блок скриншотов на многих клиентах)
    except Exception:
        return False
    t = asyncio.create_task(delete_later(bot, uid, msg.message_id))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return True


async def mark_read(bot, w, user):
    """Отмечаем прочтение. Если прочитали все — шепот удаляется, сообщение в чате заменяется."""
    if user.id == w["owner"]:
        return
    add_contact(w["owner"], user.id, (user.username or "").lower() or None)  # отправитель запомнил получателя
    add_contact(user.id, w["owner"])                                         # получатель запомнил отправителя
    if user.id not in w["read_ids"]:
        w["read_ids"].append(user.id)
        w["readers"].append({"n": user.full_name, "t": time.time()})
        update(w)
    done = all(t["id"] in w["read_ids"] for t in w["targets"])
    if w["imid"]:
        try:
            if done:
                names = ", ".join(html.escape(label(t)) for t in w["targets"])
                await bot.edit_message_text(
                    inline_message_id=w["imid"], parse_mode="HTML", reply_markup=NO_KB,
                    text=(f"🔥 <b>Шёпот прочитан всеми и удалён.</b>\n"
                          f"Для: {names}\n\n👁 <b>Прочитали:</b>\n{readers_text(w)}"))
            else:
                await bot.edit_message_text(
                    inline_message_id=w["imid"], text=chat_text(w),
                    parse_mode="HTML", reply_markup=read_kb(w["id"]))
        except Exception:
            pass
    if done:
        db.execute("DELETE FROM w2 WHERE id=?", (w["id"],))
        db.commit()


# ---------- INLINE ----------
def make_result(uid, raw, targets, text, secure, suffix=""):
    wid = hashlib.sha1(f"{uid}:{raw}:{suffix}:{int(time.time() // 600)}".encode()).hexdigest()[:10]
    save(uid, targets, text, secure, wid, draft=True)  # черновик, пока не отправлен
    w = get(wid)
    names = ", ".join(label(t) for t in w["targets"])
    return InlineQueryResultArticle(
        id=wid, title=f"{'🔐' if secure else '🔒'} Шепнуть: {names}",
        description=text[:80],
        input_message_content=InputTextMessageContent(
            message_text=chat_text(w), parse_mode="HTML"),
        reply_markup=read_kb(wid))


@router.inline_query()
async def inline(q: InlineQuery):
    raw = q.query.strip()
    uid = q.from_user.id

    m = re.fullmatch(r"w([0-9a-f]{10})", raw)  # длинный шепот из /create
    if m:
        w = get(m.group(1))
        if w and w["owner"] == uid:
            res = InlineQueryResultArticle(
                id=w["id"], title="🔒 Отправить шепот",
                description=f"Для: {', '.join(label(t) for t in w['targets'])}",
                input_message_content=InputTextMessageContent(
                    message_text=chat_text(w), parse_mode="HTML"),
                reply_markup=read_kb(w["id"]))
            return await q.answer([res], cache_time=0, is_personal=True)

    tokens, text = parse(raw)
    secure = text.startswith("!")
    if secure:
        text = text[1:].strip()

    hint = InlineQueryResultArticle(
        id="hint", title="Как шептать",
        description="@бот @user1 @user2 текст  •  или просто @бот текст — выберешь из недавних",
        input_message_content=InputTextMessageContent(
            message_text="Формат: @бот @user1 @user2 текст шепота"))

    if not text:
        return await q.answer([hint], cache_time=0, is_personal=True)

    if tokens:
        results = [make_result(uid, raw, make_targets(tokens), text, secure)]
    else:  # только текст — предлагаем недавних
        cs = recent(uid)
        if not cs:
            return await q.answer([hint], cache_time=0, is_personal=True)
        results = [make_result(uid, raw, [c["t"]], text, secure, c["key"]) for c in cs]
        if len(cs) >= 2:
            grp = cs[:3]
            results.append(make_result(uid, raw, [c["t"] for c in grp], text, secure, "grp"))
    await q.answer(results, cache_time=0, is_personal=True)


@router.chosen_inline_result()
async def chosen(r: ChosenInlineResult):
    """Работает, если в BotFather включён /setinlinefeedback — запоминаем получателей сразу при отправке."""
    w = get(r.result_id)
    if w and w["owner"] == r.from_user.id:
        db.execute("UPDATE w2 SET draft=0 WHERE id=?", (w["id"],))
        db.commit()
        for t in w["targets"]:
            add_contact(w["owner"], t["id"], t["u"])


# ---------- ЧТЕНИЕ ----------
@router.callback_query(F.data.startswith("r:"))
async def read(cb: CallbackQuery, bot: Bot):
    w = get(cb.data[2:])
    if not w:
        return await cb.answer("Шёпот не найден или уже удалён.", show_alert=True)
    if not check(w, cb.from_user):
        return await cb.answer("Этот шёпот не для тебя 🤫", show_alert=True)

    if cb.inline_message_id:
        db.execute("UPDATE w2 SET draft=0 WHERE id=?", (w["id"],))
        db.commit()
        if w["imid"] != cb.inline_message_id:
            w["imid"] = cb.inline_message_id
            update(w)

    text = w["text"]
    if not w["secure"] and len(text) <= ALERT_LIMIT:
        await cb.answer(text, show_alert=True)
        await mark_read(bot, w, cb.from_user)
    else:
        # длинный / защищённый — сразу открываем бота; шепот придёт в личку сам и отметится прочитанным
        me = await bot.get_me()
        await cb.answer(url=f"https://t.me/{me.username}?start=read_{w['id']}")


@router.message(CommandStart())
async def start(m: Message, command: CommandObject, bot: Bot):
    if command.args and command.args.startswith("read_"):
        w = get(command.args[5:])
        if not w or not check(w, m.from_user):
            return await m.answer("Этот шёпот не для тебя или уже удалён.")
        await send_private(bot, m.from_user.id, w["text"])
        try:
            await m.delete()  # убираем служебное «/start read_…», чтобы в личке остался только шепот
        except Exception:
            pass
        return await mark_read(bot, w, m.from_user)
    await m.answer(
        "Привет! Шёпоты сразу нескольким людям.\n\n"
        "Инлайн: <code>@бот @user1 @user2 текст</code>\n"
        "Только текст: <code>@бот текст</code> — бот предложит недавних людей\n"
        "Защищённый (только в личку): <code>@бот @user1 !текст</code>\n\n"
        "/create — длинный шёпот (до 4000 символов)\n"
        "/list — мои шёпоты\n/cancel — отмена", parse_mode="HTML")


# ---------- /create ----------
class Create(StatesGroup):
    targets = State()
    text = State()
    mode = State()


def contacts_kb(owner, sel):
    rows = []
    for c in recent(owner):
        mark = "✅" if c["key"] in sel else "☐"
        rows.append([InlineKeyboardButton(text=f"{mark} {c['name']}", callback_data=f"c:{c['key']}")])
    if sel:
        rows.append([InlineKeyboardButton(text=f"➡️ Готово ({len(sel)})", callback_data="cdone")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.message(Command("create"))
async def create(m: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Create.targets)
    await state.update_data(sel={})
    kb = contacts_kb(m.from_user.id, {})
    hint = "Выбери из недавних кнопками или пришли " if kb else "Пришли "
    await m.answer(f"Кому шепчем? {hint}@username / ID через пробел.", reply_markup=kb)


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.")


async def ask_text(msg: Message, state: FSMContext, targets):
    await state.update_data(targets=targets)
    await state.set_state(Create.text)
    await msg.answer(f"Кому: {', '.join(label(t) for t in targets)}\n"
                     f"Теперь текст шепота (до {MAX_LEN} символов).")


@router.callback_query(Create.targets, F.data.startswith("c:"))
async def toggle_contact(cb: CallbackQuery, state: FSMContext):
    key = cb.data[2:]
    data = await state.get_data()
    sel = data.get("sel", {})
    if key in sel:
        sel.pop(key)
    else:
        c = next((c for c in recent(cb.from_user.id) if c["key"] == key), None)
        if c:
            sel[key] = c["t"]
    await state.update_data(sel=sel)
    await cb.message.edit_reply_markup(reply_markup=contacts_kb(cb.from_user.id, sel))
    await cb.answer()


@router.callback_query(Create.targets, F.data == "cdone")
async def contacts_done(cb: CallbackQuery, state: FSMContext):
    sel = (await state.get_data()).get("sel", {})
    if not sel:
        return await cb.answer("Никого не выбрано", show_alert=True)
    await cb.answer()
    await ask_text(cb.message, state, list(sel.values()))


@router.message(Create.targets, F.text)
async def got_targets(m: Message, state: FSMContext):
    tokens, _ = parse(m.text + " ")
    sel = (await state.get_data()).get("sel", {})
    targets = list(sel.values()) + make_targets(tokens)
    if not targets:
        return await m.answer("Не вижу получателей. Пример: @user1 @user2 123456789")
    await ask_text(m, state, targets)


@router.message(Create.text, F.text)
async def got_text(m: Message, state: FSMContext):
    if len(m.text) > MAX_LEN:
        return await m.answer(f"Слишком длинно ({len(m.text)}). Максимум {MAX_LEN}.")
    await state.update_data(text=m.text)
    await state.set_state(Create.mode)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Обычный", callback_data="s:0")],
        [InlineKeyboardButton(text="🔐 Защищённый (только в личку)", callback_data="s:1")]])
    await m.answer("Какой режим?", reply_markup=kb)


@router.callback_query(Create.mode, F.data.startswith("s:"))
async def got_mode(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    wid = save(cb.from_user.id, data["targets"], data["text"], cb.data == "s:1")
    for t in data["targets"]:
        add_contact(cb.from_user.id, t["id"], t["u"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Отправить в чат", switch_inline_query=f"w{wid}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"d:{wid}")]])
    await cb.message.edit_text("Шёпот готов. Нажми кнопку и выбери чат 👇", reply_markup=kb)
    await cb.answer()


# ---------- /list ----------
@router.message(Command("list"))
async def list_(m: Message):
    rows = db.execute("SELECT id FROM w2 WHERE owner=? AND draft=0 ORDER BY rowid DESC LIMIT 20",
                      (m.from_user.id,)).fetchall()
    if not rows:
        return await m.answer("Пока пусто.")
    for (wid,) in rows:
        w = get(wid)
        if not w:
            continue
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📤 В чат", switch_inline_query=f"w{wid}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"d:{wid}")]])
        await m.answer(
            f"Для: {', '.join(label(t) for t in w['targets'])}\n"
            f"Прочитали: {len(w['read_ids'])}/{len(w['targets'])}\n{w['text'][:100]}",
            reply_markup=kb)


@router.callback_query(F.data.startswith("d:"))
async def delete(cb: CallbackQuery, bot: Bot):
    w = get(cb.data[2:])
    if w and w["owner"] == cb.from_user.id:
        db.execute("DELETE FROM w2 WHERE id=?", (w["id"],))
        db.commit()
        if w["imid"]:
            try:
                await bot.edit_message_text(inline_message_id=w["imid"],
                                            text="🗑 Шёпот удалён отправителем.", reply_markup=NO_KB)
            except Exception:
                pass
        await cb.answer("Удалено")
        await cb.message.edit_text("🗑 Удалено")
    else:
        await cb.answer("Нельзя или уже удалено", show_alert=True)


async def cleanup_loop(bot: Bot):
    """Раз в 10 минут удаляем шепоты старше суток (любые: из чата и из /create)."""
    while True:
        try:
            limit = time.time() - WHISPER_TTL
            db.execute("DELETE FROM w2 WHERE draft=1 AND ts < ?", (time.time() - 3600,))
            rows = db.execute("SELECT id, imid FROM w2 WHERE ts IS NULL OR ts < ?", (limit,)).fetchall()
            for wid, imid in rows:
                if imid:
                    try:
                        await bot.edit_message_text(inline_message_id=imid,
                                                    text="⌛ Шёпот истёк и удалён.", reply_markup=NO_KB)
                    except Exception:
                        pass
                db.execute("DELETE FROM w2 WHERE id=?", (wid,))
            db.commit()
        except Exception:
            logging.exception("cleanup failed")
        await asyncio.sleep(600)


async def main():
    logging.basicConfig(level=logging.INFO)
    for obs in (router.inline_query, router.callback_query, router.message):
        obs.outer_middleware(Remember())
    bot = Bot(TOKEN)
    me = await asyncio.wait_for(bot.get_me(), timeout=20)
    print(f"✅ Бот запущен: @{me.username}. Не закрывай окно. Стоп: Ctrl+C")
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(cleanup_loop(bot))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
