#!/usr/bin/env python3
"""page_parser.py — this IS the instagram.com site knowledge.

Written 2026-10-04 from live captures of public pages, fetched logged out
(no account, no cookies) from a residential IP: a profile
(`/natgeo/`), an image post, a Reel, a carousel post, a username that does
not exist, a shortcode that does not exist, and the login page. The
trimmed captures are in `tests/fixtures/`.

What a logged-out visitor is actually served:

  - **Every page is the same React shell, and its data is server-rendered
    into `<script type="application/json" data-sjs>` blocks.** No JSON-LD
    (0 blocks on every capture), and the Open Graph tags round the numbers
    ("268M Followers", "48K likes"), so neither is a row source. A plain
    HTTP GET returns the same blocks as a browser: the data does not need
    JavaScript to run.
  - **Which page was served is named by the route**, in the page config:
    `"canonicalRouteName":"comet.igweb.<Route>"`.
      * `PolarisLoggedOutDesktopWWWProfileRoute` — a profile;
      * `PolarisLoggedOutDesktopWWWPostRoute` — a post or a Reel;
      * `PolarisErrorRoute` — the "page isn't available" page. A username or
        shortcode that does not exist gets it under HTTP **200**, so the
        status code cannot tell a dead URL from a live one;
      * `PolarisCAAIGLoginHomepageRoute` — the login page: what Instagram
        serves instead of the content when it wants a login. That is a
        block, not an empty result.
  - **A throttled client gets the right route with the data left out.**
    Measured 2026-10-04 from one residential IP: after about 75 logged-out
    page loads in an hour, plain HTTP requests (curl) got every profile and
    post as the usual route and the usual Open Graph tags, but with no
    `xig_user_by_username` / `xig_polaris_media` and no `…RootContentQuery`
    preloader — the shell of the page without its content
    (`tests/fixtures/instagram_throttled_post_live_20261004.html`). A real
    browser on the SAME IP, minutes later, still got the full data for 6
    of 6 pages: the limit follows the client, not only the address. This
    is a rate limit (`rate_limited`), not a missing profile and not a parse
    failure.
  - **A profile** carries `xig_user_by_username` twice: once with the
    profile fields (`pk`, `username`, `full_name`, `biography`,
    `bio_links`, `is_verified`, `is_private`, `follower_count`,
    `following_count`, `profile_pic_url`), once with
    `polaris_ordered_timeline_connection` — the first 12 timeline posts.
    Those nodes carry only `code`, `caption`, `display_uri`, `media_type`,
    `product_type` and an alt text: no like count and no timestamp. Pinned
    posts come first, so the order is not chronological.
    `all_media_count` is `null` for a logged-out visitor, which is why there
    is no post-count column (the "32K Posts" in `og:description` is rounded).
  - **A post or Reel** carries `xig_polaris_media`, whose
    `if_not_gated_logged_out` holds the full media object: `taken_at`,
    `like_count`, `comment_count`, `caption`, `image_versions2`,
    `video_versions` (Reels), `carousel_media` (carousels),
    `coauthor_producers`, `usertags`, `user` (and a `location` that was
    null on all 36 posts read live, so it is not a column). The same page also
    embeds its owner's timeline, so the media object is matched by the
    URL's own shortcode, never taken as "the first one found".
  - `web_profile_info` (`/api/v1/users/web_profile_info/`), the JSON
    endpoint older Instagram scrapers used, answers **401
    `require_login`** to a logged-out visitor — from curl, from a
    residential proxy, and from inside a loaded profile page — measured
    2026-10-04. It is not used.

Not collected, on purpose: comments (the post page embeds the first ones,
but they are other people's names and words, and the README promises
public post and profile data only), anything behind a login (followers
lists, the full timeline past the first 12 posts, stories, search).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterator, List, Optional
from urllib.parse import urlparse

from output_writer import Product
from bs4 import BeautifulSoup

log = logging.getLogger("page_parser")

BASE_URL = "https://www.instagram.com"
SOURCE = "instagram.com"

MIN_CARD_MATCHES = 1  # one URL is one profile or one post (CLAUDE.md §5 names the constant; there is no grid)

# The routes a logged-out page can be served as (see the module docstring).
ROUTE_PROFILE = "comet.igweb.PolarisLoggedOutDesktopWWWProfileRoute"
ROUTE_POST = "comet.igweb.PolarisLoggedOutDesktopWWWPostRoute"
ROUTE_ERROR = "comet.igweb.PolarisErrorRoute"
ROUTE_LOGIN = "comet.igweb.PolarisCAAIGLoginHomepageRoute"
_ROUTE_RE = re.compile(r'"canonicalRouteName"\s*:\s*"([A-Za-z0-9_.]+)"')

# No captcha or bot-challenge vendor was met on any logged-out capture:
# Instagram answers a visitor it distrusts with its own login page (above),
# not with a challenge. The generic captcha_solver markers stay on; this
# site adds none. Counted on the good captures first (CLAUDE.md §18): the
# generic set matches nothing on a real profile or post page.
BOT_CHALLENGE_MARKERS: tuple = ()


# Usernames: letters, digits, `.` and `_`, at most 30 characters.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,64}$")
_POST_KINDS = ("p", "reel", "reels", "tv")
# First path segments that are Instagram's own routes, never a username.
_RESERVED_SEGMENTS = frozenset({
    "accounts", "explore", "direct", "stories", "about", "legal", "developer",
    "challenge", "api", "graphql", "web", "emails", "session", "privacy",
    "p", "reel", "reels", "tv", "locations", "popular", "ajax", "query",
    "publicapi", "logging", "client_error", "qp", "oauth", "data", "press",
})

MEDIA_TYPES = {1: "image", 2: "video", 8: "carousel"}

_HASHTAG_RE = re.compile(r"(?<![\w&])#(\w+)", re.U)
_MENTION_RE = re.compile(r"(?<![\w.])@([A-Za-z0-9._]{1,30})")


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def _instagram_parts(url: str):
    try:
        parts = urlparse(url)
        ok = (parts.scheme in ("http", "https")
              and parts.hostname in ("instagram.com", "www.instagram.com")
              and parts.username is None and parts.password is None
              and parts.port in (None, 80 if parts.scheme == "http" else 443))
    except ValueError:
        return None
    return parts if ok else None


def classify_url(url: str) -> tuple:
    """(kind, key) for a URL this scraper can read, or (None, None):
    ("profile", username) or ("post", shortcode). A post URL may carry its
    owner's name first (`/natgeo/p/CODE/`, the form `og:url` uses)."""
    parts = _instagram_parts(url)
    if parts is None:
        return None, None
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) == 3 and segs[1] in _POST_KINDS and _USERNAME_RE.match(segs[0]):
        segs = segs[1:]
    if len(segs) == 2 and segs[0] in _POST_KINDS and _SHORTCODE_RE.match(segs[1]):
        return "post", segs[1]
    if len(segs) == 1 and segs[0].lower() not in _RESERVED_SEGMENTS and _USERNAME_RE.match(segs[0]):
        return "profile", segs[0].lower()
    return None, None


