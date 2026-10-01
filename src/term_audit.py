"""Context-preserving terminology audit. Produces review suggestions, never edits translations."""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
import csv
import hashlib
import json
from pathlib import Path
import re

CATEGORIES = {'character', 'speaker_or_role', 'group', 'song', 'event', 'agency', 'brand', 'term'}
CREDIT_FIELDS = {'composer', 'lyricist', 'arranger'}
GROUPS = {'IIIX', 'ⅢX', 'LizNoir', 'TRINITYAiLE', 'サニピ', 'サニーピース',
          '月のテンペスト', '月のテンぺスト', '月スト', 'トリエル', 'リズノワ', 'スリクス', 'どりきゅん'}


@dataclass(frozen=True)
class Entry:
    source: str
    translation: str
    location: dict
    context: dict


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def read_dictionary(path):
    data = read_json(path)
    if not isinstance(data, dict) or any(not isinstance(k, str) or not k or
            not isinstance(v, str) or not v for k, v in data.items()):
        raise ValueError(f'{path}: expected nonempty source -> target strings')
    return data


def glossary_terms(name_path=None, term_path=None):
    """Legacy dictionaries are preferences, not proof of official translations."""
    output = []
    for path, category in ((name_path, 'speaker_or_role'), (term_path, 'term')):
        if not path:
            continue
        for source, target in read_dictionary(path).items():
            if re.search(r'\{[^{}]+\}', source):
                continue  # User placeholders and attached honorifics are not proper nouns.
            kind = category
            if source in GROUPS:
                kind = 'group'
            elif source.endswith(('プロ', 'プロダクション')):
                kind = 'agency'
            output.append({'source': source, 'target': target, 'category': kind,
                           'action': 'retain' if source == target else 'prefer',
                           'provenance': 'project_glossary', 'scope': {}})
    return output


def load_policy(path):
    data = read_json(path)
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('terms'), list):
        raise ValueError('Policy must have schema_version: 1 and terms: []')
    terms = data['terms']
    for term in terms:
        if (not isinstance(term, dict) or not isinstance(term.get('source'), str) or not term['source']
                or not isinstance(term.get('target'), str) or not term['target']
                or term.get('category') not in CATEGORIES or term.get('action') not in {'retain', 'prefer'}):
            raise ValueError('Invalid term policy')
        if term['action'] == 'retain' and term['source'] != term['target']:
            raise ValueError('Retain policy target must equal source')
        scope = term.get('scope', {})
        if not isinstance(scope, dict) or any(k not in {'kind', 'table', 'id', 'field', 'script', 'speaker'}
                or not isinstance(v, str) for k, v in scope.items()):
            raise ValueError('Invalid scope; use location/speaker fields with shell-style patterns')
    return terms


def master_entries(orig_dir, zh_dir):
    for path in sorted(Path(orig_dir).glob('*.json')):
        orig = read_json(path)
        target_path = Path(zh_dir) / path.name
        targets = read_json(target_path) if target_path.exists() else {}
        if not isinstance(orig, dict) or not isinstance(targets, dict):
            raise ValueError(f'{path}: expected table object')
        for record_id, fields in orig.items():
            if not isinstance(fields, dict):
                raise ValueError(f'{path}: expected record fields')
            translated_fields = targets.get(record_id, {})
            if not isinstance(translated_fields, dict):
                raise ValueError(f'{target_path}: expected translated record fields')
            for field, source in fields.items():
                translated = translated_fields.get(field, '')
                if not isinstance(source, str) or not isinstance(translated, str):
                    raise ValueError(f'{path}: text fields must be strings')
                yield Entry(source, translated, {'kind': 'master', 'table': path.stem,
                            'id': record_id, 'field': field}, {'record_labels': {
                                key: fields[key] for key in ('name', 'fullName', 'singer') if key in fields}})


def story_entries(csv_dir):
    for path in sorted(Path(csv_dir).rglob('adv_*.csv')):
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ['id', 'name', 'text', 'trans']:
                raise ValueError(f'{path}: expected id,name,text,trans')
            rows = list(reader)
        seen = set()
        text_rows = [row for row in rows if row.get('id') not in {'info', '译者'}]
        for index, row in enumerate(text_rows):
            if row.get('id') in {'info', '译者'}:
                continue
            if (set(row) != {'id', 'name', 'text', 'trans'} or
                    any(value is None for value in row.values()) or
                    not re.fullmatch(r'\d+:(text|title|choice|narration):\d+', row['id']) or row['id'] in seen):
                raise ValueError(f'{path}: invalid or duplicate story field')
            seen.add(row['id'])
            yield Entry(row['text'], row['trans'], {'kind': 'story',
                'script': path.with_suffix('.txt').name, 'field': row['id'],
                'csv': path.relative_to(csv_dir).as_posix()}, {'speaker': row['name'],
                    'neighbors': [{'field': neighbor['id'], 'speaker': neighbor['name'],
                                   'source': neighbor['text'], 'translation': neighbor['trans']}
                                  for neighbor in text_rows[max(0, index - 1):index] + text_rows[index + 1:index + 2]]})


