"""Console output and the saved JSON report."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from nltest.agent import RunResult, Step, redact
from nltest.llm import Usage
from nltest.planner import Plan


def print_plan(plan: Plan) -> None:
    print(f"Goal: {plan.goal}")
    print("Checks:")
    for check in plan.checks:
        print(f"  - {check}")
    print()


def print_step(step: Step) -> None:
    marker = " " if step.succeeded else "!"
    print(f"{marker}{step.number:>3}. {step.action}")
    print(f"      {step.outcome}")


def print_result(result: RunResult, usage: Usage, run_dir: Path, secrets: dict[str, str]) -> None:
    print()
    for item in result.evidence:
        status = "PASS" if item.passed else "FAIL"
        print(f"[{status}] {redact(item.check, secrets)}")
        print(f'       Evidence: "{redact(item.quote, secrets)}"')
    print(f"\nResult: {result.result}")
    print(f"Reason: {redact(result.reason, secrets)}")
    print(f"Steps: {len(result.steps)}, time: {result.seconds}s, AI calls: {usage.calls}")
    print(f"Report: {run_dir / 'report.json'}")


def save_report(
    result: RunResult,
    test_case: str,
    url: str | None,
    usage: Usage,
    run_dir: Path,
    secrets: dict[str, str],
) -> None:
    report = {
        "test_case": test_case,
        "url": url,
        "result": result.result,
        "reason": result.reason,
        "goal": result.plan.goal if result.plan else None,
        "checks": result.plan.checks if result.plan else [],
        "evidence": [item.model_dump() for item in result.evidence],
        "steps": [asdict(step) for step in result.steps],
        "final_url": result.final_url,
        "seconds": result.seconds,
        "usage": usage.as_dict(),
    }
    text = redact(json.dumps(report, indent=2, ensure_ascii=False), secrets)
    (run_dir / "report.json").write_text(text, encoding="utf-8")
