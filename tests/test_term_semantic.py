"""Semantic term suggestions must be grounded in exact source rows."""

import unittest
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import term_semantic as cli

from src.term_audit import Entry
from src.term_candidates import candidate_text
from src.term_semantic import (aggregate, cache_key, eligible, make_job, make_jobs,
                               retain_grounded_terms,
                               validate_terms)


class TermSemanticTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {'id': 'r1', 'source': '星見プロダクションの新しい衣装です。',
             'location': {'kind': 'story', 'script': 'adv_card_01.txt', 'field': '10:text:1'},
             'context': {'speaker': '長瀬琴乃', 'neighbors': []}},
            {'id': 'r2', 'source': 'お疲れ様でした。',
             'location': {'kind': 'story', 'script': 'adv_card_01.txt', 'field': '11:text:1'},
             'context': {'speaker': '長瀬琴乃', 'neighbors': []}},
        ]
        self.job = make_job(('story', 'adv_card_01.txt'), self.rows)

    def test_grounded_candidate_and_cache_version(self):
        suggestion = {'terms': [{'source': '星見プロダクション', 'category': 'agency',
                                 'suggested_translation': '星见事务所', 'evidence': ['r1']}]}
        self.assertEqual(validate_terms(self.job, suggestion), suggestion['terms'])
        report = aggregate([self.job], {self.job['id']: suggestion['terms']})
        self.assertEqual(report['summary']['candidates'], 1)
        self.assertEqual(report['candidates'][0]['evidence'][0]['location']['field'], '10:text:1')
        self.assertNotEqual(cache_key(self.job, 'prompt-v1', 'model'),
                            cache_key(self.job, 'prompt-v2', 'model'))
        self.assertNotEqual(cache_key(self.job, 'prompt-v1', 'model'),
                            cache_key(self.job, 'prompt-v1', 'new-model'))

    def test_rejects_hallucination_wrong_row_and_user_variable(self):
        for source, evidence in [
            ('月のテンペスト', ['r1']),
            ('星見プロダクション', ['r2']),
            ('{username}', ['r1']),
        ]:
            with self.subTest(source=source, evidence=evidence), self.assertRaises(ValueError):
                validate_terms(self.job, {'terms': [{'source': source, 'category': 'agency',
                    'suggested_translation': '', 'evidence': evidence}]})

    def test_rejects_music_credit_as_evidence(self):
        row = {'id': 'm1', 'source': '作曲家A',
               'location': {'kind': 'master', 'table': 'Music', 'id': '1', 'field': 'composer'},
               'context': {}}
        job = make_job(('master', 'Music', '1'), [row])
        with self.assertRaises(ValueError):
            validate_terms(job, {'terms': [{'source': '作曲家A', 'category': 'character',
                'suggested_translation': '作曲家A', 'evidence': ['m1']}]})

    def test_multiline_title_requires_exact_newlines_and_safe_controls(self):
        row = {'id': 'title', 'source': '温泉\nドリームギフト',
               'location': {'kind': 'master', 'table': 'Gacha', 'id': '1', 'field': 'name'},
               'context': {'record_labels': {'name': '温泉\nドリームギフト'}}}
        job = make_job(('master', 'Gacha', '1'), [row])
        def item(source):
            return {'terms': [{'source': source, 'category': 'event',
                     'suggested_translation': '', 'evidence': ['title']}]}
        self.assertEqual(validate_terms(job, item('温泉\nドリームギフト')),
                         item('温泉\nドリームギフト')['terms'])
        for value in ['温泉ドリームギフト', '温泉\n\n\nドリームギフト',
                      '温泉\tドリームギフト', '温泉\rドリームギフト']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_terms(job, item(value))

    def test_literal_linebreak_title_preserves_exact_evidence_and_rejects_variables(self):
        source = '黒百合の\\nブローチ'
        row = dict(self.rows[0], source=source)
        job = make_job(('story', 'literal-newline'), [row])
        def item(value):
            return {'terms': [{'source': value, 'category': 'item',
                              'suggested_translation': '', 'evidence': ['r1']}]}
        self.assertEqual(validate_terms(job, item(source)), item(source)['terms'])
        self.assertIsNone(candidate_text(source))  # Other discovery paths keep their policy.
        self.assertEqual(candidate_text(source, allow_literal_linebreaks=True), source)
        for value in ('黒百合の\nブローチ', '黒百合のブローチ'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'source absent'):
                validate_terms(job, item(value))
        for value in ('{user}\\nブローチ', '{username}', '<name>\\nブローチ',
                      '$user\\nブローチ', '%s\\nブローチ', '黒百合の\\rブローチ',
                      '黒百合の\\tブローチ'):
            variable_job = make_job(('story', 'variable'), [dict(row, source=value)])
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'invalid source form'):
                validate_terms(variable_job, item(value))
        self.assertIsNone(candidate_text('月9', allow_literal_linebreaks=True))

    def test_ascii_name_after_literal_linebreak_keeps_token_boundaries(self):
        for name in ('BIG4', 'LizNoir'):
            term = {'source': name, 'category': 'group',
                    'suggested_translation': '', 'evidence': ['r1']}
            job = make_job(('story', 'ascii'), [dict(self.rows[0], source='今なら\\n'+name+'なら')])
            self.assertEqual(validate_terms(job, {'terms': [term]}), [term])
            for value in ('n'+name, 'Other'+name, name+'Other'):
                invalid = make_job(('story', 'ascii'), [dict(self.rows[0], source=value)])
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'source absent'):
                    validate_terms(invalid, {'terms': [term]})

    def test_mixed_model_response_keeps_only_exactly_grounded_terms(self):
        response = {'terms': [
            {'source': '星見プロダクション', 'category': 'agency',
             'suggested_translation': '星见事务所', 'evidence': ['r1']},
            {'source': '長瀬琴乃', 'category': 'character',
             'suggested_translation': '长濑琴乃', 'evidence': ['r1']},
        ]}
        terms, rejected = retain_grounded_terms(self.job, response)
        self.assertEqual([item['source'] for item in terms], ['星見プロダクション'])
        self.assertEqual(rejected, 1)
        self.assertEqual(validate_terms(self.job, {'terms': terms}), terms)

    def test_partial_result_is_preserved_but_not_cached_as_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'results.jsonl'
            item = {'cache_key': 'key', 'job_id': self.job['id'], 'terms': [],
                    'status': 'partial', 'rejected_count': 1}
            path.write_text(__import__('json').dumps(item) + '\n', encoding='utf-8')
            self.assertEqual(cli.load_cached(path), {})
            cli.repair_incomplete_tail(path)
            self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_final_rejections_are_logged_and_reviewed_separately(self):
        import json
        absent = {'source': '長瀬琴乃', 'category': 'character',
                  'suggested_translation': '长濑琴乃', 'evidence': ['r1']}
        final = {'source': '琴乃ちゃん', 'category': 'character',
                 'suggested_translation': '琴乃', 'evidence': ['r1']}
        responses = [{'terms': [absent]}, {'terms': [absent]}, {'terms': [final]}]
        with patch.object(cli, 'urlopen') as request, \
                patch.object(cli, 'parse_response', side_effect=responses):
            request.return_value.__enter__.return_value.read.return_value = b'{}'
            with self.assertRaises(cli.PartialTermsError) as captured:
                cli.request_terms('https://example.invalid', 'test-secret', 'model',
                                  'prompt', self.job)
        error = captured.exception
        self.assertEqual(error.rejected_candidates,
                         [{'candidate': final, 'reason': 'source_absent'}])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt, plan, log, output = [root / name for name in
                                        ('prompt.txt', 'plan.jsonl', 'log.jsonl', 'review.json')]
            prompt.write_text('prompt')
            plan.write_text(json.dumps({'schema_version': 1,
                                       'kind': 'term_semantic_plan', 'inputs': []}) +
                            '\n' + json.dumps(self.job) + '\n')
            args = SimpleNamespace(plan=plan, log=log, prompt=prompt,
                                   workers=1, max_jobs=0)
            with patch.object(cli, 'api_settings', return_value=(
                    'https://example.invalid', 'model', 'test-secret')), \
                    patch.object(cli, 'request_terms', side_effect=error):
                self.assertEqual(cli.extract(args)['failed_now'], 1)
            item = json.loads(log.read_text())
            self.assertEqual(item['terms'], [])
            self.assertEqual(item['rejected_candidates'], error.rejected_candidates)
            self.assertNotIn('test-secret', log.read_text())
            self.assertEqual(cli.load_cached(log), {})
            log.write_text(log.read_text().rstrip('\n'))
            cli.repair_incomplete_tail(log)
            self.assertEqual(json.loads(log.read_text()), item)
            glossary = root / 'glossary.json'
            glossary.write_text('{}')
            review = SimpleNamespace(plan=plan, log=log, prompt=prompt, model='model',
                                     name_glossary=glossary, term_glossary=glossary,
                                     output=output)
            cli.review(review)
            report = json.loads(output.read_text())
            self.assertEqual(report['candidates'], [])
            self.assertEqual(report['summary']['completed_jobs'], 0)
            self.assertEqual(report['unverified_candidates'][0]['rejected_candidates'],
                             error.rejected_candidates)

    def test_rejected_diagnostics_strip_unknown_response_fields(self):
        item = {'source': '長瀬琴乃', 'category': 'character',
                'suggested_translation': '', 'evidence': ['r1'],
                'headers': {'Authorization': 'secret'}}
        diagnostics = []
        terms, count = retain_grounded_terms(self.job, {'terms': [item]},
                                            rejected_candidates=diagnostics)
        self.assertEqual((terms, count), ([], 1))
        self.assertEqual(diagnostics[0]['reason'], 'schema')
        self.assertNotIn('headers', diagnostics[0]['candidate'])

    def test_focused_plan_excludes_generic_tables_and_preserves_alias_scopes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master = root / 'orig'
            master.mkdir()
            output = root / 'plan.jsonl'
            def master_job(record_id, row_id):
                row = {'id': row_id, 'source': '星見プロダクション',
                       'location': {'kind': 'master', 'table': 'Item',
                                    'id': record_id, 'field': 'name'},
                       'context': {'record_labels': {'kind': 'agency'}}}
                return make_job(('master', 'Item', record_id), [row])
            first, second = master_job('1', 'a'), master_job('2', 'b')
            generic = make_job(('master', 'ConditionDescription', '3'), [{
                'id': 'c', 'source': '星見プロダクション',
                'location': {'kind': 'master', 'table': 'ConditionDescription',
                             'id': '3', 'field': 'text'},
                'context': {'record_labels': {}}}])
            args = SimpleNamespace(master_orig=master, story_csv=None,
                script_prefix=[], master_table=[], profile='focused',
                max_jobs_per_stratum=0, batch_rows=16, batch_chars=3000,
                dedupe_master=True, output=output)
            with patch.object(cli, 'make_jobs', return_value=iter([first, second, generic])), \
                    patch.object(cli, 'input_manifest', return_value=[]):
                result = cli.plan(args)
            self.assertEqual(result['jobs'], 1)
            self.assertEqual(result['coverage']['merged_master_jobs'], 1)
            self.assertEqual(result['coverage']['excluded_by_stratum'],
                             {'master:ConditionDescription': 1})
            _, jobs = cli.read_plan(output)
            self.assertEqual(jobs[0]['aliases']['a'][0]['location']['id'], '2')
            self.assertEqual(cache_key(jobs[0], 'prompt', 'model'),
                             cache_key(first, 'prompt', 'model'))
            terms = [{'source': '星見プロダクション', 'category': 'agency',
                      'suggested_translation': '星见事务所', 'evidence': ['a']}]
            report = aggregate(jobs, {jobs[0]['id']: terms})
            locations = [item['location']['id'] for item in report['candidates'][0]['evidence']]
            self.assertEqual(locations, ['1', '2'])

    def test_master_dedupe_does_not_cross_record_label_context(self):
        def job(record_id, label):
            row = {'id': record_id, 'source': '星見プロダクション',
                   'location': {'kind': 'master', 'table': 'Item',
                                'id': record_id, 'field': 'name'},
                   'context': {'record_labels': {'category': label}}}
            return make_job(('master', 'Item', record_id), [row])
        self.assertNotEqual(cli.master_signature(job('1', 'agency')),
                            cli.master_signature(job('2', 'costume')))

    def test_short_story_name_and_extra_entity_field_are_eligible(self):
        story = Entry('莉央', '', {'kind': 'story', 'script': 'adv_group_x.txt',
            'field': '12:text:1'}, {'speaker': '琴乃', 'neighbors': []})
        self.assertTrue(eligible(story))
        entity = Entry('駆け出し', '', {'kind': 'master', 'table': 'Emblem',
            'id': 'emblem-1', 'field': 'name'}, {'record_labels': {}})
        self.assertTrue(eligible(entity))
        self.assertFalse(eligible(Entry('作曲家A', '', {'kind': 'master',
            'table': 'Music', 'id': '1', 'field': 'composer'}, {'record_labels': {}})))

    def test_short_story_batches_are_separate_and_at_most_eight_rows(self):
        values = []
        for index in range(18):
            text = '莉央' if index % 2 else '井川葵が今ここで歌います。'
            values.append((('story', 'adv_group_test.txt'), {'id': str(index),
                'source': text, 'location': {'kind': 'story',
                'script': 'adv_group_test.txt', 'field': f'{index}:text:1'},
                'context': {'speaker': '琴乃', 'neighbors': []}}))
        with patch('src.term_semantic.planned_rows', return_value=iter(values)):
            jobs = list(make_jobs(max_rows=16, short_rows=8, max_chars=3000))
        self.assertEqual(sum(len(j['rows']) for j in jobs), 18)
        for job in jobs:
            sizes = {len(row['source'].strip()) < 8 for row in job['rows']}
            self.assertEqual(len(sizes), 1)
            self.assertLessEqual(len(job['rows']), 8 if True in sizes else 16)

    def test_pack_same_table_and_field_preserves_ids_labels_and_aliases(self):
        jobs = []
        for index in range(3):
            row = {'id': f'r{index}', 'source': f'星見プロダクション{index}',
                'location': {'kind': 'master', 'table': 'Item', 'id': str(index),
                             'field': 'name'},
                'context': {'record_labels': {'name': f'品目{index}'}}}
            job = make_job(('master', 'Item', str(index)), [row])
            if index == 0:
                job['aliases'] = {'r0': [dict(row, id='alias0',
                    location=dict(row['location'], id='duplicate'))]}
            jobs.append(job)
        packed = cli.pack_master_jobs(jobs, max_rows=2, max_chars=3000)
        self.assertEqual([len(job['rows']) for job in packed], [2, 1])
        self.assertEqual([row['id'] for row in packed[0]['rows']], ['r0', 'r1'])
        self.assertEqual(packed[0]['rows'][1]['context']['record_labels']['name'], '品目1')
        self.assertEqual(packed[0]['aliases']['r0'][0]['location']['id'], 'duplicate')
        terms = [{'source': '星見プロダクション0', 'category': 'agency',
                  'suggested_translation': '', 'evidence': ['r0']}]
        report = aggregate(packed, {packed[0]['id']: terms})
        self.assertEqual({e['location']['id'] for e in report['candidates'][0]['evidence']},
                         {'0', 'duplicate'})
        other = make_job(('master', 'Emblem', '0'), [dict(jobs[0]['rows'][0],
            id='other', location=dict(jobs[0]['rows'][0]['location'], table='Emblem'))])
        self.assertEqual(len(cli.pack_master_jobs(jobs[:1] + [other], 16, 3000)), 2)
        long_job = make_job(('master', 'Item', 'many'), [
            dict(jobs[0]['rows'][0], id=f'm{i}',
                 location=dict(jobs[0]['rows'][0]['location'], field=f'items[{i}].name'))
            for i in range(15)])
        self.assertEqual([len(j['rows']) for j in cli.pack_master_jobs([long_job], 8, 3000)],
                         [8, 7])

    def test_focused_extra_entity_only_sends_named_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master = root / 'orig'; master.mkdir()
            rows = [
                {'id': 'name', 'source': '星見のエンブレム',
                 'location': {'kind': 'master', 'table': 'Emblem', 'id': '1', 'field': 'name'},
                 'context': {'record_labels': {'name': '星見のエンブレム'}}},
                {'id': 'description', 'source': '普通の長い説明です。',
                 'location': {'kind': 'master', 'table': 'Emblem', 'id': '1',
                              'field': 'description'},
                 'context': {'record_labels': {'name': '星見のエンブレム'}}},
            ]
            job = make_job(('master', 'Emblem', '1'), rows)
            args = SimpleNamespace(master_orig=master, story_csv=None, script_prefix=[],
                master_table=[], profile='focused', max_jobs_per_stratum=0,
                batch_rows=16, batch_chars=3000, dedupe_master=True,
                pack_master=True, output=root/'plan.jsonl')
            with patch.object(cli, 'make_jobs', return_value=iter([job])), \
                    patch.object(cli, 'input_manifest', return_value=[]):
                result = cli.plan(args)
            self.assertEqual(result['jobs'], 1)
            _, jobs = cli.read_plan(args.output)
            self.assertEqual([row['id'] for row in jobs[0]['rows']], ['name'])

    def test_slice_preserves_job_ids_and_rejects_empty_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master_row = {'id': 'master1', 'source': '星見プロ',
                'location': {'kind': 'master', 'table': 'Item', 'id': '1', 'field': 'name'},
                'context': {'record_labels': {'name': '星見プロ'}}}
            master = make_job(('master', 'Item', '1'), [master_row])
            source = root/'source.jsonl'; output = root/'slice.jsonl'
            source.write_text('\n'.join(__import__('json').dumps(value, ensure_ascii=False)
                for value in ({'schema_version': 1, 'kind': 'term_semantic_plan',
                               'inputs': [], 'selection': {}}, self.job, master)) + '\n')
            args = SimpleNamespace(plan=source, output=output, story_prefix=[],
                                   master_table=['Item'])
            self.assertEqual(cli.slice_plan(args)['jobs'], 1)
            header, jobs = cli.read_plan(output)
            self.assertEqual(jobs[0]['id'], master['id'])
            self.assertEqual(header['coverage']['parent_jobs'], 2)
            args.master_table = []
            args.master_mode = 'independent'
            self.assertEqual(cli.slice_plan(args)['jobs'], 1)
            args.master_mode = 'narrative'
            with self.assertRaises(ValueError):
                cli.slice_plan(args)
            args.master_mode = 'none'
            args.master_table = ['Music']
            with self.assertRaises(ValueError):
                cli.slice_plan(args)

    def test_empty_repair_is_repeatable_and_keeps_exact_source_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root/'prompt.txt'; prompt.write_text('term prompt')
            short = dict(self.rows[0], id='short', source='井川葵')
            job = make_job(('story', 'adv_card_01.txt'), [short, self.rows[0]])
            plan = root/'plan.jsonl'; log = root/'results.jsonl'; output = root/'repair.jsonl'
            plan.write_text(__import__('json').dumps({'schema_version': 1,
                'kind': 'term_semantic_plan', 'inputs': [], 'selection': {}}) + '\n' +
                __import__('json').dumps(job, ensure_ascii=False) + '\n')
            log.write_text(__import__('json').dumps({'cache_key': cache_key(job,
                'term prompt', 'model'), 'job_id': job['id'], 'terms': []}) + '\n')
            args = SimpleNamespace(plan=plan, log=log, prompt=prompt, model='model',
                scope='short-and-entity', max_rows=1, output=output)
            result = cli.repair_empty(args)
            self.assertEqual((result['empty_source_jobs'], result['repair_jobs']), (1, 2))
            _, jobs = cli.read_plan(output)
            self.assertEqual([r['source'] for j in jobs for r in j['rows']],
                             ['井川葵', self.rows[0]['source']])
            self.assertTrue(all(j['repair_of'] == job['id'] for j in jobs))
            self.assertNotEqual(cache_key(jobs[0], 'term prompt', 'model'),
                                cache_key(job, 'term prompt', 'model'))
            self.assertEqual(cli.repair_empty(args)['repair_jobs'], 2)
            with log.open('a') as stream:
                stream.write(__import__('json').dumps({'cache_key': cache_key(job,
                    'term prompt', 'model'), 'job_id': job['id'], 'terms': [],
                    'status': 'partial', 'rejected_count': 1}) + '\n')
            args.status = 'partial'; args.scope = 'all'
            self.assertEqual(cli.repair_empty(args)['partial_source_jobs'], 1)
            self.assertEqual(cli.repair_empty(args)['repair_jobs'], 2)

    def test_extract_resumes_by_input_prompt_and_model_without_repeat_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / 'plan.jsonl'
            prompt = root / 'prompt.txt'
            log = root / 'results.jsonl'
            prompt.write_text('Extract exact terms', encoding='utf-8')
            plan.write_text(__import__('json').dumps({'schema_version': 1,
                'kind': 'term_semantic_plan', 'inputs': []}) + '\n' +
                __import__('json').dumps(self.job, ensure_ascii=False) + '\n', encoding='utf-8')
            args = SimpleNamespace(plan=plan, prompt=prompt, log=log,
                env_file=root / 'missing.env', api_base=None, model=None,
                workers=1, max_jobs=0)
            proposal = [{'source': '星見プロダクション', 'category': 'agency',
                         'suggested_translation': '星见事务所', 'evidence': ['r1']}]
            with patch.object(cli, 'api_settings', return_value=('https://example.test/v1/chat/completions',
                                                                  'test-model', 'secret')), \
                    patch.object(cli, 'request_terms', return_value=proposal) as request:
                self.assertEqual(cli.extract(args)['completed_now'], 1)
                self.assertEqual(cli.extract(args)['completed_now'], 0)
                request.assert_called_once()

            second = make_job(('story', 'adv_card_02.txt'), [dict(self.rows[0], id='r3')])
            with plan.open('a', encoding='utf-8') as stream:
                stream.write(__import__('json').dumps(second, ensure_ascii=False) + '\n')
            args.max_jobs = 1
            with patch.object(cli, 'api_settings', return_value=('https://example.test/v1/chat/completions',
                                                                  'test-model', 'secret')), \
                    patch.object(cli, 'request_terms', return_value=[]):
                progress = cli.extract(args)
            self.assertEqual(progress['planned'], 2)
            self.assertEqual(progress['cached_before'], 1)
            self.assertEqual(progress['completed_now'], 1)
            review_args = SimpleNamespace(plan=plan, prompt=prompt, log=log,
                model='test-model', name_glossary=root / 'names.json',
                term_glossary=root / 'terms.json', output=root / 'review.json')
            review_args.name_glossary.write_text('{}')
            review_args.term_glossary.write_text('{}')
            self.assertEqual(cli.review(review_args)['candidates'], 1)
            with log.open('ab') as output:
                output.write(b'{"cache_key":')
            cli.repair_incomplete_tail(log)
            self.assertEqual(len(cli.load_cached(log)), 2)

    def test_extract_stops_after_64_consecutive_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root/'plan.jsonl'; prompt = root/'prompt.txt'; log = root/'log.jsonl'
            prompt.write_text('Extract')
            jobs = [make_job(('story', f'adv_card_{i}.txt'),
                    [dict(self.rows[0], id=f'row{i}',
                     location=dict(self.rows[0]['location'], script=f'adv_card_{i}.txt'))])
                    for i in range(65)]
            plan.write_text('\n'.join(__import__('json').dumps(x, ensure_ascii=False)
                for x in [{'schema_version': 1, 'kind': 'term_semantic_plan'}, *jobs])+'\n')
            args = SimpleNamespace(plan=plan, prompt=prompt, log=log,
                workers=1, max_jobs=0)
            with patch.object(cli, 'api_settings', return_value=(
                    'https://example.test/v1/chat/completions', 'test', 'secret')), \
                    patch.object(cli, 'request_terms', side_effect=RuntimeError(
                        'Terminology API connection failed')) as request:
                result = cli.extract(args)
            self.assertEqual(request.call_count, 64)
            self.assertTrue(result['stopped_early'])
            self.assertEqual(result['unattempted_now'], 1)

    def rolling_fixture(self, root, count, workers):
        import json
        plan, prompt, log = [root / name for name in ('plan.jsonl', 'prompt.txt', 'log.jsonl')]
        prompt.write_text('Extract')
        jobs = [make_job(('story', str(i)), [dict(self.rows[0], id=f'r{i}')])
                for i in range(count)]
        plan.write_text('\n'.join(json.dumps(x) for x in [
            {'schema_version': 1, 'kind': 'term_semantic_plan'}, *jobs]) + '\n')
        return SimpleNamespace(plan=plan, prompt=prompt, log=log,
                               workers=workers, max_jobs=0), jobs

    def test_rolling_submission_passes_slow_first_job_and_bounds_concurrency(self):
        import threading
        with tempfile.TemporaryDirectory() as temporary:
            args, jobs = self.rolling_fixture(Path(temporary), 70, 2)
            release = threading.Event()
            lock = threading.Lock()
            active = peak = 0
            def worker(url, key, model, prompt, job):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    if job['id'] == jobs[0]['id']:
                        if not release.wait(3):
                            raise RuntimeError('slow first request held later jobs behind a barrier')
                    if job['id'] == jobs[-1]['id']:
                        release.set()
                    return []
                finally:
                    with lock:
                        active -= 1
            with patch.object(cli, 'api_settings', return_value=('https://example.test', 'm', 's')), \
                    patch.object(cli, 'request_terms', side_effect=worker) as request:
                result = cli.extract(args)
                self.assertEqual(result['completed_now'], 70)
                self.assertEqual(peak, 2)
                self.assertEqual(request.call_count, 70)
                args.max_jobs = 10
                resumed = cli.extract(args)
                self.assertEqual(resumed['cached_before'], 70)
                self.assertEqual(resumed['completed_now'], 0)
                self.assertEqual(request.call_count, 70)

    def test_rolling_failure_circuit_drains_bounded_inflight_and_partial_is_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, jobs = self.rolling_fixture(Path(temporary), 150, 3)
            def worker(url, key, model, prompt, job):
                raise cli.PartialTermsError([], 1, {'source_absent': 1})
            with patch.object(cli, 'api_settings', return_value=('https://example.test', 'm', 's')), \
                    patch.object(cli, 'request_terms', side_effect=worker) as request:
                result = cli.extract(args)
            self.assertTrue(result['stopped_early'])
            self.assertEqual(result['completed_now'], 0)
            self.assertGreaterEqual(request.call_count, 64)
            self.assertLessEqual(request.call_count, 66)
            self.assertEqual(result['failed_now'], request.call_count)
            self.assertEqual(result['unattempted_now'], 150 - request.call_count)
            self.assertEqual(cli.load_cached(args.log), {})

    def test_rolling_success_resets_failure_streak_and_max_jobs_resume_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, jobs = self.rolling_fixture(Path(temporary), 140, 1)
            def worker(url, key, model, prompt, job):
                if job['id'] == jobs[63]['id']:
                    return []
                raise RuntimeError('Terminology API HTTP 401')
            with patch.object(cli, 'api_settings', return_value=('https://example.test', 'm', 's')), \
                    patch.object(cli, 'request_terms', side_effect=worker):
                result = cli.extract(args)
            self.assertEqual(result['completed_now'], 1)
            self.assertEqual(result['failed_now'], 127)
            self.assertEqual(result['unattempted_now'], 12)
            args.max_jobs = 10
            with patch.object(cli, 'api_settings', return_value=('https://example.test', 'm', 's')), \
                    patch.object(cli, 'request_terms', return_value=[]) as request:
                resumed = cli.extract(args)
            self.assertEqual(resumed['cached_before'], 1)
            self.assertEqual(resumed['completed_now'], 10)
            self.assertEqual(resumed['unattempted_now'], 0)
            self.assertEqual(request.call_count, 10)


if __name__ == '__main__':
    unittest.main()
