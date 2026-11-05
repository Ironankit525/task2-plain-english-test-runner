"""Builds a compact, numbered summary of the page for the AI model to read."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Page

MAX_ELEMENTS_PER_FRAME = 250
MAX_TEXT_CHARS = 8000
MAX_BRIEF_MESSAGES = 12

# Runs before each page's own scripts. Keeps short text as it's added, because toast messages
# such as "Please select a date" can vanish before the next look at the page.
MESSAGE_TRACKER_SCRIPT = r"""
(() => {
  const MAX_MESSAGE_LENGTH = 160;
  const MAX_MESSAGES = 40;
  window.__nltestMessages = [];

  const keep = (text) => {
    const message = (text || '').replace(/\s+/g, ' ').trim();
    if (message && message.length <= MAX_MESSAGE_LENGTH) {
      window.__nltestMessages.push(message);
      window.__nltestMessages.splice(0, window.__nltestMessages.length - MAX_MESSAGES);
    }
  };

  // Page code isn't something a person can read, so it must never count as a message.
  const NOT_DISPLAYED = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|HEAD|TITLE|META|LINK)$/;
  const isDisplayable = (node) => {
    const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    return element !== null && !NOT_DISPLAYED.test(element.nodeName) && !element.closest('head,script,style,noscript,template');
  };

  new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === 'characterData' && isDisplayable(mutation.target)) keep(mutation.target.nodeValue);
      for (const node of mutation.addedNodes) {
        if (isDisplayable(node)) keep(node.innerText || node.textContent);
      }
    }
  }).observe(document, { childList: true, subtree: true, characterData: true });
})();
"""

# Runs in each frame. Numbers every interactive element and keeps a reference to it in
# window.__nltestElements, so an action can find exactly the element the model chose.
SNAPSHOT_SCRIPT = r"""
(maxElements) => {
  const clean = (text) => (text || '').replace(/\s+/g, ' ').trim();
  const short = (text, limit) => { const t = clean(text); return t.length > limit ? t.slice(0, limit) + '...' : t; };

  const INTERACTIVE = 'a[href],button,input,select,textarea,summary,[contenteditable="true"],[onclick],' +
    '[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],' +
    '[role=combobox],[role=textbox],[role=switch],[role=searchbox],[tabindex]:not([tabindex="-1"])';

  function allRoots(root, found) {
    found.push(root);
    root.querySelectorAll('*').forEach((element) => { if (element.shadowRoot) allRoots(element.shadowRoot, found); });
    return found;
  }

  function isShown(element) {
    if (element.checkVisibility && !element.checkVisibility({ checkOpacity: false, checkVisibilityCSS: true })) return false;
    const box = element.getBoundingClientRect();
    return box.width > 1 && box.height > 1;
  }

  function inViewport(element) {
    const box = element.getBoundingClientRect();
    return box.bottom > 0 && box.right > 0 && box.top < innerHeight && box.left < innerWidth;
  }

  // Clickable by styling only, such as a <div> or <li> used as a menu item.
  function looksClickable(element) {
    if (getComputedStyle(element).cursor !== 'pointer') return false;
    const parent = element.parentElement;
    if (parent && getComputedStyle(parent).cursor === 'pointer') return false;
    return clean(element.innerText).length > 0;
  }

  function nameOf(element) {
    const labels = element.labels ? Array.from(element.labels).map((label) => label.innerText).join(' ') : '';
    const labelledBy = (element.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map((id) => { const target = element.getRootNode().getElementById?.(id); return target ? target.innerText : ''; }).join(' ');
    const image = element.querySelector ? element.querySelector('img[alt]') : null;
    return short(element.getAttribute('aria-label') || labelledBy || labels || element.innerText ||
      element.getAttribute('placeholder') || element.getAttribute('title') || (image && image.alt) ||
      element.getAttribute('name') || '', 90);
  }

  function describe(element) {
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute('type') || '').toLowerCase();
    const info = { tag, type, role: element.getAttribute('role') || '', name: nameOf(element), inViewport: inViewport(element) };
    const hint = element.getAttribute('placeholder');
    if (hint && hint !== info.name) info.placeholder = short(hint, 60);
    if (element.disabled || element.getAttribute('aria-disabled') === 'true') info.disabled = true;
    if (element.checked || element.getAttribute('aria-checked') === 'true') info.checked = true;
    if (element.getAttribute('aria-expanded')) info.expanded = element.getAttribute('aria-expanded');
    if (element.getAttribute('aria-selected') === 'true') info.selected = true;
    if (tag === 'a') info.href = element.getAttribute('href') || '';
    if (tag === 'select') {
      const options = Array.from(element.options);
      info.options = options.slice(0, 40).map((option) => short(option.text, 40));
      info.optionCount = options.length;
      info.value = element.selectedIndex >= 0 ? short(options[element.selectedIndex].text, 60) : '';
    } else if ((tag === 'input' || tag === 'textarea') && !['checkbox', 'radio'].includes(type)) {
      // Password values are never read, so they can't reach the model or the logs.
      info.value = type === 'password' ? (element.value ? '(filled)' : '') : short(element.value, 80);
    }
    return info;
  }

  // Text of the page including shadow DOM, which innerText alone leaves out.
  function pageText(roots) {
    const parts = [document.body ? document.body.innerText : ''];
    roots.slice(1).forEach((root) => {
      const text = Array.from(root.children).filter((c) => !['STYLE', 'SCRIPT'].includes(c.tagName))
        .map((child) => child.innerText || '').join('\n');
      if (text) parts.push(text);
    });
    return parts.join('\n');
  }

  // Current values of the controls, so a selected or typed value can be used as evidence.
  function controlValues(roots) {
    const values = [];
    roots.forEach((root) => root.querySelectorAll('input,select,textarea').forEach((element) => {
      const type = (element.getAttribute('type') || '').toLowerCase();
      if (type === 'password' || type === 'hidden') return;
      if (element.tagName === 'SELECT') {
        if (element.selectedIndex >= 0) values.push(element.options[element.selectedIndex].text);
      } else if (['checkbox', 'radio'].includes(type)) {
        if (element.checked) values.push(element.value);
      } else if (element.value) {
        values.push(element.value);
      }
    }));
    return values;
  }

  // Hands over the messages kept since the last look, so each is only reported once.
  function takeMessages() {
    const messages = window.__nltestMessages || [];
    window.__nltestMessages = [];
    return messages;
  }

  const roots = allRoots(document, []);
  const seen = new Set();
  const found = [];
  roots.forEach((root) => {
    root.querySelectorAll('*').forEach((element) => {
      if (seen.has(element) || !isShown(element)) return;
      if (element.matches(INTERACTIVE) || looksClickable(element)) {
        seen.add(element);
        found.push(element);
      }
    });
  });

  // Elements on screen come first, so anything left out is the part furthest away.
  found.sort((a, b) => Number(inViewport(b)) - Number(inViewport(a)));
  const kept = found.slice(0, maxElements);
  window.__nltestElements = kept;

  return {
    elements: kept.map(describe),
    omitted: found.length - kept.length,
    text: pageText(roots),
    values: controlValues(roots),
    messages: takeMessages(),
  };
}
"""


@dataclass(frozen=True)
class ElementRef:
    """One numbered element, and how to find it again."""

    number: int
    frame: Frame
    index: int
    info: dict


@dataclass
class Observation:
    url: str
    title: str
    elements: dict[int, ElementRef]
    text: str
    values: list[str]
    omitted: int = 0
    dialogs: list[str] = field(default_factory=list)
    brief_messages: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        """Changes when the page changes, for spotting a run that's stuck."""
        content = self.url + self.text + "|".join(self.values)
        return hashlib.sha256(content.encode()).hexdigest()

    @property
    def evidence_text(self) -> str:
        """Everything a claim about the page may be checked against."""
        return "\n".join([self.url, self.title, self.text, *self.values, *self.dialogs, *self.brief_messages])


