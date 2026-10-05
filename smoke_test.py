#!/usr/bin/env python3
"""smoke_test.py — one file of plain functions with inline/synthetic-
fixture checks. No pytest, no conftest. `tests/test_smoke.py` wraps this as
a single pytest entry point so `pytest` also works, without a second copy
of the checks.

**What the fixtures are**: every page fixture in `tests/fixtures/` is a
REAL logged-out capture of instagram.com from 2026-10-04 (a profile, an
image post, a Reel, a carousel, the "page isn't available" page, the login
page and the throttled shell), trimmed to the route and the data blocks
page_parser.py reads, with comments and tracking tokens removed — see each
file's header comment. Values are asserted against what those captures
hold, not just that a column is populated (CLAUDE.md §10).

Run directly: `python3 smoke_test.py`
"""
from __future__ import annotations

import asyncio
import inspect as _inspect
import json
import tempfile
from pathlib import Path

import captcha_solver
import diff_runs
import env_config
import output_writer
import page_flow
import page_parser as pp
import proxy_pool
import puppeteer_scraper
import scraper_api_client
import selenium_scraper

try:
    import playwright_scraper
except Exception as exc:  # pragma: no cover — this import itself must never fail
    raise AssertionError(f"playwright_scraper must import cleanly even without playwright installed: {exc}") from exc

ROOT = Path(__file__).parent

RESULTS = []  # (name, ok, detail)


def check(name):
    """Runs the decorated function IMMEDIATELY (at module-load time) and
    records the outcome — same pattern as every other family member's
    smoke_test.py; every check function is named `_` because only RESULTS
    is ever read, nothing looks a check up by name."""
    def decorator(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc)))
        except Exception as exc:  # a check that crashes is still a failure, not an uncaught traceback
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        except SystemExit as exc:  # a CLI helper exiting inside a check would otherwise end the whole suite silently
            RESULTS.append((name, False, f"SystemExit: {exc}"))
        return fn
    return decorator


def asyncio_run_maybe(mod, args):
    """playwright_scraper.run()/puppeteer_scraper.run() are coroutines;
    selenium_scraper.run() is plain sync."""
    result = mod.run(args)
    if _inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


# --------------------------------------------------------------------------- #
# Engine import/CLI hygiene (CLAUDE.md §6)
# --------------------------------------------------------------------------- #
@check("engines import cleanly regardless of installed drivers")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        assert hasattr(mod, "build_arg_parser")
        assert hasattr(mod, "run")


