import json
from pathlib import Path
import tempfile
import unittest
from src.term_audit import Entry, audit, contains, load_policy, master_entries, story_entries
from term_audit import main


def entry(source, target, table='Music', field='name', identifier='music-1'):
    return Entry(source, target, {'kind': 'master', 'table': table, 'id': identifier, 'field': field}, {})


def term(source='どりきゅん', target='どりきゅん', **extra):
    return dict({'source': source, 'target': target, 'category': 'group', 'action': 'retain'}, **extra)


class TermAuditTests(unittest.TestCase):
    def test_credit_retention_does_not_apply_to_singers(self):
        report = audit([entry('畑亜貴', '畑亚贵', field='lyricist'),
                        entry('長瀬琴乃', '长濑琴乃', field='singer')], [])
        self.assertEqual(len(report['findings']), 1)
        self.assertEqual(report['findings'][0]['suggestion'], '畑亜貴')

    def test_ascii_substrings_do_not_match(self):
        self.assertFalse(contains('kanata', 'kana'))
        self.assertTrue(contains('kanaとmiho', 'kana'))
        self.assertEqual(audit([entry('kanata', '卡娜塔')], [term('kana', 'kana')])['findings'], [])

    def test_nested_terms_choose_longest(self):
        report = audit([entry('星見プロダクション', '星见Production')], [
            term('星見プロ', '错误译名'), term('星見プロダクション', '星见Production')])
        self.assertEqual(report['findings'], [])

    def test_context_scope(self):
        report = audit([entry('どりきゅん', 'DoriKyun'), entry('どりきゅん', 'DoriKyun', field='singer')],
                       [term(scope={'table': 'Music', 'field': 'singer'})])
        self.assertEqual(len(report['findings']), 1)
        self.assertEqual(report['findings'][0]['location']['field'], 'singer')

    def test_contextual_variants_are_preserved(self):
        report = audit([entry('どりきゅん', 'どりきゅん'),
                        entry('どりきゅん', 'DoriKyun', identifier='music-2')], [term()])
        self.assertEqual(len(report['source_variants']), 1)
        self.assertEqual(len(report['source_variants'][0]['variants']), 2)
        self.assertIn('music-2', str(report['source_variants']))

    def test_missing_translation_separate_from_preference(self):
        report = audit([entry('どりきゅん', '')], [term()])
        self.assertEqual(report['summary']['untranslated'], 1)
        self.assertEqual(report['findings'], [])

    def test_single_character_ordinary_meaning_not_flagged(self):
        report = audit([entry('愛がある', '充满爱意')], [term('愛', '小美山爱')])
        self.assertEqual(report['findings'], [])

    def test_embedded_mentions_never_propose_full_replacement(self):
        report = audit([entry('どりきゅんの新曲', 'DoriKyun的新曲')], [term()])
        self.assertIsNone(report['findings'][0]['suggestion'])

    def test_policy_rejects_invalid_retention_and_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'policy.json'
            for value in [term(target='翻译'), term(scope={'secret': '*'}), term(category='unknown')]:
                path.write_text(json.dumps({'schema_version': 1, 'terms': [value]}))
                with self.assertRaises(ValueError):
                    load_policy(path)

    def test_cli_preserves_files_and_writes_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orig, zh = root / 'orig', root / 'zh'
            orig.mkdir(); zh.mkdir()
            (orig / 'Music.json').write_text(json.dumps({'m1': {'composer': '畑亜貴'}}))
            (zh / 'Music.json').write_text(json.dumps({'m1': {'composer': '畑亚贵'}}))
            before = (zh / 'Music.json').read_bytes()
            main(['--master-orig', str(orig), '--master-zh', str(zh), '--output', str(root / 'report.json')])
            data = json.loads((root / 'report.json').read_text())
            self.assertEqual(data['findings'][0]['location']['id'], 'm1')
            self.assertEqual((zh / 'Music.json').read_bytes(), before)
            self.assertEqual(len(data['inputs']), 2)

    def test_story_location_and_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'adv_main_01.csv'
            path.write_text('id,name,text,trans\n12:text:1,琴乃,どりきゅん！,DoriKyun！\ninfo,file,hash,\n译者,,,\n')
            row = next(story_entries(Path(tmp)))
            self.assertEqual(row.context['speaker'], '琴乃')
            self.assertEqual(row.location['script'], 'adv_main_01.txt')

    def test_semantic_story_ids_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'adv_main_01.csv'
            path.write_text('id,name,text,trans\n12:choice:1,,はい,是\n'
                            '13:narration:1,,夜が明けた,天亮了\ninfo,file,hash,\n译者,,,\n')
            rows = list(story_entries(Path(tmp)))
            self.assertEqual([row.location['field'] for row in rows],
                             ['12:choice:1', '13:narration:1'])

    def test_duplicate_story_fields_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'adv_main_01.csv'
            path.write_text('id,name,text,trans\n12:text:1,,原文,译文\n12:text:1,,原文,译文\n')
            with self.assertRaises(ValueError):
                list(story_entries(Path(tmp)))


if __name__ == '__main__':
    unittest.main()
