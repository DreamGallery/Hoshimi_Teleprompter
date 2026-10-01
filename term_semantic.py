#!/usr/bin/env python3
"""Plan, run and review evidence-bound AI terminology discovery."""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from main import read_settings
from src.review_inputs import write_json
from src.term_audit import input_manifest
from src.term_semantic import (aggregate, cache_key, make_job, make_jobs, retain_grounded_terms,
                               validate_terms)
from src.term_candidates import candidate_text


ROOT = Path(__file__).resolve().parent
PROMPT = ROOT / 'prompts/term-extraction.txt'

# The first semantic pass targets narrative and named-entity sources. Every
# excluded table is counted in the plan coverage report for a later pass.
FOCUSED_MASTER_TABLES = frozenset({
    'Accessory', 'Card', 'Character', 'CharacterGroup', 'Costume', 'EventStory',
    'ExtraStory', 'ExtraStoryPart', 'Hair', 'HomeAction', 'HomeTalk', 'Item',
    'LoveHomeAction', 'Message', 'MessageGroup', 'Music', 'Story', 'Telephone',
    'Wording',
})

# Named objects outside the narrative tables. Rule-heavy fields such as
# SkillEfficacy and PhotoRecipe are deliberately not sent record by record.
EXTRA_ENTITY_TABLES = frozenset({
    'ActivityPromotion', 'Area', 'CostumeType', 'Decoration', 'DutyPoint',
    'Emblem', 'Gacha', 'GachaStamp', 'HelpCategory', 'HomeBackground',
    'LiveBonus', 'Loading', 'LoginBonus', 'PhotoActivity', 'PhotoAllInOne',
    'PhotoContestActivity', 'PhotoContestQuestMusic', 'PhotoContestQuestStage',
    'PhotoContestSection', 'PhotoExpression', 'PhotoPose', 'PhotoQuestMusic',
    'PhotoQuestStage', 'ShowcaseFrame', 'ShowcaseMusicFilter', 'ShowcaseToy',
    'ShowcaseToyCategory', 'Stage', 'StatusEffectName', 'StoryPart',
})
NARRATIVE_MASTER_TABLES = frozenset({'Message', 'HomeTalk', 'Story'})
NARRATIVE_PHASE_TABLES = frozenset({
    'Message', 'HomeTalk', 'Story', 'HomeAction', 'LoveHomeAction', 'Telephone',
})
ENTITY_FIELD = re.compile(r'(?:^|\.)(?:name|title|fullName)$', re.I)
ARRAY_INDEX = re.compile(r'\[\d+\]')


def entity_rows(job):
    return [row for row in job['rows'] if ENTITY_FIELD.search(row['location']['field'])
            and candidate_text(row['source']) is not None]


def normalized_field(row):
    return ARRAY_INDEX.sub('[]', row['location']['field'])


def pack_master_jobs(jobs, max_rows, max_chars):
    """Batch independent records by table/field without dropping row provenance."""
    buckets = {}
    passthrough = []
    for job in jobs:
        table = job['group'][0]
        if table in NARRATIVE_MASTER_TABLES:
            passthrough.append(job)
            continue
        by_field = {}
        for row in job['rows']:
            by_field.setdefault(normalized_field(row), []).append(row)
        for field, rows in by_field.items():
            for start in range(0, len(rows), max_rows):
                chunk = rows[start:start + max_rows]
                part = (job if len(chunk) == len(job['rows']) else make_job(
                    ('master', table, job['group'][1], 'part:' + str(start)), chunk))
                aliases = {row['id']: job['aliases'][row['id']] for row in chunk
                           if row['id'] in job.get('aliases', {})}
                if aliases:
                    part = dict(part, aliases=aliases)
                buckets.setdefault((table, field), []).append(part)
    packed = list(passthrough)
    for (table, field), parts in buckets.items():
        group = []
        length = 0

        def flush():
            if not group:
                return
            if len(group) == 1:
                packed.append(group[0])
                return
            rows = [row for part in group for row in part['rows']]
            combined = make_job(('master', table, 'packed:' + field), rows)
            aliases = {key: values for part in group
                       for key, values in part.get('aliases', {}).items()}
            if aliases:
                combined['aliases'] = aliases
            packed.append(combined)

        for part in parts:
            part_length = sum(len(row['source']) for row in part['rows'])
            if group and (sum(len(p['rows']) for p in group) + len(part['rows']) > max_rows
                          or length + part_length > max_chars):
                flush()
                group, length = [], 0
            group.append(part)
            length += part_length
        flush()
    return packed


