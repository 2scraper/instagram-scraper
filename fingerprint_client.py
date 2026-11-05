#!/usr/bin/env python3
"""fingerprint_client.py — 2Captcha Fingerprint API fetch + application.
No site knowledge.

Two rules, both from the family's invariants:

  - Never set a fingerprint over `--cdp-endpoint`. The Scraping Browser API
    brings its own device identity; stacking a second on top creates a
    contradiction (mismatched UA/TLS/JS fingerprint) rather than better
    cover. `apply_fingerprint()` refuses and warns instead of applying one.
  - Only a desktop-Chrome profile is used (`choose_user_agent`), and its
    Chrome version is set to the running browser's (`align_user_agent`).
  - The user agent comes from the BROWSER, never a literal string. A
    hardcoded UA drifts from whatever Chromium build is actually installed,
    and claiming an older Chrome than the JS engine/TLS handshake report is
    itself a mismatch a bot-detector can key on. This module only ever
    forwards what the Fingerprint API returned — it never invents one.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from scraper_api_client import TwoCaptchaClient, TwoCaptchaError

log = logging.getLogger("fingerprint_client")


def fetch_fingerprint(
    client: TwoCaptchaClient, *, tags: Optional[str] = None, country: Optional[str] = None
) -> Optional[dict]:
    """Returns the raw Fingerprint API profile, or None on any failure — a
    fingerprint is a cover-quality improvement, never something a run
    should crash over. Caller logs/warns as it sees fit."""
    try:
        return client.random_fingerprint(tags=tags, country=country)
    except TwoCaptchaError as exc:
        log.warning("Fingerprint API request failed, continuing without one: %s", exc)
        return None


# A profile is used only if it is desktop Chrome: a phone's user agent gets
# Instagram's MOBILE layout, which this scraper does not read (measured
# 2026-10-26: 4 of 4 Android profiles ended in exit 5, 4 of 4 Windows ones
# read), and an Edge/Opera/Firefox/Safari brand on top of Chromium is a
# mismatch of its own. Asked again up to this many times.
FINGERPRINT_ATTEMPTS = 5
_MOBILE_UA_RE = re.compile(r"\b(Mobile|Android|iPhone|iPad|iPod)\b")
_OTHER_BRAND_RE = re.compile(r"\b(Edg|EdgA|EdgiOS|OPR|YaBrowser|SamsungBrowser|Firefox|FxiOS|CriOS|Vivaldi)/")
_CHROME_VERSION_RE = re.compile(r"\bChrome/(\d+)(?:\.\d+)*")


def unsuitable_reason(user_agent: str) -> Optional[str]:
    """Why a profile's user agent is not used, or None."""
    if _MOBILE_UA_RE.search(user_agent):
        return "a phone's (Instagram serves it the mobile layout, which is not read)"
    if _OTHER_BRAND_RE.search(user_agent) or not _CHROME_VERSION_RE.search(user_agent):
        return "not desktop Chrome (the browser here is Chromium)"
    return None


def choose_user_agent(client: TwoCaptchaClient, *, tags: Optional[str] = None,
                      country: Optional[str] = None, attempts: int = FINGERPRINT_ATTEMPTS) -> Optional[str]:
    """A desktop-Chrome user agent from the Fingerprint API, or None (the
    run then keeps the browser's own). Each unsuitable profile is logged."""
    for _ in range(max(1, attempts)):
        profile = fetch_fingerprint(client, tags=tags, country=country)
        if not profile:
            return None
        user_agent = user_agent_from(profile)
        if not user_agent:
            return None
        reason = unsuitable_reason(user_agent)
        if reason is None:
            return user_agent
        log.info("Fingerprint profile skipped — its user agent is %s: %s", reason, user_agent)
    log.warning("No desktop-Chrome profile in %d Fingerprint API answers — continuing with the browser's own user "
                "agent. Narrow the choice with --fp-tags Windows.", attempts)
    return None


def align_user_agent(user_agent: Optional[str], browser_version: Optional[str]) -> Optional[str]:
    """The profile's user agent with its Chrome major version set to the
    browser that actually runs (`Chrome/156.0.0.0`, the reduced form Chrome
    itself sends): a UA claiming another Chrome than the JS engine and the
    TLS handshake is a mismatch a bot-detector keys on. Unchanged when the
    browser version is unknown."""
    if not user_agent:
        return user_agent
    match = re.search(r"(\d+)\.", browser_version or "")
    if not match:
        return user_agent
    return _CHROME_VERSION_RE.sub("Chrome/%s.0.0.0" % match.group(1), user_agent)


def user_agent_from(fingerprint: dict) -> Optional[str]:
    """The user agent string of a Fingerprint API profile, or None.

    The live `format=chromium` response (measured 2026-10-04, /random and
    /generate alike) carries it as `userAgent.userAgent`, next to
    `fullVersion`, `platform` and `brandVersionList`. The documentation's
    /random example shows `userAgent.value` instead, which no live response
    has had — reading only that key made `--fingerprint` a silent no-op in
    every engine. `value` stays as a fallback in case the docs' shape ever
    ships; a profile with neither is reported, never applied as nothing."""
    ua = fingerprint.get("userAgent") if isinstance(fingerprint, dict) else None
    if isinstance(ua, dict):
        for key in ("userAgent", "value"):
            if isinstance(ua.get(key), str) and ua[key]:
                return ua[key]
    if isinstance(ua, str) and ua:
        return ua
    log.warning("Fingerprint API profile carried no user agent — continuing with the browser's own.")
    return None


# This module deliberately extracts and applies ONLY the user agent from a
# Fingerprint API profile — never a locale or timezone, even though the
# real API almost certainly carries signal for both somewhere in the
# response. The failure mode this avoids: an older sibling repo shipped `f"en-{country}"` as a fabricated locale
# and never applied the API's own timezone field at all, and both went
# undetected for months because nothing exercises a credentialed path in
# an offline suite. This session could not get a confirmed field name for
# either from 2Captcha's own reference (the public docs page for this
# endpoint 404s, and a fetched marketing page's JSON sample is not a
# citable API contract) — inventing one now would repeat that exact
# mistake with a different guessed key. Add locale/timezone application
# here ONLY once a real API response (or 2Captcha support) confirms the
# field name; until then, omitting it is more honest than guessing it
# ("never present a guess as a fact").


def refuse_if_cdp(cdp_endpoint: Optional[str]) -> bool:
    """Call before requesting/applying a fingerprint. Returns True (and the
    caller should skip fingerprinting) when a remote CDP endpoint is in
    play — the remote browser already has its own device identity."""
    if cdp_endpoint:
        log.warning(
            "Ignoring --fingerprint: a --cdp-endpoint session already carries "
            "its own device identity, and stacking a second is a contradiction, "
            "not better cover."
        )
        return True
    return False
