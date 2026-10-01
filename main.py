#!/usr/bin/env python3
"""Plan and run IDOLY PRIDE adventure CSV translation."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path

from src.csv_workflow import coverage, csv_files, load_csv, translate_file, verify_source
from src.master_workflow import (collect as collect_master, run as translate_master,
                                 write_table)
from src.master_scoped_workflow import (collect as collect_scoped_master,
                                        run as translate_scoped_master)
from src.notice_workflow import (checksum as notice_checksum, collect as collect_notice,
                                 run as translate_notice)
from src.translation_service import TranslationService
from src.terminology import ScopedService, load_policy


def read_settings(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    result = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in {"OPENAI_API_BASE", "OPENAI_MODEL", "OPENAI_API_KEY"}:
            raise ValueError(f"{path}:{number}: unsupported setting")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def translate_files(files: list[Path], service: TranslationService,
                    glossary: dict[str, str], batch_size: int, source_dir: Path,
                    max_batches: int | None, workers: int) -> tuple[int, list[str]]:
    translated = 0
    failures = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(translate_file, path, service, glossary, batch_size,
                               source_dir, max_batches): path for path in files}
        for number, future in enumerate(as_completed(futures), 1):
            path = futures[future]
            try:
                translated += future.result()
                if number % 100 == 0 or number == len(files):
                    print(f"[{number}/{len(files)}] translated {translated} text fields",
                          flush=True)
            except Exception as exc:
                failures.append(path.name)
                print(f"[{number}/{len(files)}] {path.name}: FAILED: {exc}", flush=True)
    return translated, failures


def load_glossary(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(text, str) or not text
            for key, text in value.items()):
        raise ValueError("Glossary must map original terms to nonempty translations")
    return value


def add_api_options(command: argparse.ArgumentParser, default_prompt: str,
                    term_policy: bool = True) -> None:
    command.add_argument("--prompt", type=Path,
                         default=Path(__file__).parent / "prompts" / default_prompt)
    command.add_argument("--env-file", type=Path, default=Path(__file__).parent / ".env")
    command.add_argument("--api-base")
    command.add_argument("--model")
    command.add_argument("--api-key-env", default="OPENAI_API_KEY")
    command.add_argument("--batch-size", type=int, default=12)
    command.add_argument("--temperature", type=float, default=0.2)
    command.add_argument("--max-tokens", type=int, default=4096)
    command.add_argument("--thinking", choices=["disabled", "enabled"],
                         default="disabled", help="DeepSeek thinking mode")
    if term_policy:
        command.add_argument("--term-policy", type=Path,
                             help="Optional schema v1 scoped terminology policy JSON")


def make_service(args: argparse.Namespace, parser: argparse.ArgumentParser) -> TranslationService:
    settings = read_settings(args.env_file)
    api_base = args.api_base or os.getenv("OPENAI_API_BASE") or settings.get("OPENAI_API_BASE")
    model = args.model or os.getenv("OPENAI_MODEL") or settings.get("OPENAI_MODEL")
    key = os.getenv(args.api_key_env) or settings.get(args.api_key_env)
    if not api_base or not model or not key:
        parser.error("Set API base, model and key through .env or environment variables")
    return TranslationService(api_base, key, model,
                              args.prompt.read_text(encoding="utf-8"),
                              args.temperature, args.max_tokens, args.thinking)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    project_root = Path(__file__).resolve().parent
    data_dir = project_root / "data"
    glossary_dir = Path(__file__).resolve().parent / "glossaries"
    master_common = argparse.ArgumentParser(add_help=False)
    master_common.add_argument("--orig-dir", type=Path,
                               default=data_dir / "master/orig")
    master_common.add_argument("--zh-dir", type=Path,
                               default=data_dir / "master/zh-Hans")
    master_common.add_argument("--log-file", type=Path,
                               default=data_dir / "master/working/results.jsonl")
    master_common.add_argument("--glossary", type=Path,
                               default=glossary_dir / "name-glossary.json")
    master_common.add_argument("--term-glossary", type=Path,
                               default=glossary_dir / "term-glossary.json")
    master_common.add_argument("--term-policy", type=Path,
                               help="Optional schema v1 scoped terminology policy JSON")
    commands.add_parser("master-plan", parents=[master_common],
                        help="Count pending MasterDB JSON strings")
    master_translate = commands.add_parser("master-translate", parents=[master_common],
                                           help="Translate MasterDB JSON dictionaries")
    add_api_options(master_translate, "master.txt", term_policy=False)
    master_translate.add_argument("--workers", type=int, default=64)
    master_translate.add_argument("--max-batches", type=int, default=0)
    master_translate.add_argument("--max-chars", type=int, default=1600)
    notice_common = argparse.ArgumentParser(add_help=False)
    notice_common.add_argument("--source", type=Path,
                               default=data_dir / "notice/source.json")
    notice_common.add_argument("--translations", type=Path,
                               default=data_dir / "notice/zh-Hans.json")
    notice_common.add_argument("--ui", type=Path,
                               default=data_dir / "notice/ui.json")
    notice_common.add_argument("--log-file", type=Path,
                               default=data_dir / "notice/working/results.jsonl")
    notice_common.add_argument("--glossary", type=Path,
                               default=glossary_dir / "name-glossary.json")
    notice_common.add_argument("--term-glossary", type=Path,
                               default=glossary_dir / "term-glossary.json")
    notice_plan = commands.add_parser("notice-plan", parents=[notice_common],
                                      help="Count untranslated official notice DOM strings")
    notice_plan.add_argument("--output", type=Path,
                             help="Write the exact pending page/text list for review")
    notice_translate = commands.add_parser("notice-translate", parents=[notice_common],
                                           help="Translate official notice DOM strings")
    notice_translate.add_argument("--review-list", type=Path, required=True,
                                  help="Exact source list exported by notice-plan --output")
    add_api_options(notice_translate, "notice.txt")
    notice_translate.add_argument("--workers", type=int, default=64)
    notice_translate.add_argument("--max-batches", type=int, default=0)
    notice_translate.add_argument("--max-chars", type=int, default=1600)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--csv-dir", type=Path, default=project_root / "adv/csv")
    common.add_argument("--source-dir", type=Path,
                        help="Verify each CSV against its original adventure TXT")
    common.add_argument("--file", action="append", default=[],
                        help="Select an exact adv_*.csv filename; repeatable")
    common.add_argument("--prefix", action="append", default=[],
                        help="Select CSV filenames by prefix; repeatable")
    common.add_argument("--glossary", type=Path,
                        default=glossary_dir / "name-glossary.json",
                        help="JSON mapping original speaker names to Chinese")
    common.add_argument("--term-glossary", type=Path,
                        default=glossary_dir / "term-glossary.json",
                        help="JSON mapping IDOLY PRIDE terms to canonical Chinese names")
    commands.add_parser("plan", parents=[common], help="Count untranslated fields without API calls")
    translate = commands.add_parser("translate", parents=[common],
                                    help="Fill untranslated text rows using an LLM API")
    add_api_options(translate, "default.txt")
    translate.add_argument("--workers", type=int, default=64,
                           help="Concurrent CSV files; defaults to 64, maximum 100")
    translate.add_argument("--max-files", type=int, default=0,
                           help="Limit files for a pilot run; 0 means all")
    translate.add_argument("--max-batches-per-file", type=int, default=0,
                           help="Limit API batches per file for a pilot run; 0 means all")
    args = parser.parse_args()
    if args.command.startswith("master-"):
        if args.command == "master-plan":
            if args.term_policy is None:
                pending, _, total = collect_master(args.orig_dir, args.zh_dir,
                                                   args.log_file)
                unit = "unique MasterDB strings"
            else:
                pending, _, total = collect_scoped_master(
                    args.orig_dir, args.zh_dir, args.log_file,
                    load_policy(args.term_policy))
                unit = "MasterDB fields"
            print(f"{total - len(pending)}/{total} {unit} ready; {len(pending)} pending")
            return
        glossary = load_glossary(args.glossary)
        glossary.update(load_glossary(args.term_glossary))
        service = make_service(args, parser)
        if args.term_policy is None:
            new, applied, failures, total = translate_master(
                args.orig_dir, args.zh_dir, args.log_file, service, glossary,
                args.workers, args.batch_size, args.max_chars, args.max_batches)
            unit, total_unit = "unique strings", "sources"
        else:
            terms = load_policy(args.term_policy)
            service = ScopedService(service, terms, 'master')
            new, applied, failures, total = translate_scoped_master(
                args.orig_dir, args.zh_dir, args.log_file, service, glossary, terms,
                args.workers, args.batch_size, args.max_chars, args.max_batches)
            unit, total_unit = "fields", "fields"
        print(f"MasterDB: {new} new {unit}, {applied} JSON fields updated, "
              f"{failures} fields pending after errors from {total} {total_unit}")
        if failures:
            raise SystemExit(1)
        return
    if args.command.startswith("notice-"):
        if args.command == "notice-plan":
            source_hash = notice_checksum(args.source)
            pending, memory, total = collect_notice(
                args.source, args.translations, args.ui, args.log_file)
            if notice_checksum(args.source) != source_hash:
                raise ValueError("Notice source changed while planning; run notice-plan again")
            if args.output:
                pages = {}
                for page, text in pending:
                    pages.setdefault(page, []).append(text)
                write_table(args.output, {"source_sha256": source_hash,
                                          "pages": pages})
            print(f"{total - len(pending)}/{total} notice text nodes ready or unchanged; "
                  f"{len(pending)} pending; {len(memory)} saved model results")
            return
        glossary = load_glossary(args.glossary)
        glossary.update(load_glossary(args.term_glossary))
        service = make_service(args, parser)
        terms = load_policy(args.term_policy)
        if terms:
            service = ScopedService(service, terms, 'notice')
        new, applied, failures, total = translate_notice(
            args.source, args.translations, args.ui, args.log_file, args.review_list, service,
            glossary, args.workers, args.batch_size, args.max_chars, args.max_batches)
        pending, _, _ = collect_notice(args.source, args.translations, args.ui,
                                       args.log_file)
        print(f"Notice: {new} new strings, {applied} JSON values updated, "
              f"{len(pending)} pending from {total} source nodes; "
              f"{failures} failed in this run")
        if failures:
            raise SystemExit(1)
        return
    files = csv_files(args.csv_dir, args.file, args.prefix)
    if not files:
        parser.error("No matching adventure CSV files")
    glossary = load_glossary(args.glossary)
    for source, target in load_glossary(args.term_glossary).items():
        if source in glossary and glossary[source] != target:
            parser.error(f"Conflicting glossary translation for {source}")
        glossary[source] = target

    if args.command == "plan":
        totals = [0, 0]
        for path in files:
            rows = load_csv(path)
            verify_source(rows, args.source_dir)
            for index, value in enumerate(coverage(rows)):
                totals[index] += value
        print(f"{len(files)} CSV files; text {totals[0]}/{totals[1]}")
        return

    if args.source_dir is None:
        parser.error("translate requires --source-dir to verify original TXT files")
    if (args.batch_size < 1 or not 1 <= args.workers <= 100 or
            args.max_files < 0 or args.max_batches_per_file < 0):
        parser.error("Batch size must be positive, workers 1-100, limits nonnegative")
    service = make_service(args, parser)
    terms = load_policy(args.term_policy)
    if terms:
        service = ScopedService(service, terms, 'story')
    if args.max_files:
        files = files[:args.max_files]
    translated, failures = translate_files(
        files, service, glossary, args.batch_size, args.source_dir,
        args.max_batches_per_file or None, args.workers)
    print(f"Translated {translated} text fields; {len(failures)} files failed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
