"""Translate reviewed IDOLY PRIDE notice DOM text into page-scoped JSON."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re

from .master_workflow import (batches, normalize_line_endings, repair_log_tail,
                              translate_batch, validate, write_table)


NOTICE_PATH = re.compile(r"^/notice/[0-9a-f]{64}/index\.html$")
JAPANESE_OR_HAN = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
DATE_ONLY = re.compile(r"[0-9\s/\-:().~〜～年月火水木金土日]+")
NUMBER = re.compile(r"[0-9０-９]+(?:[,.][0-9０-９]+)*")


def validate_notice(source: str, target: str) -> None:
    validate(source, target)
    if NUMBER.findall(source) != NUMBER.findall(target):
        raise ValueError("notice numbers, dates or amounts changed")


class NumberCheckedService:
    def __init__(self, inner):
        self.inner = inner

    def translate_batch(self, lines, glossary):
        result = self.inner.translate_batch(lines, glossary)
        for line in lines:
            identifier = line["id"]
            if identifier in result:
                value = normalize_line_endings(line["source"], result[identifier])
                validate_notice(line["source"], value)
                result[identifier] = value
        return result


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_pages(source_path: Path, translation_path: Path, ui_path: Path):
    source = json.loads(source_path.read_text(encoding="utf-8"))
    translated = json.loads(translation_path.read_text(encoding="utf-8"))
    ui = json.loads(ui_path.read_text(encoding="utf-8")) if ui_path else {}
    if (not isinstance(source, dict) or not isinstance(source.get("pages"), dict) or
            not isinstance(translated, dict) or not isinstance(translated.get("pages"), dict) or
            not isinstance(ui, dict) or not isinstance(ui.get("text", {}), dict)):
        raise ValueError("Notice source, translations and UI aliases must be JSON dictionaries")
    sources = source["pages"]
    translations = translated["pages"]
    if set(translations) - set(sources):
        raise ValueError("Notice translations contain an unknown page")
    for page, data in sources.items():
        if (not NOTICE_PATH.fullmatch(page) or not isinstance(data, dict) or
                not isinstance(data.get("title"), str) or
                not isinstance(data.get("texts"), list) or
                any(not isinstance(text, str) or not text
                    for text in data["texts"]) or
                len(data["texts"]) != len(set(data["texts"]))):
            raise ValueError(f"Invalid notice source page: {page}")
        values = translations.get(page, {})
        if (not isinstance(values, dict) or set(values) - set(data["texts"]) or
                any(not isinstance(value, str) or not value.strip()
                    for value in values.values())):
            raise ValueError(f"Invalid notice translations for {page}")
        for text, value in values.items():
            validate_notice(text, value)
    aliases = ui.get("text", {})
    if any(not isinstance(key, str) or not isinstance(value, str)
           for key, value in aliases.items()):
        raise ValueError("UI notice aliases must map strings to strings")
    return sources, translations, aliases


def needs_translation(text: str) -> bool:
    is_date = DATE_ONLY.fullmatch(text) and any(char.isascii() and char.isdigit()
                                                for char in text)
    return bool(JAPANESE_OR_HAN.search(text)) and not is_date


def approved_sources(review_list: Path, source_path: Path) -> set[tuple[str, str]]:
    review = json.loads(review_list.read_text(encoding="utf-8"))
    if (not isinstance(review, dict) or review.get("source_sha256") != checksum(source_path)
            or not isinstance(review.get("pages"), dict)):
        raise ValueError("Notice review list is stale or invalid; run notice-plan again")
    approved = set()
    for page, texts in review["pages"].items():
        if (not NOTICE_PATH.fullmatch(page) or not isinstance(texts, list) or
                any(not isinstance(text, str) or not text for text in texts) or
                len(texts) != len(set(texts))):
            raise ValueError("Notice review list contains invalid page texts")
        approved.update((page, text) for text in texts)
    return approved


def collect(source_path: Path, translation_path: Path, ui_path: Path,
            log_path: Path) -> tuple[list[tuple[str, str]], dict[tuple[str, str], str], int]:
    sources, translations, aliases = read_pages(source_path, translation_path, ui_path)
    known = {(page, text) for page, data in sources.items() for text in data["texts"]}
    memory = {}
    if log_path.is_file():
        with log_path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    page, text, value = (entry[key] for key in
                                         ("page", "source", "translation"))
                    if not all(isinstance(item, str) for item in (page, text, value)):
                        raise ValueError("log entry fields must be strings")
                    if (page, text) in known:
                        validate_notice(text, value)
                        memory[(page, text)] = value
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError(f"{log_path}:{number}: invalid notice result") from error
    pending = []
    for page, data in sources.items():
        for text in data["texts"]:
            key = (page, text)
            if not needs_translation(text) or text in translations.get(page, {}):
                continue
            alias = aliases.get(text)
            if alias:
                validate_notice(text, alias)
                if alias != text:
                    continue
            if key not in memory:
                pending.append(key)
    return pending, memory, len(known)


def apply_memory(source_path: Path, translation_path: Path, ui_path: Path,
                 memory: dict[tuple[str, str], str]) -> int:
    sources, translations, aliases = read_pages(source_path, translation_path, ui_path)
    changes = 0
    for page, data in sources.items():
        values = translations.setdefault(page, {})
        for text in data["texts"]:
            if text in values or not needs_translation(text):
                continue
            alias = aliases.get(text)
            if alias and alias != text:
                continue
            value = memory.get((page, text))
            if value:
                validate_notice(text, value)
                values[text] = value
                changes += 1
    if changes:
        write_table(translation_path, {"pages": translations})
    return changes


def run(source_path: Path, translation_path: Path, ui_path: Path, log_path: Path,
        review_list: Path, service, glossary: dict[str, str],
        workers: int = 64, batch_size: int = 12,
        max_chars: int = 1600, max_batches: int = 0) -> tuple[int, int, int, int]:
    if not 1 <= workers <= 100 or batch_size < 1 or max_chars < 1 or max_batches < 0:
        raise ValueError("invalid worker, batch or character limit")
    if repair_log_tail(log_path):
        print("Removed an incomplete final notice log record; its source will be retried",
              flush=True)
    source_hash = checksum(source_path)
    pending, memory, total = collect(source_path, translation_path, ui_path, log_path)
    approved = approved_sources(review_list, source_path)
    unreviewed = set(pending) - approved
    if unreviewed:
        raise ValueError(f"Notice review list excludes {len(unreviewed)} pending strings; "
                         "run notice-plan again before sending")
    memory = {key: value for key, value in memory.items() if key in approved}
    by_page = {}
    for page, text in pending:
        by_page.setdefault(page, []).append((text, "Notice", page))
    work = [(page, batch) for page, items in by_page.items()
            for batch in batches(items, batch_size, max_chars)]
    if max_batches:
        work = work[:max_batches]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    failures = new = 0
    checked_service = NumberCheckedService(service)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(translate_batch, checked_service, glossary, items):
                   (page, items)
                   for page, items in work}
        with log_path.open("a", encoding="utf-8") as log:
            for completed, future in enumerate(as_completed(futures), start=1):
                page, items = futures[future]
                try:
                    result = future.result()
                    for text, value in result.items():
                        value = normalize_line_endings(text, value)
                        validate_notice(text, value)
                        log.write(json.dumps({"page": page, "source": text,
                                              "translation": value}, ensure_ascii=False) + "\n")
                        memory[(page, text)] = value
                    log.flush()
                    new += len(result)
                    failures += len(items) - len(result)
                except Exception as error:
                    failures += len(items)
                    print(f"Notice batch failed ({type(error).__name__}): {error}", flush=True)
                if completed % 100 == 0 or completed == len(work):
                    print(f"[{completed}/{len(work)}] {new} new notice strings; "
                          f"{failures} pending after errors", flush=True)
    if checksum(source_path) != source_hash:
        raise ValueError("Notice source changed while translating; rerun before publishing")
    applied = apply_memory(source_path, translation_path, ui_path, memory)
    return new, applied, failures, total
