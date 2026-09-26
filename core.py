"""Subtitle and dictionary helpers for Series English.

Only Python's standard library is required. The media player itself is mpv.
"""

from __future__ import annotations

import html
import json
import csv
import sqlite3
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


WORD_RE = re.compile(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*")
DEFAULT_ECDICT_DB = Path.home() / ".series_english" / "ecdict.sqlite3"
HAN_RE = re.compile(r"[\u3400-\u9fff]")
ASS_TAG_RE = re.compile(r"\{[^{}]*\}")
ASS_LINE_BREAK_RE = re.compile(r"\\[Nn]")

@dataclass(frozen=True)
class SplitAss:
    english: str
    chinese: str
    bilingual_events: int


def decode_subtitle(data: bytes) -> str:
    """Read common English/Chinese ASS encodings without adding a dependency."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise UnicodeError("无法识别字幕编码，请转换为 UTF-8 后重试")


def _segment_language(segment: str) -> str:
    clean = ASS_TAG_RE.sub("", segment).replace(r"\h", " ")
    latin = len(re.findall(r"[A-Za-z]", clean))
    chinese = len(HAN_RE.findall(clean))
    if latin and chinese:
        return "mixed"
    if latin:
        return "en"
    if chinese:
        return "zh"
    return "other"


def split_bilingual_ass(source: str) -> SplitAss | None:
    """Split ASS events with separate English/Chinese lines, retaining their timing.

    Ambiguous mixed-language segments are rejected instead of silently hiding
    part of the subtitle. ASS drawings/signs are beyond this splitter.
    """
    lines = source.splitlines(keepends=True)
    english_lines: list[str] = []
    chinese_lines: list[str] = []
    in_events = False
    field_count: int | None = None
    text_index: int | None = None
    paired = 0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.lower() == "[events]"
        if in_events and stripped.lower().startswith("format:"):
            names = [x.strip().lower() for x in stripped.split(":", 1)[1].split(",")]
            field_count = len(names)
            text_index = names.index("text") if "text" in names else None

        if not (in_events and stripped.lower().startswith("dialogue:")):
            english_lines.append(line)
            chinese_lines.append(line)
            continue
        if field_count is None or text_index != field_count - 1:
            return None

        prefix, payload = line.split(":", 1)
        body = payload.rstrip("\r\n")
        newline = payload[len(body):]
        fields = body.split(",", field_count - 1)
        if len(fields) != field_count:
            return None
        segments = ASS_LINE_BREAK_RE.split(fields[text_index])
        kinds = [_segment_language(s) for s in segments]
        if "mixed" in kinds:
            return None
        en = [s for s, kind in zip(segments, kinds) if kind == "en"]
        zh = [s for s, kind in zip(segments, kinds) if kind == "zh"]
        if en and zh:
            paired += 1
        if en:
            changed = fields.copy()
            changed[text_index] = r"\N".join(en)
            english_lines.append(prefix + ":" + ",".join(changed) + newline)
        if zh:
            changed = fields.copy()
            changed[text_index] = r"\N".join(zh)
            chinese_lines.append(prefix + ":" + ",".join(changed) + newline)

    if paired == 0:
        return None
    return SplitAss("".join(english_lines), "".join(chinese_lines), paired)


def language_hint(track: dict[str, Any]) -> str | None:
    """Use track metadata when available; leave uncertain tracks for the user."""
    lang = str(track.get("lang") or "").lower().replace("_", "-")
    if lang.split("-")[0] in {"en", "eng"}:
        return "en"
    if lang.split("-")[0] in {"zh", "zho", "chi", "chs", "cht", "cmn"}:
        return "zh"
    title = str(track.get("title") or "")
    filename = Path(str(track.get("external-filename") or "")).name
    name = (title + " " + filename).lower()
    if re.search(r"(?:^|[._\s\[(-])(en|eng|english)(?:$|[._\s\])+-])", name):
        return "en"
    if re.search(r"(?:^|[._\s\[(-])(zh|zho|chi|chs|cht|chinese|简体|繁体|中文)(?:$|[._\s\])+-])", name):
        return "zh"
    return None


def suggest_tracks(tracks: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    subtitles = [t for t in tracks if t.get("type") == "sub" and isinstance(t.get("id"), int)]
    en = next((t["id"] for t in subtitles if language_hint(t) == "en"), None)
    zh = next((t["id"] for t in subtitles if language_hint(t) == "zh" and t["id"] != en), None)
    if en is None and len(subtitles) == 1 and zh is None:
        en = subtitles[0]["id"]
    return en, zh


def normalize_word(word: str) -> str:
    match = WORD_RE.fullmatch(word.strip())
    return match.group(0).lower() if match else ""


@dataclass(frozen=True)
class WordSense:
    definition: str
    example: str = ""
    chinese: str = ""


@dataclass(frozen=True)
class WordGroup:
    part_of_speech: str
    senses: tuple[WordSense, ...]


@dataclass(frozen=True)
class WordEntry:
    word: str
    phonetic: str
    chinese: tuple[str, ...]
    meanings: tuple[WordGroup, ...]
    source: str
    chinese_groups: tuple["ChineseGroup", ...] = ()


@dataclass(frozen=True)
class ChineseGroup:
    part_of_speech: str
    senses: tuple[str, ...]


POS_NAMES = {
    "n": "名词 n.", "v": "动词 v.", "vt": "及物动词 vt.", "vi": "不及物动词 vi.",
    "adj": "形容词 adj.", "a": "形容词 adj.", "adv": "副词 adv.", "ad": "副词 adv.",
    "prep": "介词 prep.", "conj": "连词 conj.", "pron": "代词 pron.",
    "int": "感叹词 int.", "interj": "感叹词 interj.", "num": "数词 num.",
    "abbr": "缩写 abbr.",
}


def parse_chinese_groups(translation: str) -> tuple[ChineseGroup, ...]:
    """Preserve ECDICT's lines and split multiple senses within each part of speech."""
    groups: dict[str, list[str]] = {}
    for line in translation.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z]+)\.\s*(.*)$", line)
        if match and match.group(1).lower() in POS_NAMES:
            part, body = POS_NAMES[match.group(1).lower()], match.group(2)
        elif line.startswith("[网络]"):
            part, body = "网络用法", line[len("[网络]"):].strip()
        else:
            part, body = "其他", line
        bucket = groups.setdefault(part, [])
        for sense in re.split(r"[；;]", body):
            sense = sense.strip()
            if sense and sense not in bucket:
                bucket.append(sense)
    return tuple(ChineseGroup(part, tuple(senses)) for part, senses in groups.items() if senses)


