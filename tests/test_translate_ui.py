import json
from pathlib import Path
import tempfile
import unittest

from translate_ui import (apply_completed_batch, pending_reviewed_text_lines,
                          pending_stable_lines, read_curated_variants,
                          source_variants, translate_lines, varying_keys)


class FakeService:
    def translate_batch(self, lines, glossary):
        if len(lines) == 1 and lines[0]["id"] == "ui":
            return {"ui": "前後"}
        return {line["id"]: "之前" if line["source"] == "前" else "之后"
                for line in lines}


class UiTranslationTest(unittest.TestCase):
    def test_explicit_same_source_entry_is_not_retranslated(self):
        self.assertEqual(pending_stable_lines(
            {"i18n": {"story.chapter": "{0}章", "new.key": "新しい"}},
            {"i18n": {"story.chapter": "{0}章"}}, set()),
            [{"id": "new.key", "speaker": "new.key", "source": "新しい"}])

    def test_varying_key_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "dump.jsonl"
            rows = [
                {"kind": "i18n", "key": "stable", "source": "同じ"},
                {"kind": "i18n", "key": "stable", "source": "同じ"},
                {"kind": "i18n", "key": "dynamic", "source": "一"},
                {"kind": "i18n", "key": "dynamic", "source": "二"},
                {"kind": "text", "key": "ignored", "source": "一"},
            ]
            dump.write_text("".join(("\x00" if index == 3 else "") +
                                    json.dumps(row, ensure_ascii=False) + "\n"
                                    for index, row in enumerate(rows)), encoding="utf-8")
            self.assertEqual(varying_keys(dump), {"dynamic"})
            self.assertEqual(source_variants(dump), {"dynamic": ["一", "二"]})

    def test_curated_variants_still_skip_key_when_current_dump_has_one_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dump = root / "dump.jsonl"
            reviewed = root / "reviewed.json"
            dump.write_text('\x00{"kind":"i18n","key":"dynamic","source":"一"}\n',
                            encoding="utf-8")
            reviewed.write_text(json.dumps({"dynamic": ["一", "二"]},
                                           ensure_ascii=False), encoding="utf-8")
            skipped = set(read_curated_variants(reviewed)) | varying_keys(dump)
            self.assertEqual(varying_keys(dump), set())
            self.assertEqual(pending_stable_lines(
                {"i18n": {"dynamic": "一", "stable": "固定"}},
                {"i18n": {}}, skipped),
                [{"id": "stable", "speaker": "stable", "source": "固定"}])

    def test_malformed_dump_fails_before_stable_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "dump.jsonl"
            dump.write_text('{broken}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dump.jsonl:1"):
                source_variants(dump)

    def test_invalid_curated_variants_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            reviewed = Path(directory) / "reviewed.json"
            reviewed.write_text('{"dynamic": ["一", {"bad": true}]}',
                                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "distinct source strings"):
                read_curated_variants(reviewed)

    def test_newline_fallback_preserves_structure(self):
        lines = [{"id": "ui", "speaker": "ui", "source": "前\n後"}]
        translated, errors = translate_lines(lines, FakeService(), {})
        self.assertEqual(errors, [])
        self.assertEqual(translated, {"ui": "之前\n之后"})

    def test_completed_batches_preserve_review_edits_made_during_api_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zh-Hans.json"
            path.write_text(json.dumps({
                "i18n": {"reviewed": "人工校对"},
                "i18n_by_source": {"variant": {"原文A": "人工变体"}},
                "text": {"按钮": "确定"}}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(apply_completed_batch(path, {
                "reviewed": "机器译文", "new": "新增译文"}), (1, 1))
            self.assertEqual(apply_completed_batch(path, {
                "a": "机器变体", "b": "新增变体"},
                {"a": ("variant", "原文A"), "b": ("variant", "原文B")}), (1, 1))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {
                "i18n": {"reviewed": "人工校对", "new": "新增译文"},
                "i18n_by_source": {"variant": {
                    "原文A": "人工变体", "原文B": "新增变体"}},
                "text": {"按钮": "确定"}})

    def test_only_reviewed_runtime_text_is_pending_and_results_merge(self):
        reviewed = [
            {"source": "読み込み中", "decision": "translate", "translation": ""},
            {"source": "保留", "decision": "retain", "translation": ""},
        ]
        source = {"text": {"読み込み中": "読み込み中", "保留": "保留"}}
        lines, ids = pending_reviewed_text_lines(reviewed, source, {"text": {}})
        self.assertEqual(lines, [{"id": "text:0", "speaker": "Runtime Text",
                                  "source": "読み込み中"}])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "zh-Hans.json"
            path.write_text('{"text": {}, "i18n": {}}', encoding="utf-8")
            self.assertEqual(apply_completed_batch(path, {"text:0": "加载中"}, text_ids=ids),
                             (1, 0))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["text"],
                             {"読み込み中": "加载中"})
        with self.assertRaisesRegex(ValueError, "apply before translating"):
            pending_reviewed_text_lines(reviewed, {"text": {}}, {"text": {}})


if __name__ == "__main__":
    unittest.main()
