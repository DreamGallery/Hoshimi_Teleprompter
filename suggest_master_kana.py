#!/usr/bin/env python3
"""Suggest second-pass translations for visible MasterDB kana issues.

Only static MasterDB source text is sent to the configured translation API.
Existing translations stay untouched; suggestions are written to local review files.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path

from main import read_settings
from src.review_inputs import read_table, write_json
from src.master_workflow import batches, translate_batch, validate
from src.translation_service import TranslationService

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data/master"
ORIG = DATA / "orig"
ZH = DATA / "zh-Hans"


def collect(report: Path, orig_dir: Path, zh_dir: Path, include_condition: bool):
    data = json.loads(report.read_text(encoding="utf-8"))
    source_tables = {}
    target_tables = {}
    items = {}
    for issue in data["kana_issues"]:
        table = issue["table"]
        if table == "ConditionDescription" and not include_condition:
            continue
        if table not in source_tables:
            source_tables[table] = read_table(orig_dir / f"{table}.json")
            target_tables[table] = read_table(zh_dir / f"{table}.json")
        record_id, field_path = issue["id"], issue["path"]
        source = source_tables[table][record_id][field_path]
        current = target_tables[table][record_id][field_path]
        location = {"table": table, "id": record_id, "path": field_path}
        entry = items.setdefault(source, {"source": source, "current": current,
                                          "locations": []})
        if entry["current"] != current:
            raise ValueError(f"Conflicting existing translations for {table}:{record_id}")
        entry["locations"].append(location)
    return items


def read_log(path: Path, items: dict) -> dict[str, str]:
    previous = {}
    if not path.is_file():
        return previous
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            entry = json.loads(line)
            source, suggestion = entry["source"], entry["suggestion"]
            if source in items:
                validate(source, suggestion)
                previous[source] = suggestion
    return previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path,
                        default=DATA / "master-review.json")
    parser.add_argument("--orig-dir", type=Path, default=ORIG)
    parser.add_argument("--zh-dir", type=Path, default=ZH)
    parser.add_argument("--include-condition", action="store_true",
                        help="Also suggest translations for internal ConditionDescription")
    parser.add_argument("--name-glossary", type=Path,
                        default=ROOT / "glossaries/name-glossary.json")
    parser.add_argument("--term-glossary", type=Path,
                        default=ROOT / "glossaries/term-glossary.json")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--prompt", type=Path,
                        default=ROOT / "prompts/master-kana-review.txt")
    parser.add_argument("--output", type=Path,
                        default=DATA / "working/master-kana-suggestions.json")
    parser.add_argument("--log", type=Path,
                        default=DATA / "working/master-kana-suggestions.jsonl")
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--max-chars", type=int, default=1400)
    parser.add_argument("--max-batches", type=int, default=0,
                        help="Process only this many batches for a pilot; 0 means all")
    args = parser.parse_args()
    if not 1 <= args.workers <= 100 or args.batch_size < 1 or args.max_chars < 1:
        parser.error("workers must be 1..100; batch-size and max-chars must be positive")
    items = collect(args.report, args.orig_dir, args.zh_dir, args.include_condition)
    existing = read_log(args.log, items)
    pending = []
    for source, entry in items.items():
        if source not in existing:
            first = entry["locations"][0]
            pending.append((source, first["table"], first["path"]))
    if pending:
        settings = read_settings(args.env_file)
        base = os.getenv("OPENAI_API_BASE") or settings.get("OPENAI_API_BASE")
        model = os.getenv("OPENAI_MODEL") or settings.get("OPENAI_MODEL")
        key = os.getenv("OPENAI_API_KEY") or settings.get("OPENAI_API_KEY")
        if not base or not model or not key:
            parser.error("Set API base, model, and key through .env or environment variables")
        service = TranslationService(base, key, model,
                                     args.prompt.read_text(encoding="utf-8"))
        glossary = json.loads(args.name_glossary.read_text(encoding="utf-8"))
        glossary.update(json.loads(args.term_glossary.read_text(encoding="utf-8")))
        work = list(batches(pending, args.batch_size, args.max_chars))
        if args.max_batches:
            work = work[:args.max_batches]
        args.log.parent.mkdir(parents=True, exist_ok=True)
        failures = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(translate_batch, service, glossary, batch): batch
                       for batch in work}
            with args.log.open("a", encoding="utf-8") as stream:
                for future in as_completed(futures):
                    try:
                        suggestions = future.result()
                    except Exception as exc:
                        failures.append({"size": len(futures[future]),
                                         "error": type(exc).__name__})
                        continue
                    for source, suggestion in suggestions.items():
                        existing[source] = suggestion
                        stream.write(json.dumps({"source": source, "suggestion": suggestion},
                                                ensure_ascii=False) + "\n")
                    stream.flush()
        if failures:
            print(f"{len(failures)} batches failed; rerun to retry. No source text was printed.")
    output = [{**entry, "suggestion": existing.get(source, "")}
              for source, entry in items.items()]
    write_json(args.output, {"total_fields": sum(len(item["locations"]) for item in output),
                             "total_unique": len(output),
                             "suggested": sum(bool(item["suggestion"]) for item in output),
                             "items": output})
    print(f"Saved {sum(bool(item['suggestion']) for item in output)}/{len(output)} "
          f"unique suggestions to {args.output}")


if __name__ == "__main__":
    main()
