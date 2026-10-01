#!/usr/bin/env python3
"""Audit terminology across MasterDB and adventure CSV without making changes."""
import argparse
import json
from itertools import chain
from pathlib import Path
from src.term_audit import audit, glossary_terms, input_manifest, load_policy, master_entries, story_entries
from src.review_inputs import write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--master-orig', type=Path)
    parser.add_argument('--master-zh', type=Path)
    parser.add_argument('--story-csv', type=Path)
    parser.add_argument('--name-glossary', type=Path)
    parser.add_argument('--term-glossary', type=Path)
    parser.add_argument('--policy', type=Path, help='Optional versioned terms with category and context scope')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--sample-limit', type=int, default=8)
    args = parser.parse_args(argv)
    if bool(args.master_orig) != bool(args.master_zh):
        parser.error('--master-orig and --master-zh must be supplied together')
    if not args.master_orig and not args.story_csv:
        parser.error('Provide MasterDB or story CSV inputs')
    inputs = [p for p in (args.master_orig, args.master_zh, args.story_csv,
                          args.name_glossary, args.term_glossary, args.policy) if p]
    if any(not path.exists() for path in inputs):
        parser.error('An input path does not exist')
    if any(args.output.resolve() == p.resolve() or (p.is_dir() and args.output.resolve().is_relative_to(p.resolve())) for p in inputs):
        parser.error('Output must be outside input files and directories')
    terms = glossary_terms(args.name_glossary, args.term_glossary)
    if args.policy:
        policy = load_policy(args.policy)
        # Explicit scoped policies override legacy fallback entries for that source.
        sources = {term['source'] for term in policy}
        terms = [term for term in terms if term['source'] not in sources] + policy
    streams = []
    if args.master_orig:
        streams.append(master_entries(args.master_orig, args.master_zh))
    if args.story_csv:
        streams.append(story_entries(args.story_csv))
    manifest = input_manifest(inputs)
    report = audit(chain.from_iterable(streams), terms, args.sample_limit)
    if input_manifest(inputs) != manifest:
        raise ValueError('Inputs changed during audit; rerun after translation has stopped')
    report['inputs'] = manifest
    write_json(args.output, report)
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
