"""The actions the model can choose, and how each one is carried out."""
from __future__ import annotations

from typing import Literal, Optional

from playwright.sync_api import BrowserContext, ElementHandle, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from pydantic import BaseModel, Field

from nltest.observe import MESSAGE_TRACKER_SCRIPT, ElementRef, Observation

ACTION_TIMEOUT_MS = 8000
NAVIGATION_TIMEOUT_MS = 30000
SETTLE_TIMEOUT_MS = 3000
REACTION_PAUSE_MS = 400
MAX_WAIT_SECONDS = 15
KEY_DELAY_MS = 25
TEXT_TAGS = {"input", "textarea"}
FILL_ONLY_TYPES = {"date", "datetime-local", "month", "week", "time", "color", "range"}


class Evidence(BaseModel):
    check: str = Field(description="The check from the plan, word for word.")
    passed: bool = Field(description="Whether this check held true.")
    quote: str = Field(description="Exact text copied from the page or URL that proves this.")


class Decision(BaseModel):
    """The model's next move. Only the fields its action needs are filled in."""

    thought: str = Field(description="One short sentence on what you see and why you chose this action.")
    action: Literal[
        "goto", "click", "hover", "type", "select", "press", "scroll", "wait", "back",
        "switch_tab", "finish", "give_up",
    ]
    element: Optional[int] = Field(default=None, description="Element number, for click, hover, type and select.")
    text: Optional[str] = Field(
        default=None,
        description="Text for type, option for select, key for press, or text to wait for.",
    )
    url: Optional[str] = Field(default=None, description="Address for goto.")
    direction: Optional[Literal["up", "down"]] = Field(default=None, description="Direction for scroll.")
    tab: Optional[int] = Field(default=None, description="Tab number for switch_tab, starting at 1.")
    verdict: Optional[Literal["pass", "fail"]] = Field(default=None, description="Result, for finish.")
    evidence: list[Evidence] = Field(default_factory=list, description="One entry per check, for finish.")
    reason: Optional[str] = Field(default=None, description="Why the test can't continue, for give_up.")


class ActionError(Exception):
    """An action that failed. The message goes back to the model so it can try something else."""