def is_item_url(url: str) -> bool:
    return classify_url(url)[0] is not None


def normalize_input(text: str) -> Optional[str]:
    """A line of input → a canonical URL, or None. Accepts a profile or post
    URL (with or without `https://`), `@username` or a bare username."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        text = text[1:]
    if _USERNAME_RE.match(text) and text.lower() not in _RESERVED_SEGMENTS:
        return profile_url(text)
    if text.startswith(("instagram.com/", "www.instagram.com/")):
        text = "https://" + text
    kind, key = classify_url(text)
    if kind == "profile":
        return profile_url(key)
    if kind == "post":
        return post_url(key)
    return None


def profile_url(username: str) -> str:
    return f"{BASE_URL}/{username.lower()}/"


def post_url(shortcode: str) -> str:
    return f"{BASE_URL}/p/{shortcode}/"


# --------------------------------------------------------------------------- #
# Reading the embedded JSON
# --------------------------------------------------------------------------- #
_DATA_KEYS = ('"xig_user_by_username"', '"xig_polaris_media"')


def has_embedded_data(html: str) -> bool:
    """True when the page carries a profile or media object at all — False
    on the throttled shell (see the module docstring)."""
    return any(key in (html or "") for key in _DATA_KEYS)


def page_route(html: str) -> Optional[str]:
    match = _ROUTE_RE.search(html or "")
    return match.group(1) if match else None


def json_blocks(html: str) -> List[Any]:
    """Every `<script type="application/json">` block that decodes. A block
    that does not decode is skipped: the shell carries a few non-data
    blocks, and one bad block must not hide the others."""
    out = []
    for script in BeautifulSoup(html or "", "html.parser").find_all("script", type="application/json"):
        raw = script.string or script.get_text()
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def _find_key(obj: Any, key: str) -> Iterator[Any]:
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k == key:
                    yield v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))


def _find_all(html: str, key: str) -> List[dict]:
    return [v for block in json_blocks(html) for v in _find_key(block, key) if isinstance(v, dict)]


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _as_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value != "" else None


def _iso(ts: Any) -> Optional[str]:
    seconds = _as_int(ts)
    if seconds is None or seconds <= 0:
        return None
    return _dt.datetime.fromtimestamp(seconds, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _first_candidate_url(media: dict) -> Optional[str]:
    """The largest image: `image_versions2.candidates` is ordered largest
    first. `display_uri` (640px) is the fallback."""
    for cand in ((media.get("image_versions2") or {}).get("candidates") or []):
        if isinstance(cand, dict) and _text(cand.get("url")):
            return cand["url"]
    return _text(media.get("display_uri"))


def _video_url(media: dict) -> Optional[str]:
    for version in media.get("video_versions") or []:
        if isinstance(version, dict) and _text(version.get("url")):
            return version["url"]
    return None


def _usernames(items: Any, *, path=()) -> List[str]:
    names = []
    for item in items or []:
        node = item
        for step in path:
            node = node.get(step) if isinstance(node, dict) else None
        name = node.get("username") if isinstance(node, dict) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def mentions(caption: Optional[str]) -> List[str]:
    """`@name` handles in a caption. A handle cannot END with a dot, so the
    full stop after "Presented by @Rolex." is punctuation, not part of it."""
    names = (m.rstrip(".") for m in _MENTION_RE.findall(caption or ""))
    return list(dict.fromkeys(n for n in names if n))


def _json_list(values: List[Any]) -> Optional[str]:
    return json.dumps(values, ensure_ascii=False) if values else None


def _title_from_caption(caption: Optional[str]) -> Optional[str]:
    if not caption:
        return None
    first = caption.strip().splitlines()[0].strip() if caption.strip() else ""
    if len(first) > 120:
        first = first[:119].rstrip() + "…"
    return first or None


def make_sku(kind: str, key: str) -> str:
    """`instagram-user-{pk}` for a profile (the numeric id survives a
    username change), `instagram-post-{shortcode}` for a post (the
    shortcode IS the post's permanent address)."""
    return f"instagram-{'user' if kind == 'profile' else 'post'}-{key}"


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #
def _profile_user(html: str, username: str) -> Optional[dict]:
    """The `xig_user_by_username` object carrying the profile fields, for
    THIS username (matched case-insensitively, the way the site resolves
    it)."""
    for user in _find_all(html, "xig_user_by_username"):
        if (isinstance(user.get("username"), str) and user["username"].lower() == username.lower()
                and "follower_count" in user):
            return user
    return None


def timeline_shortcodes(html: str, *, user_pk: Optional[str] = None) -> List[str]:
    """The shortcodes of the first timeline posts a profile page embeds, in
    the site's order (pinned first). Only the profile's own timeline:
    a node whose owner is someone else is skipped."""
    codes: List[str] = []
    for user in _find_all(html, "xig_user_by_username"):
        conn = user.get("polaris_ordered_timeline_connection")
        if not isinstance(conn, dict):
            continue
        if user_pk is not None and str(user.get("pk")) != str(user_pk):
            continue
        for edge in conn.get("edges") or []:
            node = edge.get("node") if isinstance(edge, dict) else None
            code = node.get("code") if isinstance(node, dict) else None
            if isinstance(code, str) and _SHORTCODE_RE.match(code) and code not in codes:
                codes.append(code)
    return codes


def timeline_state(html: str, *, user_pk: Optional[str], private: bool = False) -> str:
    """Distinguish a genuine empty/private timeline from missing or malformed
    data. A broken connection is malformed; a single edge without a post code
    (say, an inserted non-post item) is skipped, as timeline_shortcodes does,
    so one odd node does not turn every --posts run partial."""
    if private:
        return "private"
    found = False
    for user in _find_all(html, "xig_user_by_username"):
        if user_pk is not None and str(user.get("pk")) != str(user_pk):
            continue
        if "polaris_ordered_timeline_connection" not in user:
            continue
        found = True
        conn = user["polaris_ordered_timeline_connection"]
        if not isinstance(conn, dict) or not isinstance(conn.get("edges"), list):
            return "malformed"
        for edge in conn["edges"]:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict) or not isinstance(node.get("code"), str) or not _SHORTCODE_RE.fullmatch(node["code"]):
                log.warning("Skipping a timeline item without a post code for user %s", user.get("pk"))
    return "present" if found else "missing"


