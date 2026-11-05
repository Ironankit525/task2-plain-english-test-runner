"""Tests the whole loop in a real browser, with a scripted stand-in for the AI model."""
import json
from pathlib import Path

import pytest

from nltest.actions import Decision, Evidence
from nltest.agent import redact, run_test
from nltest.config import Settings
from nltest.llm import ModelError, Usage
from nltest.planner import Plan
from nltest.report import save_report
from nltest.verdict import ERROR, FAIL, PASS

SITE = (Path(__file__).parent / "site" / "login.html").resolve().as_uri()
CHECK = "A welcome message is shown"
SECRETS = {"customer_email": "shopper@example.com", "customer_password": "s3cret-pass"}


class ScriptedModel:
    """Plays back decisions in order, and records every prompt it was sent."""

    def __init__(self, decisions, checks=(CHECK,)):
        self.decisions = list(decisions)
        self.checks = list(checks)
        self.prompts = []
        self.usage = Usage()

    def generate(self, system, prompt, schema, image=None):
        self.prompts.append(prompt)
        if schema is Plan:
            return Plan(goal="Sign in", checks=self.checks)
        if not self.decisions:
            raise ModelError("The script ran out of decisions.")
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def act(action, **fields):
    return Decision(thought="Next step.", action=action, **fields)


def finish(verdict, quote, passed=True, check=CHECK):
    return act("finish", verdict=verdict, evidence=[Evidence(check=check, passed=passed, quote=quote)])


def element_number(prompt, name):
    for line in prompt.splitlines():
        if line.startswith("[") and f'"{name}"' in line:
            return int(line[1:line.index("]")])
    raise AssertionError(f"{name} isn't in the prompt")


class LoginModel(ScriptedModel):
    """Logs in by reading element numbers from the real prompt, then plays back the rest."""

    def __init__(self, email, password, ending):
        super().__init__([])
        self.queue = [("type", "Email", email), ("type", "Password", password), ("click", "Sign in", None)]
        self.ending = list(ending)

    def generate(self, system, prompt, schema, image=None):
        self.prompts.append(prompt)
        if schema is Plan:
            return Plan(goal="Sign in", checks=self.checks)
        if self.queue:
            action, name, text = self.queue.pop(0)
            return act(action, element=element_number(prompt, name), text=text)
        return self.ending.pop(0)


def run(model, tmp_path, **overrides):
    values = {"test_case": "Sign in and verify the welcome message", "url": SITE, "secrets": dict(SECRETS)}
    values.update(overrides)
    return run_test(Settings(**values), model, tmp_path)


def test_a_proven_result_passes(tmp_path):
    model = LoginModel("{{customer_email}}", "{{customer_password}}", [finish("pass", "Welcome back")])
    result = run(model, tmp_path)
    assert result.result == PASS, result.reason


def test_a_website_that_misbehaves_is_a_fail(tmp_path):
    failed = finish("fail", "Wrong password", passed=False)
    model = LoginModel("{{customer_email}}", "wrong", [failed, failed])
    assert run(model, tmp_path).result == FAIL


def test_made_up_evidence_can_never_pass(tmp_path):
    fake = finish("pass", "Your dashboard is ready")
    model = LoginModel("{{customer_email}}", "wrong", [fake, fake, fake])
    result = run(model, tmp_path)
    assert result.result == ERROR
    assert "could not be proven" in result.reason


def test_a_rejected_finish_can_recover_with_a_real_quote(tmp_path):
    model = LoginModel(
        "{{customer_email}}", "{{customer_password}}",
        [finish("pass", "Your dashboard is ready"), finish("pass", "Welcome back")],
    )
    result = run(model, tmp_path)
    assert result.result == PASS
    assert any("rejected" in prompt for prompt in model.prompts)


def test_a_quote_from_an_earlier_page_cant_prove_the_last_check(tmp_path):
    model = LoginModel("{{customer_email}}", "{{customer_password}}", [])
    model.ending = [
        act("goto", url="about:blank"),
        finish("pass", "Welcome back"),
        finish("pass", "Welcome back"),
        finish("pass", "Welcome back"),
    ]
    assert run(model, tmp_path).result == ERROR


def test_missing_a_check_can_not_pass(tmp_path):
    model = ScriptedModel([finish("pass", "Sign in")] * 3, checks=[CHECK, "A second thing happens"])
    assert run(model, tmp_path).result == ERROR


def test_a_verdict_that_contradicts_its_evidence_is_rejected(tmp_path):
    model = ScriptedModel([finish("pass", "Sign in", passed=False)] * 3)
    assert run(model, tmp_path).result == ERROR


def test_giving_up_is_an_error_with_the_reason(tmp_path):
    result = run(ScriptedModel([act("give_up", reason="A login is needed")]), tmp_path)
    assert result.result == ERROR
    assert result.reason == "A login is needed"


def test_repeating_an_action_forever_is_stopped(tmp_path):
    result = run(ScriptedModel([act("scroll", direction="up")] * 30), tmp_path)
    assert result.result == ERROR
    assert "Stuck" in result.reason


def test_the_step_limit_is_enforced(tmp_path):
    decisions = [act("wait"), act("scroll", direction="up"), act("press", text="Tab")]
    result = run(ScriptedModel(decisions), tmp_path, max_steps=3)
    assert result.result == ERROR
    assert "limit of 3 steps" in result.reason


def test_a_failed_action_is_reported_back_so_the_model_can_adjust(tmp_path):
    model = ScriptedModel([act("click", element=999), act("give_up", reason="Stopping")])
    run(model, tmp_path)
    assert "Element 999 doesn't exist" in model.prompts[-1]


