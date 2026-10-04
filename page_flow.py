#!/usr/bin/env python3
"""page_flow.py — what one fetched Instagram URL MEANS, decided once for
all engines (CLAUDE.md §1: three copies of this triage would drift, and the
drift would be silent — one engine reporting exit 3 where its twin reports
0 on the same page).

instagram.com answers a logged-out request for a profile or a post these
ways, all seen live on 2026-10-04 (see page_parser.py):

  1. the profile or post route, with the data embedded → a row
     (`source_used="embedded_json"`);
  2. the error route ("Sorry, this page isn't available") under HTTP 200
     → the username or shortcode does not exist (`not_found`): no row, not
     a block, never retried harder;
  3. the login route → Instagram wants a login before it shows this
     visitor anything: `blocked`. Seen on a throttled or distrusted exit;
     the cure is a different exit or a slower pace, not a retry;
  4. the profile or post route with NO data in it → this client is
     throttled (`rate_limited`, a block): seen live on plain HTTP requests
     after ~75 page loads from one IP in an hour. The cure is another exit,
     a pause, or a real browser;
  5. the post route with the media withheld from a logged-out visitor
     (`gating_ruling`, e.g. an age-restricted post) → `login_required`: no
     row, a failed URL, not a block — no exit IP changes that;
  6. HTTP 401/403/429, or a bot-challenge page → blocked; anything else
     unread (Chromium's own error page, an unknown route) → a failure.

A profile URL can also queue its own recent posts (`--posts N`): the
profile page embeds the shortcodes of its first 12 timeline posts, and each
becomes one more post URL in the same run.

Triage is driver-independent; the shared async flow below consumes engine
operations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import page_parser as pp
from captcha_solver import detect_from_html
from output_writer import Product

BLOCKING_STATUSES = (401, 403, 429)
# With one exit and no pool to rotate through, a throttle is per IP: every
# later URL gets the same answer, and each one asked makes it worse. After
# this many blocked answers in a row the run stops and reports the rest as
# not attempted (`rate_limited`), keeping what it has.
STOP_AFTER_CONSECUTIVE_BLOCKS = 3
MAX_TIMELINE_POSTS = 12  # what a logged-out profile page embeds (measured on 20 profiles, 2026-10-04)


def is_challenge(html: str) -> bool:
    """A bot-challenge marker on a page that is NOT one of Instagram's own
    routes. Instagram's own pages never carried one in any capture; a page
    with a route is the site's, whatever strings it contains."""
    return not pp.page_route(html or "") and detect_from_html(html or "", pp.BOT_CHALLENGE_MARKERS)


@dataclass
class Outcome:
    product: Optional[Product]
    blocked: bool
    not_found: bool
    source_used: str
    warnings: List[str] = field(default_factory=list)
    failure: Optional[str] = None
    rate_limited: bool = False
    post_codes: List[str] = field(default_factory=list)


def decide(*, url: str, http_status: Optional[int], html: str) -> Outcome:
    route = pp.page_route(html or "")
    if route == pp.ROUTE_ERROR:
        return Outcome(None, False, True, "none",
                       [f"{url}: Instagram's \"page isn't available\" page — no such profile or post (page_not_found)."])
    if route == pp.ROUTE_LOGIN:
        return Outcome(None, True, False, "none",
                       [f"{url}: Instagram served its login page instead — this exit is throttled or distrusted (blocked, not empty). "
                        "Slow down (--delay-between-pages) or use another exit (--proxy-file)."])
    if is_challenge(html):
        return Outcome(None, True, False, "none", [f"{url}: served a bot challenge — blocked."])

    result = pp.safe_parse_page(html or "", url=url)
    if result.products:
        return Outcome(result.products[0], False, False, result.source_used, post_codes=list(result.post_codes or []))
    if result.gated:
        return Outcome(None, False, False, "none",
                       [f"{url}: the post exists but Instagram withholds it from a logged-out visitor — no row (login_required)."],
                       failure="login_required")
    if http_status in BLOCKING_STATUSES:
        return Outcome(None, True, False, "none", [f"{url}: HTTP {http_status} — blocked, not empty."],
                       rate_limited=http_status == 429)
    if route in (pp.ROUTE_PROFILE, pp.ROUTE_POST) and not pp.has_embedded_data(html):
        return Outcome(None, True, False, "none",
                       [f"{url}: Instagram served the page without its data — this exit is rate-limited "
                        "(blocked, not empty). Slow down (--delay-between-pages) or rotate exits (--proxy-file)."],
                       rate_limited=True)
    if route in (pp.ROUTE_PROFILE, pp.ROUTE_POST):
        return Outcome(None, False, False, "none",
                       [f"{url}: the {route.rsplit('.', 1)[-1]} page was served but its data could not be read — "
                        "re-run with --dump-html and report it."], failure="parse_error")
    return Outcome(None, False, False, "none",
                   [f"{url}: HTTP {http_status or '-'}, not an Instagram page (route {route or 'none'}) — nothing read. "
                    "Re-run with --dump-html to inspect it."], failure="fetch_error")


