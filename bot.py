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
WHISPER_TTL = 24 * 3600  # через сколько секунд шепоты удаляются (сутки)

# ---------- ЯЗЫКИ (переводится только интерфейс, сам шепот — никогда) ----------
LANGS = ("ru", "uk", "en", "zh")

_S = {
    "hint_title": ("Как шептать", "Як шепотіти", "How to whisper", "如何发送悄悄话"),
    "hint_desc": (
        "@бот @user1 @user2 текст  •  или просто @бот текст — выберешь из недавних",
        "@бот @user1 @user2 текст  •  або просто @бот текст — обереш з нещодавніх",
        "@bot @user1 @user2 text  •  or just @bot text — pick from recent people",
        "@bot @user1 @user2 文字  •  或只输入 @bot 文字，从最近联系人中选择"),
    "hint_msg": ("Формат: @бот @user1 @user2 текст шепота", "Формат: @бот @user1 @user2 текст шепоту",
                 "Format: @bot @user1 @user2 whisper text", "格式：@bot @user1 @user2 悄悄话内容"),
    "res_title": ("Шепнуть: {names}", "Шепнути: {names}", "Whisper to: {names}", "悄悄话发给：{names}"),
    "send_title": ("Отправить шёпот", "Надіслати шепіт", "Send whisper", "发送悄悄话"),
    "send_desc": ("Для: {names}", "Для: {names}", "For: {names}", "收件人：{names}"),
    "whisper_for": ("Шёпот для:", "Шепіт для:", "Whisper for:", "悄悄话给："),
    "only_one": ("Только он может прочитать этот шёпот.", "Лише він може прочитати цей шепіт.",
                 "Only they can read this whisper.", "只有收件人能阅读这条悄悄话。"),
    "only_many": ("Только они могут прочитать этот шёпот.", "Лише вони можуть прочитати цей шепіт.",
                  "Only they can read this whisper.", "只有这些收件人能阅读这条悄悄话。"),
    "secure_note": ("Защищённый: откроется только в личке и исчезнет.",
                    "Захищений: відкриється лише в особистих і зникне.",
                    "Protected: opens only in private chat and then disappears.",
                    "受保护：仅在私聊中打开，之后消失。"),
    "read_word": ("Прочитать", "Прочитати", "Read", "阅读"),
    "readers": ("Прочитали:", "Прочитали:", "Read by:", "已阅读："),
    "all_read": ("Шёпот прочитан всеми и удалён.", "Шепіт прочитали всі, його видалено.",
                 "Everyone has read the whisper; it has been deleted.", "所有人都已阅读，悄悄话已删除。"),
    "for_": ("Для:", "Для:", "For:", "收件人："),
    "not_found": ("Шёпот не найден или уже удалён.", "Шепіт не знайдено або вже видалено.",
                  "Whisper not found or already deleted.", "未找到悄悄话，或已被删除。"),
    "not_for_you": ("Этот шёпот не для тебя 🤫", "Цей шепіт не для тебе 🤫",
                    "This whisper isn't for you 🤫", "这条悄悄话不是给你的 🤫"),
    "start_not_for": ("Этот шёпот не для тебя или уже удалён.", "Цей шепіт не для тебе або вже видалений.",
                      "This whisper isn't for you or has been deleted.", "这条悄悄话不是给你的，或已被删除。"),
    "pm_text": ("🔒 Шёпот (исчезнет через {ttl} сек):\n\n{text}", "🔒 Шепіт (зникне через {ttl} с):\n\n{text}",
                "🔒 Whisper (disappears in {ttl} sec):\n\n{text}", "🔒 悄悄话（{ttl} 秒后消失）：\n\n{text}"),
    "welcome": (
        "Привет! Шёпоты сразу нескольким людям.\n\n"
        "Инлайн: <code>@бот @user1 @user2 текст</code>\n"
        "Только текст: <code>@бот текст</code> — бот предложит недавних людей\n"
        "Защищённый (только в личку): <code>@бот @user1 !текст</code>\n\n"
        "/create — длинный шёпот (до 4000 символов)\n/list — мои шёпоты\n/cancel — отмена",
        "Привіт! Шепіт одразу кільком людям.\n\n"
        "Інлайн: <code>@бот @user1 @user2 текст</code>\n"
        "Лише текст: <code>@бот текст</code> — бот запропонує нещодавніх людей\n"
        "Захищений (лише в особисті): <code>@бот @user1 !текст</code>\n\n"
        "/create — довгий шепіт (до 4000 символів)\n/list — мої шепоти\n/cancel — скасування",
        "Hi! Whispers to several people at once.\n\n"
        "Inline: <code>@bot @user1 @user2 text</code>\n"
        "Text only: <code>@bot text</code> — I'll suggest recent people\n"
        "Protected (private chat only): <code>@bot @user1 !text</code>\n\n"
        "/create — long whisper (up to 4000 characters)\n/list — my whispers\n/cancel — cancel",
        "你好！可以同时给多个人发悄悄话。\n\n"
        "内联：<code>@bot @user1 @user2 文字</code>\n"
        "仅文字：<code>@bot 文字</code> —— 我会推荐最近联系人\n"
        "受保护（仅私聊）：<code>@bot @user1 !文字</code>\n\n"
        "/create —— 长悄悄话（最多 4000 字）\n/list —— 我的悄悄话\n/cancel —— 取消"),
    "ask_contacts": (
        "Кому шепчем? Выбери из недавних кнопками или пришли @username / ID через пробел.",
        "Кому шепочемо? Обери з нещодавніх кнопками або надішли @username / ID через пробіл.",
        "Who are we whispering to? Pick from recent people or send @username / ID separated by spaces.",
        "发给谁？从最近联系人中选择，或发送 @username / ID（用空格分隔）。"),
    "ask_plain": ("Кому шепчем? Пришли @username / ID через пробел.",
                  "Кому шепочемо? Надішли @username / ID через пробіл.",
                  "Who are we whispering to? Send @username / ID separated by spaces.",
                  "发给谁？请发送 @username / ID（用空格分隔）。"),
    "no_targets": ("Не вижу получателей. Пример: @user1 @user2 123456789",
                   "Не бачу отримувачів. Приклад: @user1 @user2 123456789",
                   "No recipients found. Example: @user1 @user2 123456789",
                   "没有找到收件人。示例：@user1 @user2 123456789"),
    "ask_text": ("Кому: {names}\nТеперь текст шепота (до {max} символов).",
                 "Кому: {names}\nТепер текст шепоту (до {max} символів).",
                 "To: {names}\nNow send the whisper text (up to {max} characters).",
                 "收件人：{names}\n请发送悄悄话内容（最多 {max} 字）。"),
    "too_long": ("Слишком длинно ({n}). Максимум {max}.", "Занадто довго ({n}). Максимум {max}.",
                 "Too long ({n}). Maximum is {max}.", "太长了（{n}）。最多 {max}。"),
    "mode_q": ("Какой режим?", "Який режим?", "Which mode?", "选择模式："),
    "mode_normal": ("💬 Обычный", "💬 Звичайний", "💬 Normal", "💬 普通"),
    "mode_secure": ("🔐 Защищённый (только в личку)", "🔐 Захищений (лише в особисті)",
                    "🔐 Protected (private chat only)", "🔐 受保护（仅私聊）"),
    "ready": ("Шёпот готов. Нажми кнопку и выбери чат 👇", "Шепіт готовий. Натисни кнопку й обери чат 👇",
              "Whisper is ready. Tap the button and choose a chat 👇", "悄悄话已就绪。点击按钮并选择聊天 👇"),
    "btn_send": ("📤 Отправить в чат", "📤 Надіслати в чат", "📤 Send to chat", "📤 发送到聊天"),
    "btn_to_chat": ("📤 В чат", "📤 У чат", "📤 To chat", "📤 发送"),
    "btn_delete": ("🗑 Удалить", "🗑 Видалити", "🗑 Delete", "🗑 删除"),
    "cancelled": ("Отменено.", "Скасовано.", "Cancelled.", "已取消。"),
    "none_selected": ("Никого не выбрано", "Нікого не обрано", "Nobody selected", "未选择任何人"),
    "done_btn": ("➡️ Готово ({n})", "➡️ Готово ({n})", "➡️ Done ({n})", "➡️ 完成（{n}）"),
    "list_empty": ("Пока пусто.", "Поки порожньо.", "Nothing here yet.", "暂无内容。"),
    "list_item": ("Для: {names}\nПрочитали: {r}/{t}\n{text}", "Для: {names}\nПрочитали: {r}/{t}\n{text}",
                  "For: {names}\nRead: {r}/{t}\n{text}", "收件人：{names}\n已阅读：{r}/{t}\n{text}"),
    "deleted": ("Удалено", "Видалено", "Deleted", "已删除"),
    "deleted_msg": ("🗑 Удалено", "🗑 Видалено", "🗑 Deleted", "🗑 已删除"),
    "deleted_by_owner": ("🗑 Шёпот удалён отправителем.", "🗑 Шепіт видалив відправник.",
                         "🗑 The whisper was deleted by the sender.", "🗑 悄悄话已被发送者删除。"),
    "cant_delete": ("Нельзя или уже удалено", "Не можна або вже видалено",
                    "Not allowed or already deleted", "无法删除或已被删除"),
    "expired": ("⌛ Шёпот истёк и удалён.", "⌛ Шепіт закінчився й видалений.",
                "⌛ The whisper expired and was deleted.", "⌛ 悄悄话已过期并删除。"),
}


