#!/usr/bin/env python3
"""
ORCA - Lightweight CLI wrapper for Alibaba's OpenCode Review (OCR) tool.

Forwards all arguments to the `ocr` executable, captures its output,
extracts review findings and project summaries, and appends them to RECAP.md
in the project root.
"""

import re
import subprocess
import sys
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


def run_ocr(args: list) -> tuple:
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

    captured_lines = []
    is_scan = len(args) > 0 and args[0] in ("scan", "s")
    spinner = Spinner() if is_scan else None
    if spinner is not None:
        spinner.start()

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            captured_lines.append(line)
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    finally:
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if spinner is not None:
            spinner.stop()

    return proc.returncode, "".join(captured_lines)


class Spinner:
    """Print a small static-frame loading animation to stderr."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        index = 0
        while not self._stop.is_set():
            sys.stderr.write("\r{} orca is scanning...".format(LOADING_FRAMES[index % len(LOADING_FRAMES)]))
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

def append_to_recap(recap_path: Path, content: str) -> bool:
    """
    Append extracted content to RECAP.md.
    Returns True if appended, False if no content or error.
    """
    if not content or not content.strip():
        return False

    try:
        if recap_path.exists() and recap_path.stat().st_size:
            with recap_path.open("rb") as recap:
                recap.seek(-1, 2)
                needs_newline = recap.read(1) != b"\n"
        else:
            needs_newline = False
        with recap_path.open("a", encoding="utf-8") as f:
            if needs_newline:
                f.write("\n")
            f.write(content)
        return True
    except OSError as e:
        print(f"Warning: Failed to append to RECAP.md: {e}", file=sys.stderr)
        return False


def main() -> int:
    """
    Main entry point for the `orca` command.
    """
    # All arguments after the command name are passed to OCR
    ocr_args = sys.argv[1:]

    # Run OCR and capture output
    exit_code, output = run_ocr(ocr_args)

    # Find RECAP.md in project root
    root = find_recap_root()
    recap_path = root / "RECAP.md"

    # Extract and append findings/summaries
    extracted = extract_report_content(output)
    is_scan = len(ocr_args) > 0 and ocr_args[0] in ("scan", "s")
    if extracted:
        append_to_recap(recap_path, extracted)
    if is_scan:
        count = len(FINDING_START_RE.findall(output))
        print("ORCA complete: captured {} issue finding(s){}".format(
            count, " and project summary" if SUMMARY_RE.search(output) else ""
        ))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
