import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .analysis import analyze_poem, format_analysis, words
from .config import MAX_POEM_CHARS, get_zone
from .db import Database
from .planning import format_local, parse_clock
from .poetry_source import FoundPoem, SearchResult, SourceError, fetch_poem, search_poems

router = Router()
db = Database()
logger = logging.getLogger(__name__)


class LearnForm(StatesGroup):
    query = State()
    choose = State()
    confirm = State()
    manual = State()
    sleep = State()
    wake = State()


async def send_long(message: Message, text: str) -> None:
    remaining = text
    while remaining:
        if len(remaining) <= 4000:
            await message.answer(remaining)
            break
        cut = remaining.rfind("\n", 0, 3900)
        if cut < 1000:
            cut = remaining.rfind(" ", 0, 3900)
        if cut < 1000:
            cut = 3900
        await message.answer(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")


def manual_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Прислать текст вручную", callback_data="poem:manual")
    ]])


def plan_keyboard(poem_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать учить по блокам", callback_data=f"blocks:{poem_id}")],
        [InlineKeyboardButton(text="Перейти к тренировке", callback_data=f"trainmenu:{poem_id}:0")],
    ])


def parse_query(value: str) -> tuple[str, str]:
    if "," in value:
        title, author = value.rsplit(",", 1)
        return title.strip(), author.strip()
    return value.strip(), ""


def valid_manual_text(text: str) -> tuple[bool, str]:
    if len(text) > MAX_POEM_CHARS:
        return False, f"Текст длиннее {MAX_POEM_CHARS} символов. Пришлите более короткое стихотворение."
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or len(words(text)) < 6:
        return False, "Это не похоже на стихотворение: нужны хотя бы 2 строки и 6 слов."
    letters = sum(char.isalpha() for char in text)
    if letters / max(1, len(text.strip())) < 0.45:
        return False, "В сообщении слишком мало букв. Пришлите сам текст стихотворения, без ссылок и служебных символов."
    return True, ""


async def show_found(message: Message, state: FSMContext, found: FoundPoem) -> None:
    await state.set_state(LearnForm.confirm)
    await state.update_data(
        title=found.title,
        author=found.author,
        text=found.text,
        source_url=found.source_url,
    )
    await message.answer(f"Нашёл: «{found.title}» — {found.author}.\nИсточник: {found.source_url}")
    await send_long(message, found.text)
    await message.answer(
        "Это тот текст, который вы хотите выучить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да, всё верно", callback_data="poem:confirm")],
            [InlineKeyboardButton(text="Нет, пришлю вручную", callback_data="poem:manual")],
        ]),
    )