def parse_profile(html: str, *, username: str) -> Optional[Product]:
    user = _profile_user(html, username)
    if user is None:
        return None
    pk = _text(str(user.get("pk"))) if user.get("pk") is not None else None
    name = _text(user.get("username")) or username
    links = [link["url"] for link in user.get("bio_links") or []
             if isinstance(link, dict) and _text(link.get("url"))]
    return Product(
        sku=make_sku("profile", pk) if pk else make_sku("profile", f"name-{name.lower()}"),
        source=SOURCE,
        category="profile",
        title=_text(user.get("full_name")) or name,
        brand=None, price=None, currency=None, price_source=None,
        product_url=profile_url(name),
        image_url=_text(user.get("profile_pic_url")),
        scraped_at=_now_iso(),
        username=name,
        user_id=pk,
        full_name=_text(user.get("full_name")),
        is_verified=_as_bool(user.get("is_verified")),
        is_private=_as_bool(user.get("is_private")),
        biography=_text(user.get("biography")),
        bio_links_json=_json_list(links),
        follower_count=_as_int(user.get("follower_count")),
        following_count=_as_int(user.get("following_count")),
        recent_posts_json=_json_list(timeline_shortcodes(html, user_pk=pk)),
    )


# --------------------------------------------------------------------------- #
# Post / Reel
# --------------------------------------------------------------------------- #
def _post_media(html: str, shortcode: str) -> tuple:
    """(media, gated) for THIS shortcode. `media` is the full logged-out
    media object, or None; `gated` is True when the site withheld it from a
    logged-out visitor (`if_not_gated_logged_out` null, a `gating_ruling`
    set)."""
    gated = False
    for wrapper in _find_all(html, "xig_polaris_media"):
        if wrapper.get("code") != shortcode:
            continue
        media = wrapper.get("if_not_gated_logged_out")
        if isinstance(media, dict) and media.get("code") == shortcode:
            return media, False
        gated = gated or bool(wrapper.get("gating_ruling")) or media is None
    return None, gated


