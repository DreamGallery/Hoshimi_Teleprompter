#!/usr/bin/env python3
"""Extract review-only proper-name candidates from MasterDB and story CSV."""

import argparse
import json
from pathlib import Path

from src.review_inputs import write_json
from src.term_audit import input_manifest
from src.term_candidates import discover


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--master-orig', type=Path)
    parser.add_argument('--story-csv', type=Path)
    root = Path(__file__).resolve().parent
    parser.add_argument('--name-glossary', type=Path,
                        default=root / 'glossaries/name-glossary.json')
    parser.add_argument('--term-glossary', type=Path,
                        default=root / 'glossaries/term-glossary.json')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.master_orig and not args.story_csv:
        parser.error('Provide --master-orig and/or --story-csv')
    inputs = [p for p in (args.master_orig, args.story_csv, args.name_glossary,
                          args.term_glossary) if p]
    for path in inputs:
        if not path.exists():
            parser.error(f'Input path does not exist: {path}')
        if args.output.resolve() == path.resolve() or (
                path.is_dir() and args.output.resolve().is_relative_to(path.resolve())):
            parser.error('Output must be outside input files and directories')
    manifest = input_manifest(inputs)
    report = discover(args.master_orig, args.story_csv,
                      args.name_glossary, args.term_glossary)
    if input_manifest(inputs) != manifest:
        raise ValueError('Inputs changed during extraction; rerun after source sync stops')
    report['inputs'] = manifest
    write_json(args.output, report)
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
