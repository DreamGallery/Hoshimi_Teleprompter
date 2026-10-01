"""Structured term discovery keeps evidence and excludes generic text fields."""

import csv
import json
from pathlib import Path
import tempfile
import unittest

from src.term_candidates import discover


class TermCandidateTests(unittest.TestCase):
    def test_known_and_unknown_candidates_keep_master_and_story_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orig = root / 'orig'
            csv_dir = root / 'csv'
            orig.mkdir()
            csv_dir.mkdir()
            for name, data in {
                'Music': {'music-1': {'name': '新しい歌', 'composer': '作曲家A',
                                      'singer': '星見プロダクション'}},
                'EventStory': {'event-1': {'name': 'イベントストーリー',
                                            'description': '花の約束'}},
                'Costume': {'costume-1': {'name': '星空ドレス'}},
                'Story': {'story-1': {'name': '新たな旅', 'description': '普通の説明文'}},
                'Card': {'card-1': {'name': '夢への一歩', 'description': '単なる説明'}},
            }.items():
                (orig / f'{name}.json').write_text(json.dumps(data, ensure_ascii=False))
            with (csv_dir / 'adv_main_01.csv').open('w', newline='', encoding='utf-8') as output:
                writer = csv.writer(output)
                writer.writerow(['id', 'name', 'text', 'trans'])
                writer.writerow(['1:text:1', '新しい歌姫', 'ステージに行こう', ''])
                writer.writerow(['2:text:1', '{username}', 'よろしく', ''])
                writer.writerow(['3:text:1', '新しい歌姫', '準備完了', ''])
            name_glossary = root / 'names.json'
            term_glossary = root / 'terms.json'
            name_glossary.write_text(json.dumps({'新しい歌姫': '新歌姬'}))
            term_glossary.write_text('{}')
            report = discover(orig, csv_dir, name_glossary, term_glossary)
            candidates = {(c['category'], c['source']): c for c in report['candidates']}
            self.assertIn(('song', '新しい歌'), candidates)
            self.assertIn(('event', '花の約束'), candidates)
            self.assertIn(('agency', '星見プロダクション'), candidates)
            self.assertIn(('costume', '星空ドレス'), candidates)
            self.assertIn(('story_title', '新たな旅'), candidates)
            self.assertIn(('card_title', '夢への一歩'), candidates)
            self.assertNotIn(('song', '作曲家A'), candidates)
            self.assertFalse(any(c['source'] in {'普通の説明文', '単なる説明',
                                                 '{username}', 'イベントストーリー'}
                                 for c in report['candidates']))
            speaker = candidates[('speaker_or_role', '新しい歌姫')]
            self.assertTrue(speaker['in_glossary'])
            self.assertEqual(speaker['occurrences'], 2)
            self.assertEqual(speaker['scopes'][0]['count'], 2)
            self.assertEqual([e['location']['line'] for e in speaker['scopes'][0]['examples']],
                             [1, 3])
            self.assertEqual(candidates[('song', '新しい歌')]['scopes'][0]['examples'][0]
                             ['location']['id'], 'music-1')


if __name__ == '__main__':
    unittest.main()