def parse_post(html: str, *, shortcode: str) -> Optional[Product]:
    media, _gated = _post_media(html, shortcode)
    if media is None:
        return None
    owner = media.get("user") if isinstance(media.get("user"), dict) else {}
    caption = _text((media.get("caption") or {}).get("text")) if isinstance(media.get("caption"), dict) else None
    hidden = _as_bool(media.get("like_and_view_counts_disabled"))
    carousel = [m for m in media.get("carousel_media") or [] if isinstance(m, dict)]
    media_urls = []
    for item in carousel or [media]:
        url = _video_url(item) or _first_candidate_url(item)
        if url:
            media_urls.append(url)
    product_type = _text(media.get("product_type"))
    return Product(
        sku=make_sku("post", shortcode),
        source=SOURCE,
        category="post",
        title=_title_from_caption(caption),
        brand=None, price=None, currency=None, price_source=None,
        product_url=f"{BASE_URL}/reel/{shortcode}/" if product_type == "clips" else post_url(shortcode),
        image_url=_first_candidate_url(media),
        scraped_at=_now_iso(),
        username=_text(owner.get("username")),
        user_id=_text(str(owner["pk"])) if owner.get("pk") is not None else None,
        full_name=_text(owner.get("full_name")),
        is_verified=_as_bool(owner.get("is_verified")),
        shortcode=shortcode,
        media_type=MEDIA_TYPES.get(_as_int(media.get("media_type"))),
        product_type=product_type,
        posted_at=_iso(media.get("taken_at")),
        # A hidden count is served as a number anyway; publishing it would
        # state something the author chose not to show as if it were shown.
        like_count=None if hidden else _as_int(media.get("like_count")),
        comment_count=_as_int(media.get("comment_count")),
        likes_hidden=hidden,
        caption=caption,
        hashtags_json=_json_list(list(dict.fromkeys(_HASHTAG_RE.findall(caption or "")))),
        mentions_json=_json_list(mentions(caption)),
        alt_text=_text(media.get("accessibility_caption")),
        coauthors_json=_json_list(_usernames(media.get("coauthor_producers"))),
        tagged_users_json=_json_list(_usernames((media.get("usertags") or {}).get("in"), path=("user",))),
        media_count=len(carousel) if carousel else 1,
        media_urls_json=_json_list(media_urls),
        video_url=_video_url(media),
        width=_as_int(media.get("original_width")),
        height=_as_int(media.get("original_height")),
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
@dataclass
class PageResult:
    products: List[Product]
    source_used: str  # "embedded_json" | "none"
    route: Optional[str] = None
    gated: bool = False
    timeline_status: Optional[str] = None
    post_codes: Optional[List[str]] = None  # a profile's timeline, for --posts


def parse_page(html: str, *, url: str) -> PageResult:
    """Zero or one Product for one URL, read from the page's embedded JSON
    and ONLY for the URL's own profile or shortcode."""
    kind, key = classify_url(url)
    route = page_route(html)
    if kind == "profile" and route == ROUTE_PROFILE:
        product = parse_profile(html, username=key)
        if product is not None:
            codes = json.loads(product.recent_posts_json) if product.recent_posts_json else []
            return PageResult([product], "embedded_json", route, post_codes=codes,
                              timeline_status=timeline_state(html, user_pk=product.user_id, private=bool(product.is_private)))
    elif kind == "post" and route == ROUTE_POST:
        product = parse_post(html, shortcode=key)
        if product is not None:
            return PageResult([product], "embedded_json", route)
        return PageResult([], "none", route, gated=_post_media(html, key)[1])
    return PageResult([], "none", route)


def safe_parse_page(html: str, **kwargs) -> PageResult:
    """Engine entry point: a parse exception degrades this ONE url to a
    rejected result instead of crashing the batch (CLAUDE.md §6/§10)."""
    try:
        return parse_page(html, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.error("A page failed to parse — rejecting this URL: %s", exc)
        return PageResult(products=[], source_used="none", route=page_route(html or ""))