def pick_lang(code) -> str:
    """Язык интерфейса Telegram у человека -> один из наших."""
    c = (code or "").lower()
    if c.startswith("uk"):
        return "uk"
    if c.startswith(("ru", "be")):
        return "ru"
    if c.startswith("zh"):
        return "zh"
    return "en"


def ul(user) -> str:
    return pick_lang(getattr(user, "language_code", None))


def tr(lang: str, key: str, **kw) -> str:
    s = _S[key][LANGS.index(lang) if lang in LANGS else 2]
    return s.format(**kw) if kw else s


# ---------- БАЗА ----------
router = Router()
db = sqlite3.connect("whispers.db", check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, ts REAL)")
try:
    db.execute("ALTER TABLE users ADD COLUMN lang TEXT")
except sqlite3.OperationalError:
    pass
db.execute("CREATE INDEX IF NOT EXISTS ux ON users(username)")
db.execute("""CREATE TABLE IF NOT EXISTS w2(
    id TEXT PRIMARY KEY, owner INTEGER, targets TEXT, text TEXT,
    secure INTEGER DEFAULT 0, readers TEXT DEFAULT '[]',
    read_ids TEXT DEFAULT '[]', imid TEXT)""")
for col in ("ts REAL", "draft INTEGER DEFAULT 0", "lang TEXT DEFAULT 'ru'"):
    try:
        db.execute(f"ALTER TABLE w2 ADD COLUMN {col}")
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
    code = getattr(user, "language_code", None)
    db.execute("""INSERT INTO users(id,username,ts,lang) VALUES(?,?,?,?)
                  ON CONFLICT(id) DO UPDATE SET username=excluded.username, ts=excluded.ts,
                  lang=COALESCE(excluded.lang, users.lang)""",
               (user.id, (user.username or "").lower() or None, time.time(),
                pick_lang(code) if code else None))
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


