import json
from pathlib import Path
import tempfile
import unittest

from src.notice_credit_policy import POLICY, apply_policy, retained_credit
from src.notice_workflow import collect


class CreditPolicyTests(unittest.TestCase):
    def test_explicit_prefix_preserves_every_name_character(self):
        self.assertEqual(retained_credit("作詞・作曲・編曲： ハヤシベトモノリ（Plus-Tech Squeeze Box）"),
                         "作词・作曲・编曲： ハヤシベトモノリ（Plus-Tech Squeeze Box）")
        self.assertEqual(retained_credit("作曲：利根川貴之 、坂和也、Dr.Usui"), "作曲：利根川貴之 、坂和也、Dr.Usui")
        for source in ("深夜テンションで作曲", "本文 作詞：人名", "作詞：", "作詞： \t", "作詞：名前\n本文"):
            self.assertIsNone(retained_credit(source))

    def test_existing_values_protected_new_values_and_log_agree_on_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            page = "/notice/" + "a" * 64 + "/index.html"
            sources = ["作詞：けんたあろは", "編曲：前澤寛之", "作曲：岸田勇気"]
            initial = {"pages": {page: {sources[0]: "原有人工稿"}}}
            target = {"pages": {page: {sources[0]: "原有人工稿", sources[1]: "编曲：前泽宽之"}}}
            for name, value in [("source.json", {"pages": {page: {"title": "公告", "texts": sources}}}),
                                ("initial.json", initial), ("zh.json", target), ("ui.json", {"text": {}})]:
                (root / name).write_text(json.dumps(value, ensure_ascii=False))
            (root / "log.jsonl").write_text(json.dumps({"page": page, "source": sources[1], "translation": "编曲：前泽宽之"}) + "\n")
            args = [root / "source.json", root / "zh.json", root / "initial.json", root / "log.jsonl", root / "report.json"]
            report = apply_policy(*args)
            self.assertEqual(report["protected_initial_nodes"], 1)
            self.assertEqual(report["changed_new_nodes"], 1)
            self.assertEqual(report["filled_new_nodes"], 1)
            pending, memory, _ = collect(root / "source.json", root / "zh.json", root / "ui.json", root / "log.jsonl")
            self.assertFalse(pending)
            self.assertEqual(memory[(page, sources[1])], "编曲：前澤寛之")
            self.assertEqual(json.loads((root / "zh.json").read_text())["pages"][page][sources[0]], "原有人工稿")
            self.assertEqual(apply_policy(*args)["log_records_added"], 0)

    def test_explicit_old_credit_policy_restores_only_names_and_preserves_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            page = "/notice/" + "a" * 64 + "/index.html"
            source = "作詞：利根川貴之"
            initial = {"pages": {page: {source: "作词：利根川贵之", "お知らせ": "已校对公告"}}}
            for name, data in [("source.json", {"pages": {page: {"title": "公告", "texts": [source, "お知らせ"]}}}),
                               ("initial.json", initial), ("zh.json", initial)]:
                (root / name).write_text(json.dumps(data, ensure_ascii=False))
            args = [root / "source.json", root / "zh.json", root / "initial.json", root / "log.jsonl", root / "report.json"]
            report = apply_policy(*args, restore_initial_names=True)
            self.assertEqual(report["restored_initial_nodes"], 1)
            values = json.loads((root / "zh.json").read_text())["pages"][page]
            self.assertEqual(values[source], "作词：利根川貴之")
            self.assertEqual(values["お知らせ"], "已校对公告")
            self.assertEqual(report["provenance"][0]["initial_translation"], "作词：利根川贵之")
            self.assertEqual(apply_policy(*args, restore_initial_names=True)["log_records_added"], 0)
