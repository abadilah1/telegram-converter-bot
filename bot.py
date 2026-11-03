"""Telegram bot: Word <-> PDF converter (friendly, guided version).

Word -> PDF uses headless LibreOffice. PDF -> Word uses pdf2docx.
Runs in webhook mode when WEBHOOK_URL / RENDER_EXTERNAL_URL is set (hosting),
otherwise polling (local testing).
"""

import asyncio
import csv
import html
import io
import logging
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("converter-bot")

TOKEN = os.environ["BOT_TOKEN"]
MAX_BYTES = 20 * 1024 * 1024  # Telegram Bot API download limit
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "8"))
CONVERT_TIMEOUT = int(os.getenv("CONVERT_TIMEOUT", "120"))
BRAND = os.getenv("BRAND_NAME", "©A.B Digital Solutions")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

WORD_EXTS = {".doc", ".docx", ".odt", ".rtf"}
PDF_EXT = ".pdf"

BTN_WORD = "📄 Word → PDF"
BTN_PDF = "📝 PDF → Word"
BTN_HELP = "❓ Help"

_jobs = asyncio.Semaphore(MAX_CONCURRENT)
_last_request: dict[int, float] = {}


class UserError(Exception):
    """An error whose message is safe and helpful to show to the user."""


# --------------------------------------------------------------------------
# Keyboards and texts
# --------------------------------------------------------------------------

def reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_WORD, BTN_PDF], [BTN_HELP]],
        resize_keyboard=True,
        input_field_placeholder="Send a Word or PDF file here…",
    )


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📄 Word → PDF", callback_data="mode_word"),
                InlineKeyboardButton("📝 PDF → Word", callback_data="mode_pdf"),
            ],
            [
                InlineKeyboardButton("❓ How it works", callback_data="how"),
                InlineKeyboardButton("💡 Tips & limits", callback_data="limits"),
            ],
        ]
    )


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to menu", callback_data="menu")]]
    )


def after_done_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Convert another file", callback_data="menu")],
            [InlineKeyboardButton("❓ Help", callback_data="how")],
        ]
    )


WELCOME = (
    "👋 <b>Hi {name}, welcome!</b>\n\n"
    "I convert documents in seconds, completely free:\n\n"
    "📄 <b>Word → PDF</b>\n"
    "📝 <b>PDF → Word</b>\n\n"
    "<b>Just send me your file</b> and I'll detect the type and convert it. "
    "Or tap a button below to get started 👇"
)

HOW_IT_WORKS = (
    "❓ <b>How it works</b>\n\n"
    "1️⃣ Tap the 📎 <b>paperclip</b> icon in the chat.\n"
    "2️⃣ Choose <b>File</b> (or <b>Document</b>), not Photo or Gallery.\n"
    "3️⃣ Pick your Word or PDF file and send it.\n"
    "4️⃣ Wait a few seconds. I'll send the converted file back.\n\n"
    "I automatically detect the direction:\n"
    "• Word file (.doc, .docx, .odt, .rtf) → PDF\n"
    "• PDF file → Word (.docx)"
)

LIMITS = (
    "💡 <b>Tips &amp; limits</b>\n\n"
    "• Maximum file size: <b>20 MB</b>\n"
    "• PDFs must contain <b>real, selectable text</b>. Scanned pages "
    "(photos of paper) can't be converted.\n"
    "• Password-protected files aren't supported. Remove the password first.\n"
    "• One file at a time. Please wait a few seconds between files.\n"
    "• Complex PDFs (many columns, tables, graphics) may not look perfect in Word.\n"
    "• Your files are deleted from my server right after conversion. 🔒"
)

MODE_WORD = (
    "📄 <b>Word → PDF</b>\n\n"
    "Great! Now send me your Word file as a <b>document</b> "
    "(.doc, .docx, .odt or .rtf) and I'll turn it into a PDF.\n\n"
    "<i>Tip: tap 📎, then choose File.</i>"
)

MODE_PDF = (
    "📝 <b>PDF → Word</b>\n\n"
    "Great! Now send me your PDF as a <b>document</b> and I'll turn it into "
    "an editable Word (.docx) file.\n\n"
    "<i>Tip: the PDF must contain real text, not scanned images.</i>"
)


async def send(update: Update, text: str, markup=None) -> None:
    await update.effective_chat.send_message(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        disable_web_page_preview=True,
    )


