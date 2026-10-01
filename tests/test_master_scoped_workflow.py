import json
from pathlib import Path
import tempfile
import unittest

from src.master_scoped_workflow import collect, run
from src.terminology import ScopedService


class RecordingService:
    def __init__(self):
        self.calls = []

    def translate_batch(self, lines, glossary):
        self.calls.extend(lines)
        return {line['id']: (line.get('term_preferences') or [{'target': '默认译文'}])[0]['target']
                for line in lines}


class ScopedMasterTests(unittest.TestCase):
    def test_per_field_policy_resume_and_preserve_reviewed_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orig, zh = root / 'orig', root / 'zh'
            orig.mkdir()
            zh.mkdir()
            source = {'1': {'name': '曲', 'description': '曲'},
                      '2': {'name': '曲', 'description': '曲'}}
            (orig / 'Music.json').write_text(json.dumps(source), encoding='utf-8')
            (zh / 'Music.json').write_text(json.dumps({'1': {'description': '人工译文'}, '2': {}}),
                                           encoding='utf-8')
            terms = [{'source': '曲', 'target': '歌曲', 'action': 'prefer',
                      'scope': {'table': 'Music', 'field': 'name'}},
                     {'source': '曲', 'target': '乐曲', 'action': 'prefer',
                      'scope': {'table': 'Music', 'id': '2', 'field': 'description'}}]
            inner = RecordingService()
            service = ScopedService(inner, terms, 'master')
            log = root / 'log.jsonl'
            self.assertEqual(run(orig, zh, log, service, {}, terms, workers=1)[:3],
                             (3, 3, 0))
            actual = json.loads((zh / 'Music.json').read_text())
            self.assertEqual(actual, {'1': {'name': '歌曲', 'description': '人工译文'},
                                      '2': {'name': '歌曲', 'description': '乐曲'}})
            self.assertEqual(len(inner.calls), 3)
            self.assertEqual({(e['table'], e['id'], e['field'], e['source'])
                              for e in map(json.loads, log.read_text().splitlines())},
                             {('Music', '1', 'name', '曲'), ('Music', '2', 'name', '曲'),
                              ('Music', '2', 'description', '曲')})
            (zh / 'Music.json').write_text(json.dumps({'1': {'description': '人工译文'}, '2': {}}),
                                           encoding='utf-8')
            again = RecordingService()
            self.assertEqual(run(orig, zh, log, ScopedService(again, terms, 'master'),
                                 {}, terms, workers=1)[:3], (0, 3, 0))
            self.assertFalse(again.calls)
            source['2']['description'] = '曲改'
            (orig / 'Music.json').write_text(json.dumps(source), encoding='utf-8')
            (zh / 'Music.json').write_text(json.dumps({'1': {'description': '人工译文'}, '2': {}}),
                                           encoding='utf-8')
            pending, _, _ = collect(orig, zh, log, terms)
            self.assertIn(('Music', '2', 'description', '曲改'), pending)

    def test_source_only_memory_does_not_cross_scopes_and_changed_source_is_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orig, zh = root / 'orig', root / 'zh'
            orig.mkdir()
            zh.mkdir()
            (orig / 'Music.json').write_text(json.dumps({'1': {'name': '曲',
                                                                'description': '曲',
                                                                'composer': '作曲家',
                                                                'subtitle': '旧文更新'}}),
                                             encoding='utf-8')
            (zh / 'Music.json').write_text('{}', encoding='utf-8')
            log = root / 'results.jsonl'
            log.write_text('\n'.join(json.dumps({'source': source, 'translation': target},
                                                 ensure_ascii=False) for source, target in
                                     [('曲', '旧译'), ('旧文', '旧译文'), ('作曲家', '错误译文')]) + '\n',
                           encoding='utf-8')
            terms = [{'source': '曲', 'target': '歌曲', 'action': 'prefer',
                      'scope': {'table': 'Music', 'field': 'name'}}]
            pending, memory, total = collect(orig, zh, log, terms)
            self.assertEqual(total, 4)
            self.assertEqual({key[2] for key in pending}, {'name', 'description', 'subtitle'})
            self.assertEqual(memory[('Music', '1', 'composer', '作曲家')], '作曲家')
            inner = RecordingService()
            run(orig, zh, log, ScopedService(inner, terms, 'master'), {}, terms, workers=1)
            actual = json.loads((zh / 'Music.json').read_text())['1']
            self.assertEqual(actual['name'], '歌曲')
            self.assertEqual(actual['composer'], '作曲家')
            self.assertEqual(actual['description'], '默认译文')
            self.assertEqual(actual['subtitle'], '默认译文')
            self.assertEqual(len(inner.calls), 3)

    def test_old_log_reused_when_no_scope_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orig, zh = root / 'orig', root / 'zh'
            orig.mkdir()
            zh.mkdir()
            (orig / 'Item.json').write_text('{"1":{"name":"春"}}')
            (zh / 'Item.json').write_text('{}')
            log = root / 'results.jsonl'
            log.write_text(json.dumps({'source': '春', 'translation': '春天'},
                                      ensure_ascii=False) + '\n')
            terms = [{'source': '春', 'target': '春季', 'action': 'prefer',
                      'scope': {'table': 'Music'}}]
            pending, memory, _ = collect(orig, zh, log, terms)
            self.assertFalse(pending)
            self.assertEqual(memory[('Item', '1', 'name', '春')], '春天')


if __name__ == '__main__':
    unittest.main()
