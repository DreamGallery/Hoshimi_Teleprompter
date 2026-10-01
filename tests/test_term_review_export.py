import unittest

from term_review_export import build_queue, decision_template, portable_paths


class ReviewExportTests(unittest.TestCase):
    def test_flags_overlap_and_unverified_rows_remain_separate(self):
        references = {'scope': 'term_reference_review_only_no_dictionary_changes',
                      'candidates': [{'source': '候補', 'status': 'needs_review',
                                      'model_reference_disagreement': True}]}
        coverage = {'unverified_rows': [{'row': {'id': 'row1', 'source': '原文'}}]}
        queue = build_queue(references, coverage)
        self.assertEqual(queue['summary']['items'], 2)
        self.assertEqual(queue['items'][0]['review_flags'],
                         ['missing_reference', 'reference_disagreement'])
        self.assertEqual(queue['items'][1]['kind'], 'unverified_source_row')
        self.assertEqual(decision_template(queue)['items'][0]['decision'], 'pending')

    def test_changed_inputs_preserve_old_decision_as_stale(self):
        queue = {'items': [{'review_id': 'a', 'input_fingerprint': 'v1'}]}
        old = decision_template(queue)
        old['items'][0]['decision'] = 'skip'
        self.assertEqual(decision_template(queue, old)['items'][0]['decision'], 'skip')
        changed = decision_template({'items': [{'review_id': 'a', 'input_fingerprint': 'v2'}]}, old)
        self.assertEqual(changed['items'][0]['decision'], 'pending')
        self.assertEqual(changed['stale_decisions'][0]['decision'], 'skip')

    def test_raw_model_output_cannot_be_exported_as_reference_review(self):
        with self.assertRaises(ValueError):
            build_queue({'terms': []}, {'unverified_rows': []})

    def test_recursive_portable_paths_keep_provenance_and_hashes(self):
        value = {'inputs': [{'path': '/Users/someone/project/Idoly-localify/.analysis/log.jsonl',
                             'sha256': '123'}],
                 'nested': {'path': '/home/someone/private/coverage.json'}}
        portable = portable_paths(value)
        self.assertEqual(portable['inputs'][0],
                         {'path': 'Idoly-localify/.analysis/log.jsonl', 'sha256': '123'})
        self.assertEqual(portable['nested']['path'], 'coverage.json')
        self.assertIn('/Users/', value['inputs'][0]['path'])  # Original report stays intact.

    def test_classification_change_invalidates_human_decision(self):
        refs = {'scope': 'term_reference_review_only_no_dictionary_changes', 'candidates': []}
        coverage = {'unverified_rows': [{'row': {'id': 'r', 'source': '原文'}}]}
        before = build_queue(refs, coverage, {'r': {'classification': 'evidence_insufficient'}})
        after = build_queue(refs, coverage, {'r': {'classification': 'no_entity'}})
        self.assertEqual(before['items'][0]['review_id'], after['items'][0]['review_id'])
        self.assertNotEqual(before['items'][0]['input_fingerprint'],
                            after['items'][0]['input_fingerprint'])
        old = decision_template(before)
        untouched = decision_template(after, old)
        self.assertEqual(untouched['items'][0]['decision'], 'pending')
        self.assertEqual(untouched['stale_decisions'], [])
        old['items'][0]['decision'] = 'no_entity'
        updated = decision_template(after, old)
        self.assertEqual(updated['items'][0]['decision'], 'pending')
        self.assertEqual(updated['stale_decisions'][0]['decision'], 'no_entity')

    def test_machine_path_change_does_not_change_portable_fingerprint(self):
        refs = {'scope': 'term_reference_review_only_no_dictionary_changes', 'candidates': []}
        coverage = {'unverified_rows': [{'row': {'id': 'r'}, 'log':
                    '/Users/first/repo/Idoly-localify/.analysis/log.jsonl'}]}
        before = build_queue(refs, coverage)
        coverage['unverified_rows'][0]['log'] = '/Users/second/repo/Idoly-localify/.analysis/log.jsonl'
        self.assertEqual(before, build_queue(refs, coverage))