@check("each engine imports its driver at MODULE level, guarded by try/except ImportError")
def _():
    for path in ("playwright_scraper.py", "selenium_scraper.py", "puppeteer_scraper.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "except ImportError as _IMPORT_ERROR" in src, f"{path}: missing guarded driver import"


@check("no forbidden overclaiming wording in any shipped .py/.md/.yml file")
def _():
    # Built from pieces so this file can be scanned too (CLAUDE.md §22: the
    # check used to exempt its own file, where the phrases sat verbatim).
    anti = "anti" + "detect"
    banned = (
        "cloud" + " browser", anti + " browser", "2scraper " + anti + " browser",
        "gate." + "2prx.com", "--" + anti, anti + "_local_api",
    )
    exempt_names = {"CLAUDE.md"}
    venvs = {p.parent for p in ROOT.rglob("pyvenv.cfg")}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in (".py", ".md", ".html", ".toml", ".cfg", ".yml", ".yaml"):
            continue
        if path.name in exempt_names or path.name.startswith("2scraper"):
            continue
        if ".git" in path.parts or "__pycache__" in path.parts or any(v in path.parents for v in venvs):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for phrase in banned:
            assert phrase not in text, f"{path.relative_to(ROOT)}: contains banned phrase {phrase!r}"


@check("all three engines expose the identical --flag set (CLAUDE.md §4)")
def _():
    def flag_set(mod):
        return {opt for a in mod.build_arg_parser()._actions for opt in a.option_strings if opt.startswith("--")}

    pw, se, pu = flag_set(playwright_scraper), flag_set(selenium_scraper), flag_set(puppeteer_scraper)
    all_engines = pw | se | pu
    for name, flags in (("playwright_scraper", pw), ("selenium_scraper", se), ("puppeteer_scraper", pu)):
        missing = all_engines - flags
        assert not missing, f"{name} is missing {sorted(missing)} that (an)other engine(s) define — flag sets have drifted apart"


@check("all three engines share the same default output filename stem")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        assert mod._default_out("json") == "instagram_results.json"


@check("all engines take --url/--urls-file, NOT --query/--category (CLAUDE.md §1 divergence)")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        flags = {opt for a in mod.build_arg_parser()._actions for opt in a.option_strings}
        assert "--url" in flags and "--urls-file" in flags
        assert "--query" not in flags and "--category" not in flags


@check("every top-level module is in the Dockerfile COPY and pyproject py-modules (a module left out breaks the image on every run — CLAUDE.md §16)")
def _():
    import re as _re
    if not (ROOT / "Dockerfile").exists() and not (ROOT / "pyproject.toml").exists():
        return  # the Docker image's own copy of this suite ships neither (CLAUDE.md §22)
    modules = sorted(pth.stem for pth in ROOT.glob("*.py"))
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    listed = set(_re.findall(r'"([a-z_]+)"', pyproject.split("py-modules", 1)[1].split("]", 1)[0]))
    for mod in modules:
        assert f"{mod}.py" in docker, f"{mod}.py missing from the Dockerfile COPY"
        assert mod in listed, f"{mod} missing from pyproject py-modules"


@check("engines skip, never fetch, an input that is not a profile/post/username — _resolve_urls filters it out")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        for bad in ("https://www.instagram.com/explore/", "https://example.com/natgeo", "https://www.instagram.com/stories/natgeo/1/"):
            urls, skipped = mod._resolve_urls(mod.build_arg_parser().parse_args(["--url", bad]))
            assert urls == [] and skipped == 1, f"{mod.__name__}: {bad} must never be attempted"


@check("BOT_CHALLENGE_MARKERS is empty, and the generic markers match NONE of the real good captures (CLAUDE.md §18: count a marker on a good page first)")
def _():
    assert pp.BOT_CHALLENGE_MARKERS == (), "Instagram answers with its own login page, not a vendor challenge — no site marker was ever seen"
    for name in ("profile_natgeo", "post_image", "post_reel", "post_carousel", "login_wall", "not_found", "throttled_post"):
        html = (ROOT / "tests" / "fixtures" / f"instagram_{name}_live_20261004.html").read_text(encoding="utf-8")
        assert not captcha_solver.detect_from_html(html, pp.BOT_CHALLENGE_MARKERS), f"{name}: a generic marker fires on a real Instagram page"
        assert not page_flow.is_challenge(html), name


# --------------------------------------------------------------------------- #
# output_writer — exit codes / precedence / dedupe (CLAUDE.md §9)
# --------------------------------------------------------------------------- #
@check("exit codes and STATUS_BY_EXIT match the family contract exactly")
def _():
    expected = {0: "complete", 1: "crashed", 2: "bad_usage", 3: "blocked", 4: "empty", 5: "remote_api_error", 6: "partial"}
    assert output_writer.STATUS_BY_EXIT == expected


def _mk_product(sku, price=None, **kw):
    defaults = dict(
        sku=sku, source="instagram.com", category="post", title="An example caption",
        brand=None, price=price, currency=None, price_source=None,
        product_url=f"https://www.instagram.com/p/{sku}/",
        image_url=None, scraped_at="2026-10-04T00:00:00Z",
        username="natgeo", shortcode=sku, like_count=5, comment_count=1,
    )
    defaults.update(kw)
    return output_writer.Product(**defaults)


@check("finish_run: rows gathered by a run that did not finish are PARTIAL (6) with the cause in stop_reason — never 5/3 with a file (CLAUDE.md §25; audit 2026-09-30 got exit 5 AND a written file). Rewrites the old pinned 'exit 5 with products' position deliberately.")
def _():
    cases = (
        (dict(blocked=True, remote_api_error=True), "remote_api_error"),
        (dict(blocked=False, remote_api_error=True), "remote_api_error"),
        (dict(blocked=True, remote_api_error=False), "blocked"),
        (dict(blocked=False, remote_api_error=False, failed_pages=[3]), "failed_pages"),
        (dict(blocked=False, remote_api_error=False, rejected_rows=2), "rejected_rows"),
    )
    for kw, reason in cases:
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            kw = {"failed_pages": None, **kw}
            code = output_writer.finish_run(
                products=[_mk_product("1")], out_path=out, fmt="json", engine="test", url="u",
                pages_requested=3, pages_completed=2, allow_empty=False, started_at=0.0, **kw,
            )
            assert code == output_writer.EXIT_PARTIAL, (kw, code)
            assert Path(out).exists(), "already-collected products must still be written out"
            meta = json.loads(Path(f"{out}.meta.json").read_text())
            assert meta["status"] == "partial" and meta["stop_reason"] == reason, (kw, meta)
            if reason == "rejected_rows":
                assert meta["rejected_rows"] == 2
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "z.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=0,
            failed_pages=None, blocked=False, remote_api_error=True, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_REMOTE_API_ERROR and not Path(out).exists(), "5 promises no file"

@check("finish_run precedence: blocked+zero-products respects --allow-empty for WHETHER to write, never for the STATUS")
def _():
    with tempfile.TemporaryDirectory() as td:
        out_a = str(Path(td) / "a.json")
        code = output_writer.finish_run(
            products=[], out_path=out_a, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=True, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert Path(out_a).exists(), "--allow-empty means a zero-product outcome DOES get written"
        meta = json.loads(Path(f"{out_a}.meta.json").read_text())
        assert meta["status"] == "blocked", "--allow-empty must never launder this into 'complete'"

        out_b = str(Path(td) / "b.json")
        code = output_writer.finish_run(
            products=[], out_path=out_b, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert not Path(out_b).exists(), "without --allow-empty, a zero-product outcome writes nothing"


@check("finish_run: zero products without --allow-empty writes neither file nor sidecar")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_ZERO_PRODUCTS
        assert not Path(out).exists()
        assert not Path(f"{out}.meta.json").exists()


@check("finish_run: partial (failed pages, some products) writes output and reports EXIT_PARTIAL")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_PARTIAL
        assert Path(out).exists()
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "partial"


@check("finish_run: a clean run with products writes output and reports EXIT_OK/complete")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a"), _mk_product("b")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_OK
        data = json.loads(Path(out).read_text())
        assert len(data) == 2


@check("Product field order: family-common fields first, instagram-specific fields after")
def _():
    expected_head = [
        "sku", "source", "category", "title", "brand", "price", "currency",
        "price_source", "product_url", "image_url", "scraped_at",
    ]
    assert output_writer.PRODUCT_FIELD_NAMES[: len(expected_head)] == expected_head
    tail = output_writer.PRODUCT_FIELD_NAMES[len(expected_head):]
    for name in ("username", "user_id", "full_name", "is_verified", "is_private", "biography", "bio_links_json",
                 "follower_count", "following_count", "recent_posts_json", "shortcode", "media_type", "product_type",
                 "posted_at", "like_count", "comment_count", "likes_hidden", "caption", "hashtags_json", "mentions_json",
                 "alt_text", "coauthors_json", "tagged_users_json", "media_count", "media_urls_json",
                 "video_url", "width", "height"):
        assert name in tail, f"{name} missing from Product's site-specific tail"
    for absent in ("post_count", "play_count", "view_count", "comments_json", "location"):
        assert absent not in tail, f"{absent}: not served to a logged-out visitor (measured 2026-10-04) — a column null on every row must not exist (CLAUDE.md §9)"


@check("brand/price/currency/price_source are always None — not applicable to a profile or a post")
def _():
    p = _mk_product("a")
    assert p.brand is None
    assert p.price is None and p.currency is None and p.price_source is None


@check("merge_pages dedupes by sku, last-write-wins, in fetch order not arrival order")
def _():
    batch1 = [_mk_product("a", title="A v1"), _mk_product("b", title="B")]
    batch2 = [_mk_product("a", title="A v2"), _mk_product("c", title="C")]  # "a" edited between runs
    merged = output_writer.merge_pages([batch1, batch2])
    skus = [p.sku for p in merged]
    assert skus == ["a", "b", "c"], f"expected batch-order with new items appended, got {skus}"
    a = next(p for p in merged if p.sku == "a")
    assert a.title == "A v2", "later batch's value must win for a repeated sku"


@check("write_csv writes a header even for zero rows")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.csv")
        output_writer.write_csv([], out)
        text = Path(out).read_text()
        assert text.strip() != ""
        assert "sku" in text.splitlines()[0]


# --------------------------------------------------------------------------- #
# proxy_pool — parsing, redaction, dead-marking (family-shared, no site knowledge)
# --------------------------------------------------------------------------- #
@check("proxy_pool rejects a malformed proxy string with ProxyParseError")
def _():
    try:
        proxy_pool.load_proxies("not a proxy!!", None)
        raise AssertionError("expected ProxyParseError")
    except proxy_pool.ProxyParseError:
        pass


@check("proxy_pool parses a credentialed proxy and masks it in logs")
def _():
    proxies = proxy_pool.load_proxies("http://user:secretpass@host.example:8080", None)
    assert len(proxies) == 1
    p = proxies[0]
    assert p.has_auth
    masked = p.masked()
    assert "secretpass" not in masked
    assert "host.example" in masked


@check("proxy_pool.redact_credentials strips login:password out of an arbitrary string")
def _():
    raw = "connect failed: ws://myuser:mysecret@cb.2captcha.com:9222 (5 attempts)"
    redacted = proxy_pool.redact_credentials(raw)
    assert "mysecret" not in redacted
    assert "myuser" not in redacted


# --------------------------------------------------------------------------- #
# captcha_solver — generic + widget-specific detection (family-shared)
# --------------------------------------------------------------------------- #
@check("captcha_solver.detect_from_html finds generic bot-challenge markers")
def _():
    assert captcha_solver.detect_from_html("<html>please complete the g-recaptcha below</html>")
    assert not captcha_solver.detect_from_html("<html><body>ordinary page, no widgets</body></html>")


@check("captcha_solver.identify_widget extracts a Turnstile sitekey")
def _():
    html = '<div class="cf-turnstile" data-sitekey="0x4AAA_example"></div>'
    signal = captcha_solver.identify_widget(html)
    assert signal is not None
    assert signal.captcha_type == captcha_solver.CaptchaType.CLOUDFLARE_TURNSTILE
    assert signal.sitekey == "0x4AAA_example"


# --------------------------------------------------------------------------- #
# env_config — INSTAGRAM_* keys, placeholder detection, precedence
# --------------------------------------------------------------------------- #
@check("env_config.ENV_KEYS matches .env.example exactly, in both directions")
def _():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = {line.split("=", 1)[0] for line in example.splitlines() if "=" in line and not line.startswith("#")}
    assert documented == set(env_config.ENV_KEYS), (documented, set(env_config.ENV_KEYS))


@check("env_config uses INSTAGRAM_ prefixed keys, not a leftover PERPLEXITY_/other-site name")
def _():
    for key in env_config.ENV_KEYS:
        assert key == "TWOCAPTCHA_KEY" or key.startswith("INSTAGRAM_"), f"unexpected env key {key!r}"


@check("env_config._is_placeholder treats a braced {...} fragment as unset")
def _():
    assert env_config._is_placeholder("")
    assert env_config._is_placeholder(None)
    assert env_config._is_placeholder("{login}-zone-scraping_browser:{password}@cb.2captcha.com")
    assert not env_config._is_placeholder("a-real-looking-value-123")


@check("env_config.apply_env never overrides an explicitly-set CLI flag")
def _():
    import os as _os
    ns = __import__("argparse").Namespace(proxy="http://explicit:pass@host:1")
    _os.environ["INSTAGRAM_PROXY"] = "http://from-env:pass@host:2"
    try:
        env_config.apply_env(ns, dotenv_path="/nonexistent/.env")
        assert ns.proxy == "http://explicit:pass@host:1"
    finally:
        del _os.environ["INSTAGRAM_PROXY"]


# --------------------------------------------------------------------------- #
# page_parser — input normalisation, routes, and the REAL captures
# --------------------------------------------------------------------------- #
_FIX = ROOT / "tests" / "fixtures"
_PROFILE_URL = "https://www.instagram.com/natgeo/"
_IMAGE_URL = "https://www.instagram.com/p/DeAZ0uAgNuA/"
_REEL_URL = "https://www.instagram.com/p/DdG4RIxIPyf/"
_CAROUSEL_URL = "https://www.instagram.com/p/DeBJRyaDIyP/"


def _fx(name):
    return (_FIX / f"instagram_{name}_live_20261004.html").read_text(encoding="utf-8")


@check("normalize_input: profile/post/Reel URLs (with or without scheme, owner prefix or query), @name and bare names become ONE canonical URL; Instagram's own routes and other hosts are refused")
def _():
    for text in ("natgeo", "@natgeo", "NatGeo", "https://instagram.com/natgeo", "instagram.com/natgeo/", "https://www.instagram.com/natgeo/?hl=en"):
        assert pp.normalize_input(text) == _PROFILE_URL, text
    for text in ("https://www.instagram.com/p/DeAZ0uAgNuA/", "instagram.com/p/DeAZ0uAgNuA",
                 "https://www.instagram.com/natgeo/p/DeAZ0uAgNuA/?img_index=1", "https://www.instagram.com/reel/DeAZ0uAgNuA/",
                 "https://www.instagram.com/reels/DeAZ0uAgNuA/"):
        assert pp.normalize_input(text) == _IMAGE_URL, text
    for text in ("https://www.instagram.com/explore/", "https://www.instagram.com/accounts/login/", "https://example.com/natgeo",
                 "https://user:pass@www.instagram.com/natgeo/", "https://www.instagram.com:8080/natgeo/", "p", "explore",
                 "https://www.instagram.com/stories/natgeo/123/", "not a name!", "", "a" * 31):
        assert pp.normalize_input(text) is None, text
    assert pp.classify_url(_PROFILE_URL) == ("profile", "natgeo") and pp.classify_url(_REEL_URL) == ("post", "DdG4RIxIPyf")


@check("make_sku: a profile is keyed by its numeric id (survives a rename), a post by its shortcode")
def _():
    assert pp.make_sku("profile", "787132") == "instagram-user-787132"
    assert pp.make_sku("post", "DeAZ0uAgNuA") == "instagram-post-DeAZ0uAgNuA"


@check("page_route names every captured page kind; has_embedded_data is False ONLY on the throttled shells")
def _():
    expected = {"profile_natgeo": pp.ROUTE_PROFILE, "post_image": pp.ROUTE_POST, "post_reel": pp.ROUTE_POST,
                "post_carousel": pp.ROUTE_POST, "not_found": pp.ROUTE_ERROR, "login_wall": pp.ROUTE_LOGIN,
                "throttled_post": pp.ROUTE_POST, "throttled_profile": pp.ROUTE_PROFILE}
    for name, route in expected.items():
        html = _fx(name)
        assert pp.page_route(html) == route, name
        assert pp.has_embedded_data(html) == (name.startswith(("profile", "post"))), name
    assert pp.page_route("<html>Chromium error page</html>") is None


@check("parse_profile on the REAL natgeo capture: values, not coverage (CLAUDE.md §10)")
def _():
    res = pp.parse_page(_fx("profile_natgeo"), url=_PROFILE_URL)
    assert res.source_used == "embedded_json" and len(res.products) == 1
    p = res.products[0]
    assert (p.sku, p.category, p.source) == ("instagram-user-787132", "profile", "instagram.com")
    assert (p.username, p.user_id, p.full_name, p.title) == ("natgeo", "787132", "National Geographic", "National Geographic")
    assert p.follower_count == 268457110 and p.following_count == 194
    assert p.is_verified is True and p.is_private is False
    assert p.biography.startswith("Step into wonder and find your inner explorer")
    links = json.loads(p.bio_links_json)
    assert links[0] == "http://visitstore.bio/natgeo" and all("l.instagram.com" not in u for u in links), "the real link, not the tracking redirect"
    codes = json.loads(p.recent_posts_json)
    assert len(codes) == 12 and codes[:3] == ["DdG4RIxIPyf", "DbIdlf0j6uA", "DeBJRyaDIyP"], "pinned first, in the site's order"
    assert res.post_codes == codes
    assert p.product_url == _PROFILE_URL and p.image_url.startswith("https://scontent.cdninstagram.com/")
    assert p.shortcode is None and p.like_count is None, "post columns stay None on a profile row"
    assert p.price is None and p.brand is None and p.currency is None


@check("parse_post on the REAL image post: counts, UTC time, caption, hashtags, mentions (trailing '.' is punctuation), alt text")
def _():
    p = pp.parse_page(_fx("post_image"), url=_IMAGE_URL).products[0]
    assert (p.sku, p.category, p.shortcode) == ("instagram-post-DeAZ0uAgNuA", "post", "DeAZ0uAgNuA")
    assert (p.media_type, p.product_type, p.media_count) == ("image", "feed", 1)
    assert p.posted_at == "2026-10-02T21:05:27Z" and p.like_count == 47528 and p.comment_count == 154
    assert p.likes_hidden is False
    assert p.caption.startswith("Presented by @Rolex. When Matt Shirley") and p.title.startswith("Presented by @Rolex.")
    assert len(p.title) <= 120
    assert "Rolex" in json.loads(p.mentions_json) and "Rolex." not in json.loads(p.mentions_json)
    assert p.alt_text.startswith("Photo by National Geographic on October 02, 2026")
    assert (p.username, p.user_id, p.is_verified) == ("natgeo", "787132", True)
    assert (p.width, p.height) == (1350, 1688) and p.video_url is None
    assert p.product_url == _IMAGE_URL and json.loads(p.media_urls_json) == [p.image_url]


@check("parse_post on the REAL Reel: video URL, /reel/ canonical URL, coauthor and tag usernames — matched by THIS shortcode, not the owner's embedded timeline")
def _():
    p = pp.parse_page(_fx("post_reel"), url="https://www.instagram.com/reel/DdG4RIxIPyf/").products[0]
    assert (p.media_type, p.product_type) == ("video", "clips")
    assert p.product_url == "https://www.instagram.com/reel/DdG4RIxIPyf/"
    assert p.video_url and ".mp4" in p.video_url and json.loads(p.media_urls_json) == [p.video_url]
    assert json.loads(p.coauthors_json) == ["rolex"] and json.loads(p.tagged_users_json) == ["rolex"]
    assert p.posted_at == "2026-09-10T13:00:14Z" and p.like_count == 78676 and p.comment_count == 525
    assert json.loads(p.hashtags_json) == ["Rolex", "PerpetualPlanet"]


@check("parse_post on the REAL carousel: one URL per slide, media_count 6, carousel type")
def _():
    p = pp.parse_page(_fx("post_carousel"), url=_CAROUSEL_URL).products[0]
    assert (p.media_type, p.product_type, p.media_count) == ("carousel", "carousel_container", 6)
    urls = json.loads(p.media_urls_json)
    assert len(urls) == 6 and len(set(urls)) == 6
    assert p.like_count == 63958 and p.comment_count == 140


@check("a URL never takes another item's data: the natgeo page read as another username, or a post read under another shortcode, is NO row")
def _():
    assert pp.parse_page(_fx("profile_natgeo"), url="https://www.instagram.com/nasa/").products == []
    assert pp.parse_page(_fx("post_image"), url="https://www.instagram.com/p/DdG4RIxIPyf/").products == []
    assert pp.parse_page(_fx("post_image"), url=_PROFILE_URL).products == [], "a post page is not a profile"


@check("a hidden like count is None with likes_hidden=True, never the number the page still carries")
def _():
    html = _fx("post_image").replace('"like_and_view_counts_disabled":false', '"like_and_view_counts_disabled":true')
    p = pp.parse_page(html, url=_IMAGE_URL).products[0]
    assert p.likes_hidden is True and p.like_count is None


@check("a post withheld from a logged-out visitor (gating_ruling, media null) is gated, not a row and not a parse error")
def _():
    html = ('<script type="application/json">{"canonicalRouteName":"%s"}</script>'
            '<script type="application/json">{"xig_polaris_media":{"code":"DeAZ0uAgNuA","gating_ruling":{"x":1},'
            '"if_not_gated_logged_out":null}}</script>') % pp.ROUTE_POST
    res = pp.parse_page(html, url=_IMAGE_URL)
    assert res.products == [] and res.gated
    o = page_flow.decide(url=_IMAGE_URL, http_status=200, html=html)
    assert o.failure == "login_required" and not o.blocked and not o.not_found


@check("safe_parse_page degrades a bad page instead of crashing the whole batch")
def _():
    original = pp.parse_profile
    pp.parse_profile = lambda *a, **kw: (_ for _ in ()).throw(ValueError("simulated parser defect"))
    try:
        res = pp.safe_parse_page(_fx("profile_natgeo"), url=_PROFILE_URL)
    finally:
        pp.parse_profile = original
    assert res.products == [] and res.source_used == "none" and res.route == pp.ROUTE_PROFILE


@check("page_flow.decide on every REAL capture: row; error route = not_found (HTTP 200!); login route = blocked; data-less shell = rate_limited; Chromium's own error page = fetch_error, never a block or a row")
def _():
    o = page_flow.decide(url=_PROFILE_URL, http_status=200, html=_fx("profile_natgeo"))
    assert o.product and not o.blocked and not o.failure and len(o.post_codes) == 12
    o = page_flow.decide(url="https://www.instagram.com/zzqx_no_such_user_8812/", http_status=200, html=_fx("not_found"))
    assert o.not_found and not o.blocked and o.product is None and not o.failure
    o = page_flow.decide(url=_PROFILE_URL, http_status=200, html=_fx("login_wall"))
    assert o.blocked and not o.rate_limited and o.product is None
    for name, url in (("throttled_post", _IMAGE_URL), ("throttled_profile", _PROFILE_URL)):
        o = page_flow.decide(url=url, http_status=200, html=_fx(name))
        assert o.blocked and o.rate_limited and not o.failure, name
    o = page_flow.decide(url=_PROFILE_URL, http_status=None, html="<html><title>www.instagram.com</title>ERR_PROXY_CONNECTION_FAILED</html>")
    assert o.failure == "fetch_error" and not o.blocked and o.product is None
    o = page_flow.decide(url=_PROFILE_URL, http_status=429, html="")
    assert o.blocked and o.rate_limited
    broken = _fx("profile_natgeo").replace('"follower_count"', '"follower_kount"')
    o = page_flow.decide(url=_PROFILE_URL, http_status=200, html=broken)
    assert o.failure == "parse_error" and not o.blocked, "data present but unreadable is OUR bug, reported as such"


# --------------------------------------------------------------------------- #
# CLI validation — bad usage never crashes, never writes output
# --------------------------------------------------------------------------- #
@check("each engine: same --posts flag (0-12, default 0), and an unusable input is skipped (never fetched), leaving EXIT_BAD_USAGE when nothing is left")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        args = mod.build_arg_parser().parse_args(["--url", "natgeo", "--posts", "12", "--max-results", "5"])
        assert args.posts == 12 and args.max_results == 5 and mod.build_arg_parser().parse_args(["--url", "x"]).posts == 0
        with tempfile.TemporaryDirectory() as td:
            args = mod.build_arg_parser().parse_args(["--url", "https://www.instagram.com/explore/", "--out", str(Path(td) / "o.json")])
            assert asyncio_run_maybe(mod, args) == output_writer.EXIT_BAD_USAGE, mod.__name__
            args = mod.build_arg_parser().parse_args(["--url", "natgeo", "--posts", "13", "--out", str(Path(td) / "o.json")])
            assert asyncio_run_maybe(mod, args) == output_writer.EXIT_BAD_USAGE, f"{mod.__name__}: a logged-out profile embeds only 12"
            assert not Path(td, "o.json").exists()


@check("each engine: no --url/--urls-file is EXIT_BAD_USAGE, not a crash")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            args = mod.build_arg_parser().parse_args(["--out", out])
            code = asyncio_run_maybe(mod, args)
            assert code == output_writer.EXIT_BAD_USAGE, f"{mod.__name__}: expected EXIT_BAD_USAGE, got {code}"
            assert not Path(out).exists()


@check("each engine: a malformed --proxy is EXIT_BAD_USAGE, not a crash, and writes nothing")
def _():
    # Best-effort, not a requirement that a driver be installed: CLAUDE.md
    # §6 requires smoke_test.py to run cleanly with ZERO engine drivers
    # present (that's exactly what the "offline checks" CI job installs).
    # "This particular engine wasn't skipped" is asserted per engine by
    # tests.yml's own engine-smoke jobs.
    _IMPORT_ERROR_ATTR = {
        "playwright_scraper": "_PLAYWRIGHT_IMPORT_ERROR",
        "selenium_scraper": "_SELENIUM_IMPORT_ERROR",
        "puppeteer_scraper": "_PYPPETEER_IMPORT_ERROR",
    }
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        if getattr(mod, _IMPORT_ERROR_ATTR[mod.__name__], None) is not None:
            continue
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            args = mod.build_arg_parser().parse_args(["--url", "natgeo", "--proxy", "not a proxy!!", "--out", out])
            code = asyncio_run_maybe(mod, args)
            assert code == output_writer.EXIT_BAD_USAGE, (
                f"{mod.__name__}: a malformed --proxy must exit {output_writer.EXIT_BAD_USAGE} "
                f"(bad usage), got {code}"
            )
            assert not Path(out).exists(), f"{mod.__name__}: a bad-usage run must never write output"


@check("selenium_scraper refuses a credentialed --cdp-endpoint with EXIT_BAD_USAGE")
def _():
    args = selenium_scraper.build_arg_parser().parse_args([
        "--url", "natgeo", "--cdp-endpoint", "ws://user:pass@cb.2captcha.com:9222",
    ])
    code = selenium_scraper.run(args)
    assert code == output_writer.EXIT_BAD_USAGE


@check("each engine rejects --max-results 0 at the argparse level")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        try:
            mod.build_arg_parser().parse_args(["--url", "natgeo", "--max-results", "0"])
            raise AssertionError(f"{mod.__name__}: expected argparse to reject --max-results 0")
        except SystemExit:
            pass


# --------------------------------------------------------------------------- #
# diff_runs / scraper_api_client — sanity only (family-shared, no site knowledge)
# --------------------------------------------------------------------------- #
@check("diff_runs reports added/removed/changed between two real finish_run() outputs")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a", title="A v1"), _mk_product("b", title="B")],
            out_path=old_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a", title="A v1"), _mk_product("c", title="C")],
            out_path=new_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        result = diff_runs.diff(old_out, new_out)
        assert result["added"] == ["c"]
        assert result["removed"] == ["b"]


@check("diff_runs refuses to compare a non-'complete' run")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=old_out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=new_out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        try:
            diff_runs.diff(old_out, new_out)
            raise AssertionError("expected a refusal — old run is 'partial', not 'complete'")
        except SystemExit:
            pass


@check("scraper_api_client.TwoCaptchaClient._require_key rejects a missing/empty key")
def _():
    client = scraper_api_client.TwoCaptchaClient("")
    try:
        client._require_key()
        raise AssertionError("expected TwoCaptchaAuthError")
    except scraper_api_client.TwoCaptchaAuthError:
        pass


@check("scraper_api_client honors --captcha-api override, not the module-level API_BASE")
def _():
    client = scraper_api_client.TwoCaptchaClient("fakekey", api_base="https://mock.example.test")
    assert client.api_base == "https://mock.example.test"
    assert client.api_base != scraper_api_client.API_BASE


def _diff_run(td, name, rows, url, **kw):
    out = str(Path(td) / name)
    kw.setdefault("allow_empty", False)
    output_writer.finish_run(products=rows, out_path=out, fmt="json", engine="t", url=url, pages_requested=1,
                             pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False,
                             started_at=0.0, **kw)
    return out


@check("diff_runs refuses different selections, never calls a currency switch a price change (even at the same number), reads a capped top-N's missing SKU as left_selection, and rejects a sidecar that does not describe its file (audit 2026-09-30)")
def _():
    dress, jeans = "https://us.shein.com/pdsearch/dress/", "https://us.shein.com/pdsearch/jeans/"
    with tempfile.TemporaryDirectory() as td:
        a = _diff_run(td, "a.json", [_mk_product("s1", 9.93, currency="USD")], dress)
        b = _diff_run(td, "b.json", [_mk_product("s1", 19.93, currency="EUR")], jeans)
        try:
            diff_runs.diff(a, b)
            raise AssertionError("different selections must be refused")
        except SystemExit as exc:
            assert "different selections" in str(exc)
        r = diff_runs.diff(a, b, allow_different_scope=True)
        assert not r["changed"] and len(r["currency_changed"]) == 1

        c = _diff_run(td, "c.json", [_mk_product("s1", 10.0, currency="USD")], dress)
        e = _diff_run(td, "e.json", [_mk_product("s1", 10.0, currency="EUR")], dress + "?")
        r = diff_runs.diff(c, e)
        assert r["currency_changed"] and not r["changed"], "same number, other currency must still be reported"

        f = _diff_run(td, "f.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=2)
        g = _diff_run(td, "g.json", [_mk_product("s1"), _mk_product("s3")], dress, max_results=2)
        r = diff_runs.diff(f, g)
        assert r["capped"] and r["left_selection"] == ["s2"] and r["removed"] == [] and r["added"] == ["s3"]
        h = _diff_run(td, "h.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=50)
        i = _diff_run(td, "i.json", [_mk_product("s1")], dress, max_results=50)
        r = diff_runs.diff(h, i)
        assert r["removed"] == ["s2"] and not r["capped"], "an uncapped run's missing SKU really is removed"

        Path(g).write_text("[]", encoding="utf-8")
        try:
            diff_runs.diff(f, g)
            raise AssertionError("a sidecar whose hash does not match must be refused")
        except SystemExit as exc:
            assert "output_sha256" in str(exc)

@check("the credential scanner FINDS a planted key in every shape seen in the family (JSON-quoted, JSON-escaped, 32-hex next to a key word) and ignores placeholders, type hints and Python-name mappings — a scanner that cannot fail is not one (CLAUDE.md §24/§25)")
def _():
    import importlib.util
    scanner = ROOT / ".github" / "ci_checks.py"
    if not (ROOT / ".github").is_dir():
        return
    spec = importlib.util.spec_from_file_location("shein_ci_checks_planted", scanner)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fake32 = "0123456789abcdef" * 2
    for planted in ('"api_key": "a8f3k2m9q7x1z5b4"', '{\\"api_key\\": \\"a8f3k2m9q7x1z5b4\\"}',
                    "TWOCAPTCHA_KEY=" + fake32, '"clientKey":"' + fake32 + '"'):
        assert mod.scan_text("planted.txt", planted), f"scanner missed a planted credential: {planted!r}"
    for harmless in ("api_key: Optional[str] = None", "TWOCAPTCHA_KEY=your-key-here", '"TWOCAPTCHA_KEY": "twocaptcha_key",'):
        assert not mod.scan_text("ok.txt", harmless), f"false positive: {harmless!r}"

@check("no workflow imports a local module inline — tests.yml calls ci_checks.py instead (CLAUDE.md §26: an inline heredoc import is red only on the first push)")
def _():
    import re as _re
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no .github/ (CLAUDE.md §22)
    local = {p.stem for p in ROOT.glob("*.py")}
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        for m in _re.finditer(r"^\s*(?:from\s+([A-Za-z_]\w*)\s+import|import\s+([A-Za-z_]\w*))", text, _re.M):
            name = m.group(1) or m.group(2)
            assert name not in local, f"{wf.name}: imports local module {name!r} inline"
    assert "ci_checks.py --sample-check" in (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

@check(".gitignore covers every artefact a run writes (CLAUDE.md §22/§26): .env copies, --dump-html challenge screenshots, *.pageN dumps, live/ — while sample outputs, fixtures and .env.example stay tracked")
def _():
    import subprocess as _sp
    if not (ROOT / ".git").exists():
        return
    must_ignore = [".env", ".env.bak", ".env.local", "instagram_results_debug_1.html",
                   "out.json.page3", "live/x.html", "instagram_results.json", "run.json"]
    must_keep = [".env.example", "sample_output.json", "sample_output.csv", "tests/fixtures/instagram_profile_natgeo_live_20261004.html"]
    for path in must_ignore:
        assert _sp.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0, f"not ignored: {path}"
    for path in must_keep:
        assert _sp.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode != 0, f"wrongly ignored: {path}"


@check("sidecar records sort, total_results, solves_spent, max_results/capped and the output hash; a throttle with rows is stop_reason=rate_limited (CLAUDE.md §24); diff_runs refuses runs of another --sort")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "o.json")
        code = output_writer.finish_run(
            products=[_mk_product("1")], out_path=out, fmt="json", engine="t", url="u", pages_requested=2,
            pages_completed=1, failed_pages=None, blocked=True, remote_api_error=False, allow_empty=False,
            started_at=0.0, rate_limited=True, total_results=40, max_results=1, extra_meta={"sort": None, "solves_spent": 0},
        )
        meta = json.loads(Path(out + ".meta.json").read_text())
        assert code == output_writer.EXIT_PARTIAL and meta["stop_reason"] == "rate_limited", meta
        assert meta["total_results"] == 40 and meta["capped"] is True and len(meta["output_sha256"]) == 64
        url = "https://www.instagram.com/natgeo/"
        a = _diff_run(td, "a.json", [_mk_product("s1")], url, extra_meta={"sort": "a"})
        b = _diff_run(td, "b.json", [_mk_product("s1")], url, extra_meta={"sort": "b"})
        try:
            diff_runs.diff(a, b)
            raise AssertionError("different --sort must be refused")
        except SystemExit as exc:
            assert "--sort" in str(exc)


# --------------------------------------------------------------------------- #
# page_flow — the ONE fetch loop (CLAUDE.md §26)
# --------------------------------------------------------------------------- #
_ENGINES = (playwright_scraper, selenium_scraper, puppeteer_scraper)
_SESSION = {"playwright_scraper": "_PlaywrightSession", "selenium_scraper": "_SeleniumSession", "puppeteer_scraper": "_PyppeteerSession"}
_ENGINE = {"playwright_scraper": "_PlaywrightEngine", "selenium_scraper": "_SeleniumEngine", "puppeteer_scraper": "_PyppeteerEngine"}


@check("the fetch loop exists ONCE: no engine carries its own fetch/parse/finish copy, each calls page_flow.run and page_flow.resolve_urls; the CDP connect goes through connect_with_retry (bounded, retried, 401 explained); pyppeteer disconnects instead of closing the remote browser")
def _():
    import ast as _ast
    for mod in _ENGINES:
        src = (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
        defs = {n.name for n in _ast.walk(_ast.parse(src)) if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
        for gone in ("scrape_one_page", "scrape_urls", "_fetch_json", "fetch_json"):
            assert gone not in defs, f"{mod.__name__}: still defines {gone}"
        for fragment in ("finish_run(", "page_flow.decide(", "parse_page(", "discover"):
            assert fragment not in src, f"{mod.__name__}: loop logic {fragment!r} outside page_flow"
        assert src.count("page_flow.run(") == 1 and mod._resolve_urls is page_flow.resolve_urls, mod.__name__
    for mod in (playwright_scraper, puppeteer_scraper):
        assert "scraper_api_client.connect_with_retry(" in (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
    pw = (ROOT / "playwright_scraper.py").read_text(encoding="utf-8")
    assert "reuse_default and browser.contexts" in pw
    pup = (ROOT / "puppeteer_scraper.py").read_text(encoding="utf-8")
    assert "await browser.disconnect()" in pup and "_release(remote_browser, remote=True)" in pup
    assert "get_event_loop().run_until_complete" not in pup


@check("§26 ops set, derived from page_flow's AST (every session.<op> / engine.<op> the loop uses): each engine's session and engine class provides all of them, and so does the Scraper API engine; flags the loop reads exist in all three")
def _():
    import ast as _ast
    import scraper_api_engine
    tree = _ast.parse((ROOT / "page_flow.py").read_text(encoding="utf-8"))
    ops = {"session": set(), "engine": set()}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name) and node.value.id in ops:
            ops[node.value.id].add(node.attr)
    assert {"goto", "content", "wait", "close"} <= ops["session"], ops
    assert {"open", "sleep", "solve_captcha", "readiness_s", "name"} <= ops["engine"], ops
    pairs = [(getattr(m, _SESSION[m.__name__]), getattr(m, _ENGINE[m.__name__]), m.__name__) for m in _ENGINES]
    pairs.append((scraper_api_engine._ScraperApiSession, scraper_api_engine.ScraperApiEngine, "scraper_api_engine"))
    for sc, ec, name in pairs:
        assert not [o for o in ops["session"] if not hasattr(sc, o)], name
        assert not [o for o in ops["engine"] if not hasattr(ec, o)], name
    for mod in _ENGINES:
        a = mod.build_arg_parser().parse_args(["--url", "natgeo"])
        assert a.max_solves == 8 and a.delay_between_pages == 2.0 and a.retries == 2 and a.max_results == 100, mod.__name__


class _FakeSession:
    def __init__(self, script):
        self.script, self.closed = dict(script), False

    async def goto(self, url):
        step = self.script.get("goto")
        if isinstance(step, Exception):
            raise step
        return self.script.get("status", 200)

    async def content(self):
        return self.script.get("html", "")

    async def wait(self, seconds):
        return None

    async def close(self):
        self.closed = True


class _FakeEngine:
    """Answers each URL from a {url: script} map (a script per URL, the way
    the real site answers), recording what was asked for."""
    name = "fake"
    readiness_s = 0

    def __init__(self, by_url, solve=None):
        self.by_url, self.sessions, self.slept, self.asked, self._solve = by_url, [], [], [], solve

    async def open(self, proxy):
        engine = self

        class _S(_FakeSession):
            async def goto(self, url):
                engine.asked.append(url)
                self.script = dict(engine.by_url.get(url, engine.by_url.get("*", {})))
                return await _FakeSession.goto(self, url)
        sess = _S({})
        self.sessions.append(sess)
        return sess

    async def sleep(self, seconds):
        self.slept.append(seconds)

    async def solve_captcha(self, session, *, html, url):
        return self._solve() if self._solve else None


def _pflow(by_url, argv, *, solve=None, proxy_pool=None):
    args = playwright_scraper.build_arg_parser().parse_args([*argv, "--delay-between-pages", "0"])
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    urls, _skipped = page_flow.resolve_urls(args)
    engine = _FakeEngine(by_url, solve)
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / "o.json")
        rc = asyncio.run(page_flow.run(engine, args, urls=urls, proxy_pool=proxy_pool, client=None, started_at=0.0))
        meta_p = Path(args.out + ".meta.json")
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else None
    assert all(s.closed for s in engine.sessions), "every opened session must be closed"
    return rc, meta, rows, engine


def _urls_file(lines):
    f = Path(tempfile.mkdtemp()) / "urls.txt"
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


@check("page_flow END TO END with a fake engine on the REAL captures: a profile reads fully; --posts queues its posts right after it, in the site's order, each fetched once; a dead name is page_not_found (exit 4, not blocked); duplicates collapse; --max-results caps the total")
def _():
    profile, post = _fx("profile_natgeo"), _fx("post_image")
    rc, meta, rows, eng = _pflow({_PROFILE_URL: {"html": profile}}, ["--url", "natgeo"])
    assert rc == output_writer.EXIT_OK and [r["sku"] for r in rows] == ["instagram-user-787132"] and eng.asked == [_PROFILE_URL]
    assert meta["selection"] == {"mode": "urls", "urls": [_PROFILE_URL], "posts": 0, "max_results": 100, "since": None, "incremental_sha256": None}

    rc, meta, rows, eng = _pflow({_PROFILE_URL: {"html": profile}, _IMAGE_URL: {"html": post}, "*": {"html": _fx("not_found")}},
                                 ["--urls-file", str(_urls_file(["@natgeo", "https://www.instagram.com/p/DeAZ0uAgNuA/", "natgeo"])), "--posts", "3"])
    codes = json.loads(rows[0]["recent_posts_json"])[:3]
    assert eng.asked == [_PROFILE_URL] + [pp.post_url(c) for c in codes] + [_IMAGE_URL], eng.asked
    assert rc == output_writer.EXIT_OK and meta["pages_requested"] == 5 and len(rows) == 2, (rc, meta)
    assert len(meta["not_found_urls"]) == 3, "the three timeline posts answered the error page in this fake"

    rc, meta, rows, eng = _pflow({"*": {"html": _fx("not_found")}}, ["--url", "zzqx_no_such_user_8812"])
    assert rc == output_writer.EXIT_ZERO_PRODUCTS and meta is None, "only not-found inputs: exit 4, nothing written"

    rc, meta, rows, eng = _pflow({_PROFILE_URL: {"html": profile}, "*": {"html": post}}, ["--url", "natgeo", "--posts", "12", "--max-results", "4"])
    assert len(eng.asked) == 4 and meta["capped"] is True and meta["pages_requested"] == 4, meta


@check("page_flow on blocks: the login page and the throttled shell are blocked (exit 3, nothing written); a dead proxy is fetch_error; without a pool the run STOPS after 3 blocks in a row and reports the rest as not_attempted; rows already read are kept as partial (6)")
def _():
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("login_wall")}}, ["--url", "natgeo"])
    assert rc == output_writer.EXIT_BLOCKED and meta is None
    many = [f"user{i}" for i in range(8)]
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("throttled_profile")}}, ["--urls-file", str(_urls_file(many))])
    assert rc == output_writer.EXIT_BLOCKED and len(eng.asked) == page_flow.STOP_AFTER_CONSECUTIVE_BLOCKS, eng.asked
    rc, meta, rows, eng = _pflow({_PROFILE_URL: {"html": _fx("profile_natgeo")}, "*": {"html": _fx("throttled_post")}},
                                 ["--urls-file", str(_urls_file(["natgeo"] + many))])
    assert rc == output_writer.EXIT_PARTIAL and meta["stop_reason"] == "rate_limited" and len(rows) == 1, meta
    reasons = [f["reason"] for f in meta["failed_urls"]]
    assert reasons.count("not_attempted") == 8 - page_flow.STOP_AFTER_CONSECUTIVE_BLOCKS and reasons.count("rate_limited") == 3, reasons
    rc, meta, rows, eng = _pflow({"*": {"goto": RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")}}, ["--url", "natgeo", "--retries", "1"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta is None, rc


@check("page_flow and the paid-solve budget: a page carrying a generic challenge marker (no Instagram route) gets at most --max-solves solves across the WHOLE run")
def _():
    challenge = '<html><div class="g-recaptcha" data-sitekey="x"></div></html>'
    solves = []
    rc, meta, rows, eng = _pflow({"*": {"html": challenge, "status": 403}},
                                 ["--urls-file", str(_urls_file(["a1", "a2", "a3"])), "--max-solves", "1"],
                                 solve=lambda: solves.append(1) or {"action": "warning_solver_error"})
    assert len(solves) == 1 and rc == output_writer.EXIT_BLOCKED, (len(solves), rc)


class _FakeScrapeClient:
    """scraper_api_client.TwoCaptchaClient.scrape_url, scripted: each call
    takes the next answer — a (target status, body) pair or an exception."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def scrape_url(self, url, *, data_format, timeout, cdp_url):
        self.calls.append((url, cdp_url))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return scraper_api_client.ScrapeResult(target_status=status, headers={}, body=body)


def _sapi(answers, argv, cdp="ws://u:p@cb.example:9222"):
    import scraper_api_engine
    args = playwright_scraper.build_arg_parser().parse_args([*argv, "--delay-between-pages", "0"])
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    urls, _skipped = page_flow.resolve_urls(args)
    client = _FakeScrapeClient(answers)
    engine = scraper_api_engine.ScraperApiEngine(client, cdp)
    slept = []

    async def _sleep(seconds):  # record, don't wait
        if engine.fatal is None:
            slept.append(seconds)
    engine.sleep = _sleep
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / "o.json")
        rc = asyncio.run(page_flow.run(engine, args, urls=urls, proxy_pool=None, client=None, started_at=0.0))
        meta_p = Path(args.out + ".meta.json")
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else None
    return rc, meta, rows, client, slept


@check("--scraper-api END TO END on the REAL captures (a fake Scraper API client): one call per URL, routed through the CDP profile when one is set and through the default pool when not; a profile and its posts read fully; a dead name is page_not_found; a login wall is blocked; a refused key is exit 5 with no further calls; a transient error is retried")
def _():
    rc, meta, rows, client, _ = _sapi([(200, _fx("profile_natgeo")), (200, _fx("post_reel"))], ["--url", "natgeo", "--posts", "1"])
    assert rc == output_writer.EXIT_OK and [r["category"] for r in rows] == ["profile", "post"], rows
    assert meta["engine"] == "scraper_api" and [c[1] for c in client.calls] == ["ws://u:p@cb.example:9222"] * 2
    assert [c[0] for c in client.calls] == [_PROFILE_URL, "https://www.instagram.com/p/DdG4RIxIPyf/"]

    rc, meta, rows, client, _ = _sapi([(200, _fx("post_image"))], ["--url", _IMAGE_URL], cdp=None)
    assert rc == output_writer.EXIT_OK and client.calls == [(_IMAGE_URL, None)]

    rc, meta, rows, client, _ = _sapi([(200, _fx("not_found"))], ["--url", "zzqx_no_such_user_8812"])
    assert rc == output_writer.EXIT_ZERO_PRODUCTS and len(client.calls) == 1, "not found is final, never retried"

    rc, meta, rows, client, _ = _sapi([(200, _fx("login_wall"))], ["--url", "natgeo"])
    assert rc == output_writer.EXIT_BLOCKED

    refused = scraper_api_client.TwoCaptchaAuthError("Scraper API: invalid/missing TWOCAPTCHA_KEY")
    rc, meta, rows, client, slept = _sapi([refused], ["--urls-file", str(_urls_file(["a1", "a2", "a3"]))])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and len(client.calls) == 1 and slept == [], (rc, len(client.calls), slept)

    flaky = scraper_api_client.TwoCaptchaError("Scraper API returned HTTP 502: bad gateway")
    rc, meta, rows, client, _ = _sapi([flaky, (200, _fx("post_image"))], ["--url", _IMAGE_URL])
    assert rc == output_writer.EXIT_OK and len(client.calls) == 2, "a transient Scraper API error is retried"


@check("--scraper-api usage, all three engines, with NO engine driver needed: no key is exit 2 before any call; Selenium does not refuse a credentialed endpoint in this mode (2Captcha reads it, not chromedriver)")
def _():
    for mod in _ENGINES:
        base = ["--url", "natgeo", "--scraper-api", "--out", str(Path(tempfile.mkdtemp()) / "o.json")]
        a = mod.build_arg_parser().parse_args(base + ["--cdp-endpoint", "ws://u:p@cb.example:9222"])
        rc = mod.run(a) if mod is selenium_scraper else asyncio.run(mod.run(a))
        assert rc == output_writer.EXIT_BAD_USAGE, (mod.__name__, rc)
    src = (ROOT / "selenium_scraper.py").read_text(encoding="utf-8")
    assert src.index("if args.scraper_api:") < src.index("_cdp_endpoint_has_credentials(args.cdp_endpoint):\n")


@check("pyppeteer answers proxy auth over CDP Fetch, never page.authenticate() — measured 2026-10-04: current Chromium has no Network.setRequestInterception, so every proxied URL failed")
def _():
    src = (ROOT / "puppeteer_scraper.py").read_text(encoding="utf-8")
    assert "await page.authenticate(" not in src
    for op in ('"Fetch.enable"', '"Fetch.authRequired"', '"Fetch.continueWithAuth"', '"Fetch.requestPaused"', '"Fetch.continueRequest"'):
        assert op in src, op


@check("fingerprint_client.user_agent_from reads the LIVE response shape (userAgent.userAgent, measured 2026-10-04) and the documented one (userAgent.value); nothing applied when neither is there")
def _():
    import fingerprint_client as fc
    live = {"id": 6472645, "country": "US", "userAgent": {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "fullVersion": "152.0.7977.64", "platform": "Windows", "platformVersion": "10.0.0", "mobile": False}}
    assert fc.user_agent_from(live).startswith("Mozilla/5.0 (Windows NT 10.0"), "the live shape — reading only `value` made --fingerprint a no-op"
    assert fc.user_agent_from({"userAgent": {"value": "UA-doc"}}) == "UA-doc"
    for empty in ({}, {"userAgent": {}}, {"userAgent": {"userAgent": ""}}, None):
        assert fc.user_agent_from(empty) is None, empty
    for path in ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py"):
        assert 'log.info("Fingerprint applied: user agent %s", user_agent)' in (ROOT / path).read_text(encoding="utf-8"), path


def run() -> int:
    """All @check-decorated functions above already ran at import time
    (that's the point — see the `check()` docstring) and self-registered
    into RESULTS. This just reports them."""
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"smoke_test: {passed}/{len(RESULTS)} checks passed")
    for name, detail in failed:
        print(f"  FAIL: {name}\n        {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    import sys
    sys.exit(run())