# --------------------------------------------------------------------------- #
# The ONE fetch loop (CLAUDE.md §26). Each engine passes an `Engine` (open a
# page session, sleep, solve a generic captcha) whose sessions provide NAMED
# operations — goto, content, wait, close — and no JavaScript crosses this
# boundary.
# --------------------------------------------------------------------------- #
import logging as _logging
from pathlib import Path as _Path
from typing import Protocol as _Protocol

from output_writer import finish_run as _finish_run
from proxy_pool import is_proxy_dead_error as _is_proxy_dead_error, redact_credentials as _redact

_log = _logging.getLogger("page_flow")

CHALLENGE_WAIT_S = 15  # a self-clearing challenge, if one ever appears, gets this long


class PageSession(_Protocol):
    async def goto(self, url: str) -> Optional[int]: ...  # HTTP status or None; raises on failure
    async def content(self) -> str: ...
    async def wait(self, seconds: float) -> None: ...
    async def close(self) -> None: ...


class Engine(_Protocol):
    name: str
    readiness_s: float

    async def open(self, proxy) -> PageSession: ...
    async def sleep(self, seconds: float) -> None: ...
    async def solve_captcha(self, session: PageSession, *, html: str, url: str) -> Optional[dict]: ...


class SolveBudget:
    """One cap on PAID captcha solves for the whole run (CLAUDE.md §23: a
    per-page limit nothing sums is a bill). `limit=0` means never pay."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.spent = 0

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def spend(self) -> None:
        self.spent += 1


def resolve_urls(args) -> tuple:
    """(urls, skipped). `--url` wins over `--urls-file`. Each input is a
    profile URL, a post or Reel URL, `@username` or a bare username; it is
    normalised to one canonical URL, and anything else is logged and
    skipped, never fetched. Duplicates are fetched once."""
    if args.url:
        candidates = [args.url]
    elif args.urls_file:
        try:
            lines = _Path(args.urls_file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(f"could not read --urls-file {args.urls_file!r}: {exc}")
        candidates = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    else:
        return [], 0
    urls, skipped = [], 0
    for text in candidates:
        url = pp.normalize_input(text)
        if url is None:
            _log.warning("Skipping %s — not an Instagram profile, post or Reel (or a username).", text)
            skipped += 1
            continue
        if url not in urls:
            urls.append(url)
    return urls, skipped


def _dump_path(out_path: str, index: int) -> str:
    return f"{_Path(out_path).with_suffix('')}_debug_{index}.html"


async def _solve_within_budget(engine: Engine, session: PageSession, args, *, html: str, url: str) -> Optional[dict]:
    budget = getattr(args, "_solve_budget", None)
    if budget is not None and budget.remaining() == 0:
        _log.warning("Captcha solving skipped: the run's solve budget is spent (--max-solves %d).", budget.limit)
        return None
    result = await engine.solve_captcha(session, html=html, url=url)
    if budget is not None and result and result.get("action") in ("solved", "warning_solver_error"):
        budget.spend()  # a task was created and billed, whatever came back
    return result


async def fetch_item(engine: Engine, args, url: str, index: int, proxy_pool, client) -> Outcome:
    """One profile or post URL: a typed outcome, including unread and
    rejected data. Open the page, wait out a challenge (bounded), solve one
    within budget, then read the embedded JSON."""
    proxy = proxy_pool.next() if proxy_pool else None
    _log.info("Fetching %s (proxy: %s)", url, proxy.masked() if proxy else "(none — direct, or the --cdp-endpoint session's own exit)")
    try:
        session = await engine.open(proxy)
    except Exception as exc:
        _log.error("Browser connection failed — treating this URL as failed, not a crash: %s", _redact(str(exc)))
        return Outcome(None, False, False, "none", failure="fetch_error")
    try:
        last_error, status = None, None
        for attempt in range(args.retries + 1):
            try:
                status = await session.goto(url)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
                last_error = _redact(str(exc)).splitlines()[0] if str(exc) else type(exc).__name__
                if proxy_pool is not None and proxy is not None and _is_proxy_dead_error(last_error):
                    proxy_pool.report_failure(proxy, dead=True)
                    _log.warning("Proxy reported dead: %s", last_error)
                else:
                    _log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, args.retries + 1, url, last_error)
                if attempt < args.retries:
                    await engine.sleep(args.retry_delay)
        if last_error is not None:
            _log.error("%s permanently failed to load: %s", url, last_error)
            return Outcome(None, False, False, "none",
                           failure="remote_api_error" if getattr(engine, "last_remote_error", False) else "fetch_error")

        html = await session.content()
        waited = 0.0
        while is_challenge(html) and waited < CHALLENGE_WAIT_S:
            await session.wait(1)
            waited += 1
            try:
                html = await session.content()
            except Exception:  # noqa: BLE001 — mid-navigation while the challenge redirects
                continue
        if waited and not is_challenge(html):
            _log.info("%s: challenge cleared by itself after %.0fs.", url, waited)
        if is_challenge(html) and args.solve_captcha != "off":
            result = await _solve_within_budget(engine, session, args, html=html, url=url)
            if result and result.get("action") == "solved":
                await session.wait(engine.readiness_s)
                html = await session.content()

        outcome = decide(url=url, http_status=status, html=html)
        for message in outcome.warnings:
            _log.warning("%s", message)
        if proxy_pool is not None and proxy is not None:
            if outcome.blocked:
                # A login wall is a property of the EXIT (CLAUDE.md §8: a
                # rotation is a fresh browser), so count it against the proxy.
                proxy_pool.report_failure(proxy, dead=status in (403, 429))
            else:
                proxy_pool.report_success(proxy)
        if args.dump_html:
            _Path(_dump_path(args.out, index)).write_text(html, encoding="utf-8")
        if getattr(engine, "last_remote_error", False) and outcome.product is None:
            outcome.failure = "remote_api_error"
        return outcome
    except Exception as exc:
        _log.warning("Fetch failed for %s: %s", url, _redact(str(exc)))
        return Outcome(None, False, False, "none", failure="fetch_error")
    finally:
        await _close_session(session)


async def _close_session(session):
    try:
        await session.close()
    except Exception as exc:
        _log.warning("Session cleanup failed: %s", _redact(str(exc)))


async def run(engine: Engine, args, *, urls: List[str], proxy_pool, client, started_at: float) -> int:
    """Fetch every URL in input order. A profile's `--posts N` shortcodes
    are queued right after it, so the output keeps each profile next to its
    posts. `--max-results` caps the total number of URLs fetched."""
    posts_per_profile = min(max(0, int(getattr(args, "posts", 0) or 0)), MAX_TIMELINE_POSTS)
    queue: List[str] = list(urls)
    queued = set(queue)
    products: List[Product] = []
    failed_pages: List[int] = []
    failures = []
    not_found = []
    any_blocked = rate_limited = remote_api_error = False
    completed = 0
    consecutive_blocks = 0
    i = 0
    capped = len(queue) > args.max_results
    while i < len(queue) and i < args.max_results:
        url = queue[i]
        i += 1
        outcome = await fetch_item(engine, args, url, i, proxy_pool, client)
        any_blocked = any_blocked or outcome.blocked
        rate_limited = rate_limited or outcome.rate_limited
        remote_api_error = remote_api_error or outcome.failure == "remote_api_error"
        if outcome.failure or outcome.blocked:
            failed_pages.append(i)
            failures.append({"url": url, "reason": outcome.failure or ("rate_limited" if outcome.rate_limited else "blocked")})
        else:
            completed += 1
        if outcome.not_found:
            not_found.append(url)
        if outcome.product is not None:
            products.append(outcome.product)
            if posts_per_profile and outcome.product.category == "profile":
                new = [pp.post_url(c) for c in outcome.post_codes[:posts_per_profile]]
                new = [u for u in new if u not in queued]
                queue[i:i] = new
                queued.update(new)
                if outcome.product.is_private and not new:
                    _log.info("%s is private: its posts are not shown to a logged-out visitor.", url)
        capped = capped or len(queue) > args.max_results
        consecutive_blocks = consecutive_blocks + 1 if outcome.blocked else 0
        if proxy_pool is None and consecutive_blocks >= STOP_AFTER_CONSECUTIVE_BLOCKS and i < min(len(queue), args.max_results):
            _log.error("%d blocked answers in a row from the same exit — stopping instead of asking again. "
                       "Rerun later, or with --proxy-file to rotate exits.", consecutive_blocks)
            for j, pending in enumerate(queue[i:args.max_results], i + 1):
                failed_pages.append(j)
                failures.append({"url": pending, "reason": "not_attempted"})
            break
        if getattr(engine, "fatal", None):
            for j, pending in enumerate(queue[i:args.max_results], i + 1):
                failed_pages.append(j)
                failures.append({"url": pending, "reason": "remote_api_error"})
            break
        if i < min(len(queue), args.max_results):
            await engine.sleep(args.delay_between_pages)
    return finish(args, products=products, blocked=any_blocked, remote_api_error=remote_api_error,
                  engine_name=engine.name, urls=urls, fetched=queue[:args.max_results], started_at=started_at,
                  pages_completed=completed, failed_pages=failed_pages, failures=failures,
                  rate_limited=rate_limited, not_found=not_found, capped=capped)


def finish(args, *, products: List[Product], blocked: bool, remote_api_error: bool, engine_name: str,
           urls: List[str], started_at: float, pages_completed: int, failed_pages: List[int],
           failures=None, rate_limited=False, fetched: Optional[List[str]] = None, not_found=None,
           capped: Optional[bool] = None) -> int:
    budget = getattr(args, "_solve_budget", None)
    fetched = list(urls if fetched is None else fetched)
    selection = {"mode": "urls", "urls": urls, "posts": int(getattr(args, "posts", 0) or 0),
                 "max_results": args.max_results}
    extra = {"solves_spent": budget.spent if budget is not None else 0, "selection": selection,
             "failed_urls": failures or [], "not_found_urls": not_found or []}
    return _finish_run(
        products=products, out_path=args.out, fmt=args.format, engine=engine_name, url=urls[0] if urls else "",
        pages_requested=len(fetched), pages_completed=pages_completed,
        failed_pages=failed_pages or None, blocked=blocked, remote_api_error=remote_api_error,
        allow_empty=args.allow_empty, started_at=started_at, price_confirmed_pct=None,
        max_results=args.max_results, rate_limited=rate_limited,
        incomplete_reason=next((f["reason"] for f in (failures or []) if f["reason"] == "parse_error"), None),
        capped=bool(capped) if capped is not None else len(urls) > args.max_results,
        extra_meta=extra,
    )


def validate_common(args, *, urls, skipped, print_err) -> Optional[int]:
    """The usage checks every engine makes before launching anything;
    returns an exit code to stop with, or None to go on."""
    from output_writer import EXIT_BAD_USAGE
    if not urls:
        if skipped:
            print_err(f"Error: none of the inputs is an Instagram profile, post or username ({skipped} skipped) — nothing left to fetch")
        else:
            print_err("Error: provide --url or --urls-file")
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print_err(f"Error: unsupported --format {args.format!r}")
        return EXIT_BAD_USAGE
    if not 0 <= int(getattr(args, "posts", 0) or 0) <= MAX_TIMELINE_POSTS:
        print_err(f"Error: --posts must be between 0 and {MAX_TIMELINE_POSTS} — a logged-out profile page embeds only its first {MAX_TIMELINE_POSTS} posts")
        return EXIT_BAD_USAGE
    return None