class Browser:
    """Carries out decisions on the current page and keeps track of tabs and dialogs."""

    def __init__(self, context: BrowserContext, page: Page):
        self.context = context
        self.page = page
        self.dialogs: list[str] = []
        context.add_init_script(MESSAGE_TRACKER_SCRIPT)
        self._watch(page)
        context.on("page", self._watch)

    def _watch(self, page: Page) -> None:
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        page.on("dialog", self._accept_dialog)

    def _accept_dialog(self, dialog) -> None:
        self.dialogs.append(f"{dialog.type}: {dialog.message}")
        dialog.accept()

    def perform(self, decision: Decision, observation: Observation, secrets: dict[str, str]) -> str:
        """Runs one action and returns a description of what happened."""
        handlers = {
            "goto": self._goto,
            "click": self._click,
            "hover": self._hover,
            "type": self._type,
            "select": self._select,
            "press": self._press,
            "scroll": self._scroll,
            "wait": self._wait,
            "back": self._back,
            "switch_tab": self._switch_tab,
        }
        tabs_before = len(self.context.pages)
        try:
            outcome = handlers[decision.action](decision, observation, secrets)
        except PlaywrightTimeout as problem:
            raise ActionError(f"Timed out: {_first_line(problem)}") from problem
        except PlaywrightError as problem:
            raise ActionError(_first_line(problem)) from problem

        self._settle()
        if len(self.context.pages) > tabs_before:
            self.page = self.context.pages[-1]
            self._settle()
            outcome += " A new tab opened and is now active."
        return outcome

    def _settle(self) -> None:
        """Gives the page a moment to finish reacting, without failing if it stays busy."""
        self.page.wait_for_timeout(REACTION_PAUSE_MS)
        for state in ("domcontentloaded", "networkidle"):
            try:
                self.page.wait_for_load_state(state, timeout=SETTLE_TIMEOUT_MS)
            except PlaywrightError:
                pass

    def _goto(self, decision: Decision, _observation, _secrets) -> str:
        url = _required(decision.url, "goto needs a url")
        self.page.goto(url, wait_until="domcontentloaded")
        return f"Opened {self.page.url}."

    def _click(self, decision: Decision, observation: Observation, _secrets) -> str:
        ref = _element(decision, observation)
        _handle(ref).click()
        return f'Clicked [{ref.number}] "{ref.info["name"]}".'

    def _hover(self, decision: Decision, observation: Observation, _secrets) -> str:
        ref = _element(decision, observation)
        _handle(ref).hover()
        return f'Hovered over [{ref.number}] "{ref.info["name"]}".'

    def _type(self, decision: Decision, observation: Observation, secrets: dict[str, str]) -> str:
        ref = _element(decision, observation)
        text = _required(decision.text, "type needs text")
        handle = _handle(ref)
        value = fill_placeholders(text, secrets)
        if ref.info["type"] in FILL_ONLY_TYPES:
            handle.fill(value)
        elif ref.info["tag"] in TEXT_TAGS:
            # Real keystrokes, because autocomplete boxes often ignore pasted text.
            handle.fill("")
            handle.type(value, delay=KEY_DELAY_MS)
        else:
            # A styled box that opens a real input when clicked, as a person would use it.
            handle.click()
            self.page.keyboard.type(value, delay=KEY_DELAY_MS)
        return f'Typed "{text}" into [{ref.number}] "{ref.info["name"]}".'

    def _select(self, decision: Decision, observation: Observation, _secrets) -> str:
        ref = _element(decision, observation)
        option = _required(decision.text, "select needs an option in text")
        handle = _handle(ref)
        if ref.info["tag"] != "select":
            raise ActionError("select only works on a <select>. Click this element to open it instead.")
        try:
            handle.select_option(label=option, timeout=ACTION_TIMEOUT_MS)
        except PlaywrightTimeout:
            handle.select_option(value=option, timeout=ACTION_TIMEOUT_MS)
        return f'Selected "{option}" in [{ref.number}] "{ref.info["name"]}".'

    def _press(self, decision: Decision, _observation, _secrets) -> str:
        key = _required(decision.text, "press needs a key name in text, such as Enter")
        self.page.keyboard.press(key)
        return f"Pressed {key}."

    def _scroll(self, decision: Decision, _observation, _secrets) -> str:
        direction = decision.direction or "down"
        distance = 700 if direction == "down" else -700
        self.page.mouse.wheel(0, distance)
        return f"Scrolled {direction}."

    def _wait(self, decision: Decision, _observation, _secrets) -> str:
        if decision.text:
            limit = MAX_WAIT_SECONDS * 1000
            self.page.get_by_text(decision.text).first.wait_for(state="visible", timeout=limit)
            return f'"{decision.text}" appeared.'
        self.page.wait_for_timeout(3000)
        return "Waited 3 seconds."

    def _back(self, _decision, _observation, _secrets) -> str:
        self.page.go_back()
        return "Went back."

    def _switch_tab(self, decision: Decision, _observation, _secrets) -> str:
        pages = self.context.pages
        tab = decision.tab
        if tab is None or not 1 <= tab <= len(pages):
            raise ActionError(f"switch_tab needs a tab from 1 to {len(pages)}.")
        self.page = pages[tab - 1]
        self.page.bring_to_front()
        return f"Switched to tab {tab}."


def fill_placeholders(text: str, secrets: dict[str, str]) -> str:
    """Swaps {{name}} for its real value at the last moment before typing."""
    for name, value in secrets.items():
        text = text.replace("{{" + name + "}}", value)
    return text


def _required(value, message: str):
    if value in (None, ""):
        raise ActionError(message)
    return value


def _element(decision: Decision, observation: Observation) -> ElementRef:
    if decision.element is None:
        raise ActionError(f"{decision.action} needs an element number.")
    ref = observation.elements.get(decision.element)
    if ref is None:
        raise ActionError(f"Element {decision.element} doesn't exist on this page.")
    return ref


def _handle(ref: ElementRef) -> ElementHandle:
    handle = ref.frame.evaluate_handle("(index) => window.__nltestElements[index]", ref.index).as_element()
    if handle is None:
        raise ActionError(f"Element {ref.number} is no longer on the page.")
    return handle


def _first_line(problem: Exception) -> str:
    return str(problem).split("\n")[0][:300]
