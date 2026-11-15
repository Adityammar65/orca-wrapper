import contextlib
import io
import os
import unittest
import tempfile
import unittest.mock as mock
from pathlib import Path

import orca.__main__ as orca_main


class TestRunOcr(unittest.TestCase):
    def test_missing_ocr_executable(self):
        stderr = io.StringIO()
        with mock.patch("orca.__main__.subprocess.Popen", side_effect=FileNotFoundError("ocr")):
            with contextlib.redirect_stderr(stderr):
                exit_code, output = orca_main.run_ocr([])
        self.assertEqual(exit_code, 127)
        self.assertEqual(output, "")
        self.assertIn("'ocr' executable not found in PATH", stderr.getvalue())

    def test_run_ocr_passes_args(self):
        fake_proc = mock.MagicMock()
        fake_proc.stdout = iter(["hello\n"])
        fake_proc.returncode = 0
        with mock.patch("orca.__main__.subprocess.Popen", return_value=fake_proc) as mock_popen:
            exit_code, output = orca_main.run_ocr(["review", "--from", "main"])
        args = mock_popen.call_args[0][0]
        self.assertEqual(args, ["ocr", "review", "--from", "main"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(output, "hello\n")

    def test_run_ocr_hides_output_for_scan(self):
        fake_proc = mock.MagicMock()
        fake_proc.stdout = iter(["[ocr] log\n", "finding\n"])
        fake_proc.returncode = 0
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("orca.__main__.subprocess.Popen", return_value=fake_proc):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code, output = orca_main.run_ocr(["scan"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(output, "[ocr] log\nfinding\n")
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("orca is scanning...", stderr.getvalue())

    def test_run_ocr_hides_output_for_review(self):
        fake_proc = mock.MagicMock()
        fake_proc.stdout = iter(["[ocr] log\n", "finding\n"])
        fake_proc.returncode = 0
        with mock.patch("orca.__main__.subprocess.Popen", return_value=fake_proc):
            with mock.patch("builtins.print") as printed:
                exit_code, output = orca_main.run_ocr(["review"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(output, "[ocr] log\nfinding\n")
        printed.assert_not_called()


class TestExtractReportContent(unittest.TestCase):
    def test_empty_output_returns_empty(self):
        self.assertEqual(orca_main.extract_report_content(""), "")
        self.assertEqual(orca_main.extract_report_content("   \n  \n"), "")

    def test_operational_only_output_returns_empty(self):
        operational = "Running review task...\nBatch 1: 3 files\nToken usage: 12345\nProgress: 50% complete\n"
        self.assertEqual(orca_main.extract_report_content(operational), "")

    def test_no_findings_or_summary_returns_empty(self):
        plain = "OpenCodeReview - AI-Powered Code Review CLI\nVersion: 1.13.10\n"
        self.assertEqual(orca_main.extract_report_content(plain), "")

    def test_extracts_issue_markers_and_final_summary_only(self):
        report = """[ocr] scan dispatch: 2 batch(es)
[ocr] Summary: 2 file(s), 2 comments

────── src/app.py:42-44 ───
[security · high] SQL injection vulnerability.

- Use parameterized queries.

────── src/auth.py:10 ───
[bug · medium] Expired tokens are accepted.

──────── Project Summary ────────
Two findings need fixes.
[ocr] Session: abc123
"""
        extracted = orca_main.extract_report_content(report)
        self.assertIn("────── src/app.py:42-44 ───", extracted)
        self.assertIn("────── src/auth.py:10 ───", extracted)
        self.assertIn("[security · high]", extracted)
        self.assertIn("──────── Project Summary ────────", extracted)
        self.assertIn("Two findings need fixes.", extracted)
        self.assertNotIn("[ocr]", extracted)
        self.assertNotIn("scan dispatch", extracted)
        self.assertNotIn("Session:", extracted)
        self.assertEqual(extracted.count("────── src/"), 2)

    def test_finding_without_summary_and_operational_only_return_empty(self):
        report = "[ocr] Summary: 1 file, 0 comments\nNo findings.\n"
        self.assertEqual(orca_main.extract_report_content(report), "")

    def test_summary_before_findings_is_bounded(self):
        report = """──────── Project Summary ────────
Summary text.
────── src/a.py:1 ───
[bug · low] Finding text.
"""
        extracted = orca_main.extract_report_content(report)
        self.assertEqual(extracted.count("Project Summary"), 1)
        self.assertIn("[bug · low]", extracted)
        self.assertNotIn("[ocr]", extracted)

    def test_extracts_summary_only(self):
        report = "──────── Project Summary ────────\nGood change overall.\n"
        extracted = orca_main.extract_report_content(report)
        self.assertIn("Project Summary", extracted)

class TestAppendToRecap(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.recap_path = Path(self.tmpdir.name) / "RECAP.md"
        self.recap_path.write_text("", encoding="utf-8")

    def test_appends_content(self):
        self.assertTrue(orca_main.append_to_recap(self.recap_path, "## Findings\n\n### Finding 1\n"))
        self.assertEqual(self.recap_path.read_text(encoding="utf-8"), "## Findings\n\n### Finding 1\n")

    def test_empty_content_not_appended(self):
        self.assertFalse(orca_main.append_to_recap(self.recap_path, ""))
        self.assertFalse(orca_main.append_to_recap(self.recap_path, "   \n"))
        self.assertEqual(self.recap_path.read_text(encoding="utf-8"), "")

    def test_adds_newline_after_existing_content_without_one(self):
        self.recap_path.write_text("existing", encoding="utf-8")
        self.assertTrue(orca_main.append_to_recap(self.recap_path, "new report\n"))
        self.assertEqual(self.recap_path.read_text(encoding="utf-8"), "existing\nnew report\n")

    def test_empty_recap_does_not_get_extra_newline(self):
        self.assertTrue(orca_main.append_to_recap(self.recap_path, "new report\n"))
        self.assertEqual(self.recap_path.read_text(encoding="utf-8"), "new report\n")

class TestFindRecapRoot(unittest.TestCase):
    def test_finds_recap_upward(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "sub" / "dir"
            sub.mkdir(parents=True)
            (Path(tmp) / "RECAP.md").write_text("", encoding="utf-8")
            with mock.patch("orca.__main__.Path.cwd", return_value=sub):
                self.assertEqual(orca_main.find_recap_root(), Path(tmp))


class TestSpinner(unittest.TestCase):
    def test_spinner_uses_required_message(self):
        from threading import Event
        spinner = orca_main.Spinner()
        spinner._stop = mock.MagicMock(spec=Event)
        spinner._stop.is_set.side_effect = [False, True]
        output = io.StringIO()
        with mock.patch("orca.__main__.sys.stderr", output):
            spinner._run()
        self.assertIn("orca is scanning...", output.getvalue())


class TestMain(unittest.TestCase):
    def test_scan_completion_and_recap_capture(self):
        report = "────── src/a.py:2 ───\n[bug · low] Problem\n"
        with mock.patch.object(orca_main.sys, "argv", ["orca", "scan"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, report)), \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.append_to_recap", return_value=True) as append, \
             mock.patch("builtins.print") as printed:
            result = orca_main.main()
        self.assertEqual(result, 0)
        append.assert_called_once_with(Path("/tmp/RECAP.md"), report)
        printed.assert_called_once_with("ORCA complete: captured 1 issue finding(s)")

    def test_main_passes_args_and_returns_exit_code(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review", "--from", "main"]):
            with mock.patch("orca.__main__.run_ocr", return_value=(3, "some output")) as mock_run, \
                 mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
                 mock.patch("orca.__main__.append_to_recap", return_value=False) as mock_append:
                exit_code = orca_main.main()
        mock_run.assert_called_once_with(["review", "--from", "main"])
        mock_append.assert_not_called()
        self.assertEqual(exit_code, 3)


if __name__ == "__main__":
    unittest.main()