class PartialTermsError(ValueError):
    def __init__(self, terms, rejected, reasons=None, rejected_candidates=None):
        super().__init__('Model still returned candidates failing strict evidence validation')
        self.terms = terms
        self.rejected = rejected
        self.reasons = dict(reasons or {})
        self.rejected_candidates = list(rejected_candidates or [])


def read_plan(path):
    with path.open(encoding='utf-8') as stream:
        header = json.loads(next(stream))
        if header.get('schema_version') != 1 or header.get('kind') != 'term_semantic_plan':
            raise ValueError('Invalid semantic extraction plan')
        jobs = [json.loads(line) for line in stream if line.strip()]
    if len({job['id'] for job in jobs}) != len(jobs):
        raise ValueError('Duplicate semantic job ID')
    return header, jobs


def slice_plan(args):
    """Select complete jobs from a validated plan without changing IDs/cache keys."""
    if args.output.resolve() == args.plan.resolve():
        raise ValueError('Slice output must differ from source plan')
    mode = getattr(args, 'master_mode', 'none')
    if not args.story_prefix and not args.master_table and mode == 'none':
        raise ValueError('Provide story prefixes and/or Master tables')
    header, jobs = read_plan(args.plan)
    def master_selected(table):
        return (table in args.master_table or mode == 'all' or
                (mode == 'narrative' and table in NARRATIVE_PHASE_TABLES) or
                (mode == 'independent' and table not in NARRATIVE_PHASE_TABLES))

    selected = [job for job in jobs if (job['kind'] == 'story' and any(
        job['group'][0].startswith(prefix) for prefix in args.story_prefix)) or (
        job['kind'] == 'master' and master_selected(job['group'][0]))]
    if not selected:
        raise ValueError('Slice selected no jobs')
    counts = Counter(('master:' + job['group'][0]) if job['kind'] == 'master'
                     else ('story:' + '_'.join(job['group'][0].split('_')[:2]))
                     for job in selected)
    with args.plan.open('rb') as original:
        digest = hashlib.file_digest(original, 'sha256').hexdigest()
    output_header = dict(header)
    output_header['selection'] = dict(header.get('selection', {}),
        slice_story_prefix=args.story_prefix, slice_master_table=args.master_table,
        slice_master_mode=mode,
        parent_plan_sha256=digest)
    output_header['coverage'] = {'selected_after_dedupe': len(selected),
                                 'selected_by_stratum': dict(sorted(counts.items())),
                                 'parent_jobs': len(jobs)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=args.output.parent,
                                     prefix='.term-slice-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(output_header, ensure_ascii=False) + '\n')
            for job in selected:
                stream.write(json.dumps(job, ensure_ascii=False, separators=(',', ':')) + '\n')
            stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)
    return {'jobs': len(selected), 'selected_by_stratum': dict(sorted(counts.items())),
            'output': str(args.output)}


