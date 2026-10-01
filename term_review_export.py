#!/usr/bin/env python3
"""Export a self-contained local human review queue; never apply translations."""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from src.review_inputs import write_json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def portable_paths(value):
    """Remove machine-specific prefixes without altering evidence IDs or hashes."""
    if isinstance(value, dict):
        return {key: portable_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [portable_paths(item) for item in value]
    if isinstance(value, str) and (value.startswith(('/Users/', '/home/'))
                                 or re.match(r'^[A-Za-z]:[\\/]Users[\\/]', value)):
        normalized = value.replace('\\', '/')
        for project in ('Idoly-localify', 'Hoshimi_Teleprompter'):
            marker = '/' + project + '/'
            if marker in normalized:
                return project + '/' + normalized.split(marker, 1)[1]
        return normalized.rsplit('/', 1)[-1]
    return value


def build_queue(references, coverage, classifications=None):
    if references.get('scope') != 'term_reference_review_only_no_dictionary_changes':
        raise ValueError('Expected a reconciled term reference report')
    references = portable_paths(references)
    coverage = portable_paths(coverage)
    classifications = portable_paths(classifications or {})
    rows = []
    for candidate in references['candidates']:
        flags = []
        if candidate['status'] == 'needs_review':
            flags.append('missing_reference')
        if candidate.get('model_reference_disagreement'):
            flags.append('reference_disagreement')
        # References are still proposals; they do not automatically approve a term.
        rows.append({'review_id': 'term-' + digest(candidate['source'])[:24],
                     'input_fingerprint': digest(candidate),
                     'kind': 'candidate', 'review_flags': flags,
                     'candidate': candidate})
    for entry in coverage['unverified_rows']:
        row_id = entry['row']['id']
        classification = classifications.get(row_id)
        rows.append({'review_id': 'row-' + row_id,
                     'input_fingerprint': digest({'entry': entry,
                                                  'local_classification': classification}),
                     'kind': 'unverified_source_row',
                     'review_flags': ['source_coverage_gap'],
                     'entry': entry, 'local_classification': classification})
    if len({row['review_id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate review IDs')
    return {'schema_version': 1, 'scope': 'human_review_queue_no_dictionary_changes',
            'summary': {'items': len(rows),
                        'by_kind': dict(Counter(row['kind'] for row in rows)),
                        'flag_counts': dict(Counter(flag for row in rows
                                                   for flag in row['review_flags']))},
            'coverage_summary': {key: value for key, value in coverage.items()
                                 if key != 'unverified_rows'},
            'items': rows}


def decision_template(queue, previous=None):
    previous = previous or {'items': []}
    existing = {item['review_id']: item for item in previous['items']}
    items = []
    stale = []
    for row in queue['items']:
        old = existing.pop(row['review_id'], None)
        if old and old.get('input_fingerprint') == row['input_fingerprint']:
            items.append(old)
        elif (old and old.get('decision') == 'pending'
              and not any(old.get(key) for key in ('selected_target', 'notes', 'reviewer'))):
            # An untouched pending template has no human judgment to invalidate.
            items.append(dict(old, input_fingerprint=row['input_fingerprint']))
        else:
            if old:
                stale.append(old)
            items.append({'review_id': row['review_id'],
                          'input_fingerprint': row['input_fingerprint'],
                          'decision': 'pending', 'selected_target': '',
                          'notes': '', 'reviewer': ''})
    stale.extend(existing.values())
    stale.extend(previous.get('stale_decisions', []))
    return {'schema_version': 1, 'scope': 'manual_decisions_not_applied',
            'items': items, 'stale_decisions': stale}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--references', required=True, type=Path)
    parser.add_argument('--coverage', required=True, type=Path)
    parser.add_argument('--classifications', type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    inputs = [args.references, args.coverage] + ([args.classifications]
                                               if args.classifications else [])
    outputs = [args.output_dir / name for name in
               ('queue.json', 'decisions.json', 'manifest.json', 'README.md')]
    if any(source.resolve() == target.resolve() for source in inputs for target in outputs):
        parser.error('Review outputs must not overwrite their inputs')
    manifests = [{'source_file': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                 for p in inputs]
    references, coverage = [json.loads(p.read_text(encoding='utf-8'))
                            for p in inputs[:2]]
    classifications = (json.loads(args.classifications.read_text(encoding='utf-8'))['rows']
                       if args.classifications else None)
    queue = build_queue(references, coverage, classifications)
    queue['inputs'] = manifests
    args.output_dir.mkdir(parents=True, exist_ok=True)
    decision_path = args.output_dir / 'decisions.json'
    old = json.loads(decision_path.read_text(encoding='utf-8')) if decision_path.exists() else None
    decisions = portable_paths(decision_template(queue, old))
    write_json(args.output_dir / 'queue.json', queue)
    write_json(decision_path, decisions)
    write_json(args.output_dir / 'manifest.json', {
        'schema_version': 1, 'scope': 'local_review_export_no_network',
        'inputs': manifests, 'queue_sha256': hashlib.sha256(
            (args.output_dir / 'queue.json').read_bytes()).hexdigest(),
        'summary': queue['summary']})
    (args.output_dir / 'README.md').write_text('''# 术语人工审阅包

这是本地候选与证据审阅队列，不会修改词典、译文或发送 API。

- `queue.json` 保存候选、Master/词典参考、模型建议、来源证据和未验证原始行。
- `decisions.json` 用 `review_id` 对应队列项，填写人工决定、译名、理由与审阅人。
- `manifest.json` 记录输入文件名、输入哈希与队列哈希。证据文件路径使用项目相对路径，不保存本机用户名。重导出时输入指纹不变的决定会保留；原文、参考或本地分类变化后，有人工内容的决定转入 `stale_decisions`，重新审阅。尚未填写的 pending 模板保持 pending。

## 校对顺序

1. 先处理 `source_coverage_gap`：结合原行和上下文判断 `no_entity`、`evidence_needed` 或 `retain_candidate`。模型/API失败是来源覆盖缺口，不是无实体证明。
2. 再处理 `reference_disagreement` 与 `missing_reference`：检查所指人物/组合/歌曲/商品是否相同，结合当前 Master/词典选择 `accept_reference`、`accept_suggestion`、`propose` 或 `skip`。
3. 其余现有参考项仍需人工判断候选是否为术语；参考存在不等于官方正确译名。

默认决定 `pending`。`selected_target` 是人工建议，不会自动应用。`notes` 记录理由、表/记录/字段作用范围和表记变体；不要仅因出现次数多就强制统一。不要修改 `review_id` 或 `input_fingerprint`。

原文覆盖按成功模型批的原始行并集统计。仅校验证据恢复的候选不算整批完成。人工无实体判断与模型完成率分别记录，禁止由填写决定自动提升覆盖指标。

后续维护：先保存人工决定，再依据具体表/记录/字段另作有范围的词典修改与审阅；本工具没有应用决定功能。若仍需模型复核，只选择确有证据缺口的原行，使用原始来源与缓存续跑，勿重发全量计划。
''', encoding='utf-8')
    print(json.dumps(queue['summary'], ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
