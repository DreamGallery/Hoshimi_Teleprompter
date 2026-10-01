#!/usr/bin/env python3
"""Translate anonymous official Master.Rule text in an isolated dictionary."""
import argparse
from pathlib import Path

from main import add_api_options, load_glossary, make_service
from src.master_workflow import write_table
from src.legal_workflow import compile_runtime, initialize, load_source, pending, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "plan", "translate", "compile"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--translations", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Required for compile; optional translation report")
    parser.add_argument("--workers", type=int, default=50)
    root = Path(__file__).resolve().parent
    parser.add_argument("--glossary", type=Path, default=root / "glossaries/name-glossary.json")
    parser.add_argument("--term-glossary", type=Path, default=root / "glossaries/term-glossary.json")
    add_api_options(parser, "legal.txt", term_policy=False)
    args = parser.parse_args()
    source = load_source(args.source)
    translations = initialize(source, args.translations)
    if args.command == "init":
        write_table(args.translations, translations)
    elif args.command == "translate":
        write_table(args.translations, translations)
        glossary = {**load_glossary(args.glossary), **load_glossary(args.term_glossary)}
        result = run(source, translations, args.translations, make_service(args, parser), glossary,
                     args.batch_size, args.workers)
        if args.output:
            write_table(args.output, result)
        if result["remaining_segments"]:
            return 2
    elif args.command == "compile":
        if not args.output:
            parser.error("compile requires --output")
        write_table(args.output, compile_runtime(source, translations))
    print(f"Legal rules: {len(source['rules'])}; untranslated segments: {len(pending(source, translations))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