def save(owner, targets, text, secure=False, wid=None, draft=False, lang="ru"):
    wid = wid or hashlib.sha1(os.urandom(16)).hexdigest()[:10]
    db.execute("INSERT OR IGNORE INTO w2(id,owner,targets,text,secure,ts,draft,lang) VALUES(?,?,?,?,?,?,?,?)",
               (wid, owner, json.dumps(targets), text, int(secure), time.time(), int(draft), lang))
    db.commit()
    return wid


def get(wid):
    r = db.execute("SELECT owner,targets,text,secure,readers,read_ids,imid,ts,lang FROM w2 WHERE id=?",
                   (wid,)).fetchone()
    if not r:
        return None
    if not r[7] or r[7] < time.time() - WHISPER_TTL:  # просрочен — считаем удалённым
        db.execute("DELETE FROM w2 WHERE id=?", (wid,))
        db.commit()
        return None
    return {"id": wid, "owner": r[0], "targets": json.loads(r[1]), "text": r[2],
            "secure": bool(r[3]), "readers": json.loads(r[4]),
            "read_ids": json.loads(r[5]), "imid": r[6], "lang": r[8] or "ru"}


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


def target_lang(t, fallback):
    """Язык интерфейса получателя (известен, если он хоть раз взаимодействовал с ботом)."""
    row = None
    if t["id"]:
        row = db.execute("SELECT lang FROM users WHERE id=?", (t["id"],)).fetchone()
    elif t["u"]:
        row = db.execute("SELECT lang FROM users WHERE username=? ORDER BY ts DESC LIMIT 1", (t["u"],)).fetchone()
    return row[0] if row and row[0] else fallback