def repair_empty(args):
    """Make resumable smaller jobs for empty or partial source batches."""
    if args.output.resolve() in {args.plan.resolve(), args.log.resolve(), args.prompt.resolve()}:
        raise ValueError('Repair output must differ from inputs')
    header, jobs = read_plan(args.plan)
    prompt = args.prompt.read_text(encoding='utf-8')
    status = getattr(args, 'status', 'empty')
    latest = {}
    if args.log.exists():
        with args.log.open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip():
                    item = json.loads(line)
                    latest[item['cache_key']] = item
    empty = []
    for job in jobs:
        previous = latest.get(cache_key(job, prompt, args.model))
        if previous is None or previous['job_id'] != job['id']:
            continue
        if status == 'empty' and (previous.get('status', 'complete') != 'complete'
                                  or previous['terms']):
            continue
        if status == 'partial' and previous.get('status') != 'partial':
            continue
        if args.scope == 'short-and-entity' and not any(
                (job['kind'] == 'story' and len(row['source'].strip()) < 8) or
                (job['kind'] == 'master' and ENTITY_FIELD.search(row['location']['field']))
                for row in job['rows']):
            continue
        empty.append(job)
    repairs = []
    for job in empty:
        for start in range(0, len(job['rows']), args.max_rows):
            rows = job['rows'][start:start + args.max_rows]
            part = make_job((job['kind'], job['group'][0],
                             'empty-repair:' + job['id'] + ':' + str(start)), rows)
            aliases = {row['id']: job['aliases'][row['id']] for row in rows
                       if row['id'] in job.get('aliases', {})}
            if aliases:
                part['aliases'] = aliases
            part['repair_of'] = job['id']
            repairs.append(part)
    with args.plan.open('rb') as original:
        digest = hashlib.file_digest(original, 'sha256').hexdigest()
    output_header = dict(header)
    output_header['selection'] = dict(header.get('selection', {}),
        repair_status=status, empty_repair_scope=args.scope,
        empty_repair_max_rows=args.max_rows,
        parent_plan_sha256=digest)
    output_header['coverage'] = {'selected_after_dedupe': len(repairs),
        'source_jobs': len(empty), 'selected_rows': sum(len(j['rows']) for j in repairs),
        'parent_jobs': len(jobs)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=args.output.parent,
                                     prefix='.term-empty-repair-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(output_header, ensure_ascii=False) + '\n')
            for job in repairs:
                stream.write(json.dumps(job, ensure_ascii=False, separators=(',', ':')) + '\n')
            stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)
    return {'source_jobs': len(empty), 'empty_source_jobs': len(empty) if status == 'empty' else 0,
            'partial_source_jobs': len(empty) if status == 'partial' else 0,
            'repair_jobs': len(repairs),
            'repair_rows': output_header['coverage']['selected_rows'],
            'output': str(args.output)}


