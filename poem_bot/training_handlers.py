import logging
import secrets

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .config import get_zone
from .db import Database
from .handlers import send_long
from .planning import format_local
from .training import chunk_lines, compare_texts, first_letters, mask_text

router = Router()
db = Database()
logger = logging.getLogger(__name__)


class TrainingForm(StatesGroup):
    answer = State()


class BlockForm(StatesGroup):
    answer = State()


def training_keyboard(poem_id: int, schedule_id: int, recommended: int) -> InlineKeyboardMarkup:
    rows = []
    for level in (25, 50, 75, 100):
        label = f"Скрыто {level}% слов"
        if level == recommended:
            label = "Рекомендуется: " + label
        rows.append([InlineKeyboardButton(text=label, callback_data=f"train:{poem_id}:{schedule_id}:gaps:{level}")])
    rows.append([InlineKeyboardButton(text="Только первые буквы", callback_data=f"train:{poem_id}:{schedule_id}:letters:0")])
    rows.append([InlineKeyboardButton(text="Учить по блокам", callback_data=f"blocks:{poem_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rating_keyboard(poem_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Легко", callback_data=f"rate:{poem_id}:easy"),
        InlineKeyboardButton(text="С трудом", callback_data=f"rate:{poem_id}:hard"),
        InlineKeyboardButton(text="Не помню", callback_data=f"rate:{poem_id}:forgot"),
    ]])


async def show_training_menu(message: Message, poem_id: int, schedule_id: int = 0) -> None:
    poem = db.get_poem(poem_id, message.from_user.id)
    if not poem or poem["status"] != "active":
        await message.answer("Этот стих уже неактивен. Выберите новый через /learn.")
        return
    if not schedule_id:
        item = db.get_next_schedule(poem_id)
        schedule_id = item["id"] if item else 0
    await message.answer(
        f"Тренировка: «{poem['title']}».\n"
        "Выберите опору. Я покажу текст с пропусками, а вы пришлите полный стих по памяти. "
        "После проверки я покажу ошибки. Не перечитывайте оригинал во время теста.",
        reply_markup=training_keyboard(poem_id, schedule_id, poem["current_level"]),
    )


@router.message(Command("train"))
async def train(message: Message, state: FSMContext) -> None:
    await state.clear()
    poem = db.get_active_poem(message.from_user.id)
    if not poem:
        await message.answer("Сначала добавьте стихотворение через /learn.")
        return
    await show_training_menu(message, poem["id"])


@router.callback_query(F.data.startswith("trainmenu:"))
async def training_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    _, poem_id, schedule_id = callback.data.split(":")
    await show_training_menu(callback.message, int(poem_id), int(schedule_id))


