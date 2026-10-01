import csv
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.csv_workflow import load_csv, translate_file, verify_source
from src.translation_service import TranslationService
from main import load_glossary, read_settings, translate_files


class FakeService:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def translate_batch(self, lines, glossary):
        self.calls.append([line["id"] for line in lines])
        return {line["id"]: self.values[line["id"]] for line in lines}


class CsvWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "adv_test.txt"
        self.csv = root / "adv_test.csv"
        self.source.write_text("[title title=第一話]\n"
                               r"[message text={user}、待って\nここに name=長瀬麻奈]" + "\n",
                               encoding="utf-8")
        checksum = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with self.csv.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["id", "name", "text", "trans"],
                                    lineterminator="\n")
            writer.writeheader()
            writer.writerows([
                {"id": "1:title:1", "name": "", "text": "第一話", "trans": ""},
                {"id": "2:text:1", "name": "長瀬麻奈",
                 "text": r"{user}、待って\nここに", "trans": ""},
                {"id": "info", "name": "adv_test.txt", "text": checksum, "trans": ""},
                {"id": "译者", "name": "", "text": "", "trans": ""},
            ])

    def test_resume_preserves_completed_batches_and_footer(self):
        service = FakeService({"1:title:1": "第一话",
                               "2:text:1": r"{user}，等一下\n来这里"})
        glossary = {"長瀬麻奈": "长濑麻奈"}
        self.assertEqual(translate_file(self.csv, service, glossary, 1,
                                        self.source.parent, 1), 1)
        rows = load_csv(self.csv)
        self.assertEqual(rows[0]["trans"], "第一话")
        self.assertEqual(rows[1]["trans"], "")
        self.assertEqual(translate_file(self.csv, service, glossary, 1,
                                        self.source.parent), 1)
        self.assertEqual(service.calls, [["1:title:1"], ["2:text:1"]])
        rows = load_csv(self.csv)
        self.assertEqual(rows[1]["trans"], r"{user}，等一下\n来这里")
        self.assertEqual(rows[-2]["name"], "adv_test.txt")
        verify_source(rows, self.source.parent)

    def test_semantic_choice_and_narration_ids_are_accepted(self):
        rows = load_csv(self.csv)
        rows[1]["id"] = "2:narration:1"
        from src.csv_workflow import save_csv
        save_csv(self.csv, rows)
        self.assertEqual(load_csv(self.csv)[1]["id"], "2:narration:1")
        rows[1]["id"] = "2:choice:1"
        save_csv(self.csv, rows)
        self.assertEqual(load_csv(self.csv)[1]["id"], "2:choice:1")

    def test_bad_model_output_does_not_replace_csv(self):
        service = FakeService({"1:title:1": "第一话", "2:text:1": "等一下"})
        with self.assertRaises(ValueError):
            translate_file(self.csv, service, {}, 2, self.source.parent)
        self.assertEqual(load_csv(self.csv)[0]["trans"], "第一话")
        self.assertEqual(load_csv(self.csv)[1]["trans"], "")

    def test_source_change_stops_translation(self):
        self.source.write_text("different", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "differs"):
            translate_file(self.csv, FakeService({}), {}, 2, self.source.parent)

    def test_edit_during_api_request_is_not_overwritten(self):
        csv_path = self.csv

        class EditingService:
            def translate_batch(self, lines, glossary):
                from src.csv_workflow import save_csv
                rows = load_csv(csv_path)
                rows[1]["trans"] = r"{user}，人工校对\n在这里"
                save_csv(csv_path, rows)
                return {line["id"]: "第一话" for line in lines}

        with self.assertRaisesRegex(ValueError, "CSV changed while translation"):
            translate_file(csv_path, EditingService(), {}, 1, self.source.parent)
        rows = load_csv(csv_path)
        self.assertEqual(rows[0]["trans"], "")
        self.assertEqual(rows[1]["trans"], r"{user}，人工校对\n在这里")

    def test_source_change_during_api_request_stops_csv_write(self):
        source = self.source

        class EditingService:
            def translate_batch(self, lines, glossary):
                source.write_text(source.read_text(encoding="utf-8") + "[timeline]\n",
                                  encoding="utf-8")
                return {line["id"]: "第一话" for line in lines}

        before = self.csv.read_bytes()
        with self.assertRaisesRegex(ValueError, "original TXT differs"):
            translate_file(self.csv, EditingService(), {}, 1, self.source.parent)
        self.assertEqual(self.csv.read_bytes(), before)

    def test_visible_line_break_cannot_be_removed(self):
        service = FakeService({"1:title:1": "第一话",
                               "2:text:1": "{user}，等一下来这里"})
        with self.assertRaises(ValueError):
            translate_file(self.csv, service, {}, 2, self.source.parent)
        self.assertEqual(load_csv(self.csv)[0]["trans"], "第一话")
        self.assertEqual(load_csv(self.csv)[1]["trans"], "")

    def test_model_real_line_break_and_brackets_are_safe_in_txt(self):
        service = FakeService({"1:title:1": "第一话",
                               "2:text:1": "{user}，等一下\n[这里]"})
        self.assertEqual(translate_file(self.csv, service, {}, 2,
                                        self.source.parent), 2)
        self.assertEqual(load_csv(self.csv)[1]["trans"], r"{user}，等一下\n［这里］")

    def test_invalid_multi_line_batch_is_split_and_retried(self):
        class SplitService:
            def __init__(self):
                self.calls = []

            def translate_batch(self, lines, glossary):
                self.calls.append(len(lines))
                if len(lines) > 1:
                    return {line["id"]: "缺少占位符" for line in lines}
                return {line["id"]: ("第一话" if line["id"] == "1:title:1"
                                     else r"{user}，等一下\n来这里") for line in lines}

        service = SplitService()
        self.assertEqual(translate_file(self.csv, service, {}, 2,
                                        self.source.parent), 2)
        self.assertEqual(service.calls, [2, 2, 2, 1, 1])

    def test_single_line_fallback_translates_each_visible_line_separately(self):
        class LineBreakService:
            def translate_batch(self, lines, glossary):
                return {line["id"]: {
                    "第一話": "第一话",
                    r"{user}、待って\nここに": "{user}，等一下，来这里",
                    "{user}、待って": "{user}，等一下",
                    "ここに": "来这里",
                }[line["source"]] for line in lines}

        self.assertEqual(translate_file(self.csv, LineBreakService(), {}, 2,
                                        self.source.parent), 2)
        self.assertEqual(load_csv(self.csv)[1]["trans"], r"{user}，等一下\n来这里")

    def test_name_field_rows_are_rejected(self):
        rows = load_csv(self.csv)
        rows.insert(2, {"id": "2:name:1", "name": "長瀬麻奈",
                        "text": "長瀬麻奈", "trans": "长濑麻奈"})
        from src.csv_workflow import save_csv
        save_csv(self.csv, rows)
        with self.assertRaisesRegex(ValueError, "invalid translatable field"):
            load_csv(self.csv)

    def test_file_workers_translate_distinct_csv_files_concurrently(self):
        source2 = self.source.with_name("adv_other.txt")
        source2.write_bytes(self.source.read_bytes())
        csv2 = self.csv.with_name("adv_other.csv")
        rows = load_csv(self.csv)
        rows[-2]["name"] = source2.name
        from src.csv_workflow import save_csv
        save_csv(csv2, rows)
        barrier = threading.Barrier(2, timeout=5)

        class ConcurrentService:
            def translate_batch(self, lines, glossary):
                barrier.wait()
                values = {"1:title:1": "第一话",
                          "2:text:1": r"{user}，等一下\n来这里"}
                return {line["id"]: values[line["id"]] for line in lines}

        text, failures = translate_files(
            [self.csv, csv2], ConcurrentService(), {"長瀬麻奈": "长濑麻奈"},
            2, self.source.parent, None, 2)
        self.assertEqual((text, failures), (4, []))
        self.assertEqual(load_csv(csv2)[0]["trans"], "第一话")


