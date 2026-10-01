"""Read UI dictionaries and reviewed translation inputs without a plugin dependency.

Runtime review creation stays with the capture tools. This module only reads
explicitly supplied files and validates their schema, decisions and source hash.
"""

from collections import Counter
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .master_workflow import TOKEN


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".localize-", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {"text": {}, "i18n": {}}
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    for name in ("text", "i18n"):
        if not isinstance(value.get(name, {}), dict):
            raise ValueError(f"{path}: {name} must be an object")
    if not isinstance(value.get("i18n_by_source", {}), dict):
        raise ValueError(f"{path}: i18n_by_source must be an object")
    return value


def captured_entries(dump_path: Path):
    """Read a captured JSONL file strictly, recovering NUL-padded log records."""
    with dump_path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            recovered = line.startswith("\x00")
            if recovered:
                line = line.lstrip("\x00")
            try:
                item = json.loads(line)
                kind = item["kind"]
                original = item["source"]
                key = original if kind == "text" else item["key"]
                if kind not in ("text", "i18n") or not isinstance(original, str) or not isinstance(key, str):
                    raise ValueError("invalid entry")
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError(f"{dump_path}:{number}: {error}") from error
            yield number, kind, key, original, recovered


def validate(source: str, translated: str, label: str) -> None:
    if not isinstance(translated, str):
        raise ValueError(f"{label}: translation must be a string")
    if not translated:
        return
    if Counter(TOKEN.findall(source)) != Counter(TOKEN.findall(translated)):
        raise ValueError(f"{label}: placeholders or rich-text tags differ from source")


def read_table(path):
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON dictionary")
    for record_id, fields in data.items():
        if not isinstance(record_id, str) or not isinstance(fields, dict) or any(
                not isinstance(field, str) or not isinstance(value, str)
                for field, value in fields.items()):
            raise ValueError(f"{path}: expected record ID -> field path -> string")
    return data


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_candidates(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("audit_schema_version") != 2 or
            data.get("scope") != "local_review_only_no_automatic_translation" or
            not isinstance(data.get("texts"), list)):
        raise ValueError(f"{path}: invalid or outdated runtime candidate list")
    seen = set()
    for item in data["texts"]:
        if (not isinstance(item, dict) or not isinstance(item.get("source"), str) or
                not item["source"] or item["source"] in seen or
                item.get("category") != "still_unmatched" or
                not isinstance(item.get("bucket"), str) or
                item["bucket"].split("_", 1)[0] not in ("kana", "han") or
                type(item.get("occurrences")) is not int or item["occurrences"] < 1):
            raise ValueError(f"{path}: invalid runtime candidate entry")
        seen.add(item["source"])
    return data["texts"]


def validate_review_row(item: dict) -> None:
    if (not isinstance(item, dict) or not isinstance(item.get("source"), str) or
            not item["source"] or type(item.get("occurrences")) is not int or
            item["occurrences"] < 1 or
            item.get("decision") not in ("skip", "translate", "retain") or
            not isinstance(item.get("translation"), str)):
        raise ValueError("Runtime review contains an invalid decision or candidate")
    if item["decision"] in ("skip", "retain") and item["translation"]:
        raise ValueError("Skipped or retained runtime text must not contain a translation")
    if item["translation"]:
        validate(item["source"], item["translation"], "runtime_text")
        if (item["source"].count("\n") != item["translation"].count("\n") or
                item["source"].count(r"\n") != item["translation"].count(r"\n")):
            raise ValueError("Runtime Text translation changed line break structure")


def selected_entries(candidate_path: Path, review_path: Path) -> list[dict]:
    candidates = read_candidates(candidate_path)
    data = json.loads(review_path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("scope") != "runtime_text_human_review" or
            data.get("candidate_sha256") != sha256(candidate_path) or
            not isinstance(data.get("entries"), list)):
        raise ValueError("Runtime review is invalid or stale; compare it with a fresh candidate list")
    expected = {item["source"]: item["occurrences"] for item in candidates}
    rows = data["entries"]
    if len(rows) != len(expected):
        raise ValueError("Runtime review omits or duplicates candidates")
    seen = set()
    selected = []
    for item in rows:
        validate_review_row(item)
        if (item["source"] in seen or item["source"] not in expected or
                expected[item["source"]] != item["occurrences"]):
            raise ValueError("Runtime review contains an invalid decision or candidate")
        seen.add(item["source"])
        if item["decision"] == "skip":
            continue
        selected.append(item)
    return selected