async def run_search(message: Message, state: FSMContext, title: str, author: str) -> None:
    try:
        results = await search_poems(title, author)
    except SourceError:
        logger.exception("Poetry source unavailable")
        await message.answer(
            "Открытый источник стихов сейчас недоступен. Это не мешает учить: "
            "пришлите текст стихотворения одним сообщением.",
            reply_markup=manual_keyboard(),
        )
        return
    if not results:
        await message.answer(
            "Ничего подходящего не нашлось в открытой Викитеке. Проверьте название и автора "
            "или пришлите текст стихотворения вручную одним сообщением.",
            reply_markup=manual_keyboard(),
        )
        return
    if len(results) == 1:
        await message.answer("Нашёл один вариант, загружаю полный текст…")
        try:
            found = await fetch_poem(results[0])
        except SourceError:
            await message.answer(
                "Страница найдена, но прочитать текст не удалось. Пришлите его вручную.",
                reply_markup=manual_keyboard(),
            )
            return
        await show_found(message, state, found)
        return

    await state.set_state(LearnForm.choose)
    await state.update_data(search_results=[
        {"page_id": result.page_id, "title": result.title} for result in results
    ])
    buttons = [
        [InlineKeyboardButton(text=result.title[:60], callback_data=f"poem:select:{result.page_id}")]
        for result in results
    ]
    buttons.append([InlineKeyboardButton(text="Прислать текст вручную", callback_data="poem:manual")])
    await message.answer(
        "Найдено несколько вариантов или редакций. Выберите нужную — я покажу полный текст для подтверждения.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def ask_sleep(message: Message, state: FSMContext) -> None:
    await state.set_state(LearnForm.sleep)
    user = db.get_user(message.from_user.id)
    await message.answer(
        "Теперь настроим план с учётом сна. Во сколько вы обычно ложитесь спать?\n"
        f"Напишите время в формате ЧЧ:ММ, например {user['sleep_time']}."
    )


def render_plan(poem, plan, schedule, timezone_name: str) -> str:
    zone = get_zone(timezone_name)
    lines = [
        f"«{poem['title']}» — {poem['author']}",
        f"Размер: {plan['session_minutes']} мин на основную сессию; "
        f"блоки по {plan['chunk_size']} строк; частей заучивания: {plan['total_parts']}.",
        "Метод: новый блок → воспроизведение всех пройденных блоков вместе.",
        "",
        "Расписание:",
    ]
    for item in schedule:
        label = "Заучивание" if item["kind"] == "learn" else "Повторение"
        if item["part_number"]:
            label += f", часть {item['part_number']}/{plan['total_parts']}"
        status = " ✓" if item["status"] == "done" else ""
        lines.append(f"• {format_local(item['scheduled_at'], zone)} — {label}{status}")
    lines.append("\nНапоминания придут автоматически, ночью бот молчит.")
    return "\n".join(lines)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.ensure_user(message.from_user.id)
    await message.answer(
        "Привет! Я помогу выучить стихотворение наизусть через активное припоминание, "
        "небольшие блоки и повторения с увеличивающимися интервалами.\n\n"
        "Как начать:\n"
        "1. /learn — введите название и автора, например: Парус, Лермонтов.\n"
        "2. Подтвердите найденный текст или пришлите свой.\n"
        "3. Укажите время сна и подъёма — я составлю план.\n"
        "4. Учите по блокам и возвращайтесь на тесты по напоминаниям.\n\n"
        "Команды:\n"
        "/train — тренировка с пропусками или первыми буквами\n"
        "/plan — текущий план и расписание\n"
        "/progress — статистика\n"
        "/cancel — отменить текущий стих и его напоминания"
    )


@router.message(Command("learn"))
async def learn(message: Message, state: FSMContext) -> None:
    await state.clear()
    db.ensure_user(message.from_user.id)
    await state.set_state(LearnForm.query)
    await message.answer(
        "Введите название и автора через запятую.\nНапример: Парус, Лермонтов\n"
        "Можно ввести только название, но тогда вариантов может быть больше."
    )


@router.message(LearnForm.query, F.text)
async def receive_query(message: Message, state: FSMContext) -> None:
    title, author = parse_query(message.text.strip())
    if len(title) < 2 or len(title) > 150 or len(author) > 120:
        await message.answer("Проверьте запрос: название должно быть от 2 до 150 символов. Попробуйте ещё раз.")
        return
    await state.update_data(query_title=title, query_author=author)
    await message.answer("Ищу полный текст в открытой Викитеке…")
    await run_search(message, state, title, author)


@router.callback_query(F.data.startswith("poem:select:"))
async def select_result(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        page_id = int(callback.data.rsplit(":", 1)[1])
        data = await state.get_data()
        saved = next(item for item in data.get("search_results", []) if item["page_id"] == page_id)
        result = SearchResult(page_id=page_id, title=saved["title"])
        await callback.message.answer("Загружаю выбранный вариант…")
        found = await fetch_poem(result)
    except (ValueError, StopIteration, SourceError):
        await callback.message.answer(
            "Не удалось загрузить этот вариант. Выберите другой или пришлите текст вручную.",
            reply_markup=manual_keyboard(),
        )
        return
    await show_found(callback.message, state, found)


@router.callback_query(F.data == "poem:confirm")
async def confirm_poem(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    if not data.get("text"):
        await callback.message.answer("Сначала найдите стих через /learn или пришлите его вручную.")
        return
    analysis = analyze_poem(data["text"])
    await state.update_data(analysis=analysis.to_dict())
    await callback.message.answer("Анализ готов:\n" + format_analysis(analysis))
    await ask_sleep(callback.message, state)


@router.callback_query(F.data == "poem:manual")
async def manual_poem(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(LearnForm.manual)
    await callback.message.answer(
        "Пришлите полный текст стихотворения одним сообщением. Строки и пустые строки между строфами сохранятся."
    )


@router.message(LearnForm.manual, F.text)
async def receive_manual(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    valid, error = valid_manual_text(text)
    if not valid:
        await message.answer(error + "\nПопробуйте прислать текст ещё раз.")
        return
    data = await state.get_data()
    title = data.get("query_title") or "Мой стих"
    author = data.get("query_author") or "Автор не указан"
    analysis = analyze_poem(text)
    await state.update_data(
        title=title,
        author=author,
        text=text,
        source_url=None,
        analysis=analysis.to_dict(),
    )
    await message.answer("Текст принят. Анализ:\n" + format_analysis(analysis))
    await ask_sleep(message, state)


@router.message(LearnForm.sleep, F.text)
async def receive_sleep(message: Message, state: FSMContext) -> None:
    try:
        sleep = parse_clock(message.text)
    except ValueError:
        await message.answer("Не понял время. Напишите его как ЧЧ:ММ, например 23:00.")
        return
    await state.update_data(sleep_time=sleep.strftime("%H:%M"))
    await state.set_state(LearnForm.wake)
    await message.answer("Во сколько вы обычно встаёте? Например 07:00.")


@router.message(LearnForm.wake, F.text)
async def receive_wake(message: Message, state: FSMContext) -> None:
    try:
        wake = parse_clock(message.text)
    except ValueError:
        await message.answer("Не понял время. Напишите его как ЧЧ:ММ, например 07:00.")
        return
    data = await state.get_data()
    sleep_time = data.get("sleep_time")
    if not sleep_time or not data.get("text") or not data.get("analysis"):
        await state.clear()
        await message.answer("Данные стиха потерялись. Начните заново: /learn.")
        return
    if sleep_time == wake.strftime("%H:%M"):
        await message.answer("Время сна и подъёма совпадает. Укажите другое время подъёма.")
        return
    from .analysis import PoemAnalysis

    analysis = PoemAnalysis.from_dict(data["analysis"])
    try:
        poem_id = await asyncio.to_thread(
            db.create_poem,
            message.from_user.id,
            data["title"],
            data["author"],
            data["text"],
            data.get("source_url"),
            analysis,
            sleep_time,
            wake.strftime("%H:%M"),
        )
    except Exception:
        logger.exception("Could not save poem")
        await message.answer("Не удалось сохранить план. Попробуйте ещё раз через /learn чуть позже.")
        return
    await state.clear()
    poem = db.get_poem(poem_id)
    plan = db.get_plan(poem_id)
    schedule = db.get_schedule(poem_id)
    user = db.get_user(message.from_user.id)
    await message.answer(
        "План готов! Основная сессия назначена на вечер, за 1–3 часа до сна, "
        "а длинное повторение — на следующее утро.\n\n"
        + render_plan(poem, plan, schedule, user["timezone"]),
        reply_markup=plan_keyboard(poem_id),
    )


@router.message(Command("plan"))
async def show_plan(message: Message) -> None:
    poem = db.get_active_poem(message.from_user.id)
    if not poem:
        await message.answer("Сейчас нет активного стиха. Начните с команды /learn.")
        return
    plan = db.get_plan(poem["id"])
    schedule = db.get_schedule(poem["id"], include_done=True)
    user = db.get_user(message.from_user.id)
    await message.answer(
        render_plan(poem, plan, schedule, user["timezone"]),
        reply_markup=plan_keyboard(poem["id"]),
    )


@router.message(Command("progress"))
async def progress(message: Message) -> None:
    data = db.progress(message.from_user.id)
    counts = data["counts"]
    stats = data["stats"]
    lines = [
        "Ваш прогресс:",
        f"Активных стихов: {counts.get('active', 0)}; отменено: {counts.get('cancelled', 0)}.",
        f"Пройдено тестов: {stats['attempts'] or 0}.",
        f"Средняя точность: {stats['average'] or 0}%.",
    ]
    if data["recent"]:
        lines.append("\nПоследние тесты:")
        for item in data["recent"]:
            mode = "первые буквы" if item["mode"] == "letters" else f"пропуски {item['level']}%"
            lines.append(f"• {item['title']}: {round(item['score'] * 100)}%, {mode}")
    await message.answer("\n".join(lines))


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    poem = db.get_active_poem(message.from_user.id)
    if not poem:
        await message.answer("Нет активного стиха или незавершённого действия. Чтобы начать, используйте /learn.")
        return
    db.cancel_poem(poem["id"], message.from_user.id)
    await message.answer(f"«{poem['title']}» отменён, все его напоминания отключены. Новый стих: /learn.")


@router.message(LearnForm.query)
@router.message(LearnForm.choose)
@router.message(LearnForm.confirm)
@router.message(LearnForm.manual)
@router.message(LearnForm.sleep)
@router.message(LearnForm.wake)
async def unsupported_learn_message(message: Message) -> None:
    await message.answer(
        "Сейчас нужен текстовый ответ или нажатие кнопки под сообщением. "
        "Фото, файлы и голосовые сообщения я не читаю. Чтобы начать заново, отправьте /cancel, затем /learn."
    )