# --------------------------------------------------------------------------
# Subscriber tracking (Upstash Redis, free tier). Optional: if the two
# UPSTASH_* variables are not set, the bot works normally without tracking.
# --------------------------------------------------------------------------

async def redis(*commands):
    """Run Redis commands in one request. Returns a list of results or None."""
    if not (REDIS_URL and REDIS_TOKEN):
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                f"{REDIS_URL}/pipeline",
                headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
                json=[[str(part) for part in cmd] for cmd in commands],
            )
            resp.raise_for_status()
            return [item.get("result") for item in resp.json()]
    except Exception:  # noqa: BLE001
        log.warning("Redis request failed", exc_info=True)
        return None


def _day(offset: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Record every user who messages the bot; alert the admin on new users."""
    user = update.effective_user
    if not user or user.is_bot:
        return
    uid = user.id
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    res = await redis(
        ["SADD", "users", uid],
        ["SADD", f"active:{_day()}", uid],
        ["EXPIRE", f"active:{_day()}", 40 * 86400],
        ["HSETNX", f"user:{uid}", "first_seen", now],
        ["HSET", f"user:{uid}", "name", user.full_name or "", "username",
         user.username or "", "last_seen", now],
        ["SREM", "blocked", uid],
    )
    if res and res[0] == 1 and ADMIN_ID:
        total = (await redis(["SCARD", "users"]) or [None])[0]
        who = html.escape(user.full_name or "Someone")
        handle = f" (@{html.escape(user.username)})" if user.username else ""
        try:
            await context.bot.send_message(
                ADMIN_ID,
                f"🆕 New subscriber: <b>{who}</b>{handle}\nTotal users: <b>{total}</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:  # noqa: BLE001
            pass


async def on_block_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark users who block or unblock the bot."""
    change = update.my_chat_member
    if not change or change.chat.type != "private":
        return
    uid = change.chat.id
    if change.new_chat_member.status in ("kicked", "left"):
        await redis(["SADD", "blocked", uid])
    else:
        await redis(["SREM", "blocked", uid])


async def count_conversion(kind: str) -> None:
    await redis(["INCR", f"stat:{kind}"], ["INCR", f"stat:{kind}:{_day()}"])


def _is_admin(update: Update) -> bool:
    return bool(ADMIN_ID) and update.effective_user.id == ADMIN_ID


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        return
    if not (REDIS_URL and REDIS_TOKEN):
        await send(update, "⚠️ Tracking isn't set up yet. Add the Upstash variables on Render.")
        return
    week_keys = [f"active:{_day(i)}" for i in range(7)]
    res = await redis(
        ["SCARD", "users"],
        ["SCARD", f"active:{_day()}"],
        ["SUNION", *week_keys],
        ["SCARD", "blocked"],
        ["GET", "stat:word2pdf"],
        ["GET", "stat:pdf2word"],
        ["GET", f"stat:word2pdf:{_day()}"],
        ["GET", f"stat:pdf2word:{_day()}"],
    )
    if not res:
        await send(update, "⚠️ Couldn't reach the database. Try again shortly.")
        return
    total, today, week, blocked, w2p, p2w, w2p_t, p2w_t = res
    i = lambda v: int(v or 0)  # noqa: E731
    await send(
        update,
        "📊 <b>Bot statistics</b>\n\n"
        f"👥 Total users: <b>{i(total)}</b>\n"
        f"🟢 Active today: <b>{i(today)}</b>\n"
        f"📅 Active last 7 days: <b>{len(week or [])}</b>\n"
        f"🚫 Blocked the bot: <b>{i(blocked)}</b>\n\n"
        f"📄 Word → PDF: <b>{i(w2p)}</b> (today {i(w2p_t)})\n"
        f"📝 PDF → Word: <b>{i(p2w)}</b> (today {i(p2w_t)})",
    )


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the admin a CSV of all subscribers."""
    if not _is_admin(update):
        return
    ids = ((await redis(["SMEMBERS", "users"])) or [[]])[0]
    if not ids:
        await send(update, "No users recorded yet.")
        return
    rows = []
    for n in range(0, len(ids), 100):
        chunk = ids[n:n + 100]
        res = await redis(*[["HGETALL", f"user:{u}"] for u in chunk]) or []
        for uid, flat in zip(chunk, res):
            d = dict(zip(flat[::2], flat[1::2])) if flat else {}
            rows.append([uid, d.get("name", ""), d.get("username", ""),
                         d.get("first_seen", ""), d.get("last_seen", "")])
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["telegram_id", "name", "username", "first_seen", "last_seen"])
    writer.writerows(rows)
    await update.effective_chat.send_document(
        document=io.BytesIO(buf.getvalue().encode("utf-8-sig")),
        filename="subscribers.csv",
        caption=f"👥 {len(rows)} subscribers",
    )


_bg_tasks: set = set()


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /broadcast <text>, or reply to any message with /broadcast."""
    if not _is_admin(update):
        return
    msg = update.message
    if context.bot_data.get("bc_running"):
        await send(update, "⏳ A broadcast is already running. Please wait for it to finish.")
        return

    reply = msg.reply_to_message
    parts = (msg.text or "").split(None, 1)
    if reply:
        pending = {"mode": "copy", "chat_id": reply.chat_id, "message_id": reply.message_id}
    elif len(parts) == 2:
        pending = {"mode": "text", "text": parts[1]}
    else:
        await send(
            update,
            "📣 <b>Broadcast</b>\n\n"
            "• Send <code>/broadcast Your message here</code>\n"
            "• Or <b>reply</b> to any message (text, photo, file…) with "
            "<code>/broadcast</code> to send it exactly as it is, with formatting.\n\n"
            "You'll get a preview and a confirmation button before anything is sent.",
        )
        return

    res = await redis(["SMEMBERS", "users"], ["SMEMBERS", "blocked"])
    if not res:
        await send(update, "⚠️ Tracking isn't set up or the database is unreachable.")
        return
    blocked = set(res[1] or [])
    targets = [u for u in (res[0] or []) if u not in blocked]
    if not targets:
        await send(update, "No active subscribers to message yet.")
        return
    pending["targets"] = targets
    context.bot_data["bc_pending"] = pending

    await update.effective_chat.send_message("👀 <b>Preview</b> (this is what users will receive):",
                                             parse_mode=ParseMode.HTML)
    if pending["mode"] == "copy":
        await context.bot.copy_message(msg.chat_id, pending["chat_id"], pending["message_id"])
    else:
        await update.effective_chat.send_message(pending["text"])
    await send(
        update,
        f"Send this to <b>{len(targets)}</b> subscribers?",
        InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(f"✅ Send to {len(targets)}", callback_data="bc_yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="bc_no"),
            ]]
        ),
    )


