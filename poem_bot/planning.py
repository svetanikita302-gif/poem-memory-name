from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .analysis import PoemAnalysis

# Активное припоминание эффективнее повторного чтения:
# Karpicke & Roediger, Science (2008), https://pubmed.ncbi.nlm.nih.gov/18276894/
# Roediger & Karpicke, TiCS (2010), https://pubmed.ncbi.nlm.nih.gov/20951630/
# Расширяющиеся интервалы используют spacing effect:
# Kim et al., Behavior Research Methods (2019), https://pubmed.ncbi.nlm.nih.gov/30623389/
# Cepeda et al., Psychological Bulletin (2006), https://doi.org/10.1037/0033-2909.132.3.354
# Вечернее обучение и утреннее повторение учитывают консолидацию памяти во сне:
# Sleep (2021), https://pubmed.ncbi.nlm.nih.gov/33035340/
# Блоки по 2–4 строки согласованы с ограниченной ёмкостью рабочей памяти:
# Cowan, Behavioral and Brain Sciences (2001), https://doi.org/10.1017/S0140525X01003922


def parse_clock(value: str) -> time:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Используйте формат ЧЧ:ММ") from exc
    return parsed


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(local: datetime) -> datetime:
    return local.astimezone(timezone.utc)


def next_evening(now_utc: datetime, zone: ZoneInfo, sleep: time) -> datetime:
    local_now = now_utc.astimezone(zone)
    candidates = []
    for offset in (-1, 0, 1, 2):
        day = local_now.date() + timedelta(days=offset)
        bedtime = datetime.combine(day, sleep, tzinfo=zone)
        candidates.append(bedtime - timedelta(hours=2))
    future = [candidate for candidate in candidates if candidate > local_now + timedelta(minutes=5)]
    return min(future) if future else candidates[-1] + timedelta(days=1)


def next_morning(after_utc: datetime, zone: ZoneInfo, wake: time) -> datetime:
    local = after_utc.astimezone(zone)
    morning = datetime.combine(local.date(), wake, tzinfo=zone) + timedelta(minutes=30)
    if morning <= local:
        morning += timedelta(days=1)
    return to_utc(morning)


def is_sleep_time(local: datetime, sleep: time, wake: time) -> bool:
    current = local.time()
    if sleep > wake:
        return current >= sleep or current < wake
    if sleep < wake:
        return sleep <= current < wake
    return False


def adjust_outside_night(value_utc: datetime, zone: ZoneInfo, sleep: time, wake: time) -> datetime:
    local = value_utc.astimezone(zone)
    if not is_sleep_time(local, sleep, wake):
        return value_utc
    wake_dt = datetime.combine(local.date(), wake, tzinfo=zone) + timedelta(minutes=5)
    if wake_dt <= local:
        wake_dt += timedelta(days=1)
    return to_utc(wake_dt)


def build_schedule(
    analysis: PoemAnalysis,
    now_utc: datetime,
    zone: ZoneInfo,
    sleep: time,
    wake: time,
) -> tuple[list[dict], int]:
    long_hard = analysis.size == "длинный" and analysis.difficulty == "сложный"
    if long_hard and analysis.line_count > 60:
        parts = 3
    elif analysis.size == "длинный" or analysis.difficulty == "сложный":
        parts = 2
    else:
        parts = 1

    first = next_evening(now_utc, zone, sleep)
    items = []
    for part in range(1, parts + 1):
        start = first + timedelta(days=part - 1)
        items.append({
            "kind": "learn",
            "part_number": part,
            "scheduled_at": to_utc(start).isoformat(),
        })
    final = to_utc(items[-1]["scheduled_at"] and datetime.fromisoformat(items[-1]["scheduled_at"]))

    if long_hard:
        delays = [timedelta(minutes=30), timedelta(hours=2)]
        later = [timedelta(hours=36), timedelta(days=4), timedelta(days=10)]
    elif analysis.size == "короткий" and analysis.difficulty == "лёгкий":
        delays = [timedelta(minutes=20), timedelta(minutes=45)]
        later = [timedelta(hours=24), timedelta(days=3), timedelta(days=7)]
    else:
        delays = [timedelta(minutes=25), timedelta(hours=1)]
        later = [timedelta(hours=24), timedelta(days=3), timedelta(days=7)]

    for delay in delays:
        value = adjust_outside_night(final + delay, zone, sleep, wake)
        items.append({"kind": "review", "part_number": 0, "scheduled_at": value.isoformat()})
    morning = next_morning(final, zone, wake)
    items.append({"kind": "review", "part_number": 0, "scheduled_at": morning.isoformat()})
    for delay in later:
        value = adjust_outside_night(morning + delay, zone, sleep, wake)
        items.append({"kind": "review", "part_number": 0, "scheduled_at": value.isoformat()})
    return items, parts


def format_local(iso_value: str, zone: ZoneInfo) -> str:
    return datetime.fromisoformat(iso_value).astimezone(zone).strftime("%d.%m.%Y, %H:%M")