def import_ecdict_csv(source: Path, destination: Path = DEFAULT_ECDICT_DB) -> int:
    """Create an indexed local dictionary from the user-selected ECDICT CSV."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".importing")
    count = 0
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as file, closing(sqlite3.connect(temporary)) as db, db:
            reader = csv.DictReader(file)
            if not {"word", "translation", "definition", "phonetic"}.issubset(reader.fieldnames or []):
                raise ValueError("请选择包含 word、translation、definition、phonetic 列的 ECDICT CSV 文件")
            db.execute("CREATE TABLE IF NOT EXISTS entries (word TEXT PRIMARY KEY COLLATE NOCASE, "
                       "phonetic TEXT, translation TEXT, definition TEXT)")
            db.execute("DELETE FROM entries")
            batch: list[tuple[str, str, str, str]] = []
            for row in reader:
                word = (row.get("word") or "").strip()
                if not normalize_word(word) or not (row.get("translation") or row.get("definition")):
                    continue
                batch.append((word, row.get("phonetic") or "", row.get("translation") or "",
                              row.get("definition") or ""))
                if len(batch) >= 1000:
                    db.executemany("INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?)", batch)
                    count += len(batch)
                    batch.clear()
            if batch:
                db.executemany("INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?)", batch)
                count += len(batch)
            if not count:
                raise ValueError("词库文件中没有可用的英文单词")
        temporary.replace(destination)
        return count
    finally:
        temporary.unlink(missing_ok=True)


def _local_entry(word: str, database: Path) -> WordEntry | None:
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute("SELECT phonetic, translation, definition FROM entries "
                                 "WHERE word = ?", (word,)).fetchone()
    if row is None:
        return None
    phonetic, translation, definition = row
    groups = parse_chinese_groups(translation or "")
    english = tuple(WordSense(line.strip()) for line in (definition or "").splitlines() if line.strip())
    meanings = (WordGroup("英文释义", english),) if english else ()
    return WordEntry(word, phonetic or "", (), meanings, "ECDICT 本地词库", groups)


def dictionary_links(word: str) -> dict[str, str]:
    normalized = normalize_word(word)
    if not normalized:
        raise ValueError("请选择一个英文单词")
    encoded = urllib.parse.quote(normalized, safe="")
    return {
        "有道词典": f"https://dict.youdao.com/result?word={encoded}&lang=en",
        "剑桥词典": f"https://dictionary.cambridge.org/dictionary/english-chinese-simplified/{encoded}",
    }


def _get_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "SeriesEnglish/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _dictionary_entry(word: str, payload: Any) -> WordEntry:
    if not isinstance(payload, list):
        raise ValueError("词典未返回有效的词条")
    groups: dict[str, list[WordSense]] = {}
    seen: set[tuple[str, str]] = set()
    phonetic = ""
    for item in payload:
        if not isinstance(item, dict):
            continue
        if not phonetic:
            phonetic = str(item.get("phonetic") or "").strip()
            if not phonetic:
                phonetic = next((str(p.get("text")).strip() for p in item.get("phonetics", [])
                                 if isinstance(p, dict) and p.get("text")), "")
        for meaning in item.get("meanings", []):
            if not isinstance(meaning, dict):
                continue
            part = str(meaning.get("partOfSpeech") or "其他").strip()
            bucket = groups.setdefault(part, [])
            for definition in meaning.get("definitions", []):
                if not isinstance(definition, dict):
                    continue
                explanation = str(definition.get("definition") or "").strip()
                key = (part, explanation.casefold())
                if not explanation or key in seen:
                    continue
                seen.add(key)
                bucket.append(WordSense(explanation, str(definition.get("example") or "").strip()))
    meanings = tuple(WordGroup(part, tuple(senses)) for part, senses in groups.items() if senses)
    if not meanings:
        raise ValueError("词典暂未收录该词")
    return WordEntry(word, phonetic, (), meanings, "Free Dictionary API")


def _chinese_matches(text: str, timeout: float) -> tuple[str, ...]:
    query = urllib.parse.urlencode({"q": text, "langpair": "en|zh-CN"})
    response = _get_json("https://api.mymemory.translated.net/get?" + query, timeout)
    if not isinstance(response, dict) or int(response.get("responseStatus", 0)) != 200:
        raise RuntimeError("中文翻译服务暂不可用")
    candidates = [(response.get("responseData") or {}).get("translatedText", "")]
    # Only exact matching segments: fuzzy translation-memory matches can be unrelated.
    candidates += [match.get("translation", "") for match in (response.get("matches") or [])
                   if isinstance(match, dict) and str(match.get("segment", "")).strip().casefold()
                   == text.casefold()]
    values: list[str] = []
    for candidate in candidates:
        cleaned = html.unescape(str(candidate)).strip()
        if (HAN_RE.search(cleaned) and len(cleaned) <= 240
                and cleaned not in values):
            values.append(cleaned)
    return tuple(values[:10])


def lookup_word(word: str, *, timeout: float = 5.0,
                db_path: Path = DEFAULT_ECDICT_DB) -> WordEntry:
    """Return grouped dictionary senses with Chinese hints; called off the UI thread.

    An imported local Chinese entry is returned immediately, even when offline.
    Otherwise the English dictionary provides its distinct definitions and
    translation memory adds optional Chinese references.
    """
    normalized = normalize_word(word)
    if not normalized:
        raise ValueError("请选择一个英文单词")
    try:
        local = _local_entry(normalized, db_path)
    except (OSError, sqlite3.DatabaseError):
        local = None
    if local is not None and local.chinese_groups:
        return local
    url = "https://api.dictionaryapi.dev/api/v2/entries/en/" + urllib.parse.quote(normalized, safe="")
    try:
        entry = _dictionary_entry(normalized, _get_json(url, timeout))
    except (OSError, ValueError, TypeError, KeyError) as error:
        if local is not None:
            return local
        # The secondary service still provides a short result for inflected or
        # unknown words that are absent from the English dictionary.
        try:
            chinese = _chinese_matches(normalized, timeout)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            raise RuntimeError("在线词典暂不可用，请使用下方词典链接") from error
        if not chinese:
            raise RuntimeError("词典暂未收录该词，请使用下方词典链接") from error
        return WordEntry(normalized, "", chinese, (), "MyMemory 中文速览")

    try:
        summary = _chinese_matches(normalized, timeout)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        summary = ()
    entry = replace(entry, chinese=summary)

    # Render the first two senses for each of up to three parts of speech in
    # Chinese. All remaining English senses stay available in the scroll pane.
    candidates = [(group_index, sense_index, sense.definition)
                  for group_index, group in enumerate(entry.meanings[:3])
                  for sense_index, sense in enumerate(group.senses[:2])]
    if not candidates:
        return entry
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {(group_index, sense_index): pool.submit(_chinese_matches, definition, timeout)
                   for group_index, sense_index, definition in candidates}
        updated = list(entry.meanings)
        rendered = False
        for (group_index, sense_index), future in futures.items():
            try:
                chinese = future.result()
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                continue
            if chinese:
                group = updated[group_index]
                senses = list(group.senses)
                senses[sense_index] = replace(senses[sense_index], chinese=chinese[0])
                updated[group_index] = replace(group, senses=tuple(senses))
                rendered = True
    source = entry.source + (" · MyMemory（中文参考）" if summary or rendered else "")
    return replace(entry, meanings=tuple(updated), source=source)
