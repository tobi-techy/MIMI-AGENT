"""The guardrails, all in code and all pure.

Nothing here reads the environment, the model or the network, so every rule is
directly testable. The runner calls these; the engine never decides.

The rules, from the brief:

* the approved domain and its subdomains only, anything else aborts ``off_domain``;
* a login wall pauses the task ``awaiting_owner`` and nothing is typed;
* a checkout, a card field, a messaging box or an account-settings page aborts
  the task ``forbidden_page``;
* a form is filled only with the owner's own values, never a credential;
* a step note never carries a credential, an email, a phone number or a long
  digit run.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from mimi.computer.types import GuardLimits

# Words that mark a login wall. A password input is the strong signal.
_LOGIN_TEXT = ("sign in", "log in", "login", "sign-in", "enter your password",
               "one-time code", "one time code", "verification code", "2fa",
               "two-factor", "authenticator")
_LOGIN_INPUT = re.compile(
    r"""<input[^>]*\btype\s*=\s*["']?password\b"""
    r"""|<input[^>]*\bautocomplete\s*=\s*["']?(current-password|one-time-code)\b""",
    re.IGNORECASE)

# Signals that a page is one we must never act on.
_FORBIDDEN_TEXT = (
    "place order", "buy now", "add to cart", "proceed to checkout", "checkout",
    "confirm payment", "card number", "cvv", "expiry date", "send message",
    "send email", "compose message", "account settings", "change password",
    "delete account", "transfer funds", "make payment",
)
_FORBIDDEN_INPUT = re.compile(
    r"""<input[^>]*\bautocomplete\s*=\s*["']?cc-(number|csc|exp)\b"""
    r"""|<input[^>]*\bname\s*=\s*["']?(cardnumber|card-number|cvv|cvc|pan|expiry)["']?"""
    r"""|<textarea[^>]*\bname\s*=\s*["']?(message|body|comment|reply)["']?""",
    re.IGNORECASE)

# Field labels a credential would live in. A fill against one is refused.
# Matched as whole words: "Shipping address" and "Pincode" carry the letters of
# "pin" but are not a field a credential goes in, and refusing them would
# refuse an ordinary government form.
_CREDENTIAL_WORDS = ("password", "passcode", "pin", "otp", "one-time", "one time",
                     "token", "cvv", "cvc", "card number", "cardnumber",
                     "security code", "secret", "answer")
_CREDENTIAL_FIELD = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(w) for w in _CREDENTIAL_WORDS)
    + r")s?(?![a-z0-9])", re.IGNORECASE)

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\d])(?:\+?\d[\d\s().-]{8,}\d)(?![\d])")
_DIGITS = re.compile(r"(?<![\d])\d{12,}(?![\d])")


class GuardrailError(RuntimeError):
    """A step refused before it was dispatched."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def redact(text: str) -> str:
    """A step note with anything sensitive taken out.

    Emails, phone numbers and long digit runs (a card number is 13 to 19
    digits) are replaced. Prices stay: a task note may quote a price list.
    """
    out = _EMAIL.sub("[email]", text or "")
    out = _PHONE.sub("[number]", out)
    out = _DIGITS.sub("[number]", out)
    return out


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def host_allowed(url: str, domain: str) -> bool:
    """The approved domain and its subdomains only.

    An empty domain allows nothing. IPv4/IPv6 literals are never allowed, and
    the scheme must be http or https: a ``file:`` or ``javascript:`` target is
    not a page on the approved domain.
    """
    if not domain:
        return False
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or is_ip(host):
        return False
    want = domain.strip().lower().rstrip(".")
    if want.startswith("."):
        want = want[1:]
    return host == want or host.endswith("." + want)


@dataclass(frozen=True)
class Page:
    """What the engine saw: enough for the guardrails and the step log."""

    url: str = ""
    title: str = ""
    html: str = ""
    text: str = ""
    screenshot: bytes = b""
    # The provider's own name for a downloaded file, when a step downloaded one.
    filename: str = ""


@dataclass(frozen=True)
class PageVerdict:
    kind: str  # "normal" | "login_wall" | "forbidden"
    reason: str = ""


def classify(page: Page) -> PageVerdict:
    """Is this page safe to act on, a login wall, or forbidden?

    A forbidden signal wins over a login signal: we never want to be on a
    checkout page at all, even if it also has a password box.
    """
    blob = f"{page.title}\n{page.html}".lower()
    if _FORBIDDEN_INPUT.search(page.html or "") or any(w in blob for w in _FORBIDDEN_TEXT):
        return PageVerdict("forbidden", "forbidden_page")
    if _LOGIN_INPUT.search(page.html or ""):
        return PageVerdict("login_wall", "login_wall")
    if any(w in blob for w in _LOGIN_TEXT) and "<form" in (page.html or "").lower():
        return PageVerdict("login_wall", "login_wall")
    return PageVerdict("normal")


def credential_field(field: str) -> bool:
    """A form field that only a credential belongs in. Never filled.

    Whole words only: a field labelled "Shipping address", "Pincode" or
    "Shopping list" is not a credential field, however many of its letters
    spell one.
    """
    return bool(_CREDENTIAL_FIELD.search((field or "").strip()))


def looks_like_secret(value: str) -> bool:
    """A value we refuse to type: it is a card number or a long ID run."""
    return bool(_DIGITS.search(value or ""))


def budget_stop(steps_done: int, seconds: float, limits: GuardLimits) -> str:
    """The stop reason when a budget is spent, else the empty string."""
    if steps_done >= limits.max_steps:
        return "budget_steps"
    if seconds >= limits.max_seconds:
        return "budget_seconds"
    return ""


def note_for(action: str, step_note: str, page: Page) -> str:
    """The step-log note: the plan's own words, plus what was observed."""
    bits = [step_note or action]
    if page.title:
        bits.append(page.title.strip()[:120])
    return redact(": ".join(b for b in bits if b))[:300]
