# ORCA

ORCA wraps Alibaba's OpenCode Review (`ocr`) CLI. It captures OCR output without printing it, then appends only issue blocks and the final project summary to the nearest `RECAP.md`.

For `orca scan`, ORCA shows a spinner with `orca is scanning...` and a completion summary. No findings means no recap write.

## Requirements

- Python 3.8 or newer
- OpenCode Review installed, with `ocr` available on `PATH`

## Install

```sh
python -m pip install .
```

## Usage

Use `orca` anywhere you would use `ocr`:

```sh
orca review --from main
orca scan --path src
```

OCR's exit status passes through. For scans, ORCA writes only captured issue findings and the final project summary to the first `RECAP.md` found walking upward from the current directory. If none exists, ORCA leaves recap files untouched.
