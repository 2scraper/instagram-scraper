#!/usr/bin/env python3
"""page_flow.py — what one fetched Instagram URL MEANS, decided once for
all engines (three copies of this triage would drift, and the
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
import proxy_meter as _meter
import run_state
from run_state import add_arguments as add_run_arguments
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
    timeline_status: Optional[str] = None


def _block_hint(scraper_api: bool) -> str:
    """What to do about a block. Through --scraper-api the exit is the
    Scraper API's (a --proxy-file is ignored there), so the advice differs."""
    if scraper_api:
        return ("The Scraper API's own pool often gets this for profiles — use a browser engine with a proxy, "
                "or set INSTAGRAM_CDP_ENDPOINT to route --scraper-api through a Scraping Browser profile.")
    return "Slow down (--delay-between-pages) or use another exit (--proxy-file)."


def decide(*, url: str, http_status: Optional[int], html: str, scraper_api: bool = False) -> Outcome:
    route = pp.page_route(html or "")
    if route == pp.ROUTE_ERROR:
        return Outcome(None, False, True, "none",
                       [f"{url}: Instagram's \"page isn't available\" page — no such profile or post (page_not_found)."])
    if route == pp.ROUTE_LOGIN:
        return Outcome(None, True, False, "none",
                       [f"{url}: Instagram served its login page instead — this exit is throttled or distrusted (blocked, not empty). "
                        + _block_hint(scraper_api)])
    if pp.is_mobile_route(route):
        return Outcome(None, False, False, "none",
                       [f"{url}: Instagram served its MOBILE layout ({route.rsplit('.', 1)[-1]}) — this scraper reads the "
                        "desktop page. The user agent is a phone's: drop it, or use --fp-tags Windows with --fingerprint."],
                       failure="fetch_error")
    if is_challenge(html):
        return Outcome(None, True, False, "none", [f"{url}: served a bot challenge — blocked."])

    result = pp.safe_parse_page(html or "", url=url)
    if result.products:
        return Outcome(result.products[0], False, False, result.source_used, post_codes=list(result.post_codes or []),
                       timeline_status=result.timeline_status)
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
                        "(blocked, not empty). " + _block_hint(scraper_api)],
                       rate_limited=True)
    if route in (pp.ROUTE_PROFILE, pp.ROUTE_POST):
        return Outcome(None, False, False, "none",
                       [f"{url}: the {route.rsplit('.', 1)[-1]} page was served but its data could not be read — "
                        "re-run with --dump-html and report it."], failure="parse_error")
    return Outcome(None, False, False, "none",
                   [f"{url}: HTTP {http_status or '-'}, not an Instagram page (route {route or 'none'}) — nothing read. "
                    "Re-run with --dump-html to inspect it."], failure="fetch_error")


# --------------------------------------------------------------------------- #
# The ONE fetch loop. Each engine passes an `Engine` (open a
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


# At most this many PAID solves for one URL, across every attempt at it.
# Checked BEFORE each purchase alongside the run-wide --max-solves cap, and
# never reset by a navigation retry or a proxy rotation: a fresh attempt is
# a reason to re-fetch, not a fresh allowance.
SOLVES_PER_PAGE = 1


# CLI value validators, shared so the three engines cannot disagree about
# what a flag accepts. --retries counts EXTRA attempts here, so 0 is legal
# (one fetch) and only a negative value is a usage error.
def positive_int(value: str) -> int:
    import argparse
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def nonnegative_int(value: str) -> int:
    import argparse
    ivalue = int(value)
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return ivalue


def nonnegative_float(value: str) -> float:
    import argparse
    fvalue = float(value)
    if fvalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return fvalue


