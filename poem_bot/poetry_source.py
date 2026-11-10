import asyncio
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

API_URL = "https://ru.wikisource.org/w/api.php"
USER_AGENT = "PoemMemoryBot/1.0 (educational Telegram bot; Wikisource search)"


class SourceError(Exception):
    pass


@dataclass(slots=True)
class SearchResult:
    page_id: int
    title: str


@dataclass(slots=True)
class FoundPoem:
    title: str
    author: str
    text: str
    source_url: str


def _request(params: dict) -> dict:
    url = API_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise SourceError("Открытый источник сейчас недоступен") from exc


def _search_sync(title: str, author: str) -> list[SearchResult]:
    query = f'"{title}" {author}'.strip()
    data = _request({
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srnamespace": 0,
        "srlimit": 8,
        "format": "json",
        "formatversion": 2,
    })
    results = []
    title_folded = title.casefold()
    for item in data.get("query", {}).get("search", []):
        page_title = item.get("title", "")
        if title_folded and title_folded not in page_title.casefold():
            continue
        results.append(SearchResult(page_id=int(item["pageid"]), title=page_title))
    return results[:5]


def _clean_wikiline(line: str) -> str:
    line = re.sub(r"<ref\b[^>/]*>.*?</ref>", "", line, flags=re.IGNORECASE | re.DOTALL)
    line = re.sub(r"<ref\b[^>]*/>", "", line, flags=re.IGNORECASE)
    line = re.sub(r"\{\{\s*(?:nr|номер)\s*\|[^}]*\}\}", "", line, flags=re.IGNORECASE)
    line = re.sub(r"\[\[[^]|]+\|([^\]]+)\]\]", r"\1", line)
    line = re.sub(r"\[\[([^\]]+)\]\]", r"\1", line)
    line = re.sub(r"\{\{[^{}]*\}\}", "", line)
    line = re.sub(r"<br\s*/?>", "\n", line, flags=re.IGNORECASE)
    line = re.sub(r"<[^>]+>", "", line)
    line = line.replace("'''", "").replace("''", "")
    return html.unescape(line).lstrip(": ").strip()


def _metadata(wikitext: str, key: str) -> str:
    match = re.search(rf"^\|\s*{re.escape(key)}\s*=\s*(.*?)\s*$", wikitext, re.MULTILINE | re.IGNORECASE)
    return _clean_wikiline(match.group(1)) if match else ""


def _fetch_sync(result: SearchResult) -> FoundPoem:
    data = _request({
        "action": "parse",
        "pageid": result.page_id,
        "prop": "wikitext",
        "format": "json",
        "formatversion": 2,
    })
    parsed = data.get("parse", {})
    wikitext = parsed.get("wikitext", "")
    if not wikitext:
        raise SourceError("Источник вернул пустую страницу")

    blocks = re.findall(r"<poem\b[^>]*>(.*?)</poem>", wikitext, re.IGNORECASE | re.DOTALL)
    if not blocks:
        raise SourceError("На странице не найден текст стихотворения")
    cleaned_blocks = []
    for block in blocks:
        lines = [_clean_wikiline(line) for line in block.splitlines()]
        cleaned_blocks.append("\n".join(lines).strip())
    text = "\n\n".join(block for block in cleaned_blocks if block).strip()
    if len(text.splitlines()) < 2 or len(text.split()) < 6:
        raise SourceError("Текст на странице слишком короткий или повреждён")

    title = _metadata(wikitext, "НАЗВАНИЕ") or re.sub(r"\s*\(.*\)$", "", result.title)
    author = _metadata(wikitext, "АВТОР")
    if not author:
        match = re.search(r"\(([^)]+)\)", result.title)
        author = match.group(1) if match else "Автор не указан"
    source_url = "https://ru.wikisource.org/?curid=" + str(result.page_id)
    return FoundPoem(title=title, author=author, text=text, source_url=source_url)


async def search_poems(title: str, author: str) -> list[SearchResult]:
    return await asyncio.to_thread(_search_sync, title, author)


async def fetch_poem(result: SearchResult) -> FoundPoem:
    return await asyncio.to_thread(_fetch_sync, result)
