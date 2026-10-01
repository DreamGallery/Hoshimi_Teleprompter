#!/usr/bin/env python3
"""Retry only pending reviewed notice nodes with protected source syntax."""
from pathlib import Path
import argparse

from main import add_api_options, load_glossary, make_service
from src.notice_workflow import collect, run
from src.notice_protected_service import ProtectedNoticeService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "translations", "ui", "log-file", "review-list"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--workers", type=int, default=50)
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--max-chars", type=int, default=1600)
    root = Path(__file__).resolve().parent
    parser.add_argument("--glossary", type=Path, default=root / "glossaries/name-glossary.json")
    parser.add_argument("--term-glossary", type=Path, default=root / "glossaries/term-glossary.json")
    add_api_options(parser, "notice-protected.txt", term_policy=False)
    args = parser.parse_args()
    service = ProtectedNoticeService(make_service(args, parser))
    glossary = {**load_glossary(args.glossary), **load_glossary(args.term_glossary)}
    new, applied, failures, total = run(args.source, args.translations, args.ui,
        args.log_file, args.review_list, service, glossary, args.workers,
        args.batch_size, args.max_chars, args.max_batches)
    remaining, _, _ = collect(args.source, args.translations, args.ui, args.log_file)
    print(f"Notice protected retry: {new} new, {applied} applied, {len(remaining)} pending, {failures} failed; {total} total nodes")
    return 1 if failures or remaining else 0


if __name__ == "__main__":
    raise SystemExit(main())
