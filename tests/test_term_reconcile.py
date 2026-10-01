from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from src.term_audit import Entry
from src.term_reconcile import reconcile
from term_review import main


def report(source='星見プロ', target='星见事务所', category='agency'):
    return {'schema_version': 1, 'scope': 'semantic_review_only_no_dictionary_changes',
            'candidates': [{'source': source, 'category': category,
                            'suggested_translations': {target: 1},
                            'evidence': [{'row_id': 'row1', 'location': {'kind': 'master',
                                          'table': 'Item', 'id': 'item1', 'field': 'name'}}]}]}


def entry(source, target, field='name', record='1'):
    return Entry(source, target, {'kind': 'master', 'table': 'Item', 'id': record,
                                 'field': field}, {})


class ReconcileTests(unittest.TestCase):
    def test_exact_reference_excludes_fragments_and_descriptions(self):
        result = reconcile([report()], [entry('星見プロからの贈り物', '星见事务所的礼物'),
            entry('星見プロ', '星见事务所', field='description')], [])
        self.assertEqual(result['candidates'][0]['status'], 'needs_review')
        self.assertEqual(result['candidates'][0]['master_references'], [])

    def test_conflicting_scoped_names_do_not_choose_first_or_majority(self):
        result = reconcile([report()], [entry('星見プロ', '星见事务所'),
            entry('星見プロ', '星见Production', record='2'),
            entry('星見プロ', '星见Production', record='3')], [])
        row = result['candidates'][0]
        self.assertEqual(row['status'], 'reference_conflict')
        self.assertEqual(len(row['master_references']), 3)
        self.assertNotIn('selected_translation', row)

    def test_glossary_disagreement_and_retained_values_are_preserved(self):
        result = reconcile([report()], [entry('星見プロ', '星見プロ')],
                           [{'source': '星見プロ', 'target': '星见Production', 'scope': {}}])
        self.assertEqual(result['candidates'][0]['status'], 'reference_conflict')
        self.assertTrue(result['candidates'][0]['model_reference_disagreement'])

    def test_repairs_deduplicate_evidence_not_vote_for_translation(self):
        result = reconcile([report(), report(), report(category='group')], [], [])
        row = result['candidates'][0]
        self.assertEqual(row['categories'], ['agency', 'group'])
        self.assertEqual(len(row['evidence']), 1)
        self.assertEqual(row['model_suggestions'], ['星见事务所'])

    def test_multiline_names_require_exact_linebreaks(self):
        result = reconcile([report('周年\n記念')], [entry('周年 記念', '周年纪念'),
                           entry('周年\n記念', '周年\n纪念', record='2')], [])
        self.assertEqual(result['candidates'][0]['master_targets'], ['周年\n纪念'])

    def test_same_row_with_different_context_is_not_silently_overwritten(self):
        old, new = report(), report()
        old['candidates'][0]['evidence'][0]['context'] = {'source_excerpt': 'old snapshot'}
        new['candidates'][0]['evidence'][0]['context'] = {'source_excerpt': 'new snapshot'}
        row = reconcile([old, new], [], [])['candidates'][0]
        self.assertEqual(len(row['evidence']), 2)

    def test_missing_translation_is_explicit(self):
        result = reconcile([report()], [entry('星見プロ', '')], [])
        self.assertEqual(result['candidates'][0]['untranslated_master_references'], 1)
        self.assertEqual(result['candidates'][0]['status'], 'needs_review')

    def test_empty_model_suggestion_means_abstention(self):
        row = reconcile([report(target='')], [], [])['candidates'][0]
        self.assertEqual(row['model_suggestions'], [])
        self.assertTrue(row['model_abstained'])

    def test_raw_model_output_and_scoped_policy_are_rejected(self):
        with self.assertRaises(ValueError):
            reconcile([{'terms': []}], [], [])
        with self.assertRaises(ValueError):
            reconcile([report()], [], [{'source': '星見プロ', 'target': '星见',
                                       'scope': {'table': 'Item'}}])

    def test_cli_preserves_inputs_and_records_their_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orig, zh = root / 'orig', root / 'zh'
            orig.mkdir()
            zh.mkdir()
            review, output = root / 'review.json', root / 'output.json'
            review.write_text(json.dumps(report()), encoding='utf-8')
            (orig / 'Item.json').write_text(json.dumps({'1': {'name': '星見プロ'}}))
            (zh / 'Item.json').write_text(json.dumps({'1': {'name': '星见事务所'}}))
            inputs = [review, orig / 'Item.json', zh / 'Item.json']
            before = {p: p.read_bytes() for p in inputs}
            args = ['--review', str(review), '--master-orig', str(orig),
                    '--master-zh', str(zh), '--output', str(output)]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            result = json.loads(output.read_text())
            self.assertEqual(len(result['inputs']), 3)
            self.assertEqual(result['candidates'][0]['status'], 'master_reference')
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args[:-1] + [str(zh / 'Item.json')])
            self.assertEqual(before, {p: p.read_bytes() for p in inputs})


if __name__ == '__main__':
    unittest.main()
