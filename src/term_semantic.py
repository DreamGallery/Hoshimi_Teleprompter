"""Evidence-bound semantic term extraction; model output is never a glossary edit."""

from collections import Counter, defaultdict
import hashlib
from itertools import chain
import json
import re

from .term_audit import contains, glossary_terms, master_entries, story_entries
from .term_candidates import candidate_text, master_category


SCHEMA = 1
CATEGORIES = {'character', 'group', 'agency', 'song', 'event', 'story_title',
              'costume', 'item', 'place', 'organization', 'other'}
MASTER_SKIP = {'composer', 'lyricist', 'arranger'}
JAPANESE = re.compile(r'[ぁ-んァ-ヶ一-龯]')
CREDIT_LINE = re.compile(
    r'^\s*(?:(?:作詞|作曲|編曲)(?:[・/／＆&](?:作詞|作曲|編曲))*|'
    r'Lyrics|Music|Arrangement)\s*[:：]', re.I)
CONTROL_CHAR = re.compile(r'[\x00-\x09\x0b-\x1f\x7f]')


def is_credit_row(row):
    return (row['location'].get('field') in MASTER_SKIP or
            CREDIT_LINE.match(row['source']) is not None)


def stable_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def row_id(location):
    return sha256(stable_json(location))[:24]


def eligible(entry):
    location = entry.location
    source = entry.source.strip()
    if len(source) > 1000:
        return False
    if CREDIT_LINE.match(source):
        return False
    if location['kind'] == 'master':
        if location['field'] in MASTER_SKIP:
            return False
        if master_category(entry) is not None:
            return candidate_text(source) is not None
        if re.search(r'(?:^|\.)(?:name|title|fullName)$', location['field'], re.I):
            return candidate_text(source) is not None
    else:
        if not any(f':{kind}:' in location['field']
                   for kind in ('text', 'choice', 'narration')):
            return False
        # Short dialogue can itself be a character, group or place name.
        return bool(source and JAPANESE.search(source))
    return len(source) >= 8 and JAPANESE.search(source) is not None


def planned_rows(master_orig=None, story_csv=None):
    streams = []
    if master_orig is not None:
        streams.append(master_entries(master_orig, master_orig / '.no-translations'))
    if story_csv is not None:
        streams.append(story_entries(story_csv))
    for entry in chain.from_iterable(streams):
        if not eligible(entry):
            continue
        location = dict(entry.location)
        context = {}
        if location['kind'] == 'story':
            context['speaker'] = entry.context['speaker']
            context['neighbors'] = [{'field': n['field'], 'source': n['source'][:120]}
                                    for n in entry.context['neighbors']]
            group = ('story', location['script'])
        else:
            context['record_labels'] = entry.context['record_labels']
            context['structured_category'] = master_category(entry)
            group = ('master', location['table'], location['id'])
        yield group, {'id': row_id(location), 'source': entry.source,
                      'location': location, 'context': context}


def make_jobs(master_orig=None, story_csv=None, max_rows=8, max_chars=1800,
              short_rows=8):
    """Keep scene order and record boundaries; never concatenate unrelated files."""
    if max_rows < 1 or short_rows < 1 or max_chars < 200:
        raise ValueError('max_rows, short_rows or max_chars is too small')
    group = None
    batches = {False: [], True: []}
    lengths = {False: 0, True: 0}
    for next_group, row in planned_rows(master_orig, story_csv):
        if group is not None and next_group != group:
            for short in (False, True):
                if batches[short]:
                    yield make_job(group, batches[short])
            batches = {False: [], True: []}
            lengths = {False: 0, True: 0}
        group = next_group
        short = (group[0] == 'story' and len(row['source'].strip()) < 8)
        cap = short_rows if short else max_rows
        row_length = len(row['source']) + sum(len(n['source']) for n in
            row['context'].get('neighbors', []))
        if batches[short] and (len(batches[short]) >= cap or
                               lengths[short] + row_length > max_chars):
            yield make_job(group, batches[short])
            batches[short], lengths[short] = [], 0
        batches[short].append(row)
        lengths[short] += row_length
    if group is not None:
        for short in (False, True):
            if batches[short]:
                yield make_job(group, batches[short])


def make_job(group, rows):
    value = {'kind': group[0], 'group': list(group[1:]), 'rows': rows}
    value['id'] = sha256(stable_json(value))[:24]
    return value


def cache_key(job, prompt, model):
    # Scope aliases alter review provenance but not the model input; a newly
    # observed duplicate record should not trigger a repeat API request.
    model_job = {key: value for key, value in job.items() if key != 'aliases'}
    return sha256(stable_json({'schema_version': SCHEMA, 'job': model_job,
                               'prompt_sha256': sha256(prompt), 'model': model}))