@router.callback_query(F.data.startswith("train:"))
async def start_training(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        _, poem_id_raw, schedule_id_raw, mode, level_raw = callback.data.split(":")
        poem_id, schedule_id, level = int(poem_id_raw), int(schedule_id_raw), int(level_raw)
    except (ValueError, TypeError):
        await callback.message.answer("Не удалось запустить тренировку. Откройте её заново: /train.")
        return
    poem = db.get_poem(poem_id, callback.from_user.id)
    if not poem or poem["status"] != "active":
        await callback.message.answer("Стих не найден или отменён. Начните новый через /learn.")
        return
    if mode == "gaps" and level not in (25, 50, 75, 100):
        await callback.message.answer("Неизвестный уровень. Выберите тренировку через /train.")
        return
    if schedule_id and not db.get_schedule_item(schedule_id, poem_id):
        schedule_id = 0

    if mode == "letters":
        prompt = first_letters(poem["text"])
        heading = "Опора — только первые буквы."
    else:
        prompt = mask_text(poem["text"], level, secrets.randbelow(2**31))
        heading = f"Скрыто примерно {level}% слов."
    await state.clear()
    await state.set_state(TrainingForm.answer)
    await state.update_data(
        poem_id=poem_id,
        schedule_id=schedule_id,
        mode=mode,
        level=level,
        expected=poem["text"],
    )
    await callback.message.answer(
        heading + "\nВосстановите текст и пришлите ПОЛНОЕ стихотворение одним сообщением."
    )
    await send_long(callback.message, prompt)


def comparison_feedback(score: float, errors: list[str]) -> str:
    percent = round(score * 100)
    if percent >= 90:
        heading = f"Отлично! Точность {percent}%."
    elif percent >= 75:
        heading = f"Хорошо: точность {percent}%. Ещё одна попытка закрепит текст."
    else:
        heading = f"Точность {percent}%. Повторите этот уровень ещё раз — это нормальная часть обучения."
    if errors:
        heading += "\n\nЧто исправить:\n" + "\n".join(f"• {error}" for error in errors)
    elif percent == 100:
        heading += "\nБез ошибок!"
    return heading


@router.message(TrainingForm.answer, F.text)
async def check_training(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    poem = db.get_poem(data.get("poem_id", 0), message.from_user.id)
    if not poem or not data.get("expected"):
        await state.clear()
        await message.answer("Тренировка устарела. Начните заново: /train.")
        return
    comparison = compare_texts(data["expected"], message.text)
    db.record_attempt(poem["id"], data["mode"], data["level"], comparison.score, comparison.errors)
    if data["mode"] == "gaps" and comparison.score >= 0.8:
        next_level = min(100, data["level"] + 25) if data["level"] < 100 else 100
        db.set_level(poem["id"], next_level)
    schedule_id = data.get("schedule_id", 0)
    if schedule_id:
        db.complete_schedule(schedule_id, poem["id"])
    await state.clear()
    await message.answer(
        comparison_feedback(comparison.score, comparison.errors)
        + "\n\nКак вам далось воспроизведение? Следующий интервал зависит от ответа.",
        reply_markup=rating_keyboard(poem["id"]),
    )


@router.callback_query(F.data.startswith("rate:"))
async def rate_recall(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        _, poem_id_raw, rating = callback.data.split(":")
        poem_id = int(poem_id_raw)
    except (ValueError, TypeError):
        await callback.message.answer("Не удалось сохранить оценку. Откройте /plan.")
        return
    poem = db.get_poem(poem_id, callback.from_user.id)
    if not poem or poem["status"] != "active" or rating not in ("easy", "hard", "forgot"):
        await callback.message.answer("Стих неактивен. Новый стих: /learn.")
        return
    when = db.next_after_rating(poem_id, rating)
    user = db.get_user(callback.from_user.id)
    phrase = {
        "easy": "Увеличил следующий интервал.",
        "hard": "Оставил интервал без изменений.",
        "forgot": "Перенёс повторение на ближайшее время — через примерно 20 минут.",
    }[rating]
    await callback.message.answer(
        f"{phrase}\nСледующее повторение: {format_local(when.isoformat(), get_zone(user['timezone']))}. "
        "Напоминание придёт автоматически."
    )


@router.callback_query(F.data.startswith("blocks:"))
async def start_blocks(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        poem_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.message.answer("Не удалось открыть блоки. Попробуйте /train.")
        return
    poem = db.get_poem(poem_id, callback.from_user.id)
    plan = db.get_plan(poem_id) if poem else None
    if not poem or not plan or poem["status"] != "active":
        await callback.message.answer("Стих не найден или отменён. Начните новый через /learn.")
        return
    chunks = chunk_lines(poem["text"], plan["chunk_size"])
    if not chunks:
        await callback.message.answer("Не удалось разбить текст. Пришлите его заново через /learn.")
        return
    item = db.get_next_schedule(poem_id, kind="learn")
    part = item["part_number"] if item else 1
    total_parts = plan["total_parts"]
    start = (part - 1) * len(chunks) // total_parts
    end = part * len(chunks) // total_parts
    if part == total_parts:
        end = len(chunks)
    if start >= end:
        start, end = 0, len(chunks)
        item = None
    await state.clear()
    await state.update_data(
        poem_id=poem_id,
        schedule_id=item["id"] if item else 0,
        chunks=chunks,
        current=start,
        end=end,
        part=part,
        total_parts=total_parts,
    )
    await callback.message.answer(
        f"Заучивание по блокам: часть {part}/{total_parts}. "
        "Читайте только текущий блок, затем воспроизводите все пройденные блоки вместе."
    )
    await show_current_block(callback.message, state)


async def show_current_block(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chunks = data["chunks"]
    current = data["current"]
    await state.set_state(None)
    text = "\n".join(chunks[current])
    await message.answer(
        f"Блок {current + 1} из {len(chunks)}. Прочитайте его 2–3 раза, закройте текст и нажмите кнопку проверки.\n\n{text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Проверить нарастающим итогом", callback_data="block:check")
        ]]),
    )


@router.callback_query(F.data == "block:check")
async def check_block_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    if not data.get("chunks"):
        await callback.message.answer("Сессия по блокам устарела. Начните её через /train.")
        return
    expected = "\n".join(
        line
        for chunk in data["chunks"][:data["current"] + 1]
        for line in chunk
    )
    await state.update_data(expected=expected)
    await state.set_state(BlockForm.answer)
    await callback.message.answer(
        f"Теперь без подсказки воспроизведите блоки 1–{data['current'] + 1} одним сообщением."
    )


@router.callback_query(F.data == "block:repeat")
async def repeat_block(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    if not data.get("chunks"):
        await callback.message.answer("Сессия устарела. Откройте /train.")
        return
    await show_current_block(callback.message, state)


@router.message(BlockForm.answer, F.text)
async def check_block_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    poem = db.get_poem(data.get("poem_id", 0), message.from_user.id)
    if not poem or not data.get("expected"):
        await state.clear()
        await message.answer("Сессия устарела. Начните заново через /train.")
        return
    comparison = compare_texts(data["expected"], message.text)
    db.record_attempt(poem["id"], "blocks", 0, comparison.score, comparison.errors)
    if comparison.score < 0.75:
        await message.answer(
            comparison_feedback(comparison.score, comparison.errors)
            + "\n\nПока остаёмся на этом блоке. Можно ответить ещё раз или вернуться к чтению.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Показать блок ещё раз", callback_data="block:repeat")
            ]]),
        )
        return

    if data["current"] + 1 < data["end"]:
        await state.update_data(current=data["current"] + 1)
        await message.answer(f"Верно на {round(comparison.score * 100)}%! Добавляем следующий блок.")
        await show_current_block(message, state)
        return

    schedule_id = data.get("schedule_id", 0)
    if schedule_id:
        db.complete_schedule(schedule_id, poem["id"])
    await state.clear()
    await message.answer(
        f"Часть {data['part']}/{data['total_parts']} пройдена, точность {round(comparison.score * 100)}%."
        "\nКак вам далось воспроизведение?",
        reply_markup=rating_keyboard(poem["id"]),
    )


@router.message(TrainingForm.answer)
@router.message(BlockForm.answer)
async def unsupported_training_message(message: Message) -> None:
    await message.answer(
        "Пришлите воспроизведённый текст обычным сообщением, не файлом или голосовым. "
        "Для отмены используйте /cancel."
    )
