import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", BASE_DIR / "poem_memory.db"))
DEFAULT_TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
MAX_POEM_CHARS = 8000


def get_bot_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Добавьте токен в переменные окружения; "
            "не записывайте его в код."
        )
    return token


def get_zone(name: str | None = None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo("Europe/Moscow")
