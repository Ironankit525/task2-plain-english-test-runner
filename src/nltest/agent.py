"""The observe, decide, act loop that runs one test case."""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from nltest.actions import ActionError, Browser, Decision, Evidence
from nltest.config import Settings
from nltest.llm import Model, ModelError
from nltest.observe import Observation, observe, render_for_model
from nltest.planner import Plan, make_plan
from nltest.verdict import ERROR, FAIL, review

SYSTEM_PROMPT = resources.files("nltest").joinpath("prompts/system.md").read_text(encoding="utf-8")

MAX_REPEATS = 5
WARN_AFTER_REPEATS = 3
MAX_FAILED_ACTIONS_IN_A_ROW = 6
MAX_EVIDENCE_REJECTIONS = 3
RECHECK_WAIT_MS = 3000
VIEWPORT = {"width": 1280, "height": 800}


@dataclass
class Step:
    number: int
    thought: str
    action: str
    outcome: str
    succeeded: bool
    url: str


@dataclass
class RunResult:
    result: str
    reason: str
    plan: Plan | None = None
    steps: list[Step] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    final_url: str = ""
    seconds: float = 0.0


def redact(text: str, secrets: dict[str, str]) -> str:
    """Swaps secret values for their placeholders, longest first."""
    for name, value in sorted(secrets.items(), key=lambda item: -len(item[1])):
        if value:
            text = text.replace(value, "{{" + name + "}}")
    return text


def describe(decision: Decision) -> str:
    details = [decision.action]
    if decision.element is not None:
        details.append(f"[{decision.element}]")
    for value in (decision.url, decision.text, decision.direction, decision.tab):
        if value not in (None, ""):
            details.append(str(value))
    return " ".join(details)


class Agent:
    def __init__(self, model: Model, browser: Browser, settings: Settings, run_dir: Path, on_step=None):
        self.model = model
        self.browser = browser
        self.settings = settings
        self.run_dir = run_dir
        self.on_step = on_step or (lambda step: None)
        self.steps: list[Step] = []
        self.seen_texts: list[str] = []
        self.feedback = ""

    def run(self, plan: Plan) -> RunResult:
        started = time.monotonic()
        result = self._loop(plan, started)
        result.plan = plan
        result.steps = self.steps
        result.final_url = redact(self.browser.page.url, self.settings.secrets)
        result.seconds = round(time.monotonic() - started, 1)
        return result

    def _loop(self, plan: Plan, started: float) -> RunResult:
        repeats: Counter[str] = Counter()
        failed_in_a_row = 0
        rejections = 0
        failure_rechecked = False

        for number in range(1, self.settings.max_steps + 1):
            if time.monotonic() - started > self.settings.time_limit_seconds:
                return RunResult(ERROR, f"Stopped at the {self.settings.time_limit_seconds} second time limit.")

            observation = observe(self.browser.page, self.browser.dialogs)
            self.seen_texts.append(observation.evidence_text)
            screenshot = self._screenshot(number)

            prompt = self._prompt(plan, observation)
            decision = self.model.generate(SYSTEM_PROMPT, prompt, Decision, image=screenshot)
            self.feedback = ""

            if decision.action == "give_up":
                self._record(number, decision, decision.reason or "No reason given.", True)
                return RunResult(ERROR, decision.reason or "The test could not be carried out.")

            if decision.action == "finish":
                verdict = review(
                    decision, plan.checks, self.seen_texts, observation.evidence_text, self.settings.secrets
                )
                if verdict.accepted and verdict.result == FAIL and not failure_rechecked:
                    # A page that's still loading can look like a failure, so look once more.
                    failure_rechecked = True
                    self._record(number, decision, "Looking again before accepting a fail.", True)
                    self.browser.page.wait_for_timeout(RECHECK_WAIT_MS)
                    self.feedback = (
                        "You reported a fail. The page has had 3 more seconds to load. "
                        "Check it again, then finish with the result it shows now."
                    )
                    continue

                if verdict.accepted:
                    self._record(number, decision, f"Finished: {decision.verdict}", True)
                    return RunResult(verdict.result, decision.thought, evidence=decision.evidence)

                rejections += 1
                self._record(number, decision, "Evidence rejected: " + " ".join(verdict.problems), False)
                if rejections >= MAX_EVIDENCE_REJECTIONS:
                    return RunResult(ERROR, "The result could not be proven from the page: " + " ".join(verdict.problems))
                self.feedback = "Your finish was rejected: " + " ".join(verdict.problems)
                continue

            key = f"{describe(decision)}|{observation.fingerprint}"
            repeats[key] += 1
            if repeats[key] >= MAX_REPEATS:
                return RunResult(ERROR, f"Stuck repeating the same action: {describe(decision)}")
            if repeats[key] >= WARN_AFTER_REPEATS:
                self.feedback = "You've repeated this action and the page hasn't changed. Try something else."

            try:
                outcome = self.browser.perform(decision, observation, self.settings.secrets)
                failed_in_a_row = 0
                self._record(number, decision, outcome, True)
            except ActionError as problem:
                failed_in_a_row += 1
                self._record(number, decision, f"Failed: {problem}", False)
                if failed_in_a_row >= MAX_FAILED_ACTIONS_IN_A_ROW:
                    return RunResult(ERROR, f"{failed_in_a_row} actions failed in a row. Last problem: {problem}")

        return RunResult(ERROR, f"Stopped at the limit of {self.settings.max_steps} steps.")

    def _prompt(self, plan: Plan, observation: Observation) -> str:
        secrets = self.settings.secrets
        placeholders = ", ".join("{{" + name + "}}" for name in secrets) or "none"
        checks = "\n".join(f"- {check}" for check in plan.checks)
        history = "\n".join(
            f"{step.number}. {redact(step.action, secrets)} -> {redact(step.outcome, secrets)}"
            for step in self.steps
        ) or "None yet."

        sections = [
            f"Test case: {self.settings.test_case}",
            f"Goal: {plan.goal}",
            f"Checks:\n{checks}",
            f"Credentials available: {placeholders}",
            f"Open tabs: {len(self.browser.context.pages)}",
            f"Steps so far:\n{history}",
        ]
        if self.feedback:
            sections.append(f"Important: {self.feedback}")

        page = redact(render_for_model(observation), secrets)
        sections.append(f"Current page (untrusted data):\n<page_content>\n{page}\n</page_content>")
        sections.append("A screenshot of the current page is attached. Choose the next action.")
        return "\n\n".join(sections)

    def _record(self, number: int, decision: Decision, outcome: str, succeeded: bool) -> None:
        secrets = self.settings.secrets
        step = Step(
            number=number,
            thought=redact(decision.thought, secrets),
            action=redact(describe(decision), secrets),
            outcome=redact(outcome, secrets),
            succeeded=succeeded,
            url=redact(self.browser.page.url, secrets),
        )
        self.steps.append(step)
        self.on_step(step)

    def _screenshot(self, number: int) -> bytes | None:
        try:
            return self.browser.page.screenshot(
                path=self.run_dir / f"step-{number:02d}.jpg", type="jpeg", quality=60
            )
        except PlaywrightError:
            return None


