#!/usr/bin/env python3
"""output_writer.py — the row model, JSON/CSV writer, dedupe, exit codes,
and run metadata. Carries (almost) no site knowledge: everything here is
part of the family's shared output CONTRACT — diverging from it needs a
written reason, not a per-site tweak. Ported near-verbatim from
lidl-scraper's output_writer.py (CLAUDE.md §7: porting starts by copying
the family-shared modules from the newest sibling and diffing what
changed on purpose) — in turn ported from skyscanner-scraper's, in turn
from stockx-scraper's original (commit 00b5570). The exit codes,
STATUS_BY_EXIT map, and finish_run() outcome-precedence logic are
IDENTICAL to every prior family member on purpose, including the fix
from a 2026-09-15 audit that found `blocked`/`remote_api_error` were
being silently ignored whenever products were present or --allow-empty
was passed. Only the `Product` dataclass's site-specific tail (below the
family-common fields) differs per repo.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List, Optional, Sequence

# --------------------------------------------------------------------------- #
# Exit codes — identical across playwright_scraper / selenium_scraper /
# puppeteer_scraper, and identical to every other 2scraper family repo. A
# caller (CI, a cron job, another program) must be able to tell these apart
# without parsing stdout.
# --------------------------------------------------------------------------- #
EXIT_OK = 0
EXIT_CRASH = 1
EXIT_BAD_USAGE = 2
EXIT_BLOCKED = 3
EXIT_ZERO_PRODUCTS = 4
EXIT_REMOTE_API_ERROR = 5
EXIT_PARTIAL = 6

STATUS_BY_EXIT = {
    EXIT_OK: "complete",
    EXIT_CRASH: "crashed",
    EXIT_BAD_USAGE: "bad_usage",
    EXIT_BLOCKED: "blocked",
    EXIT_ZERO_PRODUCTS: "empty",
    EXIT_REMOTE_API_ERROR: "remote_api_error",
    EXIT_PARTIAL: "partial",
}


# --------------------------------------------------------------------------- #
# Row model
# --------------------------------------------------------------------------- #
@dataclass
class Product:
    """Family-common fields first (identical name AND order to every other
    2scraper repo's row, in both JSON and CSV) so `diff_runs.py` and any
    other cross-repo tool keep working unmodified; instagram.com-specific
    fields at the end.

    One row is one PROFILE or one POST, told apart by `category` — a
    profile URL with `--posts N` yields both kinds in one run, so a second
    dataclass would split one run across two files. The family prefix is
    kept byte-identical (CLAUDE.md §9); what the commerce fields mean here:

    - `sku`: `instagram-user-{pk}` (the numeric account id survives a
      username change) or `instagram-post-{shortcode}`.
    - `category`: `"profile"` or `"post"` (a Reel is a post whose
      `product_type` is `clips`).
    - `title`: the profile's display name, or the caption's first line
      (cut at 120 characters); the full caption is `caption`.
    - `brand` / `price` / `currency` / `price_source`: **not applicable**,
      always `None`. Kept so `diff_runs.py` and the family's row prefix stay
      unchanged.
    - `product_url`: the canonical profile, post (`/p/`) or Reel (`/reel/`)
      URL; `image_url`: the profile picture, or the post's largest image.

    Site-specific tail, every value read from the page's own embedded JSON
    (see page_parser.py); a field that does not apply to the row's kind is
    `None`:

    - both kinds: `username`, `user_id`, `full_name`, `is_verified` (for a
      post: its owner's).
    - profile: `is_private`, `biography`, `bio_links_json` (the external
      links, as a JSON list), `follower_count`, `following_count`,
      `recent_posts_json` (the shortcodes of the first 12 timeline posts,
      pinned first — what `--posts` follows).
    - post: `shortcode`, `media_type` (`image`/`video`/`carousel`),
      `product_type` (the site's own: `feed`, `clips`,
      `carousel_container`), `posted_at` (UTC), `like_count` (`None` when
      the author hides it — see `likes_hidden`), `comment_count`,
      `caption`, `hashtags_json` / `mentions_json` (from the caption),
      `alt_text` (Instagram's own accessibility text), `coauthors_json`, `tagged_users_json`, `media_count` and
      `media_urls_json` (each slide of a carousel; video URLs for video),
      `video_url`, `width` / `height` (the original's).

    Not here, measured 2026-10-04: a post count (`all_media_count` is null
    for a logged-out visitor, and `og:description` rounds it to "32K"),
    a Reel's play count (not in the logged-out media object at all), a
    location (`location` was null on 36 of 36 posts, including 12 from a
    travel account — CLAUDE.md §9: a column null on every row must not
    exist; add it back with a capture that has one), and comments (see
    page_parser.py).
    """

    # --- family-common ---
    sku: Optional[str]
    source: str
    category: Optional[str]
    title: Optional[str]
    brand: Optional[str]
    price: Optional[float]
    currency: Optional[str]
    price_source: Optional[str]
    product_url: Optional[str]
    image_url: Optional[str]
    scraped_at: str

    # --- instagram.com-specific: both kinds ---
    username: Optional[str] = None
    user_id: Optional[str] = None
    full_name: Optional[str] = None
    is_verified: Optional[bool] = None
    # --- profile ---
    is_private: Optional[bool] = None
    biography: Optional[str] = None
    bio_links_json: Optional[str] = None
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    recent_posts_json: Optional[str] = None
    # --- post ---
    shortcode: Optional[str] = None
    media_type: Optional[str] = None
    product_type: Optional[str] = None
    posted_at: Optional[str] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    likes_hidden: Optional[bool] = None
    caption: Optional[str] = None
    hashtags_json: Optional[str] = None
    mentions_json: Optional[str] = None
    alt_text: Optional[str] = None
    coauthors_json: Optional[str] = None
    tagged_users_json: Optional[str] = None
    media_count: Optional[int] = None
    media_urls_json: Optional[str] = None
    video_url: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


PRODUCT_FIELD_NAMES: List[str] = [f.name for f in fields(Product)]


# --------------------------------------------------------------------------- #
# Dedupe / merge — "merge in page order, not arrival order": the caller
# passes pages already sorted by page number (or, for a concurrent run,
# sorted before calling this), never in whichever-finished-first order.
# --------------------------------------------------------------------------- #
def sku_key(p: Product) -> str:
    """The identity `merge_pages` dedupes on, and the same one an engine's
    scroll/pagination loop should track to decide "did this page add
    anything NEW" — never just "is this page non-empty" (a page repeating
    an already-seen item, e.g. past the real last batch of results, isn't
    empty but isn't new either)."""
    return p.sku or f"__no_sku__:{p.product_url}"


def merge_pages(pages: Sequence[Sequence[Product]]) -> List[Product]:
    seen: dict = {}
    order: List[str] = []
    for page in pages:
        for p in page:
            key = sku_key(p)
            if key not in seen:
                order.append(key)
            seen[key] = p  # last write for a given sku wins, in page order
    return [seen[k] for k in order]


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def write_json(products: Sequence[Product], out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)


def write_csv(products: Sequence[Product], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PRODUCT_FIELD_NAMES)
        writer.writeheader()  # written even for zero rows — a consumer reads
        for p in products:    # an empty table, not a zero-byte file.
            writer.writerow(asdict(p))


def write_output(products: Sequence[Product], out_path: str, fmt: str) -> None:
    if fmt == "json":
        write_json(products, out_path)
    elif fmt == "csv":
        write_csv(products, out_path)
    else:
        raise ValueError(f"Unsupported format: {fmt!r} (expected 'json' or 'csv')")


# --------------------------------------------------------------------------- #
# Run metadata sidecar
# --------------------------------------------------------------------------- #
def meta_path_for(out_path: str) -> str:
    return f"{out_path}.meta.json"


def write_meta(
    out_path: str,
    *,
    status: str,
    stop_reason: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]] = None,
    product_count: int,
    price_confirmed_pct: Optional[float] = None,
    started_at: float,
    finished_at: Optional[float] = None,
    extra: Optional[dict] = None,
) -> None:
    meta = {
        "status": status,
        "stop_reason": stop_reason,
        "engine": engine,
        "url": url,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "failed_pages": failed_pages or [],
        "product_count": product_count,
        "price_confirmed_pct": price_confirmed_pct,
        "started_at": started_at,
        "finished_at": finished_at or time.time(),
    }
    if extra:
        meta.update(extra)
    Path(meta_path_for(out_path)).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# finish_run — the single place every engine calls to decide exit code,
# whether to write output at all, and whether to write a sidecar. Keeping
# this in one shared function is what stops the three engines' exit-code
# mapping from drifting apart. Structurally identical to stockx-scraper's
# post-audit-fix finish_run() (commit 00b5570) — see that file's comment
# for the full incident writeup this precedence order fixes.
# --------------------------------------------------------------------------- #
def finish_run(
    *,
    products: List[Product],
    out_path: str,
    fmt: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]],
    blocked: bool,
    remote_api_error: bool,
    allow_empty: bool,
    started_at: float,
    price_confirmed_pct: Optional[float] = None,
    extra_meta: Optional[dict] = None,
    rejected_rows: int = 0,
    max_results: Optional[int] = None,
    rate_limited: bool = False,
    total_results: Optional[int] = None,
    incomplete_reason: Optional[str] = None,
    capped: Optional[bool] = None,
) -> int:
    """Decide status/exit code, write output + sidecar (or neither), return
    the process exit code. NEVER writes a sidecar for a failed run, and
    NEVER overwrites a previous good output with an empty one unless the
    caller explicitly passed --allow-empty."""
    failed_pages = failed_pages or []
    partial = bool(failed_pages) or bool(incomplete_reason)
    zero_products = len(products) == 0

    # Outcome precedence — decided ONCE, independent of --allow-empty.
    # `--allow-empty` controls only whether a zero-product result gets
    # WRITTEN as a file (below); it must never launder a blocked or
    # remote-API-error run into a "complete" status just because the
    # caller also passed --allow-empty, and it must never do so just
    # because SOME batches did return results while the run was, in fact,
    # blocked partway through.
    #
    # Rows gathered by a run that did NOT finish cleanly are `partial` (6),
    # with the cause in `stop_reason` (CLAUDE.md §25: exit 5 and exit 3
    # promise no file; 6 means "some rows, incomplete"). Audit 2026-09-30:
    # this used to return 5 or 3 AND write the rows, so a consumer keyed on
    # the exit code threw away good data. `rejected_rows` counts records
    # the parser refused — the output is then incomplete too.
    if zero_products:
        if remote_api_error:
            status, exit_code = "remote_api_error", EXIT_REMOTE_API_ERROR
        elif blocked:
            status, exit_code = "blocked", EXIT_BLOCKED
        elif partial or rejected_rows:
            status, exit_code = "remote_api_error", EXIT_REMOTE_API_ERROR
        else:
            status, exit_code = "empty", EXIT_ZERO_PRODUCTS
        stop_reason = ("rate_limited" if rate_limited and blocked else incomplete_reason
                       or ("failed_pages" if failed_pages and not blocked and not remote_api_error else status))
    elif remote_api_error or blocked or partial or rejected_rows:
        status, exit_code = "partial", EXIT_PARTIAL
        # A throttle is not a block (CLAUDE.md §24): "blocked" sends the
        # reader to buy a proxy, "rate_limited" to slow down.
        stop_reason = ("remote_api_error" if remote_api_error else "rate_limited" if rate_limited and blocked
                       else "blocked" if blocked else incomplete_reason or ("failed_pages" if partial else "rejected_rows"))
    else:
        status, exit_code = "complete", EXIT_OK
        stop_reason = status

    # The "never overwrite good output with empty" rule: a zero-product
    # outcome (whatever its status above — blocked/remote_api_error/empty
    # all zero out `products`) writes NEITHER file NOR sidecar unless the
    # caller explicitly opted in with --allow-empty. NEVER write a sidecar
    # in the not-written case — a PREVIOUS good <out>.json is left in
    # place untouched, and a stale failure sidecar sitting right beside it
    # would contradict that good data rather than describe it. The
    # engine's own logs carry the diagnostic detail — that's what a
    # failed run's output is FOR, not this file.
    if zero_products and not allow_empty:
        return exit_code

    write_output(products, out_path, fmt)
    extra = dict(extra_meta or {})
    if rejected_rows:
        extra["rejected_rows"] = rejected_rows
    # What diff_runs needs to refuse a meaningless comparison: the cap the
    # run was given (a top-N selection, where "removed" means "fell out of
    # the top N", not "delisted") and a hash binding this sidecar to the
    # exact output file beside it.
    if max_results is not None:
        extra["max_results"] = max_results
        extra["capped"] = len(products) >= max_results if capped is None else capped
    if total_results is not None:
        extra["total_results"] = total_results  # what the site says exists, vs product_count collected
    extra["output_sha256"] = hashlib.sha256(Path(out_path).read_bytes()).hexdigest()
    write_meta(
        out_path, status=status, stop_reason=stop_reason, engine=engine, url=url,
        pages_requested=pages_requested, pages_completed=pages_completed,
        failed_pages=failed_pages, product_count=len(products),
        price_confirmed_pct=price_confirmed_pct, started_at=started_at,
        extra=extra or None,
    )
    return exit_code