def validate_terms(job, output):
    """Reject invented terms, references and variable/credit candidates."""
    if not isinstance(output, dict) or set(output) != {'terms'} or not isinstance(output['terms'], list):
        raise ValueError('Model output must contain only terms array')
    rows = {row['id']: row for row in job['rows']}
    accepted = []
    seen = set()
    for number, item in enumerate(output['terms'], 1):
        if not isinstance(item, dict) or set(item) != {
                'source', 'category', 'suggested_translation', 'evidence'}:
            raise ValueError(f'Term {number}: invalid item schema')
        source, category = item['source'], item['category']
        suggestion, evidence = item['suggested_translation'], item['evidence']
        if (not isinstance(source, str) or
                candidate_text(source, allow_literal_linebreaks=True) != source or
                len(source) > 80 or source.count('\n') > 2 or
                CONTROL_CHAR.search(source)):
            raise ValueError(f'Term {number}: invalid source form')
        if not isinstance(category, str) or category not in CATEGORIES:
            raise ValueError(f'Term {number}: invalid category')
        if not isinstance(suggestion, str) or len(suggestion) > 120:
            raise ValueError(f'Term {number}: invalid suggested translation')
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f'Term {number}: missing evidence list')
        if any(not isinstance(ref, str) or ref not in rows for ref in evidence):
            raise ValueError(f'Term {number}: evidence references unknown row')
        if len(evidence) != len(set(evidence)):
            raise ValueError(f'Term {number}: duplicate evidence row')
        for ref in evidence:
            row = rows[ref]
            if is_credit_row(row):
                raise ValueError(f'Term {number}: credit row')
            if not contains(row['source'], source, literal_linebreaks=True):
                raise ValueError(f'Term {number}: source absent from cited row')
        identity = (source, category, tuple(evidence))
        if identity in seen:
            raise ValueError(f'Term {number}: duplicate candidate')
        seen.add(identity)
        accepted.append(item)
    return accepted


def validation_error_class(error):
    message = str(error)
    for needle, category in (
            ('invalid item schema', 'schema'), ('invalid source form', 'source_form'),
            ('invalid category', 'category'), ('invalid suggested translation', 'suggestion'),
            ('missing evidence list', 'missing_evidence'),
            ('evidence references unknown row', 'unknown_row'),
            ('duplicate evidence row', 'duplicate_evidence'),
            ('credit row', 'credit_row'),
            ('source absent from cited row', 'source_absent'),
            ('duplicate candidate', 'duplicate_candidate')):
        if needle in message:
            return category
    return 'other'


def retain_grounded_terms(job, output, rejected_reasons=None, rejected_candidates=None):
    """Keep only individually valid terms; one hallucination must not discard its peers.

    Every retained candidate and every cited row still passes the strict validator.
    Malformed outer responses remain failures rather than becoming empty results.
    """
    if not isinstance(output, dict) or set(output) != {'terms'} or not isinstance(output['terms'], list):
        raise ValueError('Model output must contain only terms array')
    retained = []
    rejected = 0
    seen = set()
    for item in output['terms']:
        try:
            validate_terms(job, {'terms': [item]})
            identity = (item['source'], item['category'], tuple(item['evidence']))
            if identity in seen:
                raise ValueError('Duplicate candidate in one model response')
        except (TypeError, ValueError) as error:
            rejected += 1
            reason = validation_error_class(error)
            if rejected_reasons is not None:
                rejected_reasons[reason] += 1
            if rejected_candidates is not None:
                # Keep only the candidate fields, never arbitrary response metadata.
                candidate = {key: item[key] for key in (
                    'source', 'category', 'suggested_translation', 'evidence')
                    if isinstance(item, dict) and key in item}
                rejected_candidates.append({'candidate': candidate, 'reason': reason})
            continue
        seen.add(identity)
        retained.append(item)
    validate_terms(job, {'terms': retained})
    return retained, rejected


def aggregate(jobs, results, name_glossary=None, term_glossary=None):
    known = {term['source']: term['target'] for term in
             glossary_terms(name_glossary, term_glossary)}
    grouped = defaultdict(lambda: {'evidence': [], 'suggestions': Counter()})
    completed = 0
    for job in jobs:
        result = results.get(job['id'])
        if result is None:
            continue
        completed += 1
        rows = {row['id']: row for row in job['rows']}
        for item in validate_terms(job, {'terms': result}):
            record = grouped[(item['category'], item['source'])]
            record['suggestions'][item['suggested_translation']] += 1
            for ref in item['evidence']:
                row = rows[ref]
                record['evidence'].append({'row_id': ref, 'location': row['location'],
                    'context': {'speaker': row['context'].get('speaker'),
                                'source_excerpt': row['source'][:160]}})
                for alias in job.get('aliases', {}).get(ref, []):
                    if (contains(alias['source'], item['source'], literal_linebreaks=True)
                            and not is_credit_row(alias)):
                        record['evidence'].append({'row_id': alias['id'],
                            'location': alias['location'],
                            'context': {'speaker': alias['context'].get('speaker'),
                                        'source_excerpt': alias['source'][:160]}})
    candidates = []
    for (category, source), value in sorted(grouped.items()):
        candidates.append({'source': source, 'category': category,
            'in_glossary': source in known, 'glossary_target': known.get(source),
            'suggested_translations': dict(value['suggestions']),
            'evidence': value['evidence']})
    return {'schema_version': SCHEMA,
            'scope': 'semantic_review_only_no_dictionary_changes',
            'summary': {'jobs': len(jobs), 'completed_jobs': completed,
                        'pending_jobs': len(jobs) - completed,
                        'candidates': len(candidates),
                        'unknown': sum(not item['in_glossary'] for item in candidates),
                        'by_category': dict(sorted(Counter(item['category'] for item in candidates).items()))},
            'candidates': candidates}