def chat_langs(targets, fallback):
    out = []
    for t in targets:
        lg = target_lang(t, fallback)
        if lg not in out:
            out.append(lg)
    return out or [fallback]


def wlangs(w):
    return chat_langs(w["targets"], w["lang"])


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
    """Сообщение в чате — на языке получателя (если у получателей разные языки — построчно на каждом)."""
    langs = wlangs(w)
    names = ", ".join(html.escape(label(t)) for t in w["targets"])
    title = " / ".join(tr(l, "whisper_for") for l in langs)
    key = "only_one" if len(w["targets"]) == 1 else "only_many"
    s = f"🔒 <b>{title}</b> {names}\n" + "\n".join(f"<i>{tr(l, key)}</i>" for l in langs)
    if w["secure"]:
        s += "\n" + "\n".join(f"🔐 <i>{tr(l, 'secure_note')}</i>" for l in langs)
    if w["readers"]:
        s += f"\n\n👁 <b>{' / '.join(tr(l, 'readers') for l in langs)}</b>\n{readers_text(w)}"
    return s


def read_kb(wid, langs):
    text = "👁 " + " / ".join(tr(l, "read_word") for l in langs)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=f"r:{wid}")]])


NO_KB = InlineKeyboardMarkup(inline_keyboard=[])


