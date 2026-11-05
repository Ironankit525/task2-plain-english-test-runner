"""Runs examples/suite.json and prints how each result compares with the expected one.

Usage: uv run --env-file .env python scripts/run_suite.py [--repeat 3] [--only 5,6] [--credentials file]
Cases that need a login are skipped when --credentials isn't given.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from nltest.agent import run_test
from nltest.config import Settings, load_credentials, models_from
from nltest.llm import build_model
from nltest.report import save_report

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", help="Comma-separated case ids")
    parser.add_argument("--credentials")
    parser.add_argument("--model")
    parser.add_argument("--provider", default="auto")
    args = parser.parse_args()

    secrets = load_credentials(args.credentials)
    cases = json.loads((ROOT / "examples" / "suite.json").read_text(encoding="utf-8"))
    if args.only:
        wanted = {int(number) for number in args.only.split(",")}
        cases = [case for case in cases if case["id"] in wanted]

    suite_dir = ROOT / "runs" / f"suite-{datetime.now():%Y%m%d-%H%M%S}"
    rows = []
    print(f"{'ID':<4}{'RUN':<5}{'EXPECTED':<10}{'ACTUAL':<8}{'STEPS':<7}{'SECONDS':<9}MATCH", flush=True)

    for round_number in range(1, args.repeat + 1):
        for case in cases:
            if case.get("needs_credentials") and not secrets:
                print(f"{case['id']:<4}{round_number:<5}skipped: needs --credentials", flush=True)
                continue

            run_dir = suite_dir / f"case-{case['id']:02d}-run-{round_number}"
            # Only logins a case needs are offered, so case 12 really has none.
            settings = Settings(
                test_case=case["test"],
                url=case["url"],
                secrets=secrets if case.get("needs_credentials") else {},
            )
            model = build_model(models_from(args.model), args.provider, args.model)
            result = run_test(settings, model, run_dir)
            save_report(result, case["test"], case["url"], model.usage, run_dir, settings.secrets)

            matched = result.result == case["expected"]
            rows.append({
                "id": case["id"], "run": round_number, "expected": case["expected"], "actual": result.result,
                "matched": matched, "steps": len(result.steps), "seconds": result.seconds,
                "reason": result.reason, "usage": model.usage.as_dict(),
            })
            mark = "ok" if matched else f"MISMATCH: {result.reason[:110]}"
            print(
                f"{case['id']:<4}{round_number:<5}{case['expected']:<10}{result.result:<8}"
                f"{len(result.steps):<7}{result.seconds:<9}{mark}",
                flush=True,
            )

    false_passes = sum(row["actual"] == "PASS" and row["expected"] != "PASS" for row in rows)
    matched_count = sum(row["matched"] for row in rows)
    print(f"\n{matched_count} of {len(rows)} runs gave the expected result. False PASS results: {false_passes}.")
    print(f"AI calls: {sum(row['usage']['calls'] for row in rows)}, "
          f"input tokens: {sum(row['usage']['input_tokens'] for row in rows)}, "
          f"output tokens: {sum(row['usage']['output_tokens'] for row in rows)}.")

    suite_dir.mkdir(parents=True, exist_ok=True)
    (suite_dir / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0 if matched_count == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
