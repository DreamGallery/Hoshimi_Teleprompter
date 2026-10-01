"""Keep the runtime Text review contract when used outside Idoly-localify."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.review_inputs import read_table, selected_entries, validate


class ReviewInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.candidates = root / "candidates.json"
        self.review = root / "review.json"
        items = [{"source": value, "occurrences": 1,
                  "category": "still_unmatched", "bucket": "kana_without_digit"}
                 for value in ("読み込み中", "プレイヤー名", "保留原文")]
        self.candidates.write_text(json.dumps({
            "audit_schema_version": 2,
            "scope": "local_review_only_no_automatic_translation",
            "texts": items}, ensure_ascii=False), encoding="utf-8")
        self.review_data = {
            "scope": "runtime_text_human_review",
            "candidate_sha256": hashlib.sha256(self.candidates.read_bytes()).hexdigest(),
            "entries": [{"source": item["source"], "occurrences": 1,
                         "decision": decision, "translation": ""}
                        for item, decision in zip(items, ("translate", "skip", "retain"))]}
        self.save_review()

    def save_review(self):
        self.review.write_text(json.dumps(self.review_data, ensure_ascii=False), encoding="utf-8")

    def test_only_explicit_review_decisions_are_selected(self):
        result = selected_entries(self.candidates, self.review)
        self.assertEqual([row["source"] for row in result], ["読み込み中", "保留原文"])

    def test_stale_review_is_rejected(self):
        self.candidates.write_text(self.candidates.read_text() + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale"):
            selected_entries(self.candidates, self.review)

    def test_omitted_duplicate_and_altered_review_entries_are_rejected(self):
        original = self.review.read_text()
        for mutate in (
                lambda rows: rows.pop(),
                lambda rows: rows.__setitem__(1, rows[0].copy()),
                lambda rows: rows[0].update(occurrences=2),
                lambda rows: rows[0].update(source="未审阅文本")):
            with self.subTest(mutate=mutate):
                self.review_data = json.loads(original)
                mutate(self.review_data["entries"])
                self.save_review()
                with self.assertRaises(ValueError):
                    selected_entries(self.candidates, self.review)

    def test_ui_placeholders_remain_validated(self):
        with self.assertRaisesRegex(ValueError, "placeholders"):
            validate("<b>{0} 個</b>", "<b>一个</b>", "count")

    def test_invalid_master_field_values_are_rejected(self):
        path = Path(self.temp.name) / "Story.json"
        path.write_text('{"1":{"name":42}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "record ID"):
            read_table(path)


if __name__ == "__main__":
    unittest.main()
