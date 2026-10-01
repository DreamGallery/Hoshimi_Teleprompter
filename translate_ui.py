#!/usr/bin/env python3
"""Translate captured IDOLY PRIDE i18n values through the CSV translator API."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sys

from main import read_settings
from src.translation_service import TranslationService
from src.review_inputs import (captured_entries, read_json, selected_entries,
                               validate, write_json)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data/ui"


def source_variants(dump: Path) -> dict[str, list[str]]:
    variants = defaultdict(set)
    for _, kind, key, original, _ in captured_entries(dump):
        if kind == "i18n":
            variants[key].add(original)
    return {key: sorted(values) for key, values in variants.items() if len(values) > 1}


def varying_keys(dump: Path) -> set[str]:
    return set(source_variants(dump))


def read_curated_variants(path: Path) -> dict[str, list[str]]:
    variants = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(variants, dict) or any(
            not isinstance(key, str) or not isinstance(values, list)
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) < 2 or len(values) != len(set(values))
            for key, values in variants.items()):
        raise ValueError(f"{path}: each varying key needs two or more distinct source strings")
    return variants


def pending_stable_lines(source: dict, translation: dict,
                         varying: set[str]) -> list[dict[str, str]]:
    return [{"id": key, "speaker": key, "source": value}
            for key, value in source.get("i18n", {}).items()
            if key not in translation.get("i18n", {}) and key not in varying
            and isinstance(value, str) and value]


def pending_reviewed_text_lines(reviewed: list[dict], source: dict,
                                translation: dict) -> tuple[list[dict[str, str]], dict[str, str]]:
    lines = []
    identifiers = {}
    source_text = source.get("text", {})
    translated_text = translation.get("text", {})
    for item in reviewed:
        if item["decision"] != "translate" or item["translation"]:
            continue
        original = item["source"]
        if source_text.get(original) != original:
            raise ValueError("Run runtime_text_review.py apply before translating reviewed Text")
        if original in translated_text:
            continue
        identifier = f"text:{len(lines)}"
        identifiers[identifier] = original
        lines.append({"id": identifier, "speaker": "Runtime Text", "source": original})
    return lines, identifiers


def batches(lines: list[dict[str, str]], max_rows: int, max_chars: int):
    current = []
    length = 0
    for line in lines:
        size = len(line["source"])
        if current and (len(current) >= max_rows or length + size > max_chars):
            yield current
            current = []
            length = 0
        current.append(line)
        length += size
    if current:
        yield current


def translate_lines(lines: list[dict[str, str]], service: TranslationService,
                    glossary: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    error = None
    for _ in range(3):
        try:
            result = service.translate_batch(lines, glossary)
            if set(result) != {line["id"] for line in lines}:
                raise ValueError("Model omitted or added UI keys")
            for line in lines:
                value = result[line["id"]]
                source = line["source"]
                if source.count("\n") == value.count(r"\n") + value.count("\n"):
                    value = value.replace(r"\n", "\n")
                if source.count("\n") != value.count("\n"):
                    raise ValueError(f"{line['id']}: actual line breaks differ")
                if source.count(r"\n") != value.count(r"\n"):
                    raise ValueError(f"{line['id']}: visible line breaks differ")
                validate(source, value, line["id"])
                result[line["id"]] = value
            return result, []
        except (ValueError, KeyError) as exc:
            error = exc
    if len(lines) == 1:
        line = lines[0]
        if "\n" in line["source"]:
            parts = []
            for index, source_part in enumerate(line["source"].split("\n")):
                if not source_part:
                    parts.append("")
                    continue
                segment = [{"id": f"{line['id']}:part:{index}",
                            "speaker": line["speaker"], "source": source_part}]
                result, failures = translate_lines(segment, service, glossary)
                if failures:
                    return {}, failures
                parts.append(result[segment[0]["id"]])
            value = "\n".join(parts)
            validate(line["source"], value, line["id"])
            return {line["id"]: value}, []
        return {}, [f"{lines[0]['id']}: {error}"]
    midpoint = len(lines) // 2
    left, left_errors = translate_lines(lines[:midpoint], service, glossary)
    right, right_errors = translate_lines(lines[midpoint:], service, glossary)
    return left | right, left_errors + right_errors


def apply_completed_batch(path: Path, result: dict[str, str],
                          variant_ids: dict[str, tuple[str, str]] | None = None,
                          text_ids: dict[str, str] | None = None) -> tuple[int, int]:
    """Merge one completed batch into the latest reviewed JSON on disk.

    A reviewer may edit the translation file while API requests are running.
    Existing values, including explicit same-source decisions, take priority.
    """
    current = read_json(path)
    current.setdefault("i18n", {})
    current.setdefault("i18n_by_source", {})
    current.setdefault("text", {})
    if variant_ids is not None and text_ids is not None:
        raise ValueError("A translation batch cannot mix source variants and Text")
    added = preserved = 0
    for identifier, value in result.items():
        if text_ids is not None:
            values = current["text"]
            key = text_ids[identifier]
        elif variant_ids is None:
            values = current["i18n"]
            key = identifier
        else:
            key, source = variant_ids[identifier]
            values = current["i18n_by_source"].setdefault(key, {})
            if not isinstance(values, dict):
                raise ValueError(f"i18n_by_source:{key}: expected source/translation object")
            key = source
        if key in values:
            preserved += 1
        else:
            values[key] = value
            added += 1
    if added:
        write_json(path, current)
    return added, preserved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DATA / "source.json")
    parser.add_argument("--translations", type=Path,
                        default=DATA / "zh-Hans.json")
    parser.add_argument("--stable-dump", type=Path,
                        help="Skip i18n keys observed with different Japanese values")
    parser.add_argument("--variants-only", action="store_true",
                        help="Translate every original value for varying i18n keys")
    parser.add_argument("--text-only", action="store_true",
                        help="Translate only human-approved runtime Text strings")
    parser.add_argument("--text-candidates", type=Path,
                        default=DATA / "runtime-text-candidates.json")
    parser.add_argument("--text-review", type=Path,
                        default=DATA / "runtime-text-review.json")
    parser.add_argument("--variants-source", type=Path,
                        default=DATA / "i18n-variants-source.json",
                        help="Reviewed fixed game strings to use with --variants-only")
    parser.add_argument("--glossary", type=Path,
                        default=ROOT / "glossaries/name-glossary.json")
    parser.add_argument("--prompt", type=Path, default=ROOT / "prompts/ui.txt")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--max-chars", type=int, default=1200)
    parser.add_argument("--max-batches", type=int, default=0,
                        help="Limit requests for a pilot run; 0 means all")
    args = parser.parse_args()
    if not 1 <= args.workers <= 100 or args.batch_size < 1 or args.max_chars < 1 or args.max_batches < 0:
        parser.error("workers must be 1-100 and batch limits must be positive")
    if args.variants_only and args.text_only:
        parser.error("--variants-only and --text-only cannot be combined")
    source = read_json(args.source)
    translation = read_json(args.translations)
    translation.setdefault("i18n", {})
    translation.setdefault("i18n_by_source", {})
    try:
        curated_variants = read_curated_variants(args.variants_source) if not args.text_only else {}
        observed_variants = source_variants(args.stable_dump) if args.stable_dump and not args.text_only else {}
    except (OSError, ValueError) as error:
        parser.error(str(error))
    skipped = set(curated_variants) | set(observed_variants)
    variant_ids = {}
    text_ids = {}
    if args.text_only:
        try:
            lines, text_ids = pending_reviewed_text_lines(
                selected_entries(args.text_candidates, args.text_review), source, translation)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            parser.error(str(error))
    elif args.variants_only:
        lines = []
        for key, values in sorted(curated_variants.items()):
            for original in values:
                if not original or original in translation["i18n_by_source"].get(key, {}):
                    continue
                identifier = f"variant:{len(variant_ids)}"
                variant_ids[identifier] = (key, original)
                lines.append({"id": identifier, "speaker": key, "source": original})
    else:
        lines = pending_stable_lines(source, translation, skipped)
    groups = list(batches(lines, args.batch_size, args.max_chars))
    if args.max_batches:
        groups = groups[:args.max_batches]
    kind = "reviewed Text values" if args.text_only else (
        "varying values" if args.variants_only else "stable i18n keys")
    print(f"Pending {len(lines)} {kind} "
          f"in {len(groups)} selected batches; {len(skipped)} varying keys observed", flush=True)
    if not groups:
        return
    settings = read_settings(args.env_file)
    api_base = os.getenv("OPENAI_API_BASE") or settings.get("OPENAI_API_BASE")
    model = os.getenv("OPENAI_MODEL") or settings.get("OPENAI_MODEL")
    key = os.getenv("OPENAI_API_KEY") or settings.get("OPENAI_API_KEY")
    if not api_base or not model or not key:
        parser.error("Set API base, model and key in the env file or environment")
    service = TranslationService(api_base, key, model,
                                 args.prompt.read_text(encoding="utf-8"), max_tokens=8192)
    glossary = json.loads(args.glossary.read_text(encoding="utf-8"))
    errors = []
    completed = preserved = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(translate_lines, group, service, glossary): group
                   for group in groups}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                result, batch_errors = future.result()
                added, kept = apply_completed_batch(
                    args.translations, result,
                    variant_ids if args.variants_only else None,
                    text_ids if args.text_only else None)
                completed += added
                preserved += kept
                errors.extend(batch_errors)
                if batch_errors:
                    print(f"[{number}/{len(groups)}] {len(batch_errors)} keys failed", flush=True)
                elif number % 20 == 0 or number == len(groups):
                    print(f"[{number}/{len(groups)}] translated {completed} i18n keys", flush=True)
            except Exception as exc:
                errors.append(f"Batch failed: {exc}")
                print(f"[{number}/{len(groups)}] FAILED: {exc}", flush=True)
    print(f"Added {completed} {kind}; preserved {preserved} existing values; "
          f"{len(errors)} failures", flush=True)
    for error in errors[:30]:
        print(error, file=sys.stderr)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