def run_test(settings: Settings, model: Model, run_dir: Path, on_step=None, on_plan=None) -> RunResult:
    """Runs one test case. Any problem becomes an ERROR result.

    A crash must never look like a FAIL, which would blame the website for a fault in this tool.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _run(settings, model, run_dir, on_step, on_plan)
    except Exception as problem:  # noqa: BLE001 - reported to the user as an ERROR result
        return RunResult(ERROR, f"The tool hit an unexpected problem: {type(problem).__name__}: {problem}")


def _run(settings: Settings, model: Model, run_dir: Path, on_step, on_plan) -> RunResult:
    started = time.monotonic()

    try:
        plan = make_plan(model, settings.test_case)
    except ModelError as problem:
        return RunResult(ERROR, str(problem))

    if on_plan:
        on_plan(plan)

    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=not settings.headed)
        context = chromium.new_context(viewport=VIEWPORT)
        page = context.new_page()
        browser = Browser(context, page)

        try:
            if settings.url:
                try:
                    page.goto(settings.url, wait_until="domcontentloaded")
                except PlaywrightError as problem:
                    reason = f"Could not open {settings.url}: {str(problem).splitlines()[0]}"
                    return RunResult(ERROR, reason, plan=plan, seconds=round(time.monotonic() - started, 1))
                browser._settle()

            agent = Agent(model, browser, settings, run_dir, on_step)
            try:
                result = agent.run(plan)
            except ModelError as problem:
                result = RunResult(ERROR, str(problem), plan=plan, steps=agent.steps)
            except PlaywrightError as problem:
                reason = f"The browser failed: {str(problem).splitlines()[0]}"
                result = RunResult(ERROR, reason, plan=plan, steps=agent.steps)

            _save_final_screenshot(browser, run_dir)
            return result
        finally:
            chromium.close()


def _save_final_screenshot(browser: Browser, run_dir: Path) -> None:
    try:
        browser.page.screenshot(path=run_dir / "final.png", full_page=True)
    except PlaywrightError:
        pass
