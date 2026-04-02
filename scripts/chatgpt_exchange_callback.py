#!/usr/bin/env python3
"""Exchange a ChatGPT OAuth callback URL from the command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.chatgpt_flow_runner import print_callback_summary, run_chatgpt_callback_exchange
from core.settings import AppSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Exchange a ChatGPT OAuth callback")
    parser.add_argument("--callback-url", required=True, help="OAuth callback URL")
    parser.add_argument("--state", required=True, help="Expected OAuth state")
    parser.add_argument("--code-verifier", required=True, help="OAuth PKCE code_verifier")
    parser.add_argument("--proxy", default=None, help="Optional proxy URL")
    parser.add_argument("--no-write-pool", action="store_true", help="Do not write token JSON into pool")
    parser.add_argument("--json", dest="output_json", action="store_true", help="Print JSON output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = AppSettings.from_env()
    payload = run_chatgpt_callback_exchange(
        callback_url=str(args.callback_url or "").strip(),
        expected_state=str(args.state or "").strip(),
        code_verifier=str(args.code_verifier or "").strip(),
        proxy=str(args.proxy or "").strip() or None,
        write_pool=not args.no_write_pool,
        pool_dir=settings.pool_dir,
    )
    if args.output_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_callback_summary(payload)


if __name__ == "__main__":
    main()
