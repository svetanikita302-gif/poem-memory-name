import random
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .analysis import WORD_RE, words


@dataclass(slots=True)
class Comparison:
    score: float
    errors: list[str]


def mask_text(text: str, percent: int, seed: int) -> str:
    matches = list(WORD_RE.finditer(text))
    hidden_count = round(len(matches) * percent / 100)
    hidden = set(random.Random(seed).sample(range(len(matches)), hidden_count))
    pieces = []
    cursor = 0
    for index, match in enumerate(matches):
        pieces.append(text[cursor:match.start()])
        if index in hidden:
            pieces.append("_" * max(3, min(len(match.group()), 12)))
        else:
            pieces.append(match.group())
        cursor = match.end()
    pieces.append(text[cursor:])
    return "".join(pieces)


def first_letters(text: str) -> str:
    def replace(match: re.Match) -> str:
        word = match.group()
        return word[0] + "_" * max(2, min(len(word) - 1, 8))

    return WORD_RE.sub(replace, text)


def compare_texts(expected: str, answer: str) -> Comparison:
    expected_words = words(expected)
    answer_words = words(answer)
    if not answer_words:
        return Comparison(score=0.0, errors=["Вы не прислали слова для проверки."])

    matcher = SequenceMatcher(a=expected_words, b=answer_words, autojunk=False)
    errors = []
    for operation, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        wanted = " ".join(expected_words[a_start:a_end])
        received = " ".join(answer_words[b_start:b_end])
        if wanted and received:
            errors.append(f"«{received}» → нужно «{wanted}»")
        elif wanted:
            errors.append(f"пропущено: «{wanted}»")
        else:
            errors.append(f"лишнее: «{received}»")
    return Comparison(score=round(matcher.ratio(), 3), errors=errors[:8])


def chunk_lines(text: str, preferred_size: int) -> list[list[str]]:
    lines = text.splitlines()
    chunks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        current.append(line)
        nonempty = sum(bool(item.strip()) for item in current)
        stanza_boundary = not line.strip()
        sentence_boundary = bool(re.search(r"[.!?…][\"'»”)]*$", line.strip()))
        if nonempty and (nonempty >= preferred_size or (nonempty >= 2 and (stanza_boundary or sentence_boundary))):
            while current and not current[-1].strip():
                current.pop()
            if current:
                chunks.append(current)
            current = []
    if current:
        while current and not current[-1].strip():
            current.pop()
        if current:
            chunks.append(current)
    return chunks
