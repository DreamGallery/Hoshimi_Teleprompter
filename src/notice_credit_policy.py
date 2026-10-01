"""Retain original music-credit names only in explicit notice credit fields."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from .master_workflow import write_table
from .notice_workflow import read_pages, validate_notice

POLICY = "music-credit-names-verbatim-v1"
CREDIT = re.compile(r"^(?P<label>(?:作詞|作曲|編曲)(?:・(?:作詞|作曲|編曲))*)"
                    r"(?P<separator>[ \t\u3000]*[:：])(?P<names>[^\r\n]+)$")


def retained_credit(source: str) -> str | None:
    match = CREDIT.fullmatch(source)
    if not match or not match["names"].strip():
        return None
    label = match["label"].replace("作詞", "作词").replace("編曲", "编曲")
    target = label + match["separator"] + match["names"]
    validate_notice(source, target)
    return target


def initial_credit_target(source: str, initial: str) -> str:
    match = CREDIT.fullmatch(source)
    separator = re.search(r"[:：]", initial)
    if not match or not separator:
        raise ValueError("Cannot restore an existing credit without explicit separators")
    target = initial[:separator.end()] + match["names"]
    validate_notice(source, target)
    return target


def apply_policy(source_path: Path, translations_path: Path, initial_path: Path,
                 log_path: Path, report_path: Path, *, restore_initial_names=False) -> dict:
    """Run after API writers exit; optional explicit policy restores old names."""
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    sources, translations, _ = read_pages(source_path, translations_path, None)
    initial = json.loads(initial_path.read_text(encoding="utf-8"))["pages"]
    for page, values in initial.items():
        for source, target in values.items():
            current = translations.get(page, {}).get(source)
            permitted = (restore_initial_names and retained_credit(source) is not None and
                         current == initial_credit_target(source, target))
            if current != target and not permitted:
                raise ValueError("Initial reviewed notice translation changed before credit policy")
    latest = {}
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                if retained_credit(entry["source"]) is not None:
                    latest[(entry["page"], entry["source"])] = entry
    report = {"policy": POLICY, "source_sha256": source_digest,
              "matched_nodes": 0, "protected_initial_nodes": 0, "initial_policy_conflicts": 0,
              "restored_initial_nodes": 0, "changed_new_nodes": 0,
              "filled_new_nodes": 0, "already_correct_new_nodes": 0, "log_records_added": 0,
              "provenance": []}
    append = []
    for page, data in sources.items():
        for source in data["texts"]:
            target = retained_credit(source)
            if target is None:
                continue
            report["matched_nodes"] += 1
            previous_initial = initial.get(page, {}).get(source)
            if previous_initial is not None and not restore_initial_names:
                report["protected_initial_nodes"] += 1
                existing = initial[page][source]
                report["initial_policy_conflicts"] += existing != target
                report["provenance"].append({"page": page, "source": source, "action": "preserve_initial_translation",
                    "initial_translation": existing, "policy_translation": target, "matches_policy": existing == target})
                continue
            values = translations.setdefault(page, {})
            old = values.get(source)
            if previous_initial is not None:
                target = initial_credit_target(source, previous_initial)
                counter = "restore_initial_credit_names"
                report["restored_initial_nodes"] += old != target
            else:
                counter = "filled_new_nodes" if old is None else "already_correct_new_nodes" if old == target else "changed_new_nodes"
                report[counter] += 1
            values[source] = target
            record = {"page": page, "source": source, "translation": target, "policy": POLICY,
                      "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(), "names_verbatim": True}
            previous = latest.get((page, source), {})
            if previous.get("translation") != target or previous.get("policy") != POLICY:
                append.append(record)
            provenance = {"page": page, "source": source, "action": counter,
                          "source_sha256": record["source_sha256"], "names_verbatim": True}
            if previous_initial is not None:
                provenance.update(initial_translation=previous_initial, policy_translation=target)
            report["provenance"].append(provenance)
    # A saved translation takes precedence over older model memory on resume.
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_digest:
        raise ValueError("Notice source changed while applying credit policy")
    write_table(translations_path, {"pages": translations})
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        for record in append:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    report["log_records_added"] = len(append)
    write_table(report_path, report)
    return report
