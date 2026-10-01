"""The CLI uses its own data even when launched outside any shared workspace."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class StandaloneCliTests(unittest.TestCase):
    def test_plans_read_project_data_from_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            project = workspace / "independent-translator"
            project.mkdir()
            for entry in ("main.py", "translate_ui.py", "suggest_master_kana.py"):
                shutil.copy2(PROJECT / entry, project / entry)
            shutil.copytree(PROJECT / "src", project / "src",
                            ignore=shutil.ignore_patterns("__pycache__"))
            elsewhere = workspace / "unrelated"
            elsewhere.mkdir()
            page = "/notice/" + "a" * 64 + "/index.html"
            fixtures = {
                "data/master/orig/Story.json": {"1": {"name": "物語"}},
                "data/master/zh-Hans/Story.json": {"1": {"name": ""}},
                "data/notice/source.json": {"pages": {
                    page: {"title": "お知らせ", "texts": ["お知らせ"]}}},
                "data/notice/zh-Hans.json": {"pages": {}},
                "data/notice/ui.json": {"text": {}},
                "data/ui/source.json": {"i18n": {"ready": "準備"}},
                "data/ui/zh-Hans.json": {"i18n": {"ready": "准备"}},
                "data/ui/i18n-variants-source.json": {},
                "data/master/master-review.json": {"kana_issues": []},
            }
            for relative, value in fixtures.items():
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            before = {p: p.read_bytes() for p in project.rglob("*.json")}
            for command in ("master-plan", "notice-plan"):
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, str(project / "main.py"), command],
                        cwd=elsewhere, capture_output=True, text=True,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("1 pending", result.stdout)
            for script, expected in (("translate_ui.py", "Pending 0"),
                                     ("suggest_master_kana.py", "Saved 0/0")):
                with self.subTest(script=script):
                    result = subprocess.run(
                        [sys.executable, str(project / script)], cwd=elsewhere,
                        capture_output=True, text=True,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ""})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(expected, result.stdout)
            for path, original in before.items():
                self.assertEqual(path.read_bytes(), original)
            output = project / "data/master/working/master-kana-suggestions.json"
            self.assertEqual(json.loads(output.read_text())["total_unique"], 0)
            self.assertEqual(list(elsewhere.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
