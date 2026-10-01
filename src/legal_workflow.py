"""Lossless, checksum-bound translation of official Master.Rule documents."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re

from .master_workflow import validate, write_table

JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
URL = r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"
PREFIX = r"^[ \t\u3000]*(?:(?:[（(][0-9０-９]+[）)])|(?:[0-9０-９]+[.．])|(?:[0-9０-９]+[ \t]+)|[■◆●・○※])[ \t\u3000]*"
PROTECTED = re.compile(PREFIX + "|" + URL + r"|[0-9０-９]+|^[ \t\u3000]+|[ \t\u3000]+$")
MARKER = re.compile(r"⟪KEEP\d+⟫")
TRANSLATABLE = {"1", "2", "3", "4", "5", "7"}


def checksum(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_source(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or set(data.get("rules", {})) != set(map(str, range(1, 8))):
        raise ValueError("Expected schema v1 with all seven rule categories")
    for key, rule in data["rules"].items():
        if rule["rule_type"] != int(key) or checksum(rule["source"]) != rule["source_sha256"]:
            raise ValueError(f"Rule {key}: invalid source checksum or type")
        ids = set()
        for segment in rule["segments"]:
            if segment["id"] in ids or segment["line_ending"] not in ("", "\r", "\n", "\r\n"):
                raise ValueError(f"Rule {key}: invalid segment")
            if "\r" in segment["source"] or "\n" in segment["source"]:
                raise ValueError(f"Rule {key}: segment contains line ending")
            ids.add(segment["id"])
        if "".join(s["source"] + s["line_ending"] for s in rule["segments"]) != rule["source"]:
            raise ValueError(f"Rule {key}: lossy source segmentation")
    return data


def needs_translation(key, text):
    return key in TRANSLATABLE and bool(JAPANESE.search(text))


def check_line(source, target):
    if not source.strip():
        if target != source:
            raise ValueError("Blank line changed")
        return
    validate(source, target)
    if PROTECTED.findall(source) != PROTECTED.findall(target):
        raise ValueError("Numbering, URLs, numbers or boundary whitespace changed")
    if MARKER.search(target):
        raise ValueError("Unrestored protected token")


def initialize(source, path: Path):
    output = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"schema_version": 1, "rules": {}}
    if output.get("schema_version") != 1 or not isinstance(output.get("rules"), dict):
        raise ValueError("Invalid legal translation schema")
    if set(output["rules"]) - set(source["rules"]):
        raise ValueError("Unknown translated rule type")
    for key, rule in source["rules"].items():
        item = output["rules"].setdefault(key, {"source_sha256": rule["source_sha256"], "segments": {}})
        if item.get("source_sha256") != rule["source_sha256"]:
            raise ValueError(f"Rule {key}: source changed; review and migrate translations explicitly")
        segments = item["segments"]
        if set(segments) - {s["id"] for s in rule["segments"]}:
            raise ValueError(f"Rule {key}: unknown segment IDs")
        for segment in rule["segments"]:
            sid, original = segment["id"], segment["source"]
            segments.setdefault(sid, "" if needs_translation(key, original) else original)
            target = segments[sid]
            if not isinstance(target, str):
                raise ValueError("Translation must be a string")
            if not needs_translation(key, original):
                if target != original:
                    raise ValueError(f"Rule {key}/{sid}: retained text changed")
            elif target:
                check_line(original, target)
    return output


def pending(source, translations):
    return [{"id": key + "/" + s["id"], "speaker": rule["category"], "source": s["source"]}
            for key, rule in source["rules"].items() for s in rule["segments"]
            if needs_translation(key, s["source"]) and not translations["rules"][key]["segments"][s["id"]]]


def protect(text):
    if MARKER.search(text):
        raise ValueError("Source already contains reserved markers")
    literals = {}
    def replace(match):
        token = f"⟪KEEP{len(literals):03d}⟫"
        literals[token] = match.group()
        return token
    return PROTECTED.sub(replace, text), literals


def translate_batch(rows, service, glossary):
    request, guards = [], {}
    for row in rows:
        text, literals = protect(row["source"])
        request.append({**row, "source": text})
        guards[row["id"]] = literals
    result = service.translate_batch(request, glossary)
    if set(result) != {r["id"] for r in rows}:
        raise ValueError("Model returned missing or unexpected IDs")
    checked = {}
    for row in rows:
        target = result[row["id"]]
        literals = guards[row["id"]]
        if Counter(MARKER.findall(target)) != Counter(literals.keys()):
            raise ValueError("Model changed protected tokens")
        target = MARKER.sub(lambda m: literals[m.group()], target)
        check_line(row["source"], target)
        checked[row["id"]] = target
    return checked


def run(source, translations, path, service, glossary, batch_size=8, workers=50):
    if not 1 <= batch_size <= 50 or not 1 <= workers <= 100:
        raise ValueError("batch-size must be 1..50 and workers 1..100")
    rows = pending(source, translations)
    batches, batch, chars = [], [], 0
    for row in rows:
        if batch and (len(batch) >= batch_size or chars + len(row["source"]) > 1600):
            batches.append(batch)
            batch, chars = [], 0
        batch.append(row)
        chars += len(row["source"])
    if batch:
        batches.append(batch)
    failures = []
    def attempt(batch):
        try:
            return translate_batch(batch, service, glossary), []
        except Exception:
            # One small retry isolates a malformed output without unbounded retries.
            good, failed = {}, []
            for row in batch:
                try:
                    good.update(translate_batch([row], service, glossary))
                except Exception as exc:
                    failed.append({"id": row["id"], "error": type(exc).__name__})
            return good, failed
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(attempt, batch) for batch in batches]
        for number, future in enumerate(as_completed(futures), 1):
            good, failed = future.result()
            for identifier, text in good.items():
                key, sid = identifier.split("/", 1)
                translations["rules"][key]["segments"][sid] = text
            failures.extend(failed)
            write_table(path, translations)
            print(f"Legal batches {number}/{len(batches)}; pending {len(pending(source, translations))}", flush=True)
    return {"requested_segments": len(rows), "remaining_segments": len(pending(source, translations)), "failures": failures}


def compile_runtime(source, translations):
    if pending(source, translations):
        raise ValueError("Incomplete legal translation; runtime output not updated")
    rules = {}
    for key, rule in source["rules"].items():
        item = translations["rules"][key]
        if item["source_sha256"] != rule["source_sha256"]:
            raise ValueError("Stale translation checksum")
        target = "".join(item["segments"][s["id"]] + s["line_ending"] for s in rule["segments"])
        for s in rule["segments"]:
            check_line(s["source"], item["segments"][s["id"]])
        if key not in TRANSLATABLE and target != rule["source"]:
            raise ValueError("Retained rule changed")
        rules[key] = {"source": rule["source"], "translation": target,
                      "source_sha256": rule["source_sha256"], "translation_sha256": checksum(target)}
    return {"schema_version": 1, "rules": rules}