def test_an_unreachable_site_is_an_error(tmp_path):
    result = run(ScriptedModel([]), tmp_path, url="http://localhost:9/nothing")
    assert result.result == ERROR
    assert "Could not open" in result.reason


def test_an_ai_outage_is_an_error_not_a_crash(tmp_path):
    result = run(ScriptedModel([ModelError("Everything is busy")]), tmp_path)
    assert result.result == ERROR


def test_secrets_never_reach_the_model_or_the_report(tmp_path):
    model = LoginModel("{{customer_email}}", "{{customer_password}}", [finish("pass", "Welcome back")])
    result = run(model, tmp_path)
    assert result.result == PASS

    save_report(result, "test", SITE, model.usage, tmp_path, SECRETS)
    everything = "\n".join(model.prompts) + (tmp_path / "report.json").read_text()
    for value in SECRETS.values():
        assert value not in everything
    assert "{{customer_email}}" in "\n".join(model.prompts)


def test_a_select_value_counts_as_evidence(tmp_path):
    class SelectModel(ScriptedModel):
        def generate(self, system, prompt, schema, image=None):
            self.prompts.append(prompt)
            if schema is Plan:
                return Plan(goal="Pick a size", checks=[CHECK])
            if len(self.prompts) == 2:
                return act("select", element=element_number(prompt, "Size"), text="Large")
            return finish("pass", "Large")

    assert run(SelectModel([]), tmp_path).result == PASS


def test_redact_replaces_longer_values_first():
    secrets = {"short": "abc", "long": "abcdef"}
    assert redact("abcdef", secrets) == "{{long}}"


def test_typing_uses_real_keystrokes(tmp_path):
    """Autocomplete boxes listen for key events, which pasted text doesn't send."""
    page = tmp_path / "keys.html"
    page.write_text(
        '<input aria-label="City" onkeydown="window.keys=(window.keys||0)+1;'
        "document.getElementById('n').textContent='Keys: '+window.keys\"><p id=\"n\">Keys: 0</p>"
    )

    class TypingModel(ScriptedModel):
        def generate(self, system, prompt, schema, image=None):
            self.prompts.append(prompt)
            if schema is Plan:
                return Plan(goal="Type", checks=[CHECK])
            if len(self.prompts) == 2:
                return act("type", element=element_number(prompt, "City"), text="dubai")
            return finish("pass", "Keys: 5")

    assert run(TypingModel([]), tmp_path, url=page.as_uri()).result == PASS


def test_an_unexpected_crash_is_an_error_and_never_a_fail(tmp_path):
    result = run(ScriptedModel([RuntimeError("A bug in the tool")]), tmp_path)
    assert result.result == ERROR
    assert "unexpected problem" in result.reason


def test_a_message_that_vanishes_can_be_read_and_used_as_evidence(tmp_path):
    page = tmp_path / "toast.html"
    page.write_text(
        '<button onclick="const t=document.createElement(\'div\');t.textContent=\'Please select a date\';'
        'document.body.append(t);setTimeout(()=>t.remove(),20)">Search</button>'
    )

    class ToastModel(ScriptedModel):
        def generate(self, system, prompt, schema, image=None):
            self.prompts.append(prompt)
            if schema is Plan:
                return Plan(goal="Search", checks=[CHECK])
            if len(self.prompts) == 2:
                return act("click", element=element_number(prompt, "Search"))
            return finish("fail", "Please select a date", passed=False)

    model = ToastModel([])
    assert run(model, tmp_path, url=page.as_uri()).result == FAIL
    assert any("already gone:\nPlease select a date" in prompt for prompt in model.prompts)


def test_typing_works_on_a_styled_box_that_opens_a_real_input(tmp_path):
    page = tmp_path / "box.html"
    page.write_text(
        '<div role="button" aria-label="Destination" '
        "onclick=\"document.getElementById('real').style.display='block';document.getElementById('real').focus()\">"
        'Destination</div><input id="real" style="display:none">'
    )

    class BoxModel(ScriptedModel):
        def generate(self, system, prompt, schema, image=None):
            self.prompts.append(prompt)
            if schema is Plan:
                return Plan(goal="Type", checks=[CHECK])
            if len(self.prompts) == 2:
                return act("type", element=element_number(prompt, "Destination"), text="dubai")
            return finish("pass", "dubai")

    assert run(BoxModel([]), tmp_path, url=page.as_uri()).result == PASS


def test_a_fail_is_looked_at_again_in_case_the_page_was_still_loading(tmp_path):
    page = tmp_path / "slow.html"
    page.write_text("<p>Cart</p><script>setTimeout(()=>document.body.append('Backpack'),2500)</script>")

    class SlowModel(ScriptedModel):
        def generate(self, system, prompt, schema, image=None):
            self.prompts.append(prompt)
            if schema is Plan:
                return Plan(goal="Check", checks=[CHECK])
            if "Backpack" in prompt.split("<page_content>")[1]:
                return finish("pass", "Backpack")
            return finish("fail", "Cart", passed=False)

    model = SlowModel([])
    assert run(model, tmp_path, url=page.as_uri()).result == PASS
    assert any("Check it again" in prompt for prompt in model.prompts)


def test_text_inside_page_code_can_not_be_used_as_evidence(tmp_path):
    page = tmp_path / "code.html"
    page.write_text("<p>Cart</p><script>const secretWord = 'Order confirmed';</script><style>/* Order shipped */</style>")
    fake = finish("pass", "Order confirmed")
    result = run(ScriptedModel([fake, fake, fake]), tmp_path, url=page.as_uri())
    assert result.result == ERROR