def contains(text, term, *, literal_linebreaks=False):
    """ASCII names must be whole tokens; CJK mentions remain review-only candidates."""
    left = r'(?<![A-Za-z0-9_])' if term[0].isascii() and term[0].isalnum() else ''
    if left and literal_linebreaks:
        # A game's literal \\n delimiter ends with an ASCII n, but it is
        # not part of the following name. Keep the term itself exact.
        left = r'(?:(?<![A-Za-z0-9_])|(?<=\\n))'
    right = r'(?![A-Za-z0-9_])' if term[-1].isascii() and term[-1].isalnum() else ''
    return re.search(left + re.escape(term) + right, text) is not None


def in_scope(entry, term):
    values = {**entry.location, **entry.context}
    return all(fnmatchcase(str(values.get(key, '')), pattern)
               for key, pattern in term.get('scope', {}).items())


def category_for(entry):
    table, field = entry.location.get('table', ''), entry.location['field']
    if table == 'Music' and field in CREDIT_FIELDS:
        return 'music_credit'
    if table == 'Music' and field in {'name', 'description'}:
        return 'song'
    if table == 'Character' and field in {'name', 'firstName', 'lastName', 'fullName'}:
        return 'character'
    if 'Group' in table and field == 'name':
        return 'group'
    if 'Event' in table and field == 'name':
        return 'event'
    return 'text'


def audit(entries, terms, sample_limit=8):
    if sample_limit < 1:
        raise ValueError('sample_limit must be positive')
    by_source = defaultdict(list)
    for term in terms:
        by_source[term['source']].append(term)
    pattern = re.compile('|'.join(re.escape(x) for x in sorted(by_source, key=lambda x: (-len(x), x)))) if by_source else None
    findings = []
    counts = Counter()
    variants = defaultdict(lambda: defaultdict(lambda: {'count': 0, 'examples': []}))
    term_counts = Counter()
    for entry in entries:
        counts['entries'] += 1
        category = category_for(entry)
        if not entry.translation:
            counts['untranslated'] += 1
            continue
        counts['translated'] += 1
        if category == 'music_credit':
            counts['music_credit_fields'] += 1
            if entry.source != entry.translation:
                findings.append({'type': 'music_credit_changed', 'category': category,
                    **asdict(entry), 'suggestion': entry.source,
                    'reason': 'Keep non-character music production credits in their original spelling.'})
            continue
        matches = {m.group() for m in pattern.finditer(entry.source)} if pattern else set()
        active = [term for source in sorted(matches) if contains(entry.source, source)
                  for term in by_source[source] if in_scope(entry, term)]
        if not active:
            continue
        counts['entries_with_terms'] += 1
        for term in active:
            term_counts[term['category']] += 1
            # Bare short names often have ordinary meanings; keep only exact matches.
            if len(term['source']) < 2 and entry.source != term['source']:
                continue
            if not contains(entry.translation, term['target']):
                findings.append({'type': 'term_preference_mismatch', 'category': term['category'],
                    **asdict(entry), 'term': term,
                    'reason': 'Review meaning and context; absence is not proof of mistranslation.',
                    'suggestion': term['target'] if entry.source == term['source'] else None})
        # Full source and each location are preserved; differing wording is only a candidate.
        variant = variants[entry.source][entry.translation]
        variant['count'] += 1
        if len(variant['examples']) < sample_limit:
            variant['examples'].append({'location': entry.location, 'context': entry.context})
    conflicts = [{'source': source, 'variants': [{'translation': text, **details}
                  for text, details in sorted(targets.items())],
                  'reason': 'Context may justify these variants. Do not globally replace.'}
                 for source, targets in sorted(variants.items()) if len(targets) > 1]
    return {'schema_version': 1, 'scope': 'local_review_only_no_automatic_replacement',
            'summary': {**dict(counts), 'findings': len(findings), 'source_variant_groups': len(conflicts),
                        'findings_by_type': dict(Counter(f['type'] for f in findings)),
                        'term_mentions_by_category': dict(term_counts)},
            'sample_limit_per_variant': sample_limit,
            'findings': findings, 'source_variants': conflicts}


def input_manifest(paths):
    result = []
    for root in paths:
        root = Path(root)
        paths = sorted(root.rglob('*')) if root.is_dir() else [root]
        for path in paths:
            if path.is_file() and path.suffix in {'.json', '.csv'}:
                result.append({'path': str(path.resolve()),
                               'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return result
