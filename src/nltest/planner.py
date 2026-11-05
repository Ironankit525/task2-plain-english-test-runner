"""Turns the test sentence into a goal and a list of things to verify."""
from __future__ import annotations

from pydantic import BaseModel, Field

from nltest.llm import Model

SYSTEM = """You turn a test case written in plain English into a short plan.

- goal: what the tester has to do, in one sentence.
- checks: the outcomes to verify at the end, each one observable on the page.

Only include outcomes the test case states. If it states none, use the one outcome its final action implies. Keep the test case's meaning exactly, even if it seems likely to fail. Never add checks of your own, and never change what the test case expects."""


class Plan(BaseModel):
    goal: str
    checks: list[str] = Field(min_length=1)


def make_plan(model: Model, test_case: str) -> Plan:
    return model.generate(SYSTEM, f"Test case: {test_case}", Plan)
