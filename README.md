# instagram-scraper

![release](https://img.shields.io/github/v/release/2scraper/instagram-scraper?sort=semver)
![tests](https://github.com/2scraper/instagram-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/instagram-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![no login](https://img.shields.io/badge/runs%20without-an%20account-success)

**Scrape public Instagram profiles and posts into clean JSON or CSV, without
logging in.** Give it a username, a profile URL, a post or Reel URL, or a
file of them; get back one row per profile (name, bio, links, followers,
following, verified, private) and one row per post (date, likes, comments,
caption, hashtags, mentions, co-authors, tagged accounts, every image or
video URL of a carousel). `--posts N` adds the first N posts the profile
page embeds (up to 12, pinned posts first).

- **No account, no cookies.** It reads what Instagram shows any logged-out
  visitor: the data the site embeds in the page itself. No password, no
  session, nothing that can get an account banned.
- **Verified live on 2026-10-04**, from an ordinary residential IP with no
  key and no proxy: a profile and the 12 posts its page embeds, 13 of 13 pages read;
  a 29-page mixed run (two profiles, a Reel, 24 posts, a dead username and a
  dead shortcode) read 27 rows and reported the two dead ones as not found.
  All three engines were run live, and Playwright and Puppeteer again
  through a 2Captcha residential proxy (4 of 4 and 3 of 3).
- **Honest results.** A throttled, blocked or partial run says so in its
  exit code and a `.meta.json` file next to the output. A run that finds
  nothing never overwrites your last good data.
- **Three browser engines** (Playwright, Puppeteer, Selenium) running one
  shared fetch loop, a browserless **Scraper API** mode, rotating proxies,
  2Captcha's **Scraping Browser API** over CDP, and a run-to-run diff tool.

## Quick start

```bash
git clone https://github.com/2scraper/instagram-scraper.git
cd instagram-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py --url natgeo --posts 12
```

Results land in `instagram_results.json`, with
`instagram_results.json.meta.json` beside it. No `.env` is needed for this.

**When you need more than that:** Instagram limits how much one address
may read logged out (see [Rate limits](#rate-limits)). For volume, put a
2Captcha residential proxy in `.env` (`cp .env.example .env`,
`INSTAGRAM_PROXY=...`) or rotate several with `--proxy-file`.
`python3 env_config.py` shows what was picked up, without printing
secrets.

## Examples

```bash
# a profile and the 12 posts its page embeds (pinned first), as CSV
python3 playwright_scraper.py --url natgeo --posts 12 --format csv --out natgeo.csv

# one post, or one Reel
python3 playwright_scraper.py --url "https://www.instagram.com/p/DeAZ0uAgNuA/"
python3 playwright_scraper.py --url "https://www.instagram.com/reel/DdG4RIxIPyf/"

# a batch: one username, @username, profile URL or post URL per line
# (lines starting with # are skipped)
python3 playwright_scraper.py --urls-file accounts.txt --posts 3

# the same with Puppeteer, or Selenium
python3 puppeteer_scraper.py --url natgeo --posts 2
python3 selenium_scraper.py --url natgeo

# without a browser: 2Captcha's Scraper API (needs TWOCAPTCHA_KEY) — posts and Reels
python3 playwright_scraper.py --scraper-api --urls-file posts.txt

# compare two runs
python3 diff_runs.py monday.json tuesday.json
```

## Sample output

Real rows from a run on 2026-10-04 (`sample_output.json` has four: the
profile, an image post, a Reel and a carousel; long values shortened
here):

```json
[
  {
    "sku": "instagram-user-787132",
    "source": "instagram.com",
    "category": "profile",
    "title": "National Geographic",
    "product_url": "https://www.instagram.com/natgeo/",
    "image_url": "https://scontent.cdninstagram.com/v/t51.82787-19/683576066_...jpg?...",
    "username": "natgeo",
    "user_id": "787132",
    "full_name": "National Geographic",
    "is_verified": true,
    "is_private": false,
    "biography": "Step into wonder and find your inner explorer with National Geographic 🌎",
    "bio_links_json": "[\"http://visitstore.bio/natgeo\", \"https://ngmdomsubs.nationalgeographic.com/...\"]",
    "follower_count": 268457110,
    "following_count": 194,
    "recent_posts_json": "[\"DdG4RIxIPyf\", \"DbIdlf0j6uA\", \"DeBJRyaDIyP\", ...]"
  },
  {
    "sku": "instagram-post-DeAZ0uAgNuA",
    "source": "instagram.com",
    "category": "post",
    "title": "Presented by @Rolex. When Matt Shirley first shone his flashlight into a cave in Gabon, dozens of crocodile eyes stared…",
    "product_url": "https://www.instagram.com/p/DeAZ0uAgNuA/",
    "username": "natgeo",
    "shortcode": "DeAZ0uAgNuA",
    "media_type": "image",
    "product_type": "feed",
    "posted_at": "2026-10-02T21:05:27Z",
    "like_count": 47594,
    "comment_count": 154,
    "likes_hidden": false,
    "hashtags_json": "[\"Rolex\", \"AfricaEarthsWildHome\", \"PerpetualPlanet\"]",
    "mentions_json": "[\"Rolex\", \"hammond_robin\", \"NatGeoTV\", \"DisneyPlus\", \"hulu\"]",
    "alt_text": "Photo by National Geographic on October 02, 2026. May be an image of crocodile and text.",
    "media_count": 1,
    "width": 1350,
    "height": 1688
  }
]
```

One row is one profile or one post, told apart by `category`; a column
that does not apply to a row's kind is `null`.

| Field | Meaning |
|---|---|
| `sku` | `instagram-user-{id}` for a profile (the numeric id survives a rename), `instagram-post-{shortcode}` for a post |
| `category` | `profile` or `post` (a Reel is a post with `product_type` `clips`) |
| `title` | the profile's display name, or the caption's first line (at most 120 characters) |
| `product_url` / `image_url` | canonical profile, `/p/` or `/reel/` URL; profile picture or the post's largest image |
| `username`, `user_id`, `full_name`, `is_verified` | the profile's, or for a post its owner's |
| `is_private`, `biography`, `bio_links_json`, `follower_count`, `following_count` | profile only; `bio_links_json` holds the real link targets, not Instagram's redirect |
| `recent_posts_json` | profile only: shortcodes of the 12 timeline posts the page embeds, **pinned posts first** — what `--posts` follows |
| `posted_at` | post time, UTC |
| `like_count`, `comment_count` | as the site reports them; `like_count` is `null` when the author hides likes (`likes_hidden` true) |
| `media_type` / `product_type` | `image`, `video` or `carousel` / the site's own `feed`, `clips`, `carousel_container` |
| `caption`, `hashtags_json`, `mentions_json` | the full caption, and the `#tags` and `@handles` in it |
| `alt_text` | Instagram's own accessibility description of the media |
| `coauthors_json`, `tagged_users_json` | usernames of collaborators and of accounts tagged in the media |
| `media_count`, `media_urls_json`, `video_url` | slides in a carousel (1 otherwise), each slide's image or video URL, the video of a Reel |
| `width`, `height` | the original media's size |
| `brand`, `price`, `currency`, `price_source` | always `null`: the columns keep the row layout shared with the other 2scraper repos |

Media URLs are Instagram's signed CDN links: they expire after a few days,
so download what you need soon after the run.

## How a page is read

1. Open the profile or post URL in a fresh browser context, logged out.
   Nothing is clicked or scrolled.
2. Read the JSON blocks Instagram renders into the page
   (`<script type="application/json">`). A profile page carries the
   profile and the shortcodes of its first 12 posts; a post page carries
   the full media object. Each is matched to the URL's own username or
   shortcode, never taken as "the first one on the page".
3. Turn what was served into one outcome: a **row**; **not found** (the
   "page isn't available" page, which Instagram serves under HTTP 200);
   **blocked** (the login page, or the page without its data — see below);
   **login required** (a post Instagram withholds from logged-out
   visitors); or nothing read.

The HTML's Open Graph tags are not used: they round every number ("268M
Followers", "48K likes"). The `web_profile_info` JSON endpoint older
Instagram scrapers relied on answers `401 require_login` to a logged-out
visitor (measured 2026-10-04) and is not used either.

## Captchas

None, so far. In 10 test runs on 2026-10-04 (26 pages: Playwright,
Puppeteer and Selenium, direct and through a residential proxy, plus
the Scraper API) no page carried a captcha widget, iframe or challenge
script. Instagram does not show a logged-out visitor a captcha: it shows
the login page, or the page without its data (below). The string
`arkose_captcha` that appears on every page is an entry in Instagram's
cookie-consent list of third-party services, not a challenge. The
generic captcha detection stays on, so a real one would be reported as
blocked rather than read as data.

## Rate limits

Instagram limits logged-out reading per client, and a limited client is
not refused outright: it gets the usual page **without its data**, or the
login page. Measured on 2026-10-04 from one residential IP:

- plain HTTP requests got the data-less page after about 75 page loads in
  an hour;
- a real browser (this tool's engines) on the same IP, minutes later,
  still read 6 of 6 pages, and the runs above read 13 and 29 pages back
  to back at the default 2-second pace.

The tool reports both answers as **blocked** (`stop_reason`
`rate_limited` for the data-less page), never as an empty profile. Without
a proxy pool it stops after 3 blocked answers in a row and reports the
rest of the list as `not_attempted`, instead of asking a throttled
address again and again. To read more: rotate residential exits with
`--proxy-file` (each URL gets the next exit, and an exit that keeps
getting blocked is dropped; once every exit is dropped the run stops, and
never falls back to your own address), keep `--delay-between-pages` at 2s
or more, or spread a big list over time.

## Scraper API mode

With `--scraper-api` no browser is driven: each page is one 2Captcha
Scraper API call (`TWOCAPTCHA_KEY` in `.env`), routed through the
Scraping Browser profile in `INSTAGRAM_CDP_ENDPOINT` when one is set.
Measured on 2026-10-04 on the Scraper API's own pool: **posts and Reels
read fully (3 of 3), profiles did not** — all four profiles tried got
Instagram's login page, reported as blocked. Use it for post URLs; read
profiles with a browser engine. A refused key or an empty balance stops
the run at once with exit 5.

## Run results and exit codes

Every run writes `<out>` and `<out>.meta.json` (status, `stop_reason`,
URL counts, failed URLs with reasons, not-found URLs, the full input
selection with `--posts` and `--since`, whether `--max-results` capped it,
solves spent, and a hash of the output file). With the options below it
also lists the posts left out (`excluded_urls`), every post seen so far
(`seen_post_skus`) and whether the run was incremental. A run that
collects nothing writes neither, so it never replaces your previous good
file.

| Exit | Meaning |
|---|---|
| `0` | complete |
| `6` | partial: rows were written, but the run did not finish cleanly — `stop_reason` says why (`blocked`, `rate_limited`, `remote_api_error`, `failed_pages`, `parse_error`, `proxy_pool_exhausted`) |
| `3` | blocked or throttled, no rows |
| `4` | no rows: every input was not found, or every post was older than `--since` or already collected by `--incremental-from` |
| `5` | nothing could be read due to fetch, parse, or remote-service failure; no rows |
| `2` | bad usage, including input where every line was skipped |
| `1` | crash (a bug; please report it) |

## Monitoring changes

```bash
# what changed between two runs, as JSON
python3 diff_runs.py monday.json tuesday.json --json

# only some fields, and exit 1 if anything changed (for cron or CI)
python3 diff_runs.py monday.json tuesday.json --fields follower_count,like_count,caption --fail-on-change
```

`diff_runs.py` matches rows by `sku` and lists, for each row, the old and
new value of every watched field: follower and following counts, likes,
comments, caption, biography, full name, and the verified, private and
hidden-likes flags. A number that changed also gets its `delta`; a value
missing on one side is shown as missing, never counted as zero.
`--fields` narrows the list, and a JSON run can be compared with a CSV
one. `--fail-on-change` exits 1 when a watched field changed or a row
appeared or disappeared.

A run with `--posts` sees only the 12 posts the profile page embeds, so a
post can drop out of that window without being deleted. Such a row is
listed as `left_selection`, not `removed`. A row is `removed` only when
the new run itself found its URL gone: a deleted post, or a deleted or
renamed profile.

The diff refuses comparisons that would mislead: runs with different
inputs, `--posts`, `--max-results` or `--since`; a run that did not
complete; a `.meta.json` that does not match its file; and
`--incremental-from` runs, which hold only the new posts. To monitor
engagement, keep taking full runs.

Monitoring, `--since`, `--incremental-from` and checkpoints are new in
0.2.0. They are tested offline on pages captured live, but have not yet
been run against live Instagram.

## Only new posts: `--since` and `--incremental-from`

```bash
# profiles always stay in the output; posts must be from 1 October 2026 (UTC) on
python3 playwright_scraper.py --url natgeo --posts 12 --since 2026-10-01 --out first.json

# next time: refresh the profile, open only the posts first.json does not have
python3 playwright_scraper.py --url natgeo --posts 12 --since 2026-10-01 --incremental-from first.json --out next.json

# and so on: each run remembers every post seen before it
python3 playwright_scraper.py --url natgeo --posts 12 --since 2026-10-01 --incremental-from next.json --out later.json
```

`--since` takes a date or an ISO time (`2026-10-01`,
`2026-10-01T12:00:00+02:00`); without a timezone it means UTC, and a post
published exactly at that moment is kept. Every post is checked: an old
pinned post at the top of the timeline does not hide the newer ones
behind it. A post whose date cannot be read makes the run partial rather
than slipping through. `--since` filters the 12 posts the page embeds; it
cannot reach older ones.

`--incremental-from` takes an earlier complete run (JSON or CSV, with its
`.meta.json`) made with the same inputs, `--posts`, `--max-results` and
`--since`. Anything else, or a file edited since, is refused before a page
is opened. Profiles are fetched again. Posts already in that run, or in
the runs it was built from, are skipped without being opened, and so are
posts an earlier run dropped as older than `--since` (a post's date never
changes). The list of seen posts goes into the new run's `.meta.json`
(`seen_post_skus`), so each run can feed the next one.

Two things to keep in mind. A skipped post is not refreshed: its likes
and caption stay as first collected, so keep full runs if you track
engagement. And `--max-results` counts skipped posts too: it limits the
pages selected, not the new posts found.

Left-out posts are listed in `excluded_urls` (`already_seen`,
`before_since`). If nothing is left to collect, the run exits 4 and,
without `--allow-empty`, leaves the previous output file alone.

## Resuming an interrupted run

```bash
python3 playwright_scraper.py --url natgeo --posts 12 --checkpoint progress.json --out results.json

# after Ctrl-C, a crash or a partial run: the same flags, plus --resume
python3 playwright_scraper.py --url natgeo --posts 12 --checkpoint progress.json --resume --out results.json
```

After every URL the run saves its progress to the checkpoint: the queue,
including the posts found on profile pages, and each URL's outcome and
row. The file is written to a temporary name and then renamed, so an
interruption never leaves half of it. `--resume` keeps the rows already
read, skips URLs that were read, not found or filtered out, and retries
the ones that failed, were cut off mid-request or were never reached. The
flags that decide what is collected (inputs, `--posts`, `--max-results`,
`--since`, `--incremental-from`) must match the first run, or the
checkpoint is refused.

A checkpoint is for finishing one run. Resuming a run that already
finished gives back its old data, so start a fresh run with a new path
(an existing checkpoint is never overwritten without `--resume`). The
checkpoint cannot be the output file, its `.meta.json`, the input file or
the `--incremental-from` run. It holds no key, password or HTML, but it
does hold the collected rows: keep it where you keep the results.

## Options

Same flags for all three engines. Credentials go in `.env`
(`TWOCAPTCHA_KEY`, `INSTAGRAM_PROXY`, `INSTAGRAM_CDP_ENDPOINT`), never on
the command line.

| Option | Default | |
|---|---|---|
| `--url` / `--urls-file` | | what to scrape: a username, `@username`, profile URL, post or Reel URL; a file has one per line |
| `--posts` | 0 | also fetch the first N posts the profile page embeds, 0-12, pinned first |
| `--max-results` | 100 | URLs to select in total, profiles and posts together; posts skipped by `--incremental-from` count too |
| `--since` | | keep only posts from this date or ISO time on (UTC without a timezone); profiles are always kept |
| `--incremental-from` | | an earlier complete run with the same selection: refresh profiles, skip the posts it already has |
| `--checkpoint` / `--resume` | / off | save progress after each URL / continue from that checkpoint |
| `--delay-between-pages` | 2s | pause between URLs |
| `--format` / `--out` | json / `instagram_results.<format>` | output format and path |
| `--proxy` / `--proxy-file` / `--proxy-shuffle` | `INSTAGRAM_PROXY` | one proxy or a rotating pool, for a local browser |
| `--proxy-block-retries` | 3 | blocked answers before that proxy is dropped from the pool |
| `--scraper-api` | off | no browser: fetch each page through 2Captcha's Scraper API (needs `TWOCAPTCHA_KEY`; routed through `INSTAGRAM_CDP_ENDPOINT` when set) — posts only, see [Scraper API mode](#scraper-api-mode) |
| `--cdp-endpoint` | `INSTAGRAM_CDP_ENDPOINT` | connect to a Scraping Browser API profile instead of launching a browser |
| `--solve-captcha` | when-blocked | `off` disables the Browser API's own captcha auto-solve; there is no local solver |
| `--max-solves` / `--min-score` | 8 / 0.3 | kept for the local solver, which is disabled; they change nothing today |
| `--retries` / `--retry-delay` | 2 / 3s | navigation retries per URL |
| `--fingerprint` / `--fp-tags` / `--fp-country` | off | apply a 2Captcha Fingerprint API user agent (local browsers only) |
| `--dump-html` | off | save each page's HTML next to the output, for debugging |
| `--allow-empty` | off | write an output file even when nothing was found |
| `--headless` / `--headful` | headless | show the browser window |

An input that is not a profile, post or username (`/explore/`, a story,
another site) is logged and skipped, never requested. Run any engine with
`--help` for the full list.

## Engines

- **Playwright** (`playwright_scraper.py`) is the recommended engine.
- **Puppeteer** (`puppeteer_scraper.py`, via pyppeteer) supports the same
  modes. pyppeteer itself is no longer maintained, and its own proxy login
  no longer works on current Chromium, so this engine answers the proxy's
  password prompt itself (over CDP `Fetch`).
- **Selenium** (`selenium_scraper.py`) runs a local Chrome only.
  chromedriver cannot authenticate a Scraping Browser endpoint (the run
  exits 2 before fetching; use `--scraper-api` instead), and its
  `--proxy-server` cannot use a proxy password (the credentials are
  stripped, with a warning).

Install one engine per virtualenv (`requirements-playwright.txt`,
`requirements-puppeteer.txt`, `requirements-selenium.txt`). Their
dependencies conflict with each other.

All three engines share one fetch loop (`page_flow.py`), so they agree on
results, exit codes and when money is spent. Docker:
`docker build -t instagram-scraper .` gives an image with Playwright and
Chromium.

## Known limitations

- **12 posts per profile.** That is what a logged-out profile page
  embeds; the rest of the timeline needs a login and is out of scope.
- **Pinned posts come first** in `recent_posts_json` and in `--posts`, so
  "the 3 most recent" can include an older pinned post. Sort by
  `posted_at` if you need strict date order.
- **No comments, followers lists, stories, search or hashtag pages.**
  Comments are other people's names and words; the rest needs a login.
- **No exact post count and no Reel play count**: a logged-out visitor is
  not sent either (the "32K Posts" in the page's preview text is rounded).
- **Private profiles** give a profile row with `is_private: true` and no
  posts (seen live on 2026-10-04: `@vogue` is a private "Vogue Covers"
  account; the magazine is `@voguemagazine`).
- **No location.** The post's location field was empty on all 36
  distinct posts of the live runs above, including 12 from a travel
  account, so there is no `location` column. If you find a logged-out
  post page that carries one, please open an issue with its URL.
- **A datacentre address may get the login page from the first request.**
  Not measured yet; a residential proxy is the fix.

## Is this allowed?

Instagram's `robots.txt` disallows all crawlers, and Meta's terms forbid
automated collection without its permission. This tool reads only what
Instagram serves to any logged-out visitor, does not log in, and does not
collect comments or other people's interactions. Whether your use is
permitted depends on your jurisdiction and purpose — check before you run
it, and do not use it to track individuals.

## Development

```bash
python3 smoke_test.py            # 63 offline checks, no network, no engine needed
python3 .github/ci_checks.py     # credential scan
python3 -m unittest discover -s tests -p 'test_*.py'  # failure, recovery, monitoring and resume scenarios
```

Parser checks run on real captures in `tests/fixtures/`. CI runs the
offline suite on Python 3.9 and 3.12, installs the built wheel outside
the checkout, builds the Docker image and launches Chromium in it, and
runs each engine in its own virtualenv. `TESTING.md` describes live
testing; `CHANGELOG.md` has the history.

## Licence

MIT, see `LICENSE`.
