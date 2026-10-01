#!/usr/bin/env python3
"""Reconcile semantic term review files with existing MasterDB and glossary translations."""

import argparse
import json
from pathlib import Path

from src.review_inputs import write_json
from src.term_audit import glossary_terms, input_manifest, master_entries, read_json
from src.term_reconcile import reconcile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, action='append', required=True,
                        help='Semantic review JSON; repeat for main pass and repairs')
    parser.add_argument('--master-orig', type=Path, required=True)
    parser.add_argument('--master-zh', type=Path, required=True)
    parser.add_argument('--name-glossary', type=Path)
    parser.add_argument('--term-glossary', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    inputs = args.review + [args.master_orig, args.master_zh] + [
        p for p in (args.name_glossary, args.term_glossary) if p]
    if any(not p.exists() for p in inputs):
        parser.error('An input path does not exist')
    if not args.master_orig.is_dir() or not args.master_zh.is_dir():
        parser.error('Master inputs must be directories')
    if any(not p.is_file() for p in args.review):
        parser.error('Each semantic review must be a JSON file')
    if any(args.output.resolve() == p.resolve() or
           (p.is_dir() and args.output.resolve().is_relative_to(p.resolve())) for p in inputs):
        parser.error('Output must be outside input files and directories')
    manifest = input_manifest(inputs)
    report = reconcile([read_json(p) for p in args.review],
                       master_entries(args.master_orig, args.master_zh),
                       glossary_terms(args.name_glossary, args.term_glossary))
    if input_manifest(inputs) != manifest:
        raise ValueError('Inputs changed during review; rerun against stable snapshots')
    report['inputs'] = manifest
    write_json(args.output, report)
    print(json.dumps(report['summary'], ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
