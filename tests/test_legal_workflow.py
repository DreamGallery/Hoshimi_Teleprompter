import json
from pathlib import Path
import tempfile
import unittest

from src.legal_workflow import (check_line, checksum, compile_runtime, initialize,
                               load_source, pending, protect, translate_batch)


class LegalTests(unittest.TestCase):
    def source(self):
        return {"schema_version": 1, "rules": {str(n): {
            "rule_type": n, "category": "terms", "source": "1. 原文\r\n\n" if n == 1 else "",
            "source_sha256": checksum("1. 原文\r\n\n" if n == 1 else ""),
            "segments": [{"id": "line:0", "source": "1. 原文", "line_ending": "\r\n"},
                         {"id": "line:1", "source": "", "line_ending": "\n"}] if n == 1 else []}
            for n in range(1, 8)}}

    def test_compile_preserves_mixed_line_endings_and_stale_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "zh.json"
            source = self.source()
            target = initialize(source, path)
            self.assertEqual(len(pending(source, target)), 1)
            with self.assertRaises(ValueError):
                compile_runtime(source, target)
            target["rules"]["1"]["segments"]["line:0"] = "1. 译文"
            result = compile_runtime(source, target)
            self.assertEqual(result["rules"]["1"]["translation"], "1. 译文\r\n\n")
            target["rules"]["1"]["source_sha256"] = "stale"
            path.write_text(json.dumps(target))
            with self.assertRaises(ValueError):
                initialize(source, path)

    def test_source_checksum_and_lossless_segmentation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.json"
            source = self.source()
            path.write_text(json.dumps(source))
            self.assertEqual(load_source(path), source)
            source["rules"]["1"]["segments"][0]["line_ending"] = "\n"
            path.write_text(json.dumps(source))
            with self.assertRaises(ValueError):
                load_source(path)

    def test_url_and_prefix_protection(self):
        original = "（１） 詳細 https://example.com/a?q=1をご確認ください。30日"
        masked, tokens = protect(original)
        self.assertIn("をご確認", masked)
        self.assertIn("https://example.com/a?q=1", tokens.values())
        check_line(original, "（１） 详情 https://example.com/a?q=1请确认。30日")
        with self.assertRaises(ValueError):
            check_line(original, "（２） 详情 https://example.com/a?q=1请确认。30日")

    def test_license_never_pending_and_must_retain(self):
        source = self.source()
        source["rules"]["6"].update(source="日本語 license", source_sha256=checksum("日本語 license"),
            segments=[{"id": "line:0", "source": "日本語 license", "line_ending": ""}])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "zh.json"
            target = initialize(source, path)
            self.assertFalse(any(row["id"].startswith("6/") for row in pending(source, target)))
            target["rules"]["6"]["segments"]["line:0"] = "changed"
            path.write_text(json.dumps(target))
            with self.assertRaises(ValueError):
                initialize(source, path)

    def test_nonempty_type4_is_pending_while_empty_type4_stays_empty(self):
        source = self.source()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'zh.json'
            empty = initialize(source, path)
            self.assertFalse(any(row['id'].startswith('4/') for row in pending(source, empty)))
            source['rules']['4'].update(source='1. 販売価格\n',
                source_sha256=checksum('1. 販売価格\n'),
                segments=[{'id': 'line:0', 'source': '1. 販売価格', 'line_ending': '\n'}])
            target = initialize(source, path)
            self.assertIn('4/line:0', {row['id'] for row in pending(source, target)})
            target['rules']['1']['segments']['line:0'] = '1. 译文'
            target['rules']['4']['segments']['line:0'] = '1. 销售价格'
            self.assertEqual(compile_runtime(source, target)['rules']['4']['translation'],
                             '1. 销售价格\n')

    def test_missing_ids_and_mutated_marker_rejected(self):
        class Service:
            def translate_batch(self, rows, glossary):
                return {rows[0]["id"]: "译文"}
        with self.assertRaises(ValueError):
            translate_batch([{"id": "1/a", "speaker": "terms", "source": "1. 原文"}], Service(), {})


if __name__ == "__main__":
    unittest.main()
