"""Discover review-only proper-name candidates from explicit game structures."""

from collections import Counter, defaultdict
from itertools import chain
import re

from .term_audit import glossary_terms, master_entries, story_entries


# Each rule names a source table and exact field. Descriptions and arbitrary
# `name` fields are deliberately excluded unless the table gives them meaning.
MASTER_FIELDS = {
    'Music': {'name': 'song'},
    'PhotoQuestMusic': {'name': 'song'},
    'PhotoContestQuestMusic': {'name': 'song'},
    'EventStory': {'description': 'event'},
    'Story': {'name': 'story_title'},
    'ExtraStory': {'name': 'story_title'},
    'ExtraStoryPart': {'name': 'story_title'},
    'HomeTalk': {'title': 'story_title'},
    'Card': {'name': 'card_title'},
    'Costume': {'name': 'costume'},
    'CostumeType': {'name': 'costume_type'},
    'MessageGroup': {'name': 'chat_group'},
}
AGENCY_SUFFIXES = ('事務所', 'プロダクション')
PLACEHOLDER = re.compile(r'\{[^{}]+\}|<[^<>]+>|\$\w+|%[A-Za-z0-9]+|\\[nrt]')


def candidate_text(value, *, allow_literal_linebreaks=False):
    if not isinstance(value, str):
        return None
    text = value.strip()
    # Semantic evidence may contain the game's literal two-character \\n.
    # Only exempt that sequence from the placeholder scan; preserve the exact
    # candidate surface and keep real variables and other escapes forbidden.
    placeholder_text = text.replace('\\n', '\n') if allow_literal_linebreaks else text
    if (not text or len(text) > 120 or PLACEHOLDER.search(placeholder_text) or
            re.fullmatch(r'[\W\d_]+', text, re.UNICODE) or
            re.fullmatch(r'[\d\s年月日時分秒./:：_-]+', text)):
        return None
    return text


def master_category(entry):
    table = entry.location['table']
    field = entry.location['field']
    record_id = entry.location['id']
    if field in {'composer', 'lyricist', 'arranger'}:
        return None
    if table == 'StoryPart' and field == 'name' and record_id.startswith('st-part-group-'):
        return 'group'
    if table in {'Music', 'PhotoQuestMusic', 'PhotoContestQuestMusic'} and field == 'singer':
        if entry.source.endswith(AGENCY_SUFFIXES) and not any(
                separator in entry.source for separator in ('with ', '×', '、', '/', '&')):
            return 'agency'
    return MASTER_FIELDS.get(table, {}).get(field)


def discover(master_orig=None, story_csv=None, name_glossary=None, term_glossary=None):
    """Aggregate candidates while retaining every Master record and story script scope."""
    known = {term['source']: term['target'] for term in
             glossary_terms(name_glossary, term_glossary)}
    streams = []
    if master_orig is not None:
        # master_entries only reads orig when the optional translated directory is absent.
        streams.append(master_entries(master_orig, master_orig / '.no-translations'))
    if story_csv is not None:
        streams.append(story_entries(story_csv))
    groups = {}
    scanned = Counter()
    for entry in chain.from_iterable(streams):
        scanned[entry.location['kind']] += 1
        if entry.location['kind'] == 'master':
            category = master_category(entry)
            value = entry.source
            if category is None:
                continue
            location = dict(entry.location)
            example = {'location': location, 'context': {'record_labels': entry.context['record_labels']}}
            scope_key = (location['kind'], location['table'], location['id'], location['field'])
        else:
            category = 'speaker_or_role'
            value = entry.context['speaker']
            location = {key: entry.location[key] for key in ('kind', 'script', 'field', 'csv')}
            location['speaker'] = value
            location['line'] = int(location['field'].split(':', 1)[0])
            example = {'location': location, 'context': {'dialogue_excerpt': entry.source[:120]}}
            scope_key = ('story', location['script'])
        source = candidate_text(value)
        if source is None:
            continue
        key = (category, source)
        if key not in groups:
            groups[key] = {'source': source, 'category': category,
                           'in_glossary': source in known,
                           'glossary_target': known.get(source),
                           'occurrences': 0, 'scopes': {}}
        item = groups[key]
        item['occurrences'] += 1
        scope = item['scopes'].setdefault(scope_key, {'count': 0, 'examples': []})
        scope['count'] += 1
        if len(scope['examples']) < 2:
            scope['examples'].append(example)
    candidates = []
    for item in groups.values():
        item['scopes'] = [scope for _, scope in sorted(item['scopes'].items())]
        candidates.append(item)
    candidates.sort(key=lambda item: (item['category'], item['source']))
    return {'schema_version': 1,
            'scope': 'local_structured_candidates_no_translation_or_dictionary_changes',
            'summary': {'scanned_fields': dict(scanned), 'candidates': len(candidates),
                        'unknown': sum(not item['in_glossary'] for item in candidates),
                        'by_category': dict(sorted(Counter(item['category'] for item in candidates).items())),
                        'unknown_by_category': dict(sorted(Counter(item['category'] for item in candidates
                            if not item['in_glossary']).items()))},
            'candidates': candidates}
