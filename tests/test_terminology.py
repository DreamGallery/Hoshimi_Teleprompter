import json
from pathlib import Path
import tempfile
import unittest

from src.terminology import ScopedService, load_policy, master_contexts, resolve
from src.master_workflow import run


class RecordingService:
    def __init__(self):
        self.calls = []

    def translate_batch(self, lines, glossary):
        self.calls.append((lines, glossary))
        return {line['id']: line['source'] for line in lines}


class TerminologyTests(unittest.TestCase):
    def test_story_scope_and_flat_fallback(self):
        terms = [{'source': '星', 'target': '星', 'action': 'retain',
                  'scope': {'kind': 'story', 'script': 'adv_a.txt',
                            'field': '*:text:*', 'speaker': '琴乃'}}]
        inner = RecordingService()
        service = ScopedService(inner, terms, 'story')
        lines = [{'id': '1:text:0', 'speaker': '琴乃', 'source': '星',
                  'script': 'adv_a.txt', 'field': '1:text:0'},
                 {'id': '2:text:0', 'speaker': '芽衣', 'source': '星',
                  'script': 'adv_a.txt', 'field': '2:text:0'}]
        service.translate_batch(lines, {'星': '星星', '月': '月亮'})
        sent, glossary = inner.calls[0]
        self.assertEqual(glossary, {'月': '月亮'})
        self.assertEqual(sent[0]['term_preferences'][0]['target'], '星')
        self.assertEqual(sent[1]['term_preferences'][0]['target'], '星星')

    def test_missing_context_and_specific_override(self):
        terms = [{'source': 'Blue', 'target': '蓝', 'action': 'prefer', 'scope': {}},
                 {'source': 'Blue', 'target': 'Blue', 'action': 'retain',
                  'scope': {'table': 'Music', 'field': 'name'}}]
        self.assertEqual(resolve(terms, {'kind': 'master', 'table': 'Music',
                                         'field': 'name'}, 'Blue')[0]['target'], 'Blue')
        self.assertEqual(resolve(terms[1:], {'kind': 'story'}, 'Blue'), [])
        self.assertEqual(resolve(terms, {'kind': 'master', 'table': 'Music',
                                         'field': 'name'}, 'Bluebird'), [])

    def test_conflicts_and_master_dedup_rejected(self):
        terms = [{'source': '曲', 'target': '歌曲', 'action': 'prefer',
                  'scope': {'table': 'Music', 'field': 'name'}}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Music.json').write_text(json.dumps({'1': {'name': '曲',
                                                               'description': '曲'}}))
            with self.assertRaisesRegex(ValueError, 'different scoped terms'):
                master_contexts(root, terms)

    def test_music_credits_bypass_api(self):
        inner = RecordingService()
        service = ScopedService(inner, [], 'master', {
            '作曲家': {'table': 'Music', 'field': 'composer', 'id': '1'}})
        self.assertEqual(service.translate_batch(
            [{'id': '0', 'source': '作曲家', 'speaker': 'Music:composer'}], {}),
            {'0': '作曲家'})
        self.assertFalse(inner.calls)

    def test_master_run_saves_untranslated_credit_without_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orig, zh = root / 'orig', root / 'zh'
            orig.mkdir()
            zh.mkdir()
            (orig / 'Music.json').write_text('{"1":{"composer":"作曲家"}}')
            (zh / 'Music.json').write_text('{"1":{}}')
            inner = RecordingService()
            service = ScopedService(inner, [], 'master', master_contexts(orig, []))
            self.assertEqual(run(orig, zh, root / 'log.jsonl', service, {},
                                 workers=1)[:3], (0, 1, 0))
            self.assertFalse(inner.calls)
            self.assertEqual(json.loads((zh / 'Music.json').read_text())['1']['composer'],
                             '作曲家')

    def test_load_policy_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'policy.json'
            path.write_text(json.dumps({'schema_version': 1, 'terms': [
                {'source': '曲', 'target': '歌曲', 'category': 'song',
                 'action': 'prefer', 'scope': {'unknown': '*'}}]}))
            with self.assertRaisesRegex(ValueError, 'invalid term scope'):
                load_policy(path)


if __name__ == '__main__':
    unittest.main()
