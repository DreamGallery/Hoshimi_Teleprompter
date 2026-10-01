"""Translate MasterDB JSON dictionaries with a resumable source-string log."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import tempfile


TOKEN = re.compile(
    r"\{\d+(?::[^{}]+)?\}|%(?:\d+\$)?[-+#0 ]*\d*(?:\.\d+)?[sdif]|"
    r"<[/]?[A-Za-z][^>]*>|<#[0-9A-Fa-f]{6,8}>")
LINE_ENDING = re.compile(r"\r\n|\r|\n")


def normalize_line_endings(source: str, target: str) -> str:
    """Restore the source's exact line separators without changing translated text."""
    source_breaks = LINE_ENDING.findall(source)
    parts = LINE_ENDING.split(target)
    if len(parts) != len(source_breaks) + 1:
        return target
    return "".join(part + ending for part, ending in zip(parts, source_breaks)) + parts[-1]


def read_table(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(not isinstance(fields, dict)
                                         for fields in data.values()):
        raise ValueError(f"{path}: expected nested JSON dictionary")
    return data


def write_table(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".master-", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def repair_log_tail(path: Path) -> bool:
    """Discard only an incomplete final JSONL record left by an interrupted write."""
    if not path.is_file():
        return False
    with path.open("rb+") as stream:
        stream.seek(0, os.SEEK_END)
        end = stream.tell()
        if end == 0:
            return False
        position = end
        while position:
            length = min(position, 4096)
            position -= length
            stream.seek(position)
            block = stream.read(length)
            last_break = block.rfind(b"\n")
            if last_break >= 0:
                start = position + last_break + 1
                break
        else:
            start = 0
        if start == end:
            return False
        stream.seek(start)
        tail = stream.read()
        try:
            json.loads(tail.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            stream.truncate(start)
            return True
        stream.seek(0, os.SEEK_END)
        stream.write(b"\n")
    return False


def title_can_reflow(table: str, field_path: str) -> bool:
    return table == "Story" and field_path == "name"


def validate(source: str, target: str, *, allow_line_reflow: bool = False) -> None:
    if not isinstance(target, str) or not target.strip():
        raise ValueError("empty translation")
    if Counter(TOKEN.findall(source)) != Counter(TOKEN.findall(target)):
        raise ValueError("placeholder or rich-text tag mismatch")
    if source.count("<") != target.count("<") or source.count(">") != target.count(">"):
        raise ValueError("angle bracket count changed")
    if not allow_line_reflow and source.count("\n") != target.count("\n"):
        raise ValueError("actual line break count changed")
    if not allow_line_reflow and source.count("\r") != target.count("\r"):
        raise ValueError("carriage return count changed")
    if source.count(r"\n") != target.count(r"\n"):
        raise ValueError("literal line break count changed")


def collect(orig_dir: Path, zh_dir: Path, log_path: Path):
    if (orig_dir.parent / ".master-export-in-progress.json").is_file():
        raise ValueError("MasterDB export is incomplete; rerun export before translating")
    locations = {}
    missing = {}
    existing = {}
    for path in sorted(orig_dir.glob("*.json")):
        original = read_table(path)
        target_path = zh_dir / path.name
        if not target_path.is_file():
            raise ValueError(f"Missing MasterDB translation table: {target_path}")
        translated = read_table(target_path)
        for record_id, fields in original.items():
            for field_path, source in fields.items():
                if not isinstance(source, str):
                    raise ValueError(f"{path}: invalid source value")
                locations.setdefault(source, set()).add((path.stem, field_path))
                target = translated.get(record_id, {}).get(field_path, "")
                if target:
                    validate(source, target, allow_line_reflow=title_can_reflow(
                        path.stem, field_path))
                    existing.setdefault(source, target)
                else:
                    missing.setdefault(source, set()).add((path.stem, field_path))
    if log_path.is_file():
        with log_path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                entry = json.loads(line)
                source, target = entry["source"], entry["translation"]
                if source in locations and source not in existing:
                    if any(_valid_for_context(source, target, table, field)
                           for table, field in locations[source]):
                        existing[source] = target
    pending = []
    for source, contexts in locations.items():
        target = existing.get(source)
        unresolved = (contexts if target is None else {
            context for context in missing.get(source, ())
            if not _valid_for_context(source, target, *context)})
        if unresolved:
            table, field = sorted(unresolved, key=lambda item:
                                  (title_can_reflow(*item), item))[0]
            pending.append((source, table, field))
    return pending, existing, len(locations)


def _valid_for_context(source: str, target: str, table: str, field_path: str) -> bool:
    try:
        validate(source, target, allow_line_reflow=title_can_reflow(table, field_path))
        return True
    except ValueError:
        return False


def batches(pending, batch_size: int, max_chars: int):
    batch = []
    size = 0
    for item in pending:
        if batch and (len(batch) >= batch_size or size + len(item[0]) > max_chars):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += len(item[0])
    if batch:
        yield batch


def translate_batch(service, glossary, items):
    lines = [{"id": str(index), "speaker": f"{table}:{path}", "source": source}
             for index, (source, table, path) in enumerate(items)]
    for attempt in range(3):
        try:
            result = service.translate_batch(lines, glossary)
            if set(result) != {line["id"] for line in lines}:
                raise ValueError("model returned incomplete field IDs")
            for line in lines:
                result[line["id"]] = normalize_line_endings(
                    line["source"], result[line["id"]])
                _, table, field_path = items[int(line["id"])]
                validate(line["source"], result[line["id"]],
                         allow_line_reflow=title_can_reflow(table, field_path))
            return {line["source"]: result[line["id"]] for line in lines}
        except (ValueError, KeyError):
            if attempt != 2:
                continue
            if len(items) > 1:
                midpoint = len(items) // 2
                translated = {}
                for half in (items[:midpoint], items[midpoint:]):
                    try:
                        translated.update(translate_batch(service, glossary, half))
                    except (ValueError, KeyError):
                        pass
                if translated:
                    return translated
                raise
            source, table, path = items[0]
            if LINE_ENDING.search(source):
                segments = LINE_ENDING.split(source)
                separators = LINE_ENDING.findall(source)
                output = []
                for segment in segments:
                    if segment:
                        output.append(translate_batch(service, glossary,
                                                      [(segment, table, path)])[segment])
                    else:
                        output.append("")
                target = "".join(part + ending for part, ending in
                                 zip(output, separators)) + output[-1]
                validate(source, target,
                         allow_line_reflow=title_can_reflow(table, path))
                return {source: target}
            raise


def apply_memory(orig_dir: Path, zh_dir: Path, memory: dict[str, str]):
    if (orig_dir.parent / ".master-export-in-progress.json").is_file():
        raise ValueError("MasterDB export is incomplete; rerun export before translating")
    changes = 0
    for path in sorted(orig_dir.glob("*.json")):
        original = read_table(path)
        translated = read_table(zh_dir / path.name)
        table_changes = 0
        for record_id, fields in original.items():
            for field_path, source in fields.items():
                if translated.get(record_id, {}).get(field_path):
                    continue
                target = memory.get(source)
                if target:
                    if not _valid_for_context(source, target, path.stem, field_path):
                        continue
                    translated.setdefault(record_id, {})[field_path] = target
                    table_changes += 1
        if table_changes:
            write_table(zh_dir / path.name, translated)
            changes += table_changes
    return changes


def run(orig_dir: Path, zh_dir: Path, log_path: Path, service,
        glossary: dict[str, str], workers: int = 64, batch_size: int = 12,
        max_chars: int = 1600, max_batches: int = 0):
    if not 1 <= workers <= 100 or batch_size < 1 or max_chars < 1 or max_batches < 0:
        raise ValueError("invalid worker, batch or character limit")
    if repair_log_tail(log_path):
        print("Removed an incomplete final MasterDB log record; its source will be retried",
              flush=True)
    pending, memory, total = collect(orig_dir, zh_dir, log_path)
    # ScopedService records a context only after verifying that source-based
    # reuse is safe. New music production credits never need an API request.
    contexts = getattr(service, 'contexts', {})
    credits = {source for source, context in contexts.items()
               if context.get('table') == 'Music' and
               context.get('field') in {'composer', 'lyricist', 'arranger'}}
    for source in credits:
        memory[source] = source
    pending = [item for item in pending if item[0] not in credits]
    work = list(batches(pending, batch_size, max_chars))
    if max_batches:
        work = work[:max_batches]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    new = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(translate_batch, service, glossary, items): items
                   for items in work}
        with log_path.open("a", encoding="utf-8") as log:
            for completed, future in enumerate(as_completed(futures), 1):
                try:
                    output = future.result()
                    for source, target in output.items():
                        log.write(json.dumps({"source": source, "translation": target},
                                             ensure_ascii=False) + "\n")
                        memory[source] = target
                    log.flush()
                    new += len(output)
                    missing = len(futures[future]) - len(output)
                    if missing:
                        failures += missing
                        print(f"Master batch left {missing} strings for retry", flush=True)
                except Exception as error:
                    failures += len(futures[future])
                    print(f"Master batch failed ({type(error).__name__}): {error}", flush=True)
                if completed % 100 == 0 or completed == len(work):
                    print(f"[{completed}/{len(work)}] {new} new unique strings; "
                          f"{failures} pending after errors", flush=True)
    applied = apply_memory(orig_dir, zh_dir, memory)
    return new, applied, failures, total
