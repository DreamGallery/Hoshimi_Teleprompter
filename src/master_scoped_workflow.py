"""Field-scoped MasterDB translation with exact-context resume records."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

from .master_workflow import (LINE_ENDING, _valid_for_context, normalize_line_endings,
                              read_table, repair_log_tail, validate, write_table,
                              title_can_reflow)
from .terminology import resolve

CREDITS = {'composer', 'lyricist', 'arranger'}


def _key(table, record_id, field, source):
    return table, str(record_id), field, source


def _context(key):
    table, record_id, field, _ = key
    return {'kind': 'master', 'table': table, 'id': record_id, 'field': field}


def _credit(key):
    return key[0] == 'Music' and key[2] in CREDITS


def _terms(terms, key):
    return sorted(resolve(terms, _context(key), key[3]),
                  key=lambda term: (term['source'], term['target'], term['action']))


def collect(orig_dir: Path, zh_dir: Path, log_path: Path, terms: list[dict]):
    if (orig_dir.parent / '.master-export-in-progress.json').is_file():
        raise ValueError('MasterDB export is incomplete; rerun export before translating')
    records = {}
    missing = set()
    by_source = {}
    for path in sorted(orig_dir.glob('*.json')):
        original = read_table(path)
        target_path = zh_dir / path.name
        if not target_path.is_file():
            raise ValueError(f'Missing MasterDB translation table: {target_path}')
        translated = read_table(target_path)
        for record_id, fields in original.items():
            for field, source in fields.items():
                if not isinstance(source, str):
                    raise ValueError(f'{path}: invalid source value')
                key = _key(path.stem, record_id, field, source)
                records[key] = None
                by_source.setdefault(source, []).append(key)
                target = translated.get(record_id, {}).get(field, '')
                if target:
                    validate(source, target, allow_line_reflow=title_can_reflow(path.stem, field))
                else:
                    missing.add(key)
    memory = {}
    old = {}
    if log_path.is_file():
        with log_path.open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():
                    continue
                entry = json.loads(line)
                source, target = entry['source'], entry['translation']
                if all(name in entry for name in ('table', 'id', 'field')):
                    key = _key(entry['table'], entry['id'], entry['field'], source)
                    if (key in missing and entry.get('term_preferences') == _terms(terms, key)
                            and _valid_for_context(source, target, key[0], key[2])):
                        memory[key] = target
                else:
                    old[source] = target
    # A source-only record has unknown context. Reuse it only when none of its
    # current occurrences is governed by a scoped policy rule.
    for source, keys in by_source.items():
        target = old.get(source)
        if not target or any(_terms(terms, key) for key in keys):
            continue
        for key in keys:
            if key in missing and not _credit(key) and _valid_for_context(source, target, key[0], key[2]):
                memory.setdefault(key, target)
    for key in missing:
        if _credit(key):
            memory[key] = key[3]
    pending = [key for key in records if key in missing and key not in memory]
    return pending, memory, len(records)


def batches(pending, batch_size, max_chars):
    batch, size = [], 0
    for key in pending:
        if batch and (len(batch) >= batch_size or size + len(key[3]) > max_chars):
            yield batch
            batch, size = [], 0
        batch.append(key)
        size += len(key[3])
    if batch:
        yield batch


def translate_batch(service, glossary, keys):
    lines = [{'id': str(index), 'speaker': f'{key[0]}:{key[2]}',
              'table': key[0], 'record_id': key[1], 'field': key[2],
              'source': key[3]} for index, key in enumerate(keys)]
    for attempt in range(3):
        try:
            result = service.translate_batch(lines, glossary)
            if set(result) != {line['id'] for line in lines}:
                raise ValueError('model returned incomplete field IDs')
            output = {}
            for line, key in zip(lines, keys):
                target = normalize_line_endings(key[3], result[line['id']])
                validate(key[3], target, allow_line_reflow=title_can_reflow(key[0], key[2]))
                output[key] = target
            return output
        except (ValueError, KeyError):
            if attempt != 2:
                continue
            if len(keys) > 1:
                output = {}
                for half in (keys[:len(keys) // 2], keys[len(keys) // 2:]):
                    try:
                        output.update(translate_batch(service, glossary, half))
                    except (ValueError, KeyError):
                        pass
                if output:
                    return output
                raise
            key = keys[0]
            source = key[3]
            if LINE_ENDING.search(source):
                parts = LINE_ENDING.split(source)
                endings = LINE_ENDING.findall(source)
                translated = [translate_batch(service, glossary, [(*key[:3], part)])[(*key[:3], part)]
                              if part else '' for part in parts]
                target = ''.join(part + ending for part, ending in zip(translated, endings)) + translated[-1]
                validate(source, target, allow_line_reflow=title_can_reflow(key[0], key[2]))
                return {key: target}
            raise


def apply_memory(orig_dir: Path, zh_dir: Path, memory):
    if (orig_dir.parent / '.master-export-in-progress.json').is_file():
        raise ValueError('MasterDB export is incomplete; rerun export before translating')
    changes = 0
    for path in sorted(orig_dir.glob('*.json')):
        original = read_table(path)
        translated = read_table(zh_dir / path.name)
        table_changes = 0
        for record_id, fields in original.items():
            for field, source in fields.items():
                if translated.get(record_id, {}).get(field):
                    continue
                key = _key(path.stem, record_id, field, source)
                target = memory.get(key)
                if target and _valid_for_context(source, target, path.stem, field):
                    translated.setdefault(record_id, {})[field] = target
                    table_changes += 1
        if table_changes:
            write_table(zh_dir / path.name, translated)
            changes += table_changes
    return changes


def run(orig_dir: Path, zh_dir: Path, log_path: Path, service, glossary,
        terms: list[dict], workers=64, batch_size=12, max_chars=1600, max_batches=0):
    if not 1 <= workers <= 100 or batch_size < 1 or max_chars < 1 or max_batches < 0:
        raise ValueError('invalid worker, batch or character limit')
    if repair_log_tail(log_path):
        print('Removed an incomplete final MasterDB log record; its field will be retried', flush=True)
    pending, memory, total = collect(orig_dir, zh_dir, log_path, terms)
    work = list(batches(pending, batch_size, max_chars))
    if max_batches:
        work = work[:max_batches]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    failures = new = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(translate_batch, service, glossary, keys): keys for keys in work}
        with log_path.open('a', encoding='utf-8') as log:
            for completed, future in enumerate(as_completed(futures), 1):
                try:
                    output = future.result()
                    for key, target in output.items():
                        table, record_id, field, source = key
                        log.write(json.dumps({'table': table, 'id': record_id, 'field': field,
                                              'source': source, 'translation': target,
                                              'term_preferences': _terms(terms, key)},
                                             ensure_ascii=False) + '\n')
                        memory[key] = target
                    log.flush()
                    new += len(output)
                    failures += len(futures[future]) - len(output)
                except Exception as error:
                    failures += len(futures[future])
                    print(f'Master batch failed ({type(error).__name__}): {error}', flush=True)
                if completed % 100 == 0 or completed == len(work):
                    print(f'[{completed}/{len(work)}] {new} new fields; '
                          f'{failures} pending after errors', flush=True)
    applied = apply_memory(orig_dir, zh_dir, memory)
    return new, applied, failures, total
