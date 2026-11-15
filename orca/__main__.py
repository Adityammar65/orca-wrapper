#!/usr/bin/env python3
"""
ORCA - Lightweight CLI wrapper for Alibaba's OpenCode Review (OCR) tool.

Forwards all arguments to the `ocr` executable, captures its output,
extracts review findings and project summaries, and writes them to RECAP.md
in the project root.
"""

import json
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def find_recap_root() -> Path:
    """
    Find the project root directory containing RECAP.md.
    Searches upward from current working directory.
    """
    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        recap = parent / "RECAP.md"
        if recap.exists():
            return parent
    # Default to current working directory if no RECAP.md found
    return cwd


def run_ocr(args: list, show_spinner: bool = True, spinner_message: str = "orca is scanning...") -> tuple:
    """Run OCR silently and capture output for report filtering."""
    try:
        proc = subprocess.Popen(
            ["ocr"] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print("Error: 'ocr' executable not found in PATH. Please install OCR first.", file=sys.stderr)
        return 127, ""
    except OSError as e:
        print(f"Error: Failed to start 'ocr': {e}", file=sys.stderr)
        return 1, ""

    spinner = Spinner(spinner_message) if show_spinner else None
    if spinner:
        spinner.start()
    captured_lines = []
    timed_out = False
    try:
        if proc.stdout is None:
            print("Error: OCR process did not provide an output stream.", file=sys.stderr)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        else:
            captured_lines.extend(proc.stdout)
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return 130, ""
    finally:
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        if spinner:
            spinner.stop()

    if timed_out:
        print("Error: OCR process timed out during cleanup.", file=sys.stderr)
        return 124, "".join(captured_lines)
    if proc.stdout is None:
        return 1, ""
    return proc.returncode, "".join(captured_lines)

class Spinner:
    """Print a small static-frame loading animation to stderr."""

    def __init__(self, message: str = "orca is scanning..."):
        self.message = message
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        index = 0
        while not self._stop.is_set():
            sys.stderr.write("\r{} {}".format(LOADING_FRAMES[index % len(LOADING_FRAMES)], self.message))
            sys.stderr.flush()
            index += 1
            self._stop.wait(0.1)

    def stop(self):
        self._stop.set()
        self._thread.join()
        sys.stderr.write("\r\033[2K\n")
        sys.stderr.flush()

# OCR output markers
FINDING_START_RE = re.compile(r"^─{3,}\s+(.+?):\d+(?:-\d+)?\s+─{3,}\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(r"^─+\s+Project Summary\s+─+\s*$", re.MULTILINE)
OPERATIONAL_RE = re.compile(r"^\[ocr\]", re.IGNORECASE | re.MULTILINE)

LOADING_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

def extract_report_content(output: str) -> str:
    """Keep only OCR issue blocks and its final project summary."""
    findings = []
    finding_starts = [match.start() for match in FINDING_START_RE.finditer(output)]
    summary_match = SUMMARY_RE.search(output)

    for index, start in enumerate(finding_starts):
        end = finding_starts[index + 1] if index + 1 < len(finding_starts) else len(output)
        if summary_match and start < summary_match.start() < end:
            end = summary_match.start()
        next_log = re.search(r"^\[ocr\]", output[start:end], re.IGNORECASE | re.MULTILINE)
        if next_log:
            end = start + next_log.start()
        block = output[start:end].strip()
        if block:
            findings.append(block)

    summary = ""
    if summary_match:
        summary_lines = output[summary_match.start():].splitlines()
        summary_lines = [line for line in summary_lines if not OPERATIONAL_RE.match(line)]
        summary = "\n".join(summary_lines).strip()

    parts = findings + ([summary] if summary else [])
    return "\n\n".join(parts) + "\n" if parts else ""

def command_options(args: list) -> tuple:
    """Return command, session-list flag, output path, and option error."""
    command = args[0] if args else "review"
    session_list = command == "session" and len(args) > 1 and args[1] == "list"
    output_path = None
    error = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("--output", "-o", "--format", "-f"):
            option = arg
            if index + 1 >= len(args) or (args[index + 1].startswith("-") and args[index + 1] != "-"):
                error = "{} requires a value".format(option)
                break
            value = args[index + 1]
            if not value:
                error = "{} requires a value".format(option)
                break
            if option in ("--output", "-o"):
                output_path = Path(value)
            elif value not in ("text", "json", "sarif"):
                error = "{} must be text, json, or sarif".format(option)
                break
            index += 1
        elif arg.startswith(("--output=", "-o=")):
            value = arg.split("=", 1)[1]
            if not value:
                error = "{} requires a value".format(arg.split("=", 1)[0])
                break
            output_path = Path(value)
        elif arg.startswith(("--format=", "-f=")):
            option, value = arg.split("=", 1)
            if value not in ("text", "json", "sarif"):
                error = "{} must be text, json, or sarif".format(option)
                break
        index += 1
    return command, session_list, output_path, error


def output_format(args: list) -> str:
    """Get OCR format from explicit long or short option forms."""
    for index, arg in enumerate(args):
        if arg in ("--format", "-f") and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(("--format=", "-f=")):
            return arg.split("=", 1)[1]
    return ""

def review_output(args: list, captured: str, output_path: Path) -> str:
    """Read requested OCR output file, decoding JSON reports where applicable."""
    if str(output_path) == "":
        raise ValueError("OCR output option requires a file path or '-' for stdout")
    if str(output_path) == "-":
        output = captured
    else:
        try:
            output = output_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            raise ValueError("cannot read OCR output file {}: {}".format(output_path, e))
    if not output and str(output_path) != "-":
        raise ValueError("OCR output file {} is empty".format(output_path))

    fmt = output_format(args)
    if fmt == "json":
        try:
            data = json.loads(output)
        except json.JSONDecodeError as e:
            raise ValueError("cannot parse OCR JSON output {}: {}".format(output_path, e))
        return json_report_content(data)
    if fmt == "sarif":
        try:
            data = json.loads(output)
        except json.JSONDecodeError as e:
            raise ValueError("cannot parse OCR SARIF output {}: {}".format(output_path, e))
        return sarif_report_content(data)
    return output


def sarif_report_content(data) -> str:
    """Normalize SARIF findings into ORCA's report text."""
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise ValueError("SARIF report must contain a runs array")
    blocks = []
    for run in data["runs"]:
        if not isinstance(run, dict) or not isinstance(run.get("results"), list):
            raise ValueError("each SARIF run must contain a results array")
        driver = run.get("tool", {}).get("driver", {})
        rules = driver.get("rules", []) if isinstance(driver, dict) else []
        if not isinstance(rules, list):
            rules = []
        for result in run["results"]:
            if not isinstance(result, dict):
                raise ValueError("SARIF results must be objects")
            rule_id = result.get("ruleId", "")
            if not rule_id and isinstance(result.get("ruleIndex"), int):
                rule_index = result["ruleIndex"]
                if 0 <= rule_index < len(rules) and isinstance(rules[rule_index], dict):
                    rule_id = rules[rule_index].get("id", "")
            rule_id = rule_id or "unknown"
            level = result.get("level", "note")
            message = result.get("message", {})
            message_text = message.get("text", "") if isinstance(message, dict) else str(message)
            locations = result.get("locations", [])
            file_path = "unknown"
            line = 0
            end_line = None
            if locations and isinstance(locations, list):
                loc = locations[0]
                phys = loc.get("physicalLocation", {}) if isinstance(loc, dict) else {}
                artifact = phys.get("artifactLocation", {}) if isinstance(phys, dict) else {}
                file_path = artifact.get("uri", "unknown")
                region = phys.get("region", {}) if isinstance(phys, dict) else {}
                line = region.get("startLine", 0) if isinstance(region, dict) else 0
                end_line = region.get("endLine") if isinstance(region, dict) else None
            if not isinstance(line, int) or isinstance(line, bool) or line < 0:
                line = 0
            if end_line is not None and (not isinstance(end_line, int) or isinstance(end_line, bool) or end_line < line):
                end_line = None
            end = "-{}".format(end_line) if isinstance(end_line, int) and end_line > line else ""
            location = "{}:{}{}".format(file_path, line, end)
            blocks.append("────── {} ───\n[{} · {}] {}".format(location, rule_id, level, message_text))
    return "\n\n".join(blocks) + "\n" if blocks else ""

def json_report_content(data) -> str:
    """Normalize OCR JSON findings into ORCA's report text."""
    if isinstance(data, dict):
        comments = data.get("comments", data.get("findings", []))
        summary = data.get("summary", data.get("project_summary", ""))
        if not isinstance(comments, list) and not summary:
            raise ValueError("OCR JSON report has no recognizable findings or summary")
    elif isinstance(data, list):
        comments, summary = data, ""
    else:
        raise ValueError("OCR JSON report must be an object or array")

    blocks = []
    if not isinstance(comments, list):
        raise ValueError("OCR JSON findings must be an array")
    for item in comments:
        if not isinstance(item, dict):
            continue
        path = item.get("file", item.get("path", ""))
        line = item.get("line", item.get("start_line", 0))
        classification = item.get("classification", item.get("type", "issue"))
        severity = item.get("severity", "unknown")
        description = item.get("description", item.get("message", ""))
        if not path or not isinstance(line, int) or isinstance(line, bool) or line <= 0 or not description:
            continue
        end_line = item.get("end_line")
        location = "{}:{}{}".format(path, line, "-{}".format(end_line) if isinstance(end_line, int) and end_line != line else "")
        blocks.append("────── {} ───\n[{} · {}] {}".format(location, classification, severity, description))

    if summary:
        blocks.append("──────── Project Summary ────────\n{}".format(summary))
    if comments and not blocks:
        raise ValueError("OCR JSON report has no usable findings")
    return "\n\n".join(blocks) + "\n" if blocks else ""


def replace_recap(recap_path: Path, content: str) -> bool:
    """Replace RECAP.md atomically with the latest extracted report."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=recap_path.parent, delete=False
        ) as recap:
            temporary_path = Path(recap.name)
            recap.write(content)
        temporary_path.replace(recap_path)
        return True
    except OSError as e:
        print(f"Warning: Failed to replace RECAP.md: {e}", file=sys.stderr)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        return False


def main() -> int:
    """
    Main entry point for the `orca` command.
    """
    # All arguments after the command name are passed to OCR
    ocr_args = sys.argv[1:]

    command, session_list, output_path, option_error = command_options(ocr_args)
    if option_error:
        print("Error: {}".format(option_error), file=sys.stderr)
        return 2
    if command == "delegate":
        print("delegate is not supported by ORCA", file=sys.stderr)
        return 0

    is_review = command in ("review", "r")
    is_scan = command in ("scan", "s")
    spinner_message = "orca is reviewing..." if is_review else "orca is scanning..."
    show_spinner = is_review or is_scan

    try:
        if show_spinner:
            exit_code, output = run_ocr(
                ocr_args, show_spinner=True, spinner_message=spinner_message
            )
        else:
            exit_code, output = run_ocr(ocr_args, show_spinner=False)
    except KeyboardInterrupt:
        print("orca stopped scanning", file=sys.stderr)
        return 130
    if exit_code == 130:
        print("orca stopped scanning", file=sys.stderr)
        return 130

    report = output
    if exit_code == 0 and (is_review or is_scan) and output_path is not None:
        try:
            report = review_output(ocr_args, output, output_path)
        except ValueError as e:
            print("Error: {}".format(e), file=sys.stderr)
            return 1

    if session_list:
        sys.stdout.write(output)
        sys.stdout.flush()
    elif exit_code == 0 and (is_review or is_scan):
        if not replace_recap(find_recap_root() / "RECAP.md", extract_report_content(report)):
            return 1
        action = "review" if is_review else "scan"
        count = len(FINDING_START_RE.findall(report))
        print("ORCA {} complete: captured {} issue finding(s){}".format(
            action, count, " and project summary" if SUMMARY_RE.search(report) else ""
        ))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
