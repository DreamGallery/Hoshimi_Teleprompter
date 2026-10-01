import unittest
from src.notice_protected_service import ProtectedNoticeService, protect


class FakeService:
    def __init__(self, response):
        self.response = response
    def translate_batch(self, rows, glossary):
        return {row["id"]: self.response(row["source"]) for row in rows}


def translate(source, transform):
    return ProtectedNoticeService(FakeService(transform)).translate_batch(
        [{"id": "0", "source": source, "speaker": "Notice:/page"}], {})["0"]


class NoticeProtectionTests(unittest.TestCase):
    def test_one_time_cannot_become_kanji_number(self):
        self.assertEqual(translate("1回限定", lambda s: s.replace("回限定", "次限定")), "1次限定")
        with self.assertRaises(ValueError):
            translate("1回限定", lambda s: "仅限一次")

    def test_added_number_is_rejected_after_restoration(self):
        with self.assertRaisesRegex(ValueError, "numbers"):
            translate("ガチャを引くごとに", lambda s: "每抽卡1次")
        with self.assertRaisesRegex(ValueError, "numbers"):
            translate("1回限定", lambda s: s.replace("回限定", "次，获得2张"))

    def test_fullwidth_dates_tags_and_mixed_line_endings(self):
        source = "<b>１２回</b> 2026/09/26 12:00\r\n次へ\n{0}\\n"
        target = translate(source, lambda s: s.replace("回", "次").replace("次へ", "下一步"))
        self.assertEqual(target, "<b>１２次</b> 2026/09/26 12:00\r\n下一步\n{0}\\n")

    def test_placeholder_conflict_and_reordered_markers(self):
        with self.assertRaisesRegex(ValueError, "conflicts"):
            protect("原文⟪IP_KEEP_0000⟫")
        with self.assertRaisesRegex(ValueError, "reordered"):
            translate("1回で2個", lambda s: s.replace("0000", "TEMP").replace("0001", "0000").replace("TEMP", "0001"))

    def test_url_and_context_are_preserved_without_cross_page_memory(self):
        source = "詳細 https://example.com/a?b=12をご確認ください。"
        self.assertEqual(translate(source, lambda s: s.replace("詳細", "详情").replace("をご確認ください。", "请确认。")),
                         "详情 https://example.com/a?b=12请确认。")


if __name__ == "__main__":
    unittest.main()