async def _deliver(bot, uid: str, pending: dict) -> str:
    """Send one broadcast message. Returns 'ok', 'blocked' or 'failed'."""
    for attempt in range(2):
        try:
            if pending["mode"] == "copy":
                await bot.copy_message(int(uid), pending["chat_id"], pending["message_id"])
            else:
                await bot.send_message(int(uid), pending["text"])
            return "ok"
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
        except Forbidden:
            return "blocked"
        except Exception:  # noqa: BLE001
            return "failed"
    return "failed"


async def _run_broadcast(app: Application, admin_chat: int, pending: dict) -> None:
    app.bot_data["bc_running"] = True
    sent = failed = 0
    newly_blocked = []
    try:
        for uid in pending["targets"]:
            outcome = await _deliver(app.bot, uid, pending)
            if outcome == "ok":
                sent += 1
            else:
                failed += 1
                if outcome == "blocked":
                    newly_blocked.append(uid)
            await asyncio.sleep(0.05)  # stay well under Telegram's rate limit
        if newly_blocked:
            await redis(*[["SADD", "blocked", u] for u in newly_blocked])
        await app.bot.send_message(
            admin_chat,
            f"📣 <b>Broadcast finished</b>\n\n✅ Delivered: <b>{sent}</b>\n"
            f"🚫 Failed / blocked: <b>{failed}</b>",
            parse_mode=ParseMode.HTML,
        )
    finally:
        app.bot_data["bc_running"] = False


