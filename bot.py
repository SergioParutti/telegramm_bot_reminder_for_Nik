import html
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

from telegram import Update, User
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    Defaults,
    JobQueue,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"(\d+)\s*([smhd])", re.IGNORECASE)
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
)
_REPEAT_KEYWORDS = {"every", "repeat", "каждый", "каждые"}



def parse_duration(spec: str) -> Optional[timedelta]:
    """
    Convert strings like '10m', '1h20m', '2d3h' to timedelta.
    Returns None if the format is invalid or zero.
    """
    matches = _DURATION_RE.findall(spec)
    if not matches:
        return None

    total_seconds = 0
    for value, unit in matches:
        amount = int(value)
        match unit.lower():
            case "s":
                total_seconds += amount
            case "m":
                total_seconds += amount * 60
            case "h":
                total_seconds += amount * 3600
            case "d":
                total_seconds += amount * 86400

    if total_seconds <= 0:
        return None
    return timedelta(seconds=total_seconds)


def parse_datetime_spec(tokens: list[str]) -> Optional[tuple[datetime, int]]:
    if not tokens:
        return None

    candidates = [(tokens[0], 1)]
    if len(tokens) >= 2:
        candidates.append((f"{tokens[0]} {tokens[1]}", 2))

    for value, consumed in candidates:
        for fmt in _DATETIME_FORMATS:
            try:
                return datetime.strptime(value, fmt), consumed
            except ValueError:
                continue
    return None


def humanize_delta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    parts: list[str] = []
    for unit_seconds, suffix in (
        (86400, "d"),
        (3600, "h"),
        (60, "m"),
        (1, "s"),
    ):
        value, seconds = divmod(seconds, unit_seconds)
        if value:
            parts.append(f"{value}{suffix}")
    return " ".join(parts) if parts else "0s"


def format_target_mention(user: Optional[User], fallback: str) -> str:
    # Prefer full mention; fallback uses username-like label.
    if user:
        return user.mention_html()
    cleaned = fallback.strip()
    if not cleaned.startswith("@"):
        cleaned = f"@{cleaned}"
    return html.escape(cleaned)


def normalize_target_label(label: Optional[str]) -> tuple[Optional[str], bool]:
    if not label:
        return None, False
    stripped = label.lstrip("@").strip()
    if stripped.lower() in {"all", "всем"}:
        return "всех", True
    return label, False


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if not job or not job.data:
        return

    data: Dict = job.data
    chat_id = data["chat_id"]
    mention = data["mention"]
    text = data["text"]
    reply_to = data.get("reply_to")
    repeat_interval = data.get("repeat_interval")

    if mention:
        reminder_text = f"⏰ Напоминание для {mention}:\n{html.escape(text)}"
    else:
        reminder_text = f"⏰ Напоминание:\n{html.escape(text)}"

    await context.bot.send_message(
        chat_id=chat_id,
        text=reminder_text,
        reply_to_message_id=reply_to,
        parse_mode=ParseMode.HTML,
    )

    chat_jobs = context.application.chat_data.get(chat_id, {}).get("jobs", {})
    if repeat_interval:
        data["due_at"] = datetime.now() + repeat_interval
    else:
        # Cleanup stored job info after firing.
        chat_jobs.pop(job.name, None)


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if not job or not job.data:
        return
    data: Dict = job.data
    chat_id = data.get("chat_id")
    message_id = data.get("message_id")
    if not chat_id or not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        logger.exception("Failed to delete message %s in chat %s", message_id, chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "Привет! Я бот-напоминалка созданный моим великим хозяином, специально для пацана Никиты, чтобы напоминать ему про папины сообщения.\n\n"
        "Использование:\n"
        "• Без адресата: /remind 10m текст\n"
        "• Ответом на сообщение: /remind 10m текст\n"
        "• Явно указать пользователя: /remind @username 10m текст\n"
        "• Всем сразу: /remind ALL 10m текст\n"
        "• На дату и время: /remind 2024-12-31 18:00 текст\n"
        "• Повтор: /remind 10m every 1h текст\n\n"
        "• На дату и время + Повтор: /remind 2024-12-31 18:00 every 1h текст\n"
        "Форматы времени: s — сек, m — мин, h — час, d — день.\n"
        "Форматы даты/времени: YYYY-MM-DD HH:MM или YYYY-MM-DDTHH:MM.\n\n"
        "/list — показать активные напоминания\n"
        "/cancel ID — отменить напоминание\n"
        "/cancel All — отменить все напоминания"
    )
    if update.effective_message:
        await update.effective_message.reply_html(html.escape(msg))


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        logger.info("Update without message: %s", update)
        return
    logger.info(
        "Incoming message in chat %s: text=%r entities=%s",
        message.chat_id,
        message.text,
        message.entities,
    )