def plan(args):
    if not args.master_orig and not args.story_csv:
        raise ValueError('Provide MasterDB and/or story CSV input')
    paths = [path for path in (args.master_orig, args.story_csv) if path]
    for path in paths:
        if not path.exists():
            raise ValueError(f'Missing input: {path}')
        if args.output.resolve() == path.resolve() or (
                path.is_dir() and args.output.resolve().is_relative_to(path.resolve())):
            raise ValueError('Plan output must be outside input directory')
    manifest = input_manifest(paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    explicit_selection = bool(args.script_prefix or args.master_table)
    selected = Counter()
    excluded = Counter()
    excluded_source_chars = Counter()
    strata = Counter()
    representatives = {}
    unmerged_master = []
    merged = 0
    packed_away = 0
    selected_source_chars = 0
    request_source_chars = 0
    # Story jobs are streamed to disk; only Master representatives are held so
    # later duplicate records can be attached as location aliases.
    with tempfile.NamedTemporaryFile('w+', encoding='utf-8', dir=args.output.parent,
                                     prefix='.term-body-', delete=False) as body:
        body_path = Path(body.name)
        try:
            for job in make_jobs(args.master_orig, args.story_csv,
                                 max_rows=args.batch_rows, max_chars=args.batch_chars,
                                 short_rows=getattr(args, 'short_batch_rows', 8)):
                stratum = ('master:' + job['group'][0] if job['kind'] == 'master'
                           else 'story:' + '_'.join(job['group'][0].split('_')[:2]))
                if (not explicit_selection and args.profile == 'focused' and
                        job['kind'] == 'master' and job['group'][0] in EXTRA_ENTITY_TABLES):
                    rows = entity_rows(job)
                    if rows:
                        job = make_job(('master', *job['group']), rows)
                    else:
                        excluded[stratum] += 1
                        excluded_source_chars[stratum] += sum(len(row['source']) for row in job['rows'])
                        continue
                source_chars = sum(len(row['source']) for row in job['rows'])
                if explicit_selection:
                    include = (job['kind'] == 'master' and
                               job['group'][0] in args.master_table) or (
                               job['kind'] == 'story' and any(
                                   job['group'][0].startswith(prefix)
                                   for prefix in args.script_prefix))
                else:
                    include = (job['kind'] == 'story' or args.profile == 'all' or
                               job['group'][0] in FOCUSED_MASTER_TABLES or
                               job['group'][0] in EXTRA_ENTITY_TABLES)
                if not include:
                    excluded[stratum] += 1
                    excluded_source_chars[stratum] += source_chars
                    continue
                if args.max_jobs_per_stratum and strata[stratum] >= args.max_jobs_per_stratum:
                    excluded[stratum] += 1
                    excluded_source_chars[stratum] += source_chars
                    continue
                selected[stratum] += 1
                selected_source_chars += source_chars
                strata[stratum] += 1
                if job['kind'] == 'story':
                    body.write(json.dumps(job, ensure_ascii=False, separators=(',', ':')) + '\n')
                    request_source_chars += source_chars
                    continue
                if not args.dedupe_master:
                    unmerged_master.append(job)
                    request_source_chars += source_chars
                    continue
                signature = master_signature(job)
                representative = representatives.get(signature)
                if representative is None:
                    representatives[signature] = job
                    request_source_chars += source_chars
                    continue
                for base, duplicate in zip(representative['rows'], job['rows']):
                    aliases = representative.setdefault('aliases', {}).setdefault(base['id'], [])
                    aliases.append(duplicate)
                merged += 1
            master_jobs = list(representatives.values()) if args.dedupe_master else unmerged_master
            if getattr(args, 'pack_master', True):
                master_jobs = pack_master_jobs(master_jobs,
                                               getattr(args, 'entity_batch_rows', 8),
                                               args.batch_chars)
                packed_away = (len(representatives) if args.dedupe_master
                               else len(unmerged_master)) - len(master_jobs)
            for job in master_jobs:
                body.write(json.dumps(job, ensure_ascii=False,
                                      separators=(',', ':')) + '\n')
            body.flush()
            os.fsync(body.fileno())
            if input_manifest(paths) != manifest:
                raise ValueError('Source changed while planning; retry')
            before = sum(selected.values())
            count = before - merged - packed_away
            coverage = {'selected_before_dedupe': before,
                        'selected_after_dedupe': count,
                        'merged_master_jobs': merged,
                        'packed_master_jobs': packed_away,
                        'selected_source_characters_before_dedupe': selected_source_chars,
                        'request_source_characters_after_dedupe': request_source_chars,
                        'selected_by_stratum': dict(sorted(selected.items())),
                        'excluded_by_stratum': dict(sorted(excluded.items())),
                        'excluded_source_characters_by_stratum': dict(sorted(
                            excluded_source_chars.items()))}
            header = {'schema_version': 1, 'kind': 'term_semantic_plan',
                      'inputs': manifest,
                      'selection': {'profile': args.profile,
                          'script_prefix': args.script_prefix,
                          'master_table': args.master_table,
                          'max_jobs_per_stratum': args.max_jobs_per_stratum,
                          'batch_rows': args.batch_rows,
                          'short_batch_rows': getattr(args, 'short_batch_rows', 8),
                          'entity_batch_rows': getattr(args, 'entity_batch_rows', 8),
                          'batch_chars': args.batch_chars,
                          'dedupe_master': args.dedupe_master,
                          'pack_master': getattr(args, 'pack_master', True)},
                      'coverage': coverage}
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=args.output.parent,
                                             prefix='.term-plan-', delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(json.dumps(header, ensure_ascii=False) + '\n')
                    body.seek(0)
                    shutil.copyfileobj(body, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.replace(temporary, args.output)
                finally:
                    temporary.unlink(missing_ok=True)
        finally:
            body_path.unlink(missing_ok=True)
    return {'jobs': count, 'coverage': coverage, 'output': str(args.output)}


def master_signature(job):
    """Merge only equal table, field, source and record-label context."""
    return (job['group'][0], tuple((row['location']['field'], row['source'],
        json.dumps(row['context'].get('record_labels'), ensure_ascii=False,
                   sort_keys=True, separators=(',', ':'))) for row in job['rows']))


def parse_response(payload):
    try:
        choice = payload['choices'][0]
        content = choice['message']['content']
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError('Model response has no text choice') from error
    if choice.get('finish_reason') == 'length':
        raise ValueError('Model response was truncated')
    if not isinstance(content, str):
        raise ValueError('Model response is not text')
    content = content.strip()
    if content.startswith('```'):
        if '\n' not in content or '```' not in content[3:]:
            raise ValueError('Model JSON code fence is incomplete')
        content = content.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError('Model response is not valid JSON') from error


def request_terms(url, api_key, model, prompt, job):
    task = {'rows': job['rows'], 'output_format': {
        'terms': [{'source': 'exact substring', 'category': 'one listed category',
                   'suggested_translation': 'provisional Simplified Chinese',
                   'evidence': ['exact row id']}]}}
    correction = ('The previous response cited terms absent from one or more referenced '
                  'row source fields. For every proposed term, check that its exact surface '
                  'form appears in every cited row\'s own source. Speaker and neighbors are '
                  'context only and cannot be evidence. Omit unsupported terms; return only '
                  'the required JSON object.')
    for correction_attempt in range(3):
        messages = [{'role': 'system', 'content': prompt}]
        if correction_attempt:
            messages.append({'role': 'system', 'content': correction})
        request_task = dict(task)
        if correction_attempt == 2:
            # Eliminate speaker/neighbor context on the last attempt. A model
            # otherwise repeatedly cites names present only in those fields.
            request_task['rows'] = [{'id': row['id'], 'source': row['source']}
                                    for row in job['rows']]
        messages.append({'role': 'user', 'content': json.dumps(request_task, ensure_ascii=False)})
        body = {'model': model, 'temperature': 0, 'max_tokens': 4096,
                'thinking': {'type': 'disabled'}, 'reasoning_effort': 'none',
                'messages': messages}
        request = Request(url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                          headers={'Authorization': 'Bearer ' + api_key,
                                   'Content-Type': 'application/json'}, method='POST')
        for attempt in range(4):
            try:
                with urlopen(request, timeout=120) as response:
                    output = parse_response(json.load(response))
                break
            except HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError(f'Terminology API HTTP {error.code}') from error
            except (URLError, TimeoutError) as error:
                if attempt == 3:
                    raise RuntimeError('Terminology API connection failed') from error
            time.sleep(min(2 ** attempt, 8))
        rejected_reasons = Counter()
        rejected_candidates = []
        terms, rejected = retain_grounded_terms(job, output, rejected_reasons,
                                               rejected_candidates)
        if not rejected:
            return terms
        if correction_attempt == 2:
            raise PartialTermsError(terms, rejected, rejected_reasons, rejected_candidates)
    raise AssertionError('unreachable')


def load_cached(path):
    results = {}
    if not path.exists():
        return results
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if (not isinstance(item['cache_key'], str) or
                        not isinstance(item['job_id'], str) or
                        not isinstance(item['terms'], list)):
                    raise ValueError('bad result schema')
                if item.get('status', 'complete') not in {'complete', 'partial'}:
                    raise ValueError('bad result status')
                if item.get('status') == 'partial':
                    continue
                results[item['cache_key']] = item
            except (KeyError, ValueError, TypeError) as error:
                raise ValueError(f'{path}:{number}: invalid cached response') from error
    return results


def repair_incomplete_tail(path):
    """Discard only an interrupted final JSONL record before resuming."""
    if not path.exists():
        return
    with path.open('rb+') as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        if not size:
            return
        stream.seek(size - 1)
        if stream.read(1) == b'\n':
            return
        start = size - 1
        while start > 0:
            stream.seek(start - 1)
            if stream.read(1) == b'\n':
                break
            start -= 1
        stream.seek(start)
        final = stream.read()
        try:
            item = json.loads(final)
            valid = (isinstance(item, dict) and
                     {'cache_key', 'job_id', 'terms'} <= set(item) and
                     set(item) <= {'cache_key', 'job_id', 'terms', 'status', 'rejected_count',
                                   'reject_reasons', 'rejected_candidates'})
        except (UnicodeError, json.JSONDecodeError):
            valid = False
        if valid:
            stream.seek(0, os.SEEK_END)
            stream.write(b'\n')
        else:
            stream.truncate(start)
        stream.flush()
        os.fsync(stream.fileno())


def api_settings(args):
    settings = read_settings(args.env_file)
    base = args.api_base or os.getenv('OPENAI_API_BASE') or settings.get('OPENAI_API_BASE')
    model = args.model or os.getenv('OPENAI_MODEL') or settings.get('OPENAI_MODEL')
    key = os.getenv('OPENAI_API_KEY') or settings.get('OPENAI_API_KEY')
    if not base or not model or not key:
        raise ValueError('Set API base, model and OPENAI_API_KEY in environment or .env')
    url = base.rstrip('/')
    if not url.endswith('/chat/completions'):
        url += '/chat/completions'
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Term API must be direct HTTPS without URL credentials')
    return url, model, key


def extract(args):
    if args.log.resolve() in {args.plan.resolve(), args.prompt.resolve()}:
        raise ValueError('Result log must not overwrite plan or prompt')
    _, jobs = read_plan(args.plan)
    prompt = args.prompt.read_text(encoding='utf-8')
    url, model, key = api_settings(args)
    repair_incomplete_tail(args.log)
    cached = load_cached(args.log)
    for job in jobs:
        prior = cached.get(cache_key(job, prompt, model))
        if prior is not None:
            if prior['job_id'] != job['id']:
                raise ValueError('Cached job ID does not match plan')
            validate_terms(job, {'terms': prior['terms']})
    remaining = [job for job in jobs if cache_key(job, prompt, model) not in cached]
    cached_before = len(jobs) - len(remaining)
    if args.max_jobs:
        remaining = remaining[:args.max_jobs]
    args.log.parent.mkdir(parents=True, exist_ok=True)
    completed = 0
    failures = 0
    attempted = 0
    stopped_early = False
    consecutive_failures = 0
    with args.log.open('a', encoding='utf-8') as output:
        # Rolling bounded submission keeps slow requests from holding a block open.
        # Only this coordinator writes/fsyncs the result log.
        capacity = min(args.workers, 64)
        with ThreadPoolExecutor(max_workers=capacity) as executor:
            futures = {}

            def submit_next():
                nonlocal attempted
                job = remaining[attempted]
                futures[executor.submit(request_terms, url, key, model, prompt, job)] = job
                attempted += 1

            while attempted < len(remaining) and len(futures) < capacity:
                submit_next()
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    job = futures.pop(future)
                    try:
                        terms = future.result()
                    except PartialTermsError as error:
                        failures += 1
                        consecutive_failures += 1
                        output.write(json.dumps({'cache_key': cache_key(job, prompt, model),
                            'job_id': job['id'], 'status': 'partial',
                            'rejected_count': error.rejected,
                            'reject_reasons': error.reasons,
                            'rejected_candidates': error.rejected_candidates,
                            'terms': error.terms},
                            ensure_ascii=False) + '\n')
                        output.flush()
                        os.fsync(output.fileno())
                        print(f'{job["id"]}: partial, {error.rejected} unsupported candidates '
                              f'{error.reasons}; '
                              'kept for retry', flush=True)
                    except Exception as error:
                        failures += 1
                        consecutive_failures += 1
                        # request_terms emits only controlled HTTP status or connection
                        # diagnostics; never print response bodies or credentials.
                        reason = (str(error) if isinstance(error, (ValueError, RuntimeError))
                                  else type(error).__name__)
                        print(f'{job["id"]}: {reason}; kept for retry', flush=True)
                    else:
                        output.write(json.dumps({'cache_key': cache_key(job, prompt, model),
                            'job_id': job['id'], 'terms': terms}, ensure_ascii=False) + '\n')
                        output.flush()
                        os.fsync(output.fileno())
                        completed += 1
                        consecutive_failures = 0
                    if consecutive_failures >= 64 and not stopped_early:
                        print('64 consecutive tasks failed; stopping new submissions '
                              'and draining in-flight requests', flush=True)
                        stopped_early = True
                # Process every already-completed result before replenishing so the
                # circuit cannot be hidden by queued completions. Once opened it
                # stays open even if a request already in flight later succeeds.
                while (not stopped_early and attempted < len(remaining)
                       and len(futures) < capacity):
                    submit_next()
    return {'planned': len(jobs), 'cached_before': cached_before,
            'completed_now': completed, 'failed_now': failures,
            'unattempted_now': len(remaining) - attempted,
            'stopped_early': stopped_early}


def review(args):
    if args.output.resolve() in {args.plan.resolve(), args.log.resolve(), args.prompt.resolve(),
                                 args.name_glossary.resolve(), args.term_glossary.resolve()}:
        raise ValueError('Review output must not overwrite an input')
    header, jobs = read_plan(args.plan)
    prompt = args.prompt.read_text(encoding='utf-8')
    cached = load_cached(args.log)
    by_job = {}
    for job in jobs:
        prior = cached.get(cache_key(job, prompt, args.model))
        if prior is not None:
            if prior['job_id'] != job['id']:
                raise ValueError('Cached job ID does not match plan')
            by_job[job['id']] = prior['terms']
    report = aggregate(jobs, by_job, args.name_glossary, args.term_glossary)
    # Partial diagnostics are deliberately separate from grounded candidates.
    latest = {}
    if args.log.exists():
        for line in args.log.read_text(encoding='utf-8').splitlines():
            if line.strip():
                item = json.loads(line)
                latest[item['cache_key']] = item
    unverified = []
    for job in jobs:
        item = latest.get(cache_key(job, prompt, args.model))
        if item is not None and item.get('status') == 'partial':
            if item['job_id'] != job['id']:
                raise ValueError('Cached job ID does not match plan')
            unverified.append({'job_id': job['id'], 'status': 'unverified',
                'rejected_count': item.get('rejected_count', 0),
                'reject_reasons': item.get('reject_reasons', {}),
                'rejected_candidates': item.get('rejected_candidates', []),
                'candidate_details_available': 'rejected_candidates' in item})
    report['unverified_candidates'] = unverified
    report['summary']['partial_jobs'] = len(unverified)
    report['inputs'] = header['inputs']
    report['model'] = args.model
    report['prompt_sha256'] = __import__('hashlib').sha256(prompt.encode()).hexdigest()
    write_json(args.output, report)
    return report['summary']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    planning = commands.add_parser('plan', help='Write exact source/evidence jobs without API')
    planning.add_argument('--master-orig', type=Path)
    planning.add_argument('--story-csv', type=Path)
    planning.add_argument('--script-prefix', action='append', default=[])
    planning.add_argument('--master-table', action='append', default=[])
    planning.add_argument('--profile', choices=['focused', 'all'], default='focused',
                          help='Default: all stories plus narrative/entity Master tables')
    planning.add_argument('--batch-rows', type=int, default=16)
    planning.add_argument('--short-batch-rows', type=int, default=8)
    planning.add_argument('--entity-batch-rows', type=int, default=8)
    planning.add_argument('--batch-chars', type=int, default=3000)
    planning.add_argument('--no-dedupe-master', dest='dedupe_master', action='store_false')
    planning.set_defaults(dedupe_master=True)
    planning.add_argument('--no-pack-master', dest='pack_master', action='store_false')
    planning.set_defaults(pack_master=True)
    planning.add_argument('--max-jobs-per-stratum', type=int, default=0,
                          help='0 means all; use a small positive value for a stratified pilot')
    planning.add_argument('--output', required=True, type=Path)
    slicing = commands.add_parser('slice', help='Select unchanged jobs from an existing plan')
    slicing.add_argument('--plan', required=True, type=Path)
    slicing.add_argument('--story-prefix', action='append', default=[])
    slicing.add_argument('--master-table', action='append', default=[])
    slicing.add_argument('--master-mode', choices=['none', 'independent', 'narrative', 'all'],
                         default='none')
    slicing.add_argument('--output', required=True, type=Path)
    repairing = commands.add_parser('repair-empty',
        help='Split completed empty short/entity jobs for a second evidence pass')
    repairing.add_argument('--plan', required=True, type=Path)
    repairing.add_argument('--log', required=True, type=Path)
    repairing.add_argument('--prompt', type=Path, default=PROMPT)
    repairing.add_argument('--model', required=True)
    repairing.add_argument('--scope', choices=['short-and-entity', 'all'],
                           default='short-and-entity')
    repairing.add_argument('--max-rows', type=int, default=4)
    repairing.add_argument('--output', required=True, type=Path)
    repairing.set_defaults(status='empty')
    partial_repair = commands.add_parser('repair-partial',
        help='Split strict-evidence partial jobs without accepting unsupported terms')
    partial_repair.add_argument('--plan', required=True, type=Path)
    partial_repair.add_argument('--log', required=True, type=Path)
    partial_repair.add_argument('--prompt', type=Path, default=PROMPT)
    partial_repair.add_argument('--model', required=True)
    partial_repair.add_argument('--scope', choices=['short-and-entity', 'all'], default='all')
    partial_repair.add_argument('--max-rows', type=int, default=4)
    partial_repair.add_argument('--output', required=True, type=Path)
    partial_repair.set_defaults(status='partial')
    running = commands.add_parser('extract', help='Call an OpenAI-compatible model, resumably')
    running.add_argument('--plan', required=True, type=Path)
    running.add_argument('--log', required=True, type=Path)
    running.add_argument('--prompt', type=Path, default=PROMPT)
    running.add_argument('--env-file', type=Path, default=ROOT / '.env')
    running.add_argument('--api-base')
    running.add_argument('--model')
    running.add_argument('--workers', type=int, default=4)
    running.add_argument('--max-jobs', type=int, default=0)
    reviewing = commands.add_parser('review', help='Validate and aggregate cached evidence')
    reviewing.add_argument('--plan', required=True, type=Path)
    reviewing.add_argument('--log', required=True, type=Path)
    reviewing.add_argument('--prompt', type=Path, default=PROMPT)
    reviewing.add_argument('--model', required=True)
    reviewing.add_argument('--name-glossary', type=Path, default=ROOT / 'glossaries/name-glossary.json')
    reviewing.add_argument('--term-glossary', type=Path, default=ROOT / 'glossaries/term-glossary.json')
    reviewing.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == 'plan' and args.max_jobs_per_stratum < 0:
        parser.error('max-jobs-per-stratum must be nonnegative')
    if args.command == 'plan' and (args.batch_rows < 1 or args.short_batch_rows < 1
                                   or args.entity_batch_rows < 1 or args.batch_chars < 200):
        parser.error('batch rows must be positive and batch-chars at least 200')
    if args.command == 'extract' and (not 1 <= args.workers <= 100 or args.max_jobs < 0):
        parser.error('workers must be 1–100 and max-jobs nonnegative')
    if args.command in {'repair-empty', 'repair-partial'} and not 1 <= args.max_rows <= 8:
        parser.error('repair max-rows must be 1–8')
    result = {'plan': plan, 'slice': slice_plan, 'repair-empty': repair_empty,
              'repair-partial': repair_empty,
              'extract': extract, 'review': review}[args.command](args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == 'extract' and (result['failed_now'] or result['stopped_early']):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
