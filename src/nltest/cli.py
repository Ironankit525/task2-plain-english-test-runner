"""Command line entry point."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from nltest.agent import run_test
from nltest.config import ConfigError, Settings, load_credentials, models_from
from nltest.llm import ModelError, build_model
from nltest.report import print_plan, print_result, print_step, save_report
from nltest.verdict import ERROR, EXIT_CODES


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="nltest",
        description="Runs a test case written in plain English in a real browser.",
        epilog="Exit codes: 0 PASS, 1 FAIL, 2 ERROR (the test couldn't be carried out).",
    )
    parser.add_argument("test_case", help='For example: "Log in and verify the dashboard is shown"')
    parser.add_argument("--url", help="Page to start on. Can be left out if the test case names a full address.")
    parser.add_argument("--credentials", help="JSON file of named logins. See credentials.example.json.")
    parser.add_argument("--provider", choices=["auto", "gemini", "openai"], default="auto",
                        help="auto tries Gemini, then the OpenAI-compatible service (default: auto)")
    parser.add_argument("--model", help="Model to try first. Also read from NLTEST_MODEL.")
    parser.add_argument("--max-steps", type=int, default=25, help="Most steps to take (default: 25)")
    parser.add_argument("--time-limit", type=int, default=600, help="Most seconds to run (default: 600)")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--output", default="runs", help="Folder for reports (default: runs)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        secrets = load_credentials(args.credentials)
        model = build_model(models_from(args.model), args.provider, args.model)
    except (ConfigError, ModelError) as problem:
        print(problem, file=sys.stderr)
        return EXIT_CODES[ERROR]

    settings = Settings(
        test_case=args.test_case,
        url=args.url,
        secrets=secrets,
        max_steps=args.max_steps,
        time_limit_seconds=args.time_limit,
        headed=args.headed,
        output_dir=Path(args.output),
    )
    run_dir = settings.output_dir / datetime.now().strftime("%Y%m%d-%H%M%S")

    print(f"Test: {settings.test_case}\n")
    result = run_test(settings, model, run_dir, on_step=print_step, on_plan=print_plan)

    save_report(result, settings.test_case, settings.url, model.usage, run_dir, secrets)
    print_result(result, model.usage, run_dir, secrets)
    return EXIT_CODES[result.result]


if __name__ == "__main__":
    sys.exit(main())
