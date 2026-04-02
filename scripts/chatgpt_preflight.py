#!/usr/bin/env python3
"""Run one ChatGPT preflight from the command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.chatgpt_flow_runner import print_preflight_summary, run_chatgpt_preflight


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one ChatGPT preflight")
    parser.add_argument("--mail-provider", default="cfmail", help="Mailbox provider")
    parser.add_argument("--proxy", default=None, help="Optional proxy URL")
    parser.add_argument("--email", default=None, help="Optional fixed email")
    parser.add_argument("--password", default=None, help="Optional fixed password")
    parser.add_argument("--json", dest="output_json", action="store_true", help="Print JSON output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = run_chatgpt_preflight(
        email=str(args.email or "").strip() or None,
        password=str(args.password or "").strip() or None,
        mail_provider=str(args.mail_provider or "").strip() or "cfmail",
        proxy=str(args.proxy or "").strip() or None,
    )
    if args.output_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_preflight_summary(payload)


if __name__ == "__main__":
    main()