class ServiceTests(unittest.TestCase):
    def test_project_term_glossary_keeps_group_branding(self):
        glossary = load_glossary(Path(__file__).resolve().parents[1] /
                                 "glossaries/term-glossary.json")
        self.assertEqual(glossary["月のテンペスト"], "月光风暴")
        self.assertEqual(glossary["サニピ"], "SUNNY PEACE")

    def test_model_source_key_fallback_preserves_field_id(self):
        service = TranslationService("https://api.example/v1", "secret", "model", "prompt")
        response = {"choices": [{"finish_reason": "stop", "message": {"content":
                    json.dumps({"translations": [{"id": "2:text:1", "speaker": "长濑麻奈",
                                                 "source": "你好"}]})}}]}

        def fake_open(request, timeout):
            return io.BytesIO(json.dumps(response).encode("utf-8"))

        with patch("src.translation_service.urlopen", side_effect=fake_open):
            result = service.translate_batch([{"id": "2:text:1", "speaker": "長瀬麻奈",
                                               "source": "こんにちは"}], {})
        self.assertEqual(result, {"2:text:1": "你好"})

    def test_env_file_stays_local_and_reads_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("OPENAI_API_BASE=https://api.example/v1\n"
                            "OPENAI_API_KEY='local-secret'\n", encoding="utf-8")
            self.assertEqual(read_settings(path)["OPENAI_API_KEY"], "local-secret")

    def test_openai_compatible_response_keeps_ids_and_relevant_glossary(self):
        service = TranslationService("https://api.example/v1", "secret", "model", "prompt")
        response = {"choices": [{"message": {"content": json.dumps(
            {"translations": [{"id": "2:text:1", "translation": "你好"}]})}}]}
        requests = []

        def fake_open(request, timeout):
            requests.append(request)
            return io.BytesIO(json.dumps(response).encode("utf-8"))

        with patch("src.translation_service.urlopen", side_effect=fake_open):
            result = service.translate_batch([{"id": "2:text:1", "speaker": "長瀬麻奈",
                                               "source": "こんにちは"}],
                                             {"長瀬麻奈": "长濑麻奈", "別人": "别人"})
        self.assertEqual(result, {"2:text:1": "你好"})
        self.assertEqual(requests[0].full_url, "https://api.example/v1/chat/completions")
        sent = json.loads(requests[0].data)
        self.assertEqual(sent["thinking"], {"type": "disabled"})
        self.assertEqual(sent["reasoning_effort"], "none")
        task = json.loads(sent["messages"][1]["content"])
        self.assertEqual(task["glossary"], {"長瀬麻奈": "长濑麻奈"})


if __name__ == "__main__":
    unittest.main()