# ---------- ЛИЧКА: отправка с самоудалением ----------
async def delete_later(bot, chat_id, msg_id):
    await asyncio.sleep(PM_TTL)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def send_private(bot, uid, text, lang) -> bool:
    try:
        msg = await bot.send_message(
            uid, tr(lang, "pm_text", ttl=PM_TTL, text=text),
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
    langs = wlangs(w)
    if w["imid"]:
        try:
            if done:
                names = ", ".join(html.escape(label(t)) for t in w["targets"])
                head = "\n".join(f"🔥 <b>{tr(l, 'all_read')}</b>" for l in langs)
                await bot.edit_message_text(
                    inline_message_id=w["imid"], parse_mode="HTML", reply_markup=NO_KB,
                    text=(f"{head}\n{' / '.join(tr(l, 'for_') for l in langs)} {names}\n\n"
                          f"👁 <b>{' / '.join(tr(l, 'readers') for l in langs)}</b>\n{readers_text(w)}"))
            else:
                await bot.edit_message_text(
                    inline_message_id=w["imid"], text=chat_text(w),
                    parse_mode="HTML", reply_markup=read_kb(w["id"], langs))
        except Exception:
            pass
    if done:
        db.execute("DELETE FROM w2 WHERE id=?", (w["id"],))
        db.commit()


# ---------- INLINE ----------
def make_result(uid, lang, raw, targets, text, secure, suffix=""):
    wid = hashlib.sha1(f"{uid}:{raw}:{suffix}:{int(time.time() // 600)}".encode()).hexdigest()[:10]
    save(uid, targets, text, secure, wid, draft=True, lang=lang)  # черновик, пока не отправлен
    w = get(wid)
    names = ", ".join(label(t) for t in w["targets"])
    return InlineQueryResultArticle(
        id=wid, title=f"{'🔐' if secure else '🔒'} {tr(lang, 'res_title', names=names)}",
        description=text[:80],
        input_message_content=InputTextMessageContent(
            message_text=chat_text(w), parse_mode="HTML"),
        reply_markup=read_kb(wid, wlangs(w)))


@router.inline_query()
async def inline(q: InlineQuery):
    raw = q.query.strip()
    uid = q.from_user.id
    lang = ul(q.from_user)

    m = re.fullmatch(r"w([0-9a-f]{10})", raw)  # длинный шепот из /create
    if m:
        w = get(m.group(1))
        if w and w["owner"] == uid:
            res = InlineQueryResultArticle(
                id=w["id"], title=f"🔒 {tr(lang, 'send_title')}",
                description=tr(lang, "send_desc", names=", ".join(label(t) for t in w["targets"])),
                input_message_content=InputTextMessageContent(
                    message_text=chat_text(w), parse_mode="HTML"),
                reply_markup=read_kb(w["id"], wlangs(w)))
            return await q.answer([res], cache_time=0, is_personal=True)

    tokens, text = parse(raw)
    secure = text.startswith("!")
    if secure:
        text = text[1:].strip()

    hint = InlineQueryResultArticle(
        id="hint", title=tr(lang, "hint_title"), description=tr(lang, "hint_desc"),
        input_message_content=InputTextMessageContent(message_text=tr(lang, "hint_msg")))

    if not text:
        return await q.answer([hint], cache_time=0, is_personal=True)

    if tokens:
        results = [make_result(uid, lang, raw, make_targets(tokens), text, secure)]
    else:  # только текст — предлагаем недавних
        cs = recent(uid)
        if not cs:
            return await q.answer([hint], cache_time=0, is_personal=True)
        results = [make_result(uid, lang, raw, [c["t"]], text, secure, c["key"]) for c in cs]
        if len(cs) >= 2:
            grp = cs[:3]
            results.append(make_result(uid, lang, raw, [c["t"] for c in grp], text, secure, "grp"))
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
    lang = ul(cb.from_user)  # интерфейс — на языке того, кто читает
    w = get(cb.data[2:])
    if not w:
        return await cb.answer(tr(lang, "not_found"), show_alert=True)
    if not check(w, cb.from_user):
        return await cb.answer(tr(lang, "not_for_you"), show_alert=True)

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
    lang = ul(m.from_user)
    if command.args and command.args.startswith("read_"):
        w = get(command.args[5:])
        if not w or not check(w, m.from_user):
            return await m.answer(tr(lang, "start_not_for"))
        await send_private(bot, m.from_user.id, w["text"], lang)
        try:
            await m.delete()  # убираем служебное «/start read_…», чтобы в личке остался только шепот
        except Exception:
            pass
        return await mark_read(bot, w, m.from_user)
    await m.answer(tr(lang, "welcome"), parse_mode="HTML")


# ---------- /create ----------
class Create(StatesGroup):
    targets = State()
    text = State()
    mode = State()


def contacts_kb(owner, sel, lang):
    rows = []
    for c in recent(owner):
        mark = "✅" if c["key"] in sel else "☐"
        rows.append([InlineKeyboardButton(text=f"{mark} {c['name']}", callback_data=f"c:{c['key']}")])
    if sel:
        rows.append([InlineKeyboardButton(text=tr(lang, "done_btn", n=len(sel)), callback_data="cdone")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.message(Command("create"))
async def create(m: Message, state: FSMContext):
    lang = ul(m.from_user)
    await state.clear()
    await state.set_state(Create.targets)
    await state.update_data(sel={})
    kb = contacts_kb(m.from_user.id, {}, lang)
    await m.answer(tr(lang, "ask_contacts" if kb else "ask_plain"), reply_markup=kb)


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(tr(ul(m.from_user), "cancelled"))


async def ask_text(msg: Message, state: FSMContext, targets, lang):
    await state.update_data(targets=targets)
    await state.set_state(Create.text)
    await msg.answer(tr(lang, "ask_text", names=", ".join(label(t) for t in targets), max=MAX_LEN))


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
    await cb.message.edit_reply_markup(reply_markup=contacts_kb(cb.from_user.id, sel, ul(cb.from_user)))
    await cb.answer()


@router.callback_query(Create.targets, F.data == "cdone")
async def contacts_done(cb: CallbackQuery, state: FSMContext):
    lang = ul(cb.from_user)
    sel = (await state.get_data()).get("sel", {})
    if not sel:
        return await cb.answer(tr(lang, "none_selected"), show_alert=True)
    await cb.answer()
    await ask_text(cb.message, state, list(sel.values()), lang)


@router.message(Create.targets, F.text)
async def got_targets(m: Message, state: FSMContext):
    lang = ul(m.from_user)
    tokens, _ = parse(m.text + " ")
    sel = (await state.get_data()).get("sel", {})
    targets = list(sel.values()) + make_targets(tokens)
    if not targets:
        return await m.answer(tr(lang, "no_targets"))
    await ask_text(m, state, targets, lang)


@router.message(Create.text, F.text)
async def got_text(m: Message, state: FSMContext):
    lang = ul(m.from_user)
    if len(m.text) > MAX_LEN:
        return await m.answer(tr(lang, "too_long", n=len(m.text), max=MAX_LEN))
    await state.update_data(text=m.text)
    await state.set_state(Create.mode)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr(lang, "mode_normal"), callback_data="s:0")],
        [InlineKeyboardButton(text=tr(lang, "mode_secure"), callback_data="s:1")]])
    await m.answer(tr(lang, "mode_q"), reply_markup=kb)


