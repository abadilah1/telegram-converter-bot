"""Telegram bot: Word -> PDF and PDF -> Word converter.

Word -> PDF uses headless LibreOffice. PDF -> Word uses pdf2docx.
Runs in webhook mode when WEBHOOK_URL is set (hosting), otherwise polling (local).
"""

import asyncio
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
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

WORD_EXTS = {".doc", ".docx", ".odt", ".rtf"}
PDF_EXT = ".pdf"

_jobs = asyncio.Semaphore(MAX_CONCURRENT)
_last_request: dict[int, float] = {}

HELP_TEXT = (
    "Send me a file and I'll convert it:\n\n"
    "• Word (.doc, .docx, .odt, .rtf) → PDF\n"
    "• PDF → Word (.docx)\n\n"
    "Limits: files up to 20 MB. PDFs must contain real text "
    "(scanned/image-only PDFs are not supported). "
    "Password-protected files can't be converted."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 Welcome to the Word ⇄ PDF converter!\n\n" + HELP_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


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
        raise RuntimeError("Conversion timed out.")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    out = outdir / (src.stem + ".pdf")
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"LibreOffice failed: {err.decode(errors='ignore')[:300]}")
    return out


def _pdf_to_docx_sync(src: Path, out: Path) -> None:
    from pdf2docx import Converter

    cv = Converter(str(src))
    try:
        cv.convert(str(out))
    finally:
        cv.close()


async def pdf_to_word(src: Path, outdir: Path) -> Path:
    out = outdir / (src.stem + ".docx")
    await asyncio.wait_for(
        asyncio.to_thread(_pdf_to_docx_sync, src, out), timeout=CONVERT_TIMEOUT
    )
    if not out.exists():
        raise RuntimeError("PDF conversion produced no output.")
    return out


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    doc = msg.document
    user_id = update.effective_user.id
    name = doc.file_name or "file"
    ext = Path(name).suffix.lower()

    if ext not in WORD_EXTS and ext != PDF_EXT:
        await msg.reply_text("❌ Unsupported file. Send a Word document or a PDF.")
        return
    if doc.file_size and doc.file_size > MAX_BYTES:
        await msg.reply_text("❌ File is too large. The limit is 20 MB.")
        return

    now = time.monotonic()
    wait = COOLDOWN_SECONDS - (now - _last_request.get(user_id, 0))
    if wait > 0:
        await msg.reply_text(f"⏳ Please wait {int(wait) + 1}s before sending another file.")
        return
    _last_request[user_id] = now

    status = await msg.reply_text("⏳ Converting, please wait...")
    workdir = Path(tempfile.mkdtemp(prefix="conv_"))
    try:
        async with _jobs:
            await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_DOCUMENT)
            tg_file = await doc.get_file()
            # Neutral local name avoids odd characters breaking the converters.
            src = workdir / f"input{ext}"
            await tg_file.download_to_drive(custom_path=src)

            if ext == PDF_EXT:
                result = await pdf_to_word(src, workdir)
                out_name = Path(name).stem + ".docx"
            else:
                result = await word_to_pdf(src, workdir)
                out_name = Path(name).stem + ".pdf"

        with open(result, "rb") as fh:
            await msg.reply_document(document=fh, filename=out_name, caption="✅ Done!")
        await status.delete()
    except Exception as exc:  # noqa: BLE001
        log.exception("Conversion failed for user %s", user_id)
        text = str(exc).lower()
        if "password" in text or "encrypted" in text:
            reason = "The file looks password-protected."
        elif "timed out" in text or isinstance(exc, asyncio.TimeoutError):
            reason = "The conversion took too long."
        else:
            reason = "The file may be corrupted or unsupported."
        await status.edit_text(f"❌ Conversion failed. {reason}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Please send a Word or PDF file as a document. Use /help for details.")


def main() -> None:
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
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
