import re
from dataclasses import asdict, dataclass

WORD_RE = re.compile(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*", re.UNICODE)

ARCHAISMS = {
    "аз", "безсребреники", "благоговею", "бразды", "вем", "вкупе", "вняти",
    "воистину", "вран", "глас", "гряди", "десница", "дщерь", "единыжды",
    "зело", "зри", "коль", "корабль", "ланиты", "лепта", "мзда", "млада",
    "мнѣ", "надеждо", "недуг", "нивы", "ноготок", "нѣ", "одесную", "око",
    "очи", "паки", "перст", "перси", "пощады", "пращур", "призренiе",
    "разверсти", "рамена", "ров", "се", "сей", "сице", "стражду", "сый",
    "токмо", "уныло", "утро", "хладный", "чело", "чрез", "яко", "якобы",
}


@dataclass(slots=True)
class PoemAnalysis:
    line_count: int
    stanza_count: int
    average_words_per_line: float
    rare_words: list[str]
    archaic_words: list[str]
    long_sentences: int
    possible_inversions: int
    repeated_lines: list[str]
    score: int
    difficulty: str
    size: str
    chunk_size: int
    session_minutes: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PoemAnalysis":
        return cls(**data)


def words(text: str) -> list[str]:
    return WORD_RE.findall(text.lower().replace("ё", "е"))


def _possible_inversions(lines: list[str]) -> int:
    noun_ending = r"(?:[а-я]+(?:а|я|ь|о|е|и|ы|у|ю|ем|ом|ой|ою|ам|ям|ах|ях|ов|ев))"
    adjective_ending = r"(?:[а-я]+(?:ый|ий|ой|ая|яя|ое|ее|ые|ие|ого|его|ому|ему|ым|им|ых|их|ой|ей|ою|ею|ыми|ими))"
    pattern = re.compile(rf"\b{noun_ending}\s+{adjective_ending}\b", re.IGNORECASE)
    return sum(bool(pattern.search(line.replace("ё", "е"))) for line in lines)


def analyze_poem(text: str) -> PoemAnalysis:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Пустой текст стихотворения")

    stanzas = [part for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]
    all_words = words(text)
    average = len(all_words) / len(lines)
    long_words = sorted({word for word in all_words if len(word) >= 11})
    archaic = sorted({word for word in all_words if word in ARCHAISMS})

    sentences = [part.strip() for part in re.split(r"[.!?…]+", text) if part.strip()]
    long_sentences = sum(
        len(words(sentence)) >= 17 or sentence.count(",") >= 3
        for sentence in sentences
    )
    inversions = _possible_inversions(lines)

    normalized_lines = [" ".join(words(line)) for line in lines]
    counts: dict[str, int] = {}
    originals: dict[str, str] = {}
    for line, normalized in zip(lines, normalized_lines):
        if normalized:
            counts[normalized] = counts.get(normalized, 0) + 1
            originals.setdefault(normalized, line)
    repeated = [originals[line] for line, count in counts.items() if count > 1]

    length_score = min(35, len(lines) * 0.7)
    lexical_score = min(25, len(long_words) * 0.7 / max(1, len(lines)) * 10 + len(archaic) * 3)
    syntax_score = min(25, long_sentences * 4 + inversions * 1.5)
    score = max(0, min(100, round(length_score + lexical_score + syntax_score - min(15, len(repeated) * 5))))

    difficulty = "лёгкий" if score <= 30 else "средний" if score <= 60 else "сложный"
    size = "короткий" if len(lines) < 16 else "средний" if len(lines) <= 40 else "длинный"
    if difficulty == "сложный" or size == "длинный":
        chunk_size = 2
    elif difficulty == "лёгкий" or size == "короткий":
        chunk_size = 4
    else:
        chunk_size = 3
    session_minutes = 60 if difficulty == "сложный" or size == "длинный" else 45 if size == "средний" else 30

    return PoemAnalysis(
        line_count=len(lines),
        stanza_count=len(stanzas),
        average_words_per_line=round(average, 1),
        rare_words=long_words[:12],
        archaic_words=archaic[:12],
        long_sentences=long_sentences,
        possible_inversions=inversions,
        repeated_lines=repeated[:8],
        score=score,
        difficulty=difficulty,
        size=size,
        chunk_size=chunk_size,
        session_minutes=session_minutes,
    )


def format_analysis(analysis: PoemAnalysis) -> str:
    rare = ", ".join(analysis.rare_words) if analysis.rare_words else "не обнаружены"
    archaic = ", ".join(analysis.archaic_words) if analysis.archaic_words else "не обнаружены"
    repeats = "\n".join(f"• {line}" for line in analysis.repeated_lines) if analysis.repeated_lines else "не обнаружены"
    return (
        f"Размер: {analysis.size}; сложность: {analysis.difficulty} "
        f"(индекс {analysis.score}/100).\n"
        f"Строк: {analysis.line_count}; строф: {analysis.stanza_count}; "
        f"в среднем слов в строке: {analysis.average_words_per_line}.\n"
        f"Длинные/редкие слова: {rare}.\n"
        f"Архаизмы: {archaic}.\n"
        f"Длинных синтаксических периодов: {analysis.long_sentences}; "
        f"возможных инверсий: {analysis.possible_inversions}.\n"
        f"Повторы и рефрены (облегчают запоминание):\n{repeats}\n\n"
        "Оценка лексики и синтаксиса эвристическая: бот не ставит литературоведческий диагноз, "
        "а использует её для выбора размера блоков и расписания."
    )
