import json
from pathlib import Path
import tempfile
import unittest

from src.master_workflow import collect, normalize_line_endings, run, translate_batch, validate


class FakeService:
    calls = 0

    def translate_batch(self, lines, glossary):
        self.calls += 1
        return {line["id"]: {"春の始まり": "春天的开始",
                             "輝く月": "闪耀的月亮"}[line["source"]]
                for line in lines}


class MasterWorkflowTests(unittest.TestCase):
    def test_restores_source_line_separator_positions(self):
        source = "一\r\n二\n三\r"
        target = "甲\n乙\r\n丙\r"
        self.assertEqual(normalize_line_endings(source, target), "甲\r\n乙\n丙\r")

    def test_resume_and_propagate_same_source_to_multiple_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orig, zh = root / "orig", root / "zh"
            orig.mkdir()
            zh.mkdir()
            (orig / "Story.json").write_text(json.dumps({
                "1": {"name": "春の始まり", "description": "輝く月"},
                "2": {"name": "春の始まり"}}), encoding="utf-8")
            (orig / "EventStory.json").write_text(json.dumps({
                "e": {"name": "春の始まり"}}), encoding="utf-8")
            (zh / "Story.json").write_text("{}", encoding="utf-8")
            (zh / "EventStory.json").write_text("{}", encoding="utf-8")
            log = root / "log/results.jsonl"
            service = FakeService()
            self.assertEqual(run(orig, zh, log, service, {}, workers=2,
                                 batch_size=1, max_batches=1)[:3], (1, 3, 0))
            self.assertEqual(run(orig, zh, log, service, {}, workers=2,
                                 batch_size=1)[:3], (1, 1, 0))
            self.assertEqual(len(collect(orig, zh, log)[0]), 0)
            self.assertEqual(json.loads((zh / "EventStory.json").read_text()),
                             {"e": {"name": "春天的开始"}})

    def test_rejects_placeholder_and_line_break_changes(self):
        with self.assertRaises(ValueError):
            validate("{0}を獲得", "已获得道具")
        with self.assertRaises(ValueError):
            validate("月\n星", "月亮和星星")
        with self.assertRaises(ValueError):
            validate("月\r\n星", "月\n星")
        validate("<バッファータイプのみ>{0}延長", "<仅缓冲型>{0}延长")
        with self.assertRaises(ValueError):
            validate("<バッファータイプのみ>{0}延長", "仅缓冲型{0}延长")
        with self.assertRaises(ValueError):
            validate("<color=red>{0}</color>", "{0}")

    def test_story_title_may_reflow_but_other_fields_keep_line_breaks(self):
        source = "世界に\n生まれた\n声"
        target = "诞生之声"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orig, zh = root / "orig", root / "zh"
            orig.mkdir()
            zh.mkdir()
            (orig / "Story.json").write_text(json.dumps({
                "1": {"name": source, "description": source}
            }, ensure_ascii=False), encoding="utf-8")
            (zh / "Story.json").write_text(json.dumps({
                "1": {"name": target}
            }, ensure_ascii=False), encoding="utf-8")
            pending, existing, total = collect(orig, zh, root / "results.jsonl")
            self.assertEqual(total, 1)
            self.assertEqual(existing[source], target)
            self.assertEqual(pending, [(source, "Story", "description")])
            from src.master_workflow import apply_memory
            self.assertEqual(apply_memory(orig, zh, existing), 0)
            translated = json.loads((zh / "Story.json").read_text())
            self.assertNotIn("description", translated["1"])

            class LineAwareService:
                def translate_batch(self, lines, glossary):
                    return {line["id"]: "诞生于\n世界的\n声音" for line in lines}

            self.assertEqual(run(orig, zh, root / "results.jsonl", LineAwareService(),
                                 {}, workers=1)[:3], (1, 1, 0))
            translated = json.loads((zh / "Story.json").read_text())
            self.assertEqual(translated["1"]["name"], target)
            self.assertEqual(translated["1"]["description"], "诞生于\n世界的\n声音")

    def test_collect_rejects_interrupted_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orig, zh = root / "orig", root / "zh"
            orig.mkdir()
            zh.mkdir()
            (root / ".master-export-in-progress.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "export is incomplete"):
                collect(orig, zh, root / "results.jsonl")

    def test_reviewed_json_takes_precedence_over_old_model_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orig, zh = root / "orig", root / "zh"
            orig.mkdir()
            zh.mkdir()
            source = "月\r\n星"
            (orig / "Tutorial.json").write_text(json.dumps({
                "0": {"texts[0]": source}}), encoding="utf-8")
            (zh / "Tutorial.json").write_text(json.dumps({
                "0": {"texts[0]": "月亮\r\n星星"}}), encoding="utf-8")
            log = root / "results.jsonl"
            log.write_text(json.dumps({"source": source,
                                       "translation": "月亮\n星星"}) + "\n",
                           encoding="utf-8")
            pending, existing, total = collect(orig, zh, log)
            self.assertEqual((pending, total), ([], 1))
            self.assertEqual(existing[source], "月亮\r\n星星")

    def test_invalid_single_item_does_not_discard_valid_sibling(self):
        class OneBadService:
            def translate_batch(self, lines, glossary):
                return {line["id"]: ("遗失占位符" if "{0}" in line["source"]
                                     else "春天的开始") for line in lines}

        result = translate_batch(OneBadService(), {}, [
            ("春の始まり", "Story", "name"),
            ("{0}を獲得", "Item", "description")])
        self.assertEqual(result, {"春の始まり": "春天的开始"})

    def test_invalid_multiline_response_retries_crlf_segments(self):
        class LineService:
            def translate_batch(self, lines, glossary):
                values = {"月\r\n星": "月亮和星星", "月": "月亮", "星": "星星"}
                return {line["id"]: values[line["source"]] for line in lines}

        result = translate_batch(LineService(), {}, [
            ("月\r\n星", "Tutorial", "texts[0]")])
        self.assertEqual(result, {"月\r\n星": "月亮\r\n星星"})

    def test_interrupted_log_tail_is_removed_before_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orig, zh = root / "orig", root / "zh"
            orig.mkdir()
            zh.mkdir()
            (orig / "Story.json").write_text(json.dumps({
                "1": {"name": "春の始まり", "description": "輝く月"}
            }), encoding="utf-8")
            (zh / "Story.json").write_text("{}", encoding="utf-8")
            log = root / "results.jsonl"
            valid = json.dumps({"source": "春の始まり", "translation": "春天的开始"},
                               ensure_ascii=False)
            log.write_bytes((valid + "\n").encode("utf-8") +
                            b'{"source":"unfinished')
            service = FakeService()
            self.assertEqual(run(orig, zh, log, service, {}, workers=1)[:3],
                             (1, 2, 0))
            self.assertEqual(service.calls, 1)
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line)["source"] for line in lines],
                             ["春の始まり", "輝く月"])

    def test_valid_unterminated_log_record_gets_separator_before_append(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orig, zh = root / "orig", root / "zh"
            orig.mkdir()
            zh.mkdir()
            (orig / "Story.json").write_text(json.dumps({
                "1": {"name": "春の始まり", "description": "輝く月"}
            }), encoding="utf-8")
            (zh / "Story.json").write_text("{}", encoding="utf-8")
            log = root / "results.jsonl"
            log.write_text(json.dumps({"source": "春の始まり",
                                       "translation": "春天的开始"},
                                      ensure_ascii=False), encoding="utf-8")
            run(orig, zh, log, FakeService(), {}, workers=1)
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line)["source"] for line in lines],
                             ["春の始まり", "輝く月"])


if __name__ == "__main__":
    unittest.main()