@router.callback_query(Create.mode, F.data.startswith("s:"))
async def got_mode(cb: CallbackQuery, state: FSMContext):
    lang = ul(cb.from_user)
    data = await state.get_data()
    await state.clear()
    wid = save(cb.from_user.id, data["targets"], data["text"], cb.data == "s:1", lang=lang)
    for t in data["targets"]:
        add_contact(cb.from_user.id, t["id"], t["u"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=tr(lang, "btn_send"), switch_inline_query=f"w{wid}")],
        [InlineKeyboardButton(text=tr(lang, "btn_delete"), callback_data=f"d:{wid}")]])
    await cb.message.edit_text(tr(lang, "ready"), reply_markup=kb)
    await cb.answer()


# ---------- /list ----------
@router.message(Command("list"))
async def list_(m: Message):
    lang = ul(m.from_user)
    rows = db.execute("SELECT id FROM w2 WHERE owner=? AND draft=0 ORDER BY rowid DESC LIMIT 20",
                      (m.from_user.id,)).fetchall()
    shown = 0
    for (wid,) in rows:
        w = get(wid)
        if not w:
            continue
        shown += 1
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=tr(lang, "btn_to_chat"), switch_inline_query=f"w{wid}"),
            InlineKeyboardButton(text=tr(lang, "btn_delete"), callback_data=f"d:{wid}")]])
        await m.answer(
            tr(lang, "list_item", names=", ".join(label(t) for t in w["targets"]),
               r=len(w["read_ids"]), t=len(w["targets"]), text=w["text"][:100]),
            reply_markup=kb)
    if not shown:
        await m.answer(tr(lang, "list_empty"))


@router.callback_query(F.data.startswith("d:"))
async def delete(cb: CallbackQuery, bot: Bot):
    lang = ul(cb.from_user)
    w = get(cb.data[2:])
    if w and w["owner"] == cb.from_user.id:
        db.execute("DELETE FROM w2 WHERE id=?", (w["id"],))
        db.commit()
        if w["imid"]:
            try:
                await bot.edit_message_text(inline_message_id=w["imid"],
                                            text="\n".join(tr(l, "deleted_by_owner") for l in wlangs(w)),
                                            reply_markup=NO_KB)
            except Exception:
                pass
        await cb.answer(tr(lang, "deleted"))
        await cb.message.edit_text(tr(lang, "deleted_msg"))
    else:
        await cb.answer(tr(lang, "cant_delete"), show_alert=True)


async def cleanup_loop(bot: Bot):
    """Раз в 10 минут удаляем шепоты старше суток (любые: из чата и из /create) и старые черновики."""
    while True:
        try:
            limit = time.time() - WHISPER_TTL
            db.execute("DELETE FROM w2 WHERE draft=1 AND ts < ?", (time.time() - 3600,))
            rows = db.execute("SELECT id, imid, lang, targets FROM w2 WHERE ts IS NULL OR ts < ?",
                              (limit,)).fetchall()
            for wid, imid, wlang, tjson in rows:
                if imid:
                    try:
                        langs = chat_langs(json.loads(tjson), wlang or "ru")
                        await bot.edit_message_text(
                            inline_message_id=imid,
                            text="\n".join(tr(l, "expired") for l in langs), reply_markup=NO_KB)
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