async def on_broadcast_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_admin(update):
        await query.answer()
        return
    pending = context.bot_data.pop("bc_pending", None)
    if query.data == "bc_no" or not pending:
        await query.answer("Cancelled" if query.data == "bc_no" else "Nothing pending")
        await query.edit_message_text("❌ Broadcast cancelled.")
        return
    if context.bot_data.get("bc_running"):
        await query.answer("Already running")
        return
    await query.answer("Sending…")
    await query.edit_message_text(
        f"📤 Sending to {len(pending['targets'])} subscribers… "
        "I'll message you when it's done."
    )
    task = asyncio.create_task(_run_broadcast(context.application, query.message.chat_id, pending))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# --------------------------------------------------------------------------
# Commands, buttons and menus
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = html.escape(update.effective_user.first_name or "there")
    # Show the persistent bottom keyboard first, then the inline menu.
    await update.effective_chat.send_message(
        "👇 Use the buttons at the bottom anytime.", reply_markup=reply_keyboard()
    )
    await send(update, WELCOME.format(name=name), main_menu())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send(update, HOW_IT_WORKS + "\n\n" + LIMITS, main_menu())


async def mode_word_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send(update, MODE_WORD, back_menu())


async def mode_pdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send(update, MODE_PDF, back_menu())


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    name = html.escape(update.effective_user.first_name or "there")

    screens = {
        "menu": (WELCOME.format(name=name), main_menu()),
        "how": (HOW_IT_WORKS, back_menu()),
        "limits": (LIMITS, back_menu()),
        "mode_word": (MODE_WORD, back_menu()),
        "mode_pdf": (MODE_PDF, back_menu()),
    }
    if data not in screens:
        return
    text, markup = screens[data]
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception:  # noqa: BLE001  (e.g. message is unchanged or too old)
        await send(update, text, markup)


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg.photo:
        text = (
            "📷 That looks like a photo. I can only convert <b>files</b>.\n\n"
            "Please send your Word or PDF as a <b>document</b>: tap 📎 and "
            "choose <b>File</b>, not Gallery."
        )
    else:
        text = (
            "🤔 I didn't understand that. I work with files!\n\n"
            "Send me a <b>Word</b> or <b>PDF</b> file, or tap a button below."
        )
    await send(update, text, main_menu())


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------

