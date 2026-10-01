#!/usr/bin/env python3
"""Keep explicit notice music-credit names verbatim after a translation run."""
import argparse
from pathlib import Path
from src.notice_credit_policy import apply_policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "translations", "initial-translations", "log-file", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--restore-initial-credit-names", action="store_true",
                        help="Explicitly apply the name-retention policy to old credit entries, backing up their values in the report")
    args = parser.parse_args()
    report = apply_policy(args.source, args.translations, args.initial_translations, args.log_file, args.report,
                          restore_initial_names=args.restore_initial_credit_names)
    print({key: value for key, value in report.items() if key != "provenance"})


if __name__ == "__main__":
    main()
