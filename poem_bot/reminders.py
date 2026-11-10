import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import get_zone
from .db import Database
from .planning import adjust_outside_night, is_sleep_time, parse_clock, utc_now

logger = logging.getLogger(__name__)


async def reminder_loop(bot: Bot, db: Database) -> None:
    while True:
        try:
            for item in db.due_reminders(utc_now()):
                zone = get_zone(item["timezone"])
                sleep = parse_clock(item["sleep_time"])
                wake = parse_clock(item["wake_time"])
                if is_sleep_time(utc_now().astimezone(zone), sleep, wake):
                    deferred = adjust_outside_night(utc_now(), zone, sleep, wake)
                    db.reschedule(item["id"], deferred)
                    continue
                db.mark_sent(item["id"])
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="Пройти тренировку",
                        callback_data=f"trainmenu:{item['poem_id']}:{item['id']}",
                    )
                ]])
                action = "Пора начать заучивание" if item["kind"] == "learn" else "Пора повторить"
                try:
                    await bot.send_message(
                        item["user_id"],
                        f"{action} «{item['title']}»! Пройди быстрый тест — активное припоминание "
                        "закрепляет текст лучше перечитывания.",
                        reply_markup=keyboard,
                    )
                except TelegramRetryAfter as exc:
                    db.reschedule(item["id"], utc_now() + timedelta(seconds=exc.retry_after))
                except TelegramNetworkError:
                    logger.warning("Network error while delivering reminder %s; will retry", item["id"])
                    db.reschedule(item["id"], utc_now() + timedelta(minutes=5))
                except Exception:
                    logger.exception("Could not deliver reminder %s", item["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder loop iteration failed")
        await asyncio.sleep(30)