async def handle_text_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    if message.entities and any(entity.type == "bot_command" for entity in message.entities):
        return

    text = message.text.strip()
    if not text.startswith("/"):
        return

    logger.info("Fallback command handler used for chat %s: %s", message.chat_id, text)
    parts = text.split()
    command = parts[0].split("@", 1)[0].lower()
    context.args = parts[1:]

    if command == "/start" or command == "/help":
        await start(update, context)
    elif command == "/remind":
        await remind(update, context)
    elif command == "/list":
        await list_reminders(update, context)
    elif command == "/cancel":
        await cancel(update, context)


async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    command_queue = context.job_queue or context.application.job_queue
    if command_queue:
        command_queue.run_once(
            delete_message_job,
            when=10,
            data={"chat_id": message.chat_id, "message_id": message.message_id},
        )

    args = context.args
    logger.info("Received /remind from chat %s: %s", message.chat_id, message.text)
    target_user: Optional[User] = None
    target_label: Optional[str] = None
    is_all_target = False
    repeat_interval: Optional[timedelta] = None
    reminder_text = ""

    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        if not args:
            await message.reply_text("Нужно указать время, например: /remind 10m текст")
            return
        time_args = args
        target_label = target_user.username or target_user.full_name
    else:
        # Два формата:
        # 1) /remind 10m текст        (без адресата)
        # 2) /remind @username 10m текст
        if not args:
            await message.reply_text(
                "Использование:\n"
                "• /remind 10m текст — без адресата\n"
                "• /remind @username 10m текст\n"
                "• или ответом на сообщение: /remind 10m текст"
            )
            return

        tentative_delay = parse_duration(args[0])
        tentative_datetime = parse_datetime_spec(args)
        if tentative_delay or tentative_datetime:
            time_args = args
            target_label = None
        else:
            if len(args) < 2:
                await message.reply_text(
                    "Использование:\n"
                    "• /remind 10m текст — без адресата\n"
                    "• /remind @username 10m текст\n"
                    "• /remind ALL 10m текст\n"
                    "• /remind 2024-12-31 18:00 текст\n"
                    "• или ответом на сообщение: /remind 10m текст"
                )
                return
            target_label = args[0]
            time_args = args[1:]

    now = datetime.now()
    delay = parse_duration(time_args[0]) if time_args else None
    if delay:
        consumed = 1
        due_at = now + delay
    else:
        parsed_dt = parse_datetime_spec(time_args)
        if not parsed_dt:
            await message.reply_text(
                "Не понял время. Используй форматы вроде 10m, 1h20m, 2d3h "
                "или дату 2024-12-31 18:00."
            )
            return
        due_at, consumed = parsed_dt
        if due_at <= now:
            await message.reply_text("Указанная дата/время уже прошла.")
            return
        delay = due_at - now

    remaining = time_args[consumed:]
    if remaining and remaining[0].lower() in _REPEAT_KEYWORDS:
        if len(remaining) < 2:
            await message.reply_text("Укажи интервал повторения, например: every 1h")
            return
        repeat_interval = parse_duration(remaining[1])
        if not repeat_interval:
            await message.reply_text("Не понял интервал повторения. Пример: every 1h")
            return
        remaining = remaining[2:]

    reminder_text = " ".join(remaining).strip() or "Напоминание!"
    target_label, is_all_target = normalize_target_label(target_label)

    job_queue = context.job_queue or context.application.job_queue
    if not job_queue:
        await message.reply_text("Внутренняя ошибка: очередь задач недоступна.")
        return

    chat_id = message.chat_id
    job_id = f"{chat_id}-{int(time.time() * 1000)}"
    mention = None
    if target_label:
        mention = html.escape(target_label) if is_all_target else format_target_mention(target_user, target_label)

    job_data = {
        "chat_id": chat_id,
        "mention": mention,
        "text": reminder_text,
        "reply_to": message.reply_to_message.message_id if message.reply_to_message else None,
        "due_at": due_at,
        "repeat_interval": repeat_interval,
    }

    if repeat_interval:
        job = job_queue.run_repeating(
            send_reminder,
            interval=repeat_interval,
            first=delay,
            name=job_id,
            data=job_data,
        )
    else:
        job = job_queue.run_once(
            send_reminder,
            when=delay,
            name=job_id,
            data=job_data,
        )

    chat_jobs = context.chat_data.setdefault("jobs", {})
    chat_jobs[job_id] = {
        "job": job,
        "target": mention,
        "text": reminder_text,
        "due_at": due_at,
        "repeat_interval": repeat_interval,
    }

    target_display = chat_jobs[job_id]["target"] or "без адресата"
    repeat_note = (
        f"\nПовтор: каждые {humanize_delta(repeat_interval)}"
        if repeat_interval
        else ""
    )
    confirmation = await message.reply_html(
        f"🗓️ Запланировано для {target_display} через {humanize_delta(delay)}.\n"
        f"ID: <code>{job_id}</code>\n"
        f"Текст: {html.escape(reminder_text)}"
        f"{repeat_note}"
    )
    if confirmation:
        context.job_queue.run_once(
            delete_message_job,
            when=15,
            data={"chat_id": confirmation.chat_id, "message_id": confirmation.message_id},
        )


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    chat_jobs: Dict = context.chat_data.get("jobs", {})
    if not chat_jobs:
        await message.reply_text("Активных напоминаний нет.")
        return

    now = datetime.now()
    lines = []
    for job_id, data in chat_jobs.items():
        eta = max(data["due_at"] - now, timedelta(0))
        target_display = data["target"] or "без адресата"
        repeat_interval = data.get("repeat_interval")
        repeat_text = (
            f" — повтор каждые {humanize_delta(repeat_interval)}" if repeat_interval else ""
        )
        lines.append(
            f"{job_id}: {target_display} — через {humanize_delta(eta)}{repeat_text} — {data['text']}"
        )

    response = await message.reply_html(
        "Активные напоминания:\n" + "\n".join(html.escape(line) for line in lines)
    )
    if response:
        context.job_queue.run_once(
            delete_message_job,
            when=10,
            data={"chat_id": response.chat_id, "message_id": response.message_id},
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    chat_jobs: Dict = context.chat_data.get("jobs", {})
    if not context.args:
        if not chat_jobs:
            await message.reply_text("Активных напоминаний нет.")
            return
        if len(chat_jobs) == 1:
            job_id = next(iter(chat_jobs.keys()))
        else:
            lines = "\n".join(f"• <code>{html.escape(job_id)}</code>" for job_id in chat_jobs.keys())
            response = await message.reply_html(
                "Укажи ID напоминания: /cancel &lt;id&gt;\n"
                "Или отменить все: /cancel All\n"
                "Доступные ID:\n"
                + lines
            )
            if response:
                context.job_queue.run_once(
                    delete_message_job,
                    when=10,
                    data={"chat_id": response.chat_id, "message_id": response.message_id},
                )
            return
    else:
        job_id = context.args[0]

    if job_id.lower() == "all":
        if not chat_jobs:
            await message.reply_text("Активных напоминаний нет.")
            return
        for data in chat_jobs.values():
            data["job"].schedule_removal()
        chat_jobs.clear()
        confirmation = await message.reply_html("Отменены все напоминания.")
        if confirmation:
            context.job_queue.run_once(
                delete_message_job,
                when=10,
                data={"chat_id": confirmation.chat_id, "message_id": confirmation.message_id},
            )
        return

    job_info = chat_jobs.pop(job_id, None)

    if not job_info:
        await message.reply_text("Напоминание не найдено.")
        return

    job_info["job"].schedule_removal()
    target_display = job_info["target"] or "без адресата"
    confirmation = await message.reply_html(
        f"Отменено: <code>{html.escape(job_id)}</code> для {target_display}."
    )
    if confirmation:
        context.job_queue.run_once(
            delete_message_job,
            when=10,
            data={"chat_id": confirmation.chat_id, "message_id": confirmation.message_id},
        )


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Установи переменную окружения BOT_TOKEN с токеном бота от @BotFather.")

    application = (
        Application.builder()
        .token(token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        # Увеличиваем таймауты, чтобы сгладить нестабильную сеть.
        .read_timeout(30.0)
        .write_timeout(30.0)
        .connect_timeout(30.0)
        .job_queue(JobQueue())
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("remind", remind))
    application.add_handler(CommandHandler("list", list_reminders))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(MessageHandler(filters.ALL, handle_text_command, block=False), group=1)
    application.add_handler(MessageHandler(filters.ALL, log_update, block=False), group=2)

    logger.info("Starting bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
