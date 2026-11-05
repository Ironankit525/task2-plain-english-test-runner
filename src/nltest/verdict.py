"""Decides the final result. The model's claims only count once code has confirmed them."""
from __future__ import annotations

import re
from dataclasses import dataclass

from nltest.actions import Decision, Evidence, fill_placeholders

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"

EXIT_CODES = {PASS: 0, FAIL: 1, ERROR: 2}


@dataclass(frozen=True)
class EvidenceReview:
    """Whether a finish decision can be accepted, and if not, what's wrong with it."""

    result: str | None
    problems: list[str]

    @property
    def accepted(self) -> bool:
        return self.result is not None


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def found_in(quote: str, seen_texts: list[str]) -> bool:
    target = normalise(quote)
    return bool(target) and any(target in normalise(text) for text in seen_texts)


def review(
    decision: Decision,
    checks: list[str],
    seen_texts: list[str],
    current_text: str,
    secrets: dict[str, str],
) -> EvidenceReview:
    """Compares a finish decision with what the browser really showed during this run.

    seen_texts and current_text come from the browser, never from the model, so a quote the
    model made up can't be found in them. PASS needs every check covered, passed and proven.
    Earlier checks may be proven by an earlier page, such as "the cart shows 1" before the
    item is removed. The last check must hold on the page the run ends on.
    """
    problems = []

    if decision.verdict not in ("pass", "fail"):
        problems.append("finish needs a verdict of pass or fail.")

    for check in checks:
        if not _evidence_for(check, decision.evidence):
            problems.append(f'No evidence was given for the check: "{check}".')

    for evidence in decision.evidence:
        quote = fill_placeholders(evidence.quote, secrets)
        if not found_in(quote, seen_texts):
            problems.append(f'The quote "{evidence.quote}" was not found on the page. Quote the page exactly.')

    last_check = next((item for item in decision.evidence if normalise(item.check) == normalise(checks[-1])), None)
    if decision.verdict == "pass" and last_check is not None:
        quote = fill_placeholders(last_check.quote, secrets)
        if found_in(quote, seen_texts) and not found_in(quote, [current_text]):
            problems.append(f'The quote "{last_check.quote}" isn\'t on the current page, so the last check isn\'t proven.')

    all_passed = bool(decision.evidence) and all(evidence.passed for evidence in decision.evidence)
    if decision.verdict == "pass" and not all_passed:
        problems.append("The verdict is pass, but at least one check did not pass.")
    if decision.verdict == "fail" and all_passed:
        problems.append("The verdict is fail, but every check passed.")

    if problems:
        return EvidenceReview(None, problems)
    return EvidenceReview(PASS if decision.verdict == "pass" else FAIL, [])


def _evidence_for(check: str, evidence: list[Evidence]) -> bool:
    return any(normalise(item.check) == normalise(check) for item in evidence)
