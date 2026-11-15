import contextlib
import json
import io
import os
import subprocess
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

    def test_run_ocr_interrupt_terminates_child(self):
        fake_proc = mock.MagicMock()
        fake_proc.stdout = self._interrupting_lines()
        fake_proc.wait.side_effect = [0]
        with mock.patch("orca.__main__.subprocess.Popen", return_value=fake_proc), \
             mock.patch("orca.__main__.Spinner") as spinner:
            code, output = orca_main.run_ocr(["review"])
        self.assertEqual(code, 130)
        self.assertEqual(output, "")
        fake_proc.terminate.assert_called_once()
        spinner.return_value.stop.assert_called_once()

    @staticmethod
    def _interrupting_lines():
        raise KeyboardInterrupt
        yield "unreachable"

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

    def test_run_ocr_shows_spinner_for_review(self):
        fake_proc = mock.MagicMock()
        fake_proc.stdout = iter(["[ocr] log\n", "finding\n"])
        fake_proc.returncode = 0
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("orca.__main__.subprocess.Popen", return_value=fake_proc):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code, output = orca_main.run_ocr(["review"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(output, "[ocr] log\nfinding\n")
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("orca is scanning...", stderr.getvalue())


class TestExtractReportContent(unittest.TestCase):
    def test_extracts_representative_review_output(self):
        report = """Reviewing changes...

────── src/auth.py:18-20 ───
[security · high] Expired tokens remain valid.

- Reject expired tokens.

──────── Project Summary ────────
One security issue requires a fix.
[ocr] Session: session-id
"""
        extracted = orca_main.extract_report_content(report)
        self.assertIn("[security · high]", extracted)
        self.assertIn("──────── Project Summary ────────", extracted)
        self.assertNotIn("Reviewing changes", extracted)
        self.assertNotIn("[ocr]", extracted)

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

class TestCommandOptions(unittest.TestCase):
    def test_detects_session_list_and_output_path(self):
        command, session_list, output_path, error = orca_main.command_options(
            ["session", "list", "--format", "json", "--output", "ocr.json"]
        )
        self.assertEqual(command, "session")
        self.assertTrue(session_list)
        self.assertEqual(output_path, Path("ocr.json"))
        self.assertIsNone(error)

    def test_parses_json_findings_and_summary(self):
        data = {
            "comments": [{
                "file": "src/a.py", "line": 7, "end_line": 8,
                "classification": "bug", "severity": "high", "description": "Broken check"
            }],
            "summary": "One bug found",
        }
        report = orca_main.json_report_content(data)
        self.assertIn("src/a.py:7-8", report)
        self.assertIn("[bug · high] Broken check", report)
        self.assertIn("Project Summary", report)

    def test_malformed_json_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaises(ValueError):
                orca_main.review_output(["review", "--format", "json"], "", path)

    def test_reads_sarif_output_and_normalizes_multiple_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.sarif"
            data = {
                "version": "2.1.0",
                "runs": [{"results": [
                    {"ruleId": "AUTH-1", "level": "error", "message": {"text": "Expired token accepted"},
                     "locations": [{"physicalLocation": {"artifactLocation": {"uri": "src/auth.py"},
                                                           "region": {"startLine": 12, "endLine": 13}}}]},
                    {"ruleId": "STYLE-2", "level": "warning", "message": {"text": "Missing docstring"}},
                ]}],
            }
            raw = json.dumps(data)
            path.write_text(raw, encoding="utf-8")
            report = orca_main.review_output(["review", "--format", "sarif"], "", path)
            self.assertIn("src/auth.py:12-13", report)
            self.assertIn("[AUTH-1 · error] Expired token accepted", report)
            self.assertIn("unknown:0", report)
            self.assertIn("[STYLE-2 · warning] Missing docstring", report)
            self.assertEqual(path.read_text(encoding="utf-8"), raw)

    def test_sarif_resolves_rule_index_from_driver(self):
        data = {"runs": [{"tool": {"driver": {"rules": [{"id": "RULE-7"}]}},
                          "results": [{"ruleIndex": 0, "level": "error", "message": {"text": "Resolved rule"}}]}]}
        report = orca_main.sarif_report_content(data)
        self.assertIn("[RULE-7 · error] Resolved rule", report)

    def test_sarif_requires_runs_and_results_arrays(self):
        with self.assertRaisesRegex(ValueError, "runs array"):
            orca_main.sarif_report_content({"version": "2.1.0"})
        with self.assertRaisesRegex(ValueError, "results array"):
            orca_main.sarif_report_content({"runs": [{}]})

    def test_malformed_sarif_rejected_and_empty_sarif_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.sarif"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot parse OCR SARIF"):
                orca_main.review_output(["review", "-f=sarif"], "", path)
            path.write_text(json.dumps({"version": "2.1.0", "runs": [{"results": []}]}), encoding="utf-8")
            self.assertEqual(orca_main.review_output(["review", "-f=sarif"], "", path), "")

    def test_sarif_dash_uses_captured_stdout(self):
        raw = json.dumps({"runs": [{"results": [
            {"ruleId": "R1", "level": "warning", "message": {"text": "Finding"}}
        ]}]})
        report = orca_main.review_output(["review", "-f=sarif", "-o=-"], raw, Path("-"))
        self.assertIn("[R1 · warning] Finding", report)
        self.assertIn("unknown:0", report)

    def test_reads_json_output_file_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ocr.json"
            raw = json.dumps({"comments": [{"path": "src/a.py", "line": 3, "message": "issue"}]})
            path.write_text(raw, encoding="utf-8")
            report = orca_main.review_output(["review", "--format", "json"], "", path)
            self.assertIn("src/a.py:3", report)
            self.assertEqual(path.read_text(encoding="utf-8"), raw)


class TestReplaceRecap(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.recap_path = Path(self.tmpdir.name) / "RECAP.md"
        self.recap_path.write_text("old report\n", encoding="utf-8")

    def test_replace_with_new_report(self):
        self.assertTrue(orca_main.replace_recap(self.recap_path, "new report\n"))
        self.assertEqual(self.recap_path.read_text(encoding="utf-8"), "new report\n")

    def test_replace_with_empty_clears(self):
        self.assertTrue(orca_main.replace_recap(self.recap_path, ""))
        self.assertEqual(self.recap_path.read_text(encoding="utf-8"), "")

    def test_write_failure_does_not_lose_original(self):
        # Make target directory unwritable
        os.chmod(self.tmpdir.name, 0o555)
        self.addCleanup(lambda: os.chmod(self.tmpdir.name, 0o755))
        self.assertFalse(orca_main.replace_recap(self.recap_path, "new"))
        self.assertEqual(self.recap_path.read_text(encoding="utf-8"), "old report\n")

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
    def test_main_replaces_recap_on_success(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, "────── src/a.py:1 ───\n[bug · low] Finding\n")), \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.replace_recap", return_value=True) as replace:
            result = orca_main.main()
        self.assertEqual(result, 0)
        replace.assert_called_once_with(
            Path("/tmp/RECAP.md"), "────── src/a.py:1 ───\n[bug · low] Finding\n"
        )

    def test_ctrl_c_reports_clean_stop_and_preserves_recap(self):
        stderr = io.StringIO()
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review"]), \
             mock.patch("orca.__main__.run_ocr", side_effect=KeyboardInterrupt), \
             mock.patch("orca.__main__.find_recap_root") as find_root, \
             mock.patch("orca.__main__.replace_recap") as replace, \
             contextlib.redirect_stderr(stderr):
            result = orca_main.main()
        self.assertEqual(result, 130)
        self.assertEqual(stderr.getvalue().strip(), "orca stopped scanning")
        find_root.assert_not_called()
        replace.assert_not_called()

    def test_utility_commands_do_not_replace_recap(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "version"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, "version output")), \
             mock.patch("orca.__main__.find_recap_root") as find_root, \
             mock.patch("orca.__main__.replace_recap") as replace:
            result = orca_main.main()
        self.assertEqual(result, 0)
        find_root.assert_not_called()
        replace.assert_not_called()

    def test_main_preserves_recap_on_nonzero_exit(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(1, "some output")), \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.replace_recap") as replace:
            result = orca_main.main()
        self.assertEqual(result, 1)
        replace.assert_not_called()

    def test_success_without_findings_clears_recap(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, "[ocr] no findings\n")), \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.replace_recap") as replace, \
             mock.patch("builtins.print"):
            result = orca_main.main()
        self.assertEqual(result, 0)
        replace.assert_called_once_with(Path("/tmp/RECAP.md"), "")

    def test_utility_commands_do_not_replace_recap(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "version"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, "some output")), \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.replace_recap") as replace:
            result = orca_main.main()
        self.assertEqual(result, 0)
        replace.assert_not_called()

    def test_scan_completion_and_recap_capture(self):
        report = "────── src/a.py:2 ───\n[bug · low] Problem\n"
        with mock.patch.object(orca_main.sys, "argv", ["orca", "scan"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, report)), \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.replace_recap", return_value=True) as replace, \
             mock.patch("builtins.print") as printed:
            result = orca_main.main()
        self.assertEqual(result, 0)
        replace.assert_called_once_with(Path("/tmp/RECAP.md"), report)
        printed.assert_called_once_with("ORCA scan complete: captured 1 issue finding(s)")

    def test_session_list_displays_output_without_spinner_or_recap_change(self):
        output = io.StringIO()
        with mock.patch.object(orca_main.sys, "argv", ["orca", "session", "list"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, "session rows\n")) as run, \
             mock.patch("orca.__main__.replace_recap") as replace, \
             contextlib.redirect_stdout(output):
            result = orca_main.main()
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "session rows\n")
        run.assert_called_once_with(["session", "list"], show_spinner=False)
        replace.assert_not_called()

    def test_delegate_is_rejected_without_ocr(self):
        stderr = io.StringIO()
        with mock.patch.object(orca_main.sys, "argv", ["orca", "delegate", "preview"]), \
             mock.patch("orca.__main__.run_ocr") as run, \
             contextlib.redirect_stderr(stderr):
            result = orca_main.main()
        self.assertEqual(result, 0)
        self.assertIn("delegate is not supported by ORCA", stderr.getvalue())
        run.assert_not_called()

    def test_malformed_sarif_preserves_previous_recap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.sarif"
            path.write_text("not json", encoding="utf-8")
            with mock.patch.object(orca_main.sys, "argv", ["orca", "review", "-f=sarif", "-o={}".format(path)]), \
                 mock.patch("orca.__main__.run_ocr", return_value=(0, "")), \
                 mock.patch("orca.__main__.find_recap_root", return_value=Path(tmp)), \
                 mock.patch("orca.__main__.replace_recap") as replace, \
                 contextlib.redirect_stderr(io.StringIO()):
                result = orca_main.main()
            self.assertEqual(result, 1)
            replace.assert_not_called()

    def test_valid_empty_sarif_clears_recap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.sarif"
            path.write_text(json.dumps({"runs": [{"results": []}]}), encoding="utf-8")
            args = ["review", "-f=sarif", "-o={}".format(path)]
            with mock.patch.object(orca_main.sys, "argv", ["orca"] + args), \
                 mock.patch("orca.__main__.run_ocr", return_value=(0, "")), \
                 mock.patch("orca.__main__.find_recap_root", return_value=Path(tmp)), \
                 mock.patch("orca.__main__.replace_recap") as replace:
                result = orca_main.main()
            self.assertEqual(result, 0)
            replace.assert_called_once_with(Path(tmp) / "RECAP.md", "")

    def test_unreadable_output_preserves_recap(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review", "--output", "/missing/result.json"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, "")), \
             mock.patch("orca.__main__.replace_recap") as replace, \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             contextlib.redirect_stderr(io.StringIO()):
            result = orca_main.main()
        self.assertEqual(result, 1)
        replace.assert_not_called()

    def test_json_written_file_becomes_recap_and_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "result.json"
            raw = json.dumps({"comments": [{"file": "src/x.py", "line": 4, "type": "bug", "severity": "medium", "description": "issue"}]})
            output_path.write_text(raw, encoding="utf-8")
            args = ["review", "--format", "json", "--output", str(output_path)]
            with mock.patch.object(orca_main.sys, "argv", ["orca"] + args), \
                 mock.patch("orca.__main__.run_ocr", return_value=(0, "")) as run, \
                 mock.patch("orca.__main__.find_recap_root", return_value=Path(tmp)), \
                 mock.patch("orca.__main__.replace_recap") as replace:
                result = orca_main.main()
            self.assertEqual(result, 0)
            run.assert_called_once_with(args, show_spinner=True, spinner_message="orca is reviewing...")
            self.assertIn("[bug · medium] issue", replace.call_args.args[1])
            self.assertEqual(output_path.read_text(encoding="utf-8"), raw)

    def test_timeout_returns_124_and_preserves_recap(self):
        fake_proc = mock.MagicMock()
        fake_proc.stdout = iter(["output\n"])
        fake_proc.poll.return_value = None
        fake_proc.wait.side_effect = subprocess.TimeoutExpired("ocr", 5)
        with mock.patch("orca.__main__.subprocess.Popen", return_value=fake_proc), \
             mock.patch("orca.__main__.Spinner") as spinner, \
             mock.patch("orca.__main__.replace_recap") as replace, \
             contextlib.redirect_stderr(io.StringIO()):
            code, output = orca_main.run_ocr(["review"], show_spinner=True)
        self.assertEqual(code, 124)
        spinner.return_value.stop.assert_called_once()
        replace.assert_not_called()

    def test_command_options_parses_output_variants(self):
        self.assertEqual(
            orca_main.command_options(["review", "--output", "res.json"]),
            ("review", False, Path("res.json"), None),
        )
        self.assertEqual(
            orca_main.command_options(["review", "-o", "res.json"]),
            ("review", False, Path("res.json"), None),
        )
        self.assertEqual(
            orca_main.command_options(["review", "--output=res.json"]),
            ("review", False, Path("res.json"), None),
        )
        self.assertEqual(
            orca_main.command_options(["review", "-o", "-"]),
            ("review", False, Path("-"), None),
        )
        self.assertEqual(
            orca_main.command_options(["review"]),
            ("review", False, None, None),
        )
        self.assertEqual(
            orca_main.command_options(["session", "list"]),
            ("session", True, None, None),
        )
        self.assertEqual(
            orca_main.command_options(["review", "--output", "-"]),
            ("review", False, Path("-"), None),
        )

    def test_format_detection_does_not_search_unrelated_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report"
            path.write_text("text report", encoding="utf-8")
            self.assertEqual(
                orca_main.review_output(["review", "--background", "json"], "", path),
                "text report",
            )

    def test_review_output_uses_stdout_for_dash(self):
        report = orca_main.review_output(["review"], "captured stdout", Path("-"))
        self.assertEqual(report, "captured stdout")

    def test_recap_write_failure_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            recap = Path(tmp) / "RECAP.md"
            os.chmod(tmp, 0o555)
            try:
                ok = orca_main.replace_recap(recap, "content")
                self.assertFalse(ok)
            finally:
                os.chmod(tmp, 0o755)

    def test_timeout_status_preserves_recap(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(124, "partial output")), \
             mock.patch("orca.__main__.find_recap_root") as find_root, \
             mock.patch("orca.__main__.replace_recap") as replace:
            result = orca_main.main()
        self.assertEqual(result, 124)
        find_root.assert_not_called()
        replace.assert_not_called()

    def test_recap_write_failure_main_returns_1(self):
        report = "────── a.py:1 ───\n[bug · low] issue\n"
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, report)), \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.replace_recap", return_value=False):
            result = orca_main.main()
        self.assertEqual(result, 1)

    def test_json_normalization_followed_by_extraction(self):
        raw = json.dumps({"comments": [{"file": "src/a.py", "line": 4, "end_line": 5,
                                       "classification": "security", "severity": "high",
                                       "description": "SQLi"}],
                          "summary": "Fix this"})
        normalized = orca_main.json_report_content(json.loads(raw))
        extracted = orca_main.extract_report_content(normalized)
        self.assertIn("src/a.py:4-5", extracted)
        self.assertIn("[security · high]", extracted)
        self.assertIn("SQLi", extracted)
        self.assertIn("Project Summary", extracted)
        self.assertIn("Fix this", extracted)
    def test_missing_output_path_exits_before_ocr(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review", "--output"]), \
             mock.patch("orca.__main__.run_ocr") as run, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = orca_main.main()
        self.assertEqual(result, 2)
        self.assertIn("--output requires a value", stderr.getvalue())
        run.assert_not_called()

    def test_empty_output_value_exits_before_ocr(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review", "--output="]), \
             mock.patch("orca.__main__.run_ocr") as run, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = orca_main.main()
        self.assertEqual(result, 2)
        run.assert_not_called()

    def test_short_equals_output_and_format_are_forwarded_unchanged(self):
        args = ["review", "-o=report.json", "-f=json"]
        with mock.patch.object(orca_main.sys, "argv", ["orca"] + args), \
             mock.patch("orca.__main__.run_ocr", return_value=(1, "")) as run:
            result = orca_main.main()
        self.assertEqual(result, 1)
        run.assert_called_once_with(args, show_spinner=True, spinner_message="orca is reviewing...")
        self.assertEqual(orca_main.command_options(args), ("review", False, Path("report.json"), None))
        self.assertEqual(orca_main.output_format(args), "json")

    def test_short_output_equals_dash_uses_stdout(self):
        args = ["review", "-o=-"]
        report = "────── a.py:1 ───\n[bug · low] issue\n"
        with mock.patch.object(orca_main.sys, "argv", ["orca"] + args), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, report)), \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.replace_recap") as replace:
            result = orca_main.main()
        self.assertEqual(result, 0)
        replace.assert_called_once_with(Path("/tmp/RECAP.md"), report)
        self.assertEqual(orca_main.command_options(args), ("review", False, Path("-"), None))

    def test_missing_and_empty_format_fail_before_ocr(self):
        for args in (["review", "-f"], ["review", "--format="], ["review", "-f="]):
            with self.subTest(args=args):
                with mock.patch.object(orca_main.sys, "argv", ["orca"] + args), \
                     mock.patch("orca.__main__.run_ocr") as run, \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(orca_main.main(), 2)
                run.assert_not_called()

    def test_short_equals_values_reach_ocr_and_write_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text(json.dumps({"comments": [{"file": "a.py", "line": 2, "description": "x"}]}), encoding="utf-8")
            args = ["review", "-o={}".format(path), "-f=json"]
            with mock.patch.object(orca_main.sys, "argv", ["orca"] + args), \
                 mock.patch("orca.__main__.run_ocr", return_value=(0, "")) as run, \
                 mock.patch("orca.__main__.find_recap_root", return_value=Path(tmp)), \
                 mock.patch("orca.__main__.replace_recap") as replace:
                result = orca_main.main()
            self.assertEqual(result, 0)
            run.assert_called_once_with(args, show_spinner=True, spinner_message="orca is reviewing...")
            self.assertIn("a.py:2", replace.call_args.args[1])

    def test_flag_following_output_fails(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review", "--output", "--format"]), \
             mock.patch("orca.__main__.run_ocr") as run, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = orca_main.main()
        self.assertEqual(result, 2)
        run.assert_not_called()

    def test_output_dash_with_captured_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(orca_main.sys, "argv", ["orca", "review", "--output", "-"]), \
                 mock.patch("orca.__main__.run_ocr", return_value=(0, "────── a.py:1 ───\n[bug · low] issue\n")) as run, \
                 mock.patch("orca.__main__.find_recap_root", return_value=Path(tmp)):
                result = orca_main.main()
        self.assertEqual(result, 0)
        run.assert_called_once_with(["review", "--output", "-"], show_spinner=True, spinner_message="orca is reviewing...")

    def test_format_json_explicit_and_equals(self):
        self.assertEqual(orca_main.output_format(["review", "--format", "json"]), "json")
        self.assertEqual(orca_main.output_format(["review", "-f", "json"]), "json")
        self.assertEqual(orca_main.output_format(["review", "--format=json"]), "json")
        self.assertEqual(orca_main.output_format(["review", "--format", "text"]), "text")
        self.assertEqual(orca_main.output_format(["review", "--format", "json", "--output", "file.json"]), "json")
        self.assertNotEqual(orca_main.output_format(["review", "--json-only"]), "json")
        self.assertNotEqual(orca_main.output_format(["review", "--backend", "json"]), "json")

    def test_invalid_line_numbers_are_skipped(self):
        raw = json.dumps({"comments": [
            {"file": "src/a.py", "line": 4, "type": "bug", "severity": "high", "description": "valid"},
            {"file": "src/b.py", "line": 0, "type": "bug", "severity": "high", "description": "zero"},
            {"file": "src/c.py", "type": "bug", "severity": "high", "description": "missing"},
            {"file": "src/d.py", "line": -1, "type": "bug", "severity": "high", "description": "negative"},
        ]})
        normalized = orca_main.json_report_content(json.loads(raw))
        self.assertIn("src/a.py:4", normalized)
        self.assertNotIn("src/b.py", normalized)
        self.assertNotIn("src/c.py", normalized)
        self.assertNotIn("src/d.py", normalized)

    def test_malformed_nonempty_report_is_error(self):
        raw = json.dumps({"comments": [{"file": "src/a.py", "line": 0, "type": "bug", "severity": "high", "description": "zero"}]})
        with self.assertRaises(ValueError):
            orca_main.json_report_content(json.loads(raw))

    def test_valid_empty_report_clears_recap(self):
        raw = json.dumps({"comments": []})
        normalized = orca_main.json_report_content(json.loads(raw))
        self.assertEqual(normalized, "")
    def test_review_completion_message_and_zero_count(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, "[ocr] only noise\n")) as run, \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.replace_recap", return_value=True) as replace, \
             mock.patch("builtins.print") as printed:
            result = orca_main.main()
        self.assertEqual(result, 0)
        replace.assert_called_once()
        printed.assert_called_once_with("ORCA review complete: captured 0 issue finding(s)")
        run.assert_called_once_with(["review"], show_spinner=True, spinner_message="orca is reviewing...")

    def test_scan_completion_message_alias(self):
        report = "────── src/a.py:1 ───\n[bug · low] issue\n"
        with mock.patch.object(orca_main.sys, "argv", ["orca", "s"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, report)) as run_mock, \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.replace_recap", return_value=True) as replace, \
             mock.patch("builtins.print") as printed:
            result = orca_main.main()
        self.assertEqual(result, 0)
        printed.assert_called_once_with("ORCA scan complete: captured 1 issue finding(s)")
        run_mock.assert_called_once_with(["s"], show_spinner=True, spinner_message="orca is scanning...")

    def test_completion_includes_project_summary(self):
        report = "────── a.py:1 ───\n[bug · low] issue\n──────── Project Summary ────────\nSummary\n"
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, report)), \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.replace_recap", return_value=True), \
             mock.patch("builtins.print") as printed:
            result = orca_main.main()
        self.assertEqual(result, 0)
        printed.assert_called_once_with(
            "ORCA review complete: captured 1 issue finding(s) and project summary"
        )

    def test_no_success_message_on_recap_write_failure(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, "────── a.py:1 ───\n[bug · low] issue\n")), \
             mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
             mock.patch("orca.__main__.replace_recap", return_value=False), \
             mock.patch("builtins.print") as printed:
            result = orca_main.main()
        self.assertEqual(result, 1)
        printed.assert_not_called()

    def test_no_success_message_on_ocr_failure(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "scan"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(1, "error")), \
             mock.patch("orca.__main__.replace_recap") as replace, \
             mock.patch("builtins.print") as printed:
            result = orca_main.main()
        self.assertEqual(result, 1)
        replace.assert_not_called()
        printed.assert_not_called()

    def test_no_success_message_on_timeout(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "scan"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(124, "partial")), \
             mock.patch("orca.__main__.replace_recap") as replace, \
             mock.patch("builtins.print") as printed:
            result = orca_main.main()
        self.assertEqual(result, 124)
        replace.assert_not_called()
        printed.assert_not_called()

    def test_no_spinner_for_utility_commands(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "version"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, "version")) as run, \
             mock.patch("orca.__main__.find_recap_root") as find_root, \
             mock.patch("orca.__main__.replace_recap") as replace:
            result = orca_main.main()
        self.assertEqual(result, 0)
        run.assert_called_once_with(["version"], show_spinner=False)
        find_root.assert_not_called()
        replace.assert_not_called()

    def test_no_spinner_or_message_for_session_list(self):
        output = io.StringIO()
        with mock.patch.object(orca_main.sys, "argv", ["orca", "session", "list"]), \
             mock.patch("orca.__main__.run_ocr", return_value=(0, "session rows\n")) as run, \
             mock.patch("orca.__main__.replace_recap") as replace, \
             contextlib.redirect_stdout(output):
            result = orca_main.main()
        self.assertEqual(result, 0)
        run.assert_called_once_with(["session", "list"], show_spinner=False)
        replace.assert_not_called()
        self.assertEqual(output.getvalue(), "session rows\n")

    def test_delegate_is_rejected_without_ocr(self):
        stderr = io.StringIO()
        with mock.patch.object(orca_main.sys, "argv", ["orca", "delegate", "preview"]), \
             mock.patch("orca.__main__.run_ocr") as run, \
             contextlib.redirect_stderr(stderr):
            result = orca_main.main()
        self.assertEqual(result, 0)
        self.assertIn("delegate is not supported by ORCA", stderr.getvalue())
        run.assert_not_called()

    def test_main_passes_args_and_returns_exit_code(self):
        with mock.patch.object(orca_main.sys, "argv", ["orca", "review", "--from", "main"]):
            with mock.patch("orca.__main__.run_ocr", return_value=(3, "some output")) as mock_run, \
                 mock.patch("orca.__main__.find_recap_root", return_value=Path("/tmp")), \
                 mock.patch("orca.__main__.replace_recap", return_value=False) as mock_replace:
                exit_code = orca_main.main()
        mock_run.assert_called_once_with(["review", "--from", "main"], show_spinner=True, spinner_message="orca is reviewing...")
        mock_replace.assert_not_called()
        self.assertEqual(exit_code, 3)


if __name__ == "__main__":
    unittest.main()
