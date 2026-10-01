import json
from pathlib import Path
import tempfile
import unittest

from src.notice_workflow import (NumberCheckedService, checksum, collect, needs_translation,
                                 run, validate_notice)
from src.master_workflow import translate_batch


PAGE_A = "/notice/" + "a" * 64 + "/index.html"
PAGE_B = "/notice/" + "b" * 64 + "/index.html"


class PageService:
    def translate_batch(self, lines, glossary):
        return {line["id"]: ("公告甲" if PAGE_A in line["speaker"] else "公告乙")
                for line in lines}


class NoticeWorkflowTests(unittest.TestCase):
    def files(self, root: Path):
        source = root / "source.json"
        translation = root / "translation.json"
        ui = root / "ui.json"
        log = root / "working/results.jsonl"
        review = root / "review.json"
        source.write_text(json.dumps({"pages": {
            PAGE_A: {"title": "甲", "texts": [
                "お知らせ", "開催！", "2026/9/20 (日) 12:00 ~ 9/26 (土) 11:59"]},
            PAGE_B: {"title": "乙", "texts": ["お知らせ"]},
        }}, ensure_ascii=False), encoding="utf-8")
        translation.write_text('{"pages":{}}', encoding="utf-8")
        ui.write_text(json.dumps({"text": {"開催！": "开启！"}}, ensure_ascii=False),
                      encoding="utf-8")
        review.write_text(json.dumps({"source_sha256": checksum(source), "pages": {
            PAGE_A: ["お知らせ"], PAGE_B: ["お知らせ"]}}, ensure_ascii=False),
                          encoding="utf-8")
        return source, translation, ui, log, review

    def test_page_scoped_resume_preserves_reviewed_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            source, translation, ui, log, review = self.files(Path(directory))
            pending, _, total = collect(source, translation, ui, log)
            self.assertEqual((len(pending), total), (2, 4))
            self.assertEqual(run(source, translation, ui, log, review, PageService(), {},
                                 workers=2, max_batches=1)[:3], (1, 1, 0))
            values = json.loads(translation.read_text(encoding="utf-8"))["pages"]
            values[PAGE_A]["お知らせ"] = "人工校对"
            translation.write_text(json.dumps({"pages": values}, ensure_ascii=False),
                                   encoding="utf-8")
            self.assertEqual(run(source, translation, ui, log, review, PageService(), {},
                                 workers=2)[:3], (1, 1, 0))
            values = json.loads(translation.read_text(encoding="utf-8"))["pages"]
            self.assertEqual(values[PAGE_A]["お知らせ"], "人工校对")
            self.assertEqual(values[PAGE_B]["お知らせ"], "公告乙")
            self.assertEqual(len(collect(source, translation, ui, log)[0]), 0)

    def test_date_only_and_latin_nodes_are_not_sent(self):
        self.assertFalse(needs_translation("2026/9/20 (日) 12:00 ~ 9/26 (土) 11:59"))
        self.assertFalse(needs_translation("TRINITYAiLE"))
        self.assertTrue(needs_translation("2026/9/20(日)から開催！"))
        self.assertTrue(needs_translation("【開催期間】"))

    def test_changed_amount_is_rejected_without_losing_valid_sibling(self):
        with self.assertRaisesRegex(ValueError, "amounts changed"):
            validate_notice("★5確定券30枚", "★5保底券20张")

        class OneBadService:
            def translate_batch(self, lines, glossary):
                return {line["id"]: ("★5保底券20张" if "30" in line["source"]
                                     else "公告") for line in lines}

        result = translate_batch(NumberCheckedService(OneBadService()), {}, [
            ("お知らせ", "Notice", PAGE_A),
            ("★5確定券30枚", "Notice", PAGE_A)])
        self.assertEqual(result, {"お知らせ": "公告"})

    def test_source_change_during_request_does_not_publish_stale_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            source, translation, ui, log, review = self.files(Path(directory))

            class MutatingService:
                def translate_batch(self, lines, glossary):
                    data = json.loads(source.read_text(encoding="utf-8"))
                    data["pages"][PAGE_A]["title"] = "changed"
                    source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    return {line["id"]: "公告" for line in lines}

            with self.assertRaisesRegex(ValueError, "source changed"):
                run(source, translation, ui, log, review, MutatingService(), {},
                    workers=1, max_batches=1)
            self.assertEqual(json.loads(translation.read_text(encoding="utf-8")),
                             {"pages": {}})

    def test_stale_review_list_rejects_api_work(self):
        with tempfile.TemporaryDirectory() as directory:
            source, translation, ui, log, review = self.files(Path(directory))
            data = json.loads(source.read_text(encoding="utf-8"))
            data["pages"][PAGE_A]["texts"].append("追加告知")
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            class NoCallService:
                def translate_batch(self, lines, glossary):
                    raise AssertionError("API must not be called")

            with self.assertRaisesRegex(ValueError, "review list is stale"):
                run(source, translation, ui, log, review, NoCallService(), {}, workers=1)

    def test_review_list_cannot_omit_a_pending_page(self):
        with tempfile.TemporaryDirectory() as directory:
            source, translation, ui, log, review = self.files(Path(directory))
            data = json.loads(review.read_text(encoding="utf-8"))
            del data["pages"][PAGE_B]
            review.write_text(json.dumps(data), encoding="utf-8")

            class NoCallService:
                def translate_batch(self, lines, glossary):
                    raise AssertionError("API must not be called")

            with self.assertRaisesRegex(ValueError, "excludes 1 pending"):
                run(source, translation, ui, log, review, NoCallService(), {}, workers=1)


if __name__ == "__main__":
    unittest.main()
