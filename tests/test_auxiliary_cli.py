"""Exercise the migrated CLI entry points using a local in-memory model stub."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import suggest_master_kana
import translate_ui


class AuxiliaryCliTests(unittest.TestCase):
    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_ui_cli_fills_missing_keys_and_preserves_reviewed_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target, variants = (root / name for name in ("source.json", "zh.json", "variants.json"))
            self.put(source, {"i18n": {"new": "新しい", "reviewed": "原文", "dynamic": "一"}})
            self.put(target, {"i18n": {"reviewed": "人工译文"}})
            self.put(variants, {"dynamic": ["一", "二"]})
            before = source.read_bytes()
            argv = ["translate_ui.py", "--source", str(source), "--translations", str(target),
                    "--variants-source", str(variants), "--env-file", str(root / "no.env")]
            with patch.dict(os.environ, {"OPENAI_API_BASE": "https://example.invalid/v1",
                                         "OPENAI_MODEL": "local-stub", "OPENAI_API_KEY": "test"}), \
                    patch("sys.argv", argv), patch.object(translate_ui, "TranslationService") as service:
                service.return_value.translate_batch.return_value = {"new": "新增"}
                translate_ui.main()
                self.assertEqual(service.return_value.translate_batch.call_args.args[0],
                                 [{"id": "new", "speaker": "new", "source": "新しい"}])
                self.assertEqual(json.loads(target.read_text())["i18n"],
                                 {"new": "新增", "reviewed": "人工译文"})
                service.reset_mock()
                translate_ui.main()
                service.assert_not_called()
            self.assertEqual(source.read_bytes(), before)

    def test_kana_cli_saves_suggestions_and_resumes_without_overwriting_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "orig/Story.json", root / "zh/Story.json"
            report, output, log = root / "review.json", root / "suggestions.json", root / "results.jsonl"
            self.put(source, {"1": {"name": "物語です"}})
            self.put(target, {"1": {"name": "故事です"}})
            self.put(report, {"kana_issues": [{"table": "Story", "id": "1", "path": "name"}]})
            before = target.read_bytes()
            argv = ["suggest_master_kana.py", "--report", str(report),
                    "--orig-dir", str(source.parent), "--zh-dir", str(target.parent),
                    "--output", str(output), "--log", str(log), "--env-file", str(root / "no.env")]
            with patch.dict(os.environ, {"OPENAI_API_BASE": "https://example.invalid/v1",
                                         "OPENAI_MODEL": "local-stub", "OPENAI_API_KEY": "test"}), \
                    patch("sys.argv", argv), patch.object(suggest_master_kana, "TranslationService") as service:
                service.return_value.translate_batch.side_effect = lambda lines, glossary: {
                    item["id"]: "这是故事" for item in lines}
                suggest_master_kana.main()
                self.assertEqual(json.loads(output.read_text())["items"][0]["suggestion"], "这是故事")
                service.reset_mock()
                suggest_master_kana.main()
                service.assert_not_called()
            self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