def observe(page: Page, dialogs: list[str] | None = None) -> Observation:
    elements: dict[int, ElementRef] = {}
    texts: list[str] = []
    values: list[str] = []
    messages: list[str] = []
    omitted = 0

    for frame in page.frames:
        snapshot = _snapshot_frame(frame)
        if snapshot is None:
            continue
        for index, info in enumerate(snapshot["elements"]):
            number = len(elements) + 1
            elements[number] = ElementRef(number, frame, index, info)
        texts.append(snapshot["text"])
        values.extend(snapshot["values"])
        messages.extend(snapshot["messages"])
        omitted += snapshot["omitted"]

    text = "\n".join(item for item in texts if item.strip())
    return Observation(
        url=page.url,
        title=_title(page),
        elements=elements,
        text=text,
        values=values,
        omitted=omitted,
        dialogs=list(dialogs or []),
        brief_messages=_vanished(messages, text),
    )


def _vanished(messages: list[str], current_text: str) -> list[str]:
    """Messages that have already gone from the page, newest last, without repeats."""
    visible = " ".join(current_text.split())
    gone = [message for message in dict.fromkeys(messages) if message not in visible]
    return gone[-MAX_BRIEF_MESSAGES:]


def _snapshot_frame(frame: Frame) -> dict | None:
    try:
        return frame.evaluate(SNAPSHOT_SCRIPT, MAX_ELEMENTS_PER_FRAME)
    except PlaywrightError:
        # A frame that's navigating or has gone away can't be read this time.
        return None