async def word_to_pdf(src: Path, outdir: Path) -> Path:
    profile = Path(tempfile.gettempdir()) / f"lo_{uuid.uuid4().hex}"
    cmd = [
        "soffice",
        f"-env:UserInstallation=file://{profile}",
        "--headless",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(outdir),
        str(src),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=CONVERT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise UserError(
            "⏱ That took too long to convert. Try a smaller or simpler file."
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    out = outdir / (src.stem + ".pdf")
    if proc.returncode != 0 or not out.exists():
        log.error("LibreOffice failed: %s", err.decode(errors="ignore")[:300])
        raise UserError(
            "😕 I couldn't open that file. It may be corrupted or "
            "password-protected. Try re-saving it as .docx and send it again."
        )
    return out


def _pdf_to_docx_sync(src: Path, out: Path) -> None:
    import fitz  # PyMuPDF, installed together with pdf2docx
    from pdf2docx import Converter

    with fitz.open(str(src)) as pdf:
        if pdf.needs_pass:
            raise UserError(
                "🔒 This PDF is password-protected. Remove the password and "
                "send it again."
            )
        if pdf.page_count == 0:
            raise UserError("😕 This PDF has no pages.")
        if not any(page.get_text().strip() for page in pdf):
            raise UserError(
                "🖼 This PDF looks like a <b>scan</b> (images only, no real text), "
                "so I can't convert it to an editable Word file.\n\n"
                "Try a PDF that was saved directly from a document."
            )

    cv = Converter(str(src))
    try:
        cv.convert(str(out))
    finally:
        cv.close()


async def pdf_to_word(src: Path, outdir: Path) -> Path:
    out = outdir / (src.stem + ".docx")
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_pdf_to_docx_sync, src, out), timeout=CONVERT_TIMEOUT
        )
    except asyncio.TimeoutError:
        raise UserError(
            "⏱ That took too long to convert. Try a smaller or simpler PDF."
        )
    if not out.exists():
        raise UserError("😕 I couldn't convert that PDF. It may be damaged.")
    return out


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    doc = msg.document
    user_id = update.effective_user.id
    name = doc.file_name or "file"
    ext = Path(name).suffix.lower()

    if ext not in WORD_EXTS and ext != PDF_EXT:
        shown = html.escape(ext) if ext else "this type of file"
        await send(
            update,
            f"❌ Sorry, I can't convert <b>{shown}</b> files.\n\n"
            "I support:\n"
            "• Word: .doc, .docx, .odt, .rtf\n"
            "• PDF: .pdf",
            main_menu(),
        )
        return
    if doc.file_size and doc.file_size > MAX_BYTES:
        await send(
            update,
            "❌ That file is too big. The maximum size is <b>20 MB</b>.\n"
            "Try compressing it or splitting it into parts.",
            back_menu(),
        )
        return

    now = time.monotonic()
    wait = COOLDOWN_SECONDS - (now - _last_request.get(user_id, 0))
    if wait > 0:
        await send(
            update, f"⏳ Easy there! Please wait <b>{int(wait) + 1}s</b> and send it again."
        )
        return
    _last_request[user_id] = now

    to_pdf = ext != PDF_EXT
    label = "PDF" if to_pdf else "Word"
    safe_name = html.escape(name)
    status = await msg.reply_text(
        f"📥 Got <b>{safe_name}</b>!\nConverting to {label}…",
        parse_mode=ParseMode.HTML,
    )
    workdir = Path(tempfile.mkdtemp(prefix="conv_"))
    try:
        async with _jobs:
            await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_DOCUMENT)
            tg_file = await doc.get_file()
            # Neutral local name avoids odd characters breaking the converters.
            src = workdir / f"input{ext}"
            await tg_file.download_to_drive(custom_path=src)

            if to_pdf:
                result = await word_to_pdf(src, workdir)
                out_name = Path(name).stem + ".pdf"
                await count_conversion("word2pdf")
            else:
                result = await pdf_to_word(src, workdir)
                out_name = Path(name).stem + ".docx"
                await count_conversion("pdf2word")

        await status.edit_text("📤 Almost done, sending your file…")
        with open(result, "rb") as fh:
            await msg.reply_document(
                document=fh,
                filename=out_name,
                caption=f"✅ <b>Done!</b> Your {label} file is ready.\n\n{html.escape(BRAND)}",
                parse_mode=ParseMode.HTML,
            )
        await status.delete()
        await send(
            update,
            f"What would you like to do next?\n\n<i>{html.escape(BRAND)}</i>",
            after_done_menu(),
        )
    except UserError as exc:
        await status.edit_text(
            f"{exc}", parse_mode=ParseMode.HTML, reply_markup=back_menu()
        )
    except Exception:  # noqa: BLE001
        log.exception("Conversion failed for user %s", user_id)
        await status.edit_text(
            "😕 Something went wrong while converting your file.\n\n"
            "Please check that it isn't corrupted or password-protected, then "
            "try again. If it keeps failing, try a different file.",
            reply_markup=back_menu(),
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    """Set the command menu and the intro text shown before /start."""
    try:
        await app.bot.set_my_commands(
            [
                ("start", "Open the main menu"),
                ("word2pdf", "Convert Word to PDF"),
                ("pdf2word", "Convert PDF to Word"),
                ("help", "How to use this bot"),
            ]
        )
        await app.bot.set_my_description(
            "Convert Word to PDF and PDF to Word in seconds, free. "
            "Tap START, then send me your file."
        )
        await app.bot.set_my_short_description(
            "Free Word ⇄ PDF converter. Send a file to begin."
        )
    except Exception:  # noqa: BLE001
        log.exception("Could not set bot menu/description (non-fatal)")


def main() -> None:
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    app.add_handler(MessageHandler(filters.ALL, track, block=False), group=-1)
    app.add_handler(ChatMemberHandler(on_block_change, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CallbackQueryHandler(on_broadcast_button, pattern="^bc_"))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("word2pdf", mode_word_cmd))
    app.add_handler(CommandHandler("pdf2word", mode_pdf_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Text([BTN_WORD]), mode_word_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_PDF]), mode_pdf_cmd))
    app.add_handler(MessageHandler(filters.Text([BTN_HELP]), help_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, fallback))

    base_url = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if base_url:
        port = int(os.getenv("PORT", "10000"))
        log.info("Starting in webhook mode on port %s", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=f"{base_url.rstrip('/')}/telegram",
            secret_token=os.getenv("WEBHOOK_SECRET") or None,
            drop_pending_updates=True,
        )
    else:
        log.info("Starting in polling mode")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