class SolveBudget:
    """A cap on PAID captcha solves. One instance spans the whole run
    (`--max-solves`), and fetch_item makes one per URL (SOLVES_PER_PAGE):
    a cap that nothing sums is a bill. `limit=0` means never pay."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.spent = 0

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def spend(self) -> None:
        self.spent += 1


def resolve_urls(args) -> tuple:
    """(urls, skipped). `--url` and `--urls-file` are exclusive (a configured
    INSTAGRAM_URL never competes with a typed --urls-file: env_config skips
    it). Each input is a profile URL, a post or Reel URL, `@username` or a
    bare username; it is normalised to one canonical URL, and anything else
    is logged and skipped, never fetched. Duplicates are fetched once."""
    if args.url and args.urls_file:
        raise ValueError("use one of --url / --urls-file, not both")
    if args.url:
        candidates = [args.url]
    elif args.urls_file:
        try:
            # utf-8-sig: a list saved by Notepad or exported from Excel starts
            # with a BOM, which would otherwise glue itself to the first line.
            lines = _Path(args.urls_file).read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"could not read --urls-file {args.urls_file!r}: {exc}")
        candidates = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
        if not candidates:
            raise ValueError(f"--urls-file {args.urls_file!r} has no inputs (every line is blank or a # comment)")
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


async def _solve_within_budget(engine: Engine, session: PageSession, args, *, html: str, url: str,
                               page_budget: SolveBudget) -> Optional[dict]:
    """The ONLY place page_flow may call engine.solve_captcha: both the
    per-URL allowance and the run-wide cap are checked before a purchase."""
    budget = getattr(args, "_solve_budget", None)
    if page_budget.remaining() == 0:
        _log.warning("Captcha solving skipped for %s: its per-page solve allowance (%d) is spent.", url, page_budget.limit)
        return None
    if budget is not None and budget.remaining() == 0:
        _log.warning("Captcha solving skipped: the run's solve budget is spent (--max-solves %d).", budget.limit)
        return None
    result = await engine.solve_captcha(session, html=html, url=url)
    if result and result.get("action") in ("solved", "warning_solver_error"):
        page_budget.spend()  # a task was created and billed, whatever came back
        if budget is not None:
            budget.spend()
    return result


async def fetch_item(engine: Engine, args, url: str, index: int, proxy_pool, client) -> Outcome:
    """One profile or post URL: a typed outcome, including unread and
    rejected data. Open the page, wait out a challenge (bounded), solve one
    within budget, then read the embedded JSON."""
    proxy = proxy_pool.next() if proxy_pool is not None else None
    if proxy_pool is not None and proxy is None:
        return Outcome(None, False, False, "none", failure="proxy_pool_exhausted")
    page_budget = SolveBudget(SOLVES_PER_PAGE)
    _log.info("Fetching %s (proxy: %s)", url, proxy.masked() if proxy else "(none — direct, or the --cdp-endpoint session's own exit)")
    try:
        session = await engine.open(proxy)
    except Exception as exc:
        _log.error("Browser connection failed — treating this URL as failed, not a crash: %s", _redact(str(exc)))
        return Outcome(None, False, False, "none", failure="fetch_error")
    try:
        last_error, status = None, None
        # `?hl=en`: Instagram localises the alt text to the visitor's language
        # (Accept-Language, the OS locale, a remote profile's, or the Scraper
        # API exit's country), and the parameter outranks all of them —
        # measured 2026-11-05. The canonical `url` stays the row's identity.
        target = pp.fetch_url(url)
        for attempt in range(args.retries + 1):
            if _meter.exceeded():
                last_error = _meter.STOP_REASON
                break
            if STOP.requested:
                last_error = INTERRUPTED
                break
            try:
                status = await session.goto(target)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
                last_error = _redact(str(exc)).splitlines()[0] if str(exc) else type(exc).__name__
                rotate = proxy_pool is not None and proxy is not None and proxy_pool.live_count() > 1
                if proxy_pool is not None and proxy is not None and (_is_proxy_dead_error(last_error) or rotate):
                    # With other exits left, a failed navigation counts against
                    # THIS exit and the retry goes through the next one: a dead
                    # or refusing exit (a 407 shows up as a timeout in Chromium)
                    # would fail every retry the same way.
                    proxy_pool.report_failure(proxy, dead=True)
                    _log.warning("Navigation attempt %d/%d for %s via %s failed: %s", attempt + 1, args.retries + 1,
                                 url, proxy.masked(), last_error)
                else:
                    _log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, args.retries + 1, url, last_error)
                if getattr(engine, "fatal", None):
                    break  # a refused key or an empty balance: every retry would fail the same way
                if attempt < args.retries:
                    if rotate:
                        next_proxy = proxy_pool.next()
                        if next_proxy is None:
                            last_error = "proxy_pool_exhausted"
                            break
                        await _close_session(session)
                        session = None
                        proxy = next_proxy
                        _log.info("Retrying %s through the next exit (%s)", url, proxy.masked())
                        try:
                            session = await engine.open(proxy)
                        except Exception as open_exc:  # noqa: BLE001
                            _log.error("Browser connection failed — treating this URL as failed, not a crash: %s",
                                       _redact(str(open_exc)))
                            return Outcome(None, False, False, "none", failure="fetch_error")
                    await engine.sleep(args.retry_delay)
        if last_error == _meter.STOP_REASON:
            return Outcome(None, False, False, "none", failure=_meter.STOP_REASON)
        if last_error in (INTERRUPTED, "proxy_pool_exhausted"):
            return Outcome(None, False, False, "none", failure=last_error)
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
            result = await _solve_within_budget(engine, session, args, html=html, url=url, page_budget=page_budget)
            if result and result.get("action") == "solved":
                await session.wait(engine.readiness_s)
                html = await session.content()

        outcome = decide(url=url, http_status=status, html=html, scraper_api=engine.name == "scraper_api")
        for message in outcome.warnings:
            _log.warning("%s", message)
        if proxy_pool is not None and proxy is not None:
            if outcome.blocked:
                # A login wall is a property of the EXIT (a
                # rotation is a fresh browser), so count it against the proxy.
                proxy_pool.report_failure(proxy, dead=True)
            else:
                proxy_pool.report_success(proxy)
        if args.dump_html:
            try:
                _Path(_dump_path(args.out, index)).write_text(html, encoding="utf-8")
            except OSError as exc:
                # A debugging aid: failing to save it says nothing about the page.
                _log.warning("Could not save --dump-html for %s: %s", url, exc)
        if getattr(engine, "last_remote_error", False) and outcome.product is None:
            outcome.failure = "remote_api_error"
        return outcome
    except Exception as exc:
        _log.warning("Fetch failed for %s: %s", url, _redact(str(exc)))
        return Outcome(None, False, False, "none", failure="fetch_error")
    finally:
        await _close_session(session)


async def _close_session(session):
    if session is None:
        return
    try:
        await session.close()
    except Exception as exc:
        _log.warning("Session cleanup failed: %s", _redact(str(exc)))


def _post_sku(url: str) -> Optional[str]:
    kind, key = pp.classify_url(url)
    return pp.make_sku("post", key) if kind == "post" else None


def _already_seen(args, url: str) -> bool:
    sku = _post_sku(url)
    return sku is not None and sku in getattr(args, "_known_posts", set())


async def run(engine: Engine, args, *, urls: List[str], proxy_pool, client, started_at: float) -> int:
    """Fetch every URL in input order. A profile's `--posts N` shortcodes
    are queued right after it, so the output keeps each profile next to its
    posts. `--max-results` caps the total number of URLs fetched; every
    INPUT URL is always inside it (validate_common refuses a cap below the
    input count), so the cap only ever trims posts queued by `--posts`, and
    each trimmed post is listed in `excluded_urls` as `max_results`."""
    from dataclasses import asdict

    try:
        state = run_state.prepare(args, urls)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        _log.error("Invalid run state: %s", _redact(str(exc)))
        return 2
    posts_per_profile = min(max(0, int(getattr(args, "posts", 0) or 0)), MAX_TIMELINE_POSTS)
    queue = list(state["queue"] if state else urls)
    records = dict(state["records"] if state else {})
    queued = set(queue)
    run_state.save(args, urls, queue, records)
    consecutive_blocks = 0
    interrupted = False
    i = 0
    while i < min(len(queue), args.max_results):
        if STOP.requested:
            interrupted = True
            for pending in queue[i:args.max_results]:
                if not records.get(pending, {}).get("done"):
                    records[pending] = {"done": False, "failure": INTERRUPTED}
            run_state.save(args, urls, queue, records)
            break
        url = queue[i]
        i += 1
        if records.get(url, {}).get("done"):
            continue
        if _already_seen(args, url):
            records[url] = {"done": True, "excluded": "already_seen"}
            run_state.save(args, urls, queue, records)
            continue
        outcome = await fetch_item(engine, args, url, i, proxy_pool, client)
        trimmed = []
        if outcome.product is not None and outcome.product.category == "profile" and posts_per_profile:
            if outcome.timeline_status not in ("present", "private"):
                outcome.failure = "parse_error"
            new = [pp.post_url(c) for c in outcome.post_codes[:posts_per_profile]]
            new = list(dict.fromkeys(u for u in new if u not in queued))
            room = max(0, args.max_results - len(queue))
            new, trimmed = new[:room], new[room:]
            if trimmed:
                _log.warning("--max-results %d reached: %d of %s's posts are not fetched (listed in excluded_urls "
                             "as max_results). Raise --max-results to read them.", args.max_results, len(trimmed), url)
            queue[i:i] = new
            queued.update(new)
        excluded = None
        if outcome.product is not None and outcome.product.category == "post" and args._since:
            try:
                posted = run_state.utc_date(outcome.product.posted_at)
            except ValueError:
                outcome.failure = "parse_error"
            else:
                if posted < run_state.utc_date(args._since):
                    excluded = "before_since"
                    outcome.product = None
        record = {"done": not (outcome.failure or outcome.blocked),
                  "product": asdict(outcome.product) if outcome.product else None,
                  "blocked": outcome.blocked, "rate_limited": outcome.rate_limited,
                  "failure": outcome.failure, "not_found": outcome.not_found, "excluded": excluded}
        if trimmed:
            record["trimmed_posts"] = trimmed  # kept in the checkpoint, so a --resume still reports them
        records[url] = record
        run_state.save(args, urls, queue, records)
        consecutive_blocks = consecutive_blocks + 1 if outcome.blocked else 0
        stop = (outcome.failure == "proxy_pool_exhausted" or getattr(engine, "fatal", None) or _meter.exceeded()
                or (proxy_pool is None and consecutive_blocks >= STOP_AFTER_CONSECUTIVE_BLOCKS))
        if stop:
            for pending in queue[i:args.max_results]:
                if records.get(pending, {}).get("done"):
                    continue
                # A known post needs no fetch, so a stop does not leave it unread.
                records[pending] = ({"done": True, "excluded": "already_seen"} if _already_seen(args, pending)
                                    else {"done": False, "failure": "not_attempted"})
            run_state.save(args, urls, queue, records)
            break
        if i < min(len(queue), args.max_results):
            await engine.sleep(args.delay_between_pages)
    products, failures, failed_pages, not_found, exclusions = [], [], [], [], []
    completed = 0
    blocked = rate_limited = remote_error = False
    for index, url in enumerate(queue[:args.max_results], 1):
        record = records.get(url, {"done": False, "failure": "not_attempted"})
        if record.get("product"):
            products.append(Product(**record["product"]))
        blocked = blocked or record.get("blocked", False)
        rate_limited = rate_limited or record.get("rate_limited", False)
        remote_error = remote_error or record.get("failure") == "remote_api_error"
        if record["done"]:
            completed += 1
        else:
            failed_pages.append(index)
            failures.append({"url": url, "reason": record.get("failure") or
                             ("rate_limited" if record.get("rate_limited") else "blocked")})
        if record.get("not_found"):
            not_found.append(url)
        if record.get("excluded"):
            exclusions.append({"url": url, "reason": record["excluded"]})
        exclusions.extend({"url": u, "reason": "max_results"} for u in record.get("trimmed_posts") or [])
    code = finish(args, products=products, blocked=blocked, remote_api_error=remote_error,
                  engine_name=engine.name, urls=urls, fetched=queue[:args.max_results], started_at=started_at,
                  pages_completed=completed, failed_pages=failed_pages, failures=failures,
                  rate_limited=rate_limited, not_found=not_found,
                  capped=len(queue) > args.max_results or posts_per_profile > 0, exclusions=exclusions,
                  media_blocked=bool(getattr(engine, "media_blocked", False)))
    if interrupted or any(f["reason"] == INTERRUPTED for f in failures):
        return STOP.report(args, rows=len(products), done=completed, total=len(queue[:args.max_results]))
    return code


def finish(args, *, products: List[Product], blocked: bool, remote_api_error: bool, engine_name: str,
           urls: List[str], started_at: float, pages_completed: int, failed_pages: List[int],
           failures=None, rate_limited=False, fetched: Optional[List[str]] = None, not_found=None,
           capped: Optional[bool] = None, exclusions=None, media_blocked: bool = False) -> int:
    budget = getattr(args, "_solve_budget", None)
    fetched = list(urls if fetched is None else fetched)
    selection = run_state.selection(args, urls)
    extra = {"solves_spent": budget.spent if budget is not None else 0, "selection": selection,
             "failed_urls": failures or [], "not_found_urls": not_found or [],
             "excluded_urls": exclusions or [], "incremental": bool(getattr(args, "incremental_from", None)),
             "post_window": "embedded_pinned_first" if getattr(args, "posts", 0) else None,
             "media_blocked": bool(media_blocked),
             # A post's date never changes, so one before --since stays excluded:
             # remembering it spares the next delta a fetch that only drops it.
             "seen_post_skus": sorted(set(getattr(args, "_known_posts", set())) |
                                      {p.sku for p in products if p.category == "post" and p.sku} |
                                      {_post_sku(e["url"]) for e in exclusions or []
                                       if e["reason"] == "before_since" and _post_sku(e["url"])})}
    return _finish_run(
        products=products, out_path=args.out, fmt=args.format, engine=engine_name, url=urls[0] if urls else "",
        pages_requested=len(fetched), pages_completed=pages_completed,
        failed_pages=failed_pages or None, blocked=blocked, remote_api_error=remote_api_error,
        allow_empty=args.allow_empty, started_at=started_at, price_confirmed_pct=None,
        max_results=args.max_results, rate_limited=rate_limited,
        incomplete_reason=next((f["reason"] for f in (failures or []) if f["reason"] in (INTERRUPTED, "parse_error", "proxy_pool_exhausted", _meter.STOP_REASON)), None),
        capped=bool(capped) if capped is not None else len(urls) > args.max_results or bool(getattr(args, "posts", 0)),
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
    if len(urls) > args.max_results:
        # The cap never drops an input silently: posts queued by --posts are
        # what it trims, so it must hold every input first.
        print_err(f"Error: --max-results {args.max_results} is smaller than the {len(urls)} inputs — raise it to at "
                  f"least {len(urls)} (plus room for --posts), or split the list")
        return EXIT_BAD_USAGE
    message = check_output_path(args)
    if message:
        print_err(f"Error: {message}")
        return EXIT_BAD_USAGE
    try:
        run_state.prepare(args, urls)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print_err(f"Error: {_redact(str(exc))}")
        return EXIT_BAD_USAGE
    return None


def check_output_path(args) -> Optional[str]:
    """Why `--out` cannot be written, or None. Checked before the first
    request: finding out after a full (and possibly paid) run loses it all."""
    import os
    out = _Path(args.out or f"instagram_results.{args.format}")
    resolved = out.resolve()
    if resolved.is_dir():
        return f"--out {str(out)!r} is a directory — give a file path, e.g. {str(out / ('results.' + args.format))!r}"
    parent = resolved.parent
    if not parent.is_dir():
        return f"--out {str(out)!r}: directory {str(parent)!r} does not exist"
    if not os.access(parent, os.W_OK):
        return f"--out {str(out)!r}: directory {str(parent)!r} is not writable"
    outputs = {resolved, _Path(str(resolved) + ".meta.json")}
    for flag, value in (("--urls-file", getattr(args, "urls_file", None)),
                        ("--incremental-from", getattr(args, "incremental_from", None)),
                        ("--proxy-file", getattr(args, "proxy_file", None)),
                        ("--checkpoint", getattr(args, "checkpoint", None))):
        if value and _Path(value).resolve() in outputs:
            return f"--out {str(out)!r} would overwrite {flag} {value!r} — choose another output path"
    return None


# --------------------------------------------------------------------------- #
# Traffic savers behind a metered proxy (the helpers are in proxy_pool.py)
# --------------------------------------------------------------------------- #
# One HEAD for www.instagram.com/robots.txt, through the proxy, before a local
# browser is launched. Short on purpose: a slow answer is not a verdict, and
# the run then goes ahead exactly as it did before.
PROXY_PRECHECK_TIMEOUT_S = 20.0


def start_proxy_meter(args, proxy_pool) -> "_meter.ProxyMeter":
    """Meter this run's traffic when it goes through a LOCAL proxy (not
    over --cdp-endpoint, not the Scraper API, not without a proxy)."""
    active = (proxy_pool is not None and not getattr(args, "cdp_endpoint", None)
              and not getattr(args, "scraper_api", False))
    return _meter.start(getattr(args, "proxy_budget_mb", _meter.DEFAULT_BUDGET_MB), active=active)


PROXY_PRECHECK_WORKERS = 8


def load_proxy_pool(args, *, can_authenticate: bool = True, print_err=None):
    """(pool or None, exit code or None) — the proxy setup every engine
    shares, so a usage problem is refused BEFORE a browser starts, the same
    way in all three:

    * an unreadable `--proxy-file`, or one with no proxy in it (only blank
      or `#` lines) is a usage error — a run that was given a proxy file
      must never fall back to this machine's own address;
    * a SOCKS proxy with a login: Chromium cannot authenticate one;
    * any proxy with a login on an engine that cannot authenticate
      (`can_authenticate=False`: Selenium's --proxy-server);
    * with --cdp-endpoint the remote session carries its own exit, so a
      proxy is ignored (with a warning) after it parsed.
    """
    import sys
    from proxy_pool import ProxyPool, ProxyParseError, load_proxies
    print_err = print_err or (lambda m: print(m, file=sys.stderr))
    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        print_err(f"Error: {exc}")
        return None, 2
    except (OSError, UnicodeDecodeError) as exc:
        print_err(f"Error: could not read --proxy-file {args.proxy_file!r}: {exc}")
        return None, 2
    if getattr(args, "cdp_endpoint", None):
        if proxies:
            _log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
        return None, None
    if args.proxy_file and not args.proxy and not proxies:
        print_err(f"Error: --proxy-file {args.proxy_file!r} has no proxy in it (every line is blank or a # comment) — "
                  "refusing to fall back to this machine's own address")
        return None, 2
    for proxy in proxies:
        if proxy.has_auth and proxy.scheme.lower().startswith("socks"):
            print_err(f"Error: {proxy.masked()} — Chromium cannot authenticate a SOCKS proxy. Use an http:// proxy "
                      "for a login, or a SOCKS proxy that allows this IP without one")
            return None, 2
        if proxy.has_auth and not can_authenticate:
            print_err(f"Error: {proxy.masked()} carries a login — Selenium's --proxy-server cannot authenticate, so "
                      "every request would get 407. Use playwright_scraper.py or puppeteer_scraper.py for this proxy, "
                      "or an IP-allowlisted proxy without a login")
            return None, 2
    if not proxies:
        return None, None
    return ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries), None


def proxy_precheck(args, proxy_pool, *, urls: List[str], engine_name: str, started_at: float,
                   print_err=None) -> Optional[int]:
    """Pre-check EVERY exit in the pool; return the exit code to stop with,
    or None to go on.

    An exit whose gateway answers 407 or 402 is dropped from the pool (with
    a warning naming it masked). Only when every exit is dropped does the
    run stop: it prints why (naming INSTAGRAM_PROXY, never its value),
    writes `<out>.latest_attempt.meta.json` (status failed, stop_reason
    proxy_auth_failed), leaves the output and `<out>.meta.json` untouched,
    and returns exit 5. Never sent over --cdp-endpoint or --scraper-api (no
    local proxy there) or without a proxy. Anything else — a 200, a 403
    from the site, a timeout — keeps the exit: one request per exit, never
    retried.
    """
    import sys
    from concurrent.futures import ThreadPoolExecutor
    import output_writer
    import proxy_pool as _savers
    print_err = print_err or (lambda m: print(m, file=sys.stderr))
    if proxy_pool is None or getattr(args, "cdp_endpoint", None) or getattr(args, "scraper_api", False):
        return None
    exits = proxy_pool.live()
    if not exits or not urls:
        return None
    probe = _savers.robots_url(urls[0])

    def check(proxy):
        try:
            _savers.precheck_proxy(proxy.url(), probe, timeout=PROXY_PRECHECK_TIMEOUT_S)
        except _savers.ProxyPrecheckError as exc:
            return exc
        return None

    with ThreadPoolExecutor(max_workers=min(PROXY_PRECHECK_WORKERS, len(exits))) as pool:
        verdicts = list(pool.map(check, exits))
    refused = [(proxy, exc) for proxy, exc in zip(exits, verdicts) if exc is not None]
    if refused and len(refused) < len(exits):
        for proxy, exc in refused:
            proxy_pool.drop(proxy)
            _log.warning("Proxy pre-check: dropping %s from the pool — %s.", proxy.masked(), exc)
        _log.info("Proxy pre-check: %d of %d exits usable.", len(exits) - len(refused), len(exits))
        return None
    if refused:
        exc = refused[0][1]
        print_err(_savers.precheck_message(exc, "INSTAGRAM_PROXY", "www.instagram.com"))
        try:
            output_writer.write_failed_attempt(
                args.out, stop_reason=_savers.PROXY_PRECHECK_STOP_REASON, engine=engine_name,
                url=urls[0], pages_requested=len(urls), started_at=started_at,
                transport_error="proxy pre-check: %s" % exc)
        except OSError as write_exc:
            # The verdict stands without its record: still exit 5, not a crash.
            print_err("[!] could not write the attempt metadata: %s" % write_exc)
        return output_writer.EXIT_REMOTE_API_ERROR
    return None


def add_media_arguments(parser) -> None:
    """`--load-media` (documented) and the hidden `--force-block-media`."""
    import argparse
    parser.add_argument(
        "--load-media", action="store_true",
        help="Load images, video and web fonts. By default they are aborted while a local browser goes "
             "through --proxy / --proxy-file (the parser reads the page's embedded JSON, and a proxy bills "
             "per byte); nothing is blocked with --cdp-endpoint, --scraper-api or no proxy.")
    # Turns blocking on without a proxy, so its effect on parsing can be
    # measured for free. Not a user feature.
    parser.add_argument("--force-block-media", action="store_true", help=argparse.SUPPRESS)
    _meter.add_budget_argument(parser)


# --------------------------------------------------------------------------- #
# Ctrl-C and SIGTERM (`docker stop`): finish the URL in hand, write what was
# read as a `partial` run (stop_reason `interrupted`; a --checkpoint keeps
# the rest for --resume) and exit 130 / 143. A second signal stops at once.
# --------------------------------------------------------------------------- #
INTERRUPTED = "interrupted"


class _StopRequest:
    def __init__(self):
        self.signum: Optional[int] = None

    @property
    def requested(self) -> bool:
        return self.signum is not None

    def _handler(self, signum, _frame):
        import sys
        if self.signum is not None:
            raise KeyboardInterrupt
        self.signum = signum
        print("\n[!] Interrupted — finishing the current URL, then writing what was read. "
              "Press Ctrl-C again to stop at once.", file=sys.stderr, flush=True)

    def install(self) -> None:
        """From the main thread, before the event loop starts (asyncio.run
        then leaves SIGINT to this handler)."""
        import signal
        import threading
        self.signum = None
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is not None:
                signal.signal(sig, self._handler)

    def exit_code(self) -> int:
        import signal
        return 128 + (self.signum or signal.SIGINT)

    def report(self, args, *, rows: int, done: int, total: int) -> int:
        import sys
        written = f"{rows} row(s) written to {args.out} (status partial, stop_reason interrupted)" if rows else \
            "nothing was written"
        hint = (f"; continue with the same command plus --resume (checkpoint {args.checkpoint})"
                if getattr(args, "checkpoint", None) else "; add --checkpoint PATH next time to be able to --resume")
        print(f"[!] Interrupted after {done} of {total} URLs: {written}{hint}.", file=sys.stderr, flush=True)
        return self.exit_code()


STOP = _StopRequest()


def interrupted_exit(args=None) -> int:
    """For an engine's main(): a second Ctrl-C (or one before the loop)."""
    import sys
    hint = ""
    if args is not None and getattr(args, "checkpoint", None):
        hint = f" Progress up to the last finished URL is in {args.checkpoint}: re-run with --resume."
    print(f"\n[!] Interrupted — stopped at once, nothing more was written.{hint}", file=sys.stderr, flush=True)
    return STOP.exit_code()