def _title(page: Page) -> str:
    try:
        return page.title()
    except PlaywrightError:
        return ""


def render_for_model(observation: Observation) -> str:
    """The text the model reads. Secrets are removed by the caller."""
    lines = [f"URL: {observation.url}", f"Title: {observation.title}", "", "Interactive elements:"]
    lines += [_render_element(ref) for ref in observation.elements.values()] or ["(none)"]

    if observation.omitted:
        lines.append(f"({observation.omitted} more elements aren't listed. Scroll to bring them into view.)")

    text = re.sub(r"\n\s*\n+", "\n", observation.text).strip()
    if len(text) > MAX_TEXT_CHARS:
        hidden = len(text) - MAX_TEXT_CHARS
        text = f"{text[:MAX_TEXT_CHARS]}\n({hidden} more characters of page text aren't shown.)"

    lines += ["", "Visible text:", text or "(none)"]
    if observation.brief_messages:
        lines += ["", "Text that appeared after the last step and has already gone:", *observation.brief_messages]
    if observation.dialogs:
        lines += ["", "Browser dialogs shown so far:", *observation.dialogs]
    return "\n".join(lines)


def _render_element(ref: ElementRef) -> str:
    info = ref.info
    kind = info["role"] or info["tag"]
    if info["type"] and info["type"] != kind:
        kind = f"{kind} {info['type']}"

    parts = [f'[{ref.number}] {kind} "{info["name"]}"']
    if info.get("placeholder"):
        parts.append(f'placeholder="{info["placeholder"]}"')
    if info.get("value"):
        parts.append(f'value="{info["value"]}"')
    if info.get("href"):
        parts.append(f'href="{info["href"][:80]}"')
    if info.get("options"):
        extra = info["optionCount"] - len(info["options"])
        suffix = f" and {extra} more" if extra > 0 else ""
        parts.append(f"options={info['options']}{suffix}")
    for flag in ("disabled", "checked", "selected"):
        if info.get(flag):
            parts.append(flag)
    if info.get("expanded"):
        parts.append(f"expanded={info['expanded']}")
    if not info["inViewport"]:
        parts.append("(off screen)")
    return " ".join(parts)
