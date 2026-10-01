"""Resolve optional scoped terminology before sending translation requests."""

from fnmatch import fnmatchcase
import json
from pathlib import Path
import re

SCOPES = {'kind', 'table', 'id', 'field', 'script', 'speaker'}
CATEGORIES = {'character', 'speaker_or_role', 'group', 'song', 'event',
              'agency', 'brand', 'term'}


def load_policy(path: Path | None) -> list[dict]:
    if path is None:
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('terms'), list):
        raise ValueError(f'{path}: expected schema_version 1 and terms array')
    for term in data['terms']:
        if (not isinstance(term, dict) or not isinstance(term.get('source'), str)
                or not term['source'] or not isinstance(term.get('target'), str)
                or not term['target'] or term.get('category') not in CATEGORIES
                or term.get('action') not in {'prefer', 'retain'}):
            raise ValueError(f'{path}: invalid term')
        if term['action'] == 'retain' and term['source'] != term['target']:
            raise ValueError(f'{path}: retained term must keep its source spelling')
        scope = term.get('scope', {})
        if (not isinstance(scope, dict) or any(key not in SCOPES or
                not isinstance(value, str) or not value for key, value in scope.items())):
            raise ValueError(f'{path}: invalid term scope')
    return data['terms']


def contains(text: str, source: str) -> bool:
    left = r'(?<![A-Za-z0-9_])' if source[0].isascii() and source[0].isalnum() else ''
    right = r'(?![A-Za-z0-9_])' if source[-1].isascii() and source[-1].isalnum() else ''
    return re.search(left + re.escape(source) + right, text) is not None


def resolve(terms: list[dict], context: dict[str, str], source: str) -> list[dict]:
    selected = {}
    for term in terms:
        spelling = term['source']
        if not contains(source, spelling) or (len(spelling) < 2 and source != spelling):
            continue
        if not all(key in context and fnmatchcase(context[key], pattern)
                   for key, pattern in term.get('scope', {}).items()):
            continue
        previous = selected.get(spelling)
        specificity = len(term.get('scope', {}))
        if previous and previous[0] == specificity and previous[1]['target'] != term['target']:
            raise ValueError(f'Conflicting scoped terms for {spelling!r} at {context}')
        if previous is None or specificity >= previous[0]:
            selected[spelling] = (specificity, {key: term[key] for key in
                                               ('source', 'target', 'action')})
    return [entry for _, entry in selected.values()]


class ScopedService:
    """Add per-line terms and suppress legacy rules superseded by policy."""

    def __init__(self, inner, terms: list[dict], kind: str,
                 contexts: dict[str, dict[str, str]] | None = None):
        self.inner, self.terms, self.kind = inner, terms, kind
        self.contexts = contexts or {}
        self.overridden = {term['source'] for term in terms}

    def translate_batch(self, lines, glossary):
        filtered = {key: value for key, value in glossary.items()
                    if key not in self.overridden}
        enriched = []
        retained = {}
        for line in lines:
            context = {'kind': self.kind, **self.contexts.get(line['source'], {})}
            if self.kind == 'story':
                context.update({key: line[key] for key in ('script', 'field', 'speaker')
                                if key in line})
            elif self.kind == 'notice':
                context.update({'table': 'Notice', 'field': 'text',
                                'id': line.get('speaker', '').removeprefix('Notice:')})
            elif self.kind == 'master':
                context.update({key: line[key] for key in ('table', 'field')
                                if key in line})
                if 'record_id' in line:
                    context['id'] = line['record_id']
            source = line['source']
            matches = resolve(self.terms, context, source)
            if self.kind == 'story' and line.get('speaker'):
                for term in resolve(self.terms, context, line['speaker']):
                    if term not in matches:
                        matches.append(term)
            # A rule for one scene must not remove an existing flat preference
            # from unrelated scenes in the same request. Keep the shared map
            # free of conflicting rules and carry the fallback per line.
            matched_sources = {term['source'] for term in matches}
            text = source + ' ' + line.get('speaker', '')
            for spelling in self.overridden - matched_sources:
                if spelling in glossary and contains(text, spelling):
                    matches.append({'source': spelling, 'target': glossary[spelling],
                                    'action': 'prefer'})
            if (self.kind == 'master' and context.get('table') == 'Music'
                    and context.get('field') in {'composer', 'lyricist', 'arranger'}):
                retained[line['id']] = source
                continue
            enriched.append({**line, 'term_preferences': matches} if matches else line)
        if enriched:
            retained.update(self.inner.translate_batch(enriched, filtered))
        return retained


def master_contexts(orig_dir: Path, terms: list[dict]) -> dict[str, dict[str, str]]:
    """Reject source deduplication when occurrences demand different policies."""
    contexts = {}
    signatures = {}
    for path in sorted(orig_dir.glob('*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        for record_id, fields in data.items():
            for field, source in fields.items():
                if not isinstance(source, str):
                    continue
                context = {'kind': 'master', 'table': path.stem,
                           'id': str(record_id), 'field': field}
                match = tuple(sorted((term['source'], term['target'], term['action'])
                                     for term in resolve(terms, context, source)))
                if path.stem == 'Music' and field in {'composer', 'lyricist', 'arranger'}:
                    match += (('__music_credit__', source, 'retain'),)
                if source in signatures and signatures[source] != match:
                    raise ValueError(f'Master source {source!r} has different scoped terms across records; '
                                     'source-based translation memory cannot safely reuse it')
                signatures[source] = match
                contexts[source] = context
    return contexts
