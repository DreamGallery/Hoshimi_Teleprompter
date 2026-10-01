import json
from pathlib import Path
import tempfile
import unittest

from suggest_master_kana import collect


class SuggestMasterKanaTests(unittest.TestCase):
    def test_collects_full_master_source_and_skips_internal_table_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orig, zh = root / "orig", root / "zh"
            orig.mkdir()
            zh.mkdir()
            for folder, data in ((orig, {
                    "Message": {"1": {"text": "長い日文原文です"}},
                    "ConditionDescription": {"2": {"description": "カード所持"}}}),
                    (zh, {
                    "Message": {"1": {"text": "很长的です译文"}},
                    "ConditionDescription": {"2": {"description": "卡片所持"}}})):
                for name, rows in data.items():
                    (folder / f"{name}.json").write_text(
                        json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            report = root / "report.json"
            report.write_text(json.dumps({"kana_issues": [
                {"table": "Message", "id": "1", "path": "text",
                 "source_excerpt": "長い", "translation_excerpt": "很长"},
                {"table": "ConditionDescription", "id": "2", "path": "description",
                 "source_excerpt": "カード", "translation_excerpt": "卡片"},
            ]}, ensure_ascii=False), encoding="utf-8")
            visible = collect(report, orig, zh, False)
            self.assertEqual(list(visible), ["長い日文原文です"])
            self.assertEqual(visible["長い日文原文です"]["current"], "很长的です译文")
            self.assertEqual(len(collect(report, orig, zh, True)), 2)


if __name__ == "__main__":
    unittest.main()
