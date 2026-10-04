# Testing with real credentials and the live site

**Live status, 2026-10-04.** From one residential IP, logged out, no key
and no proxy:

| Engine | Run | Result |
|---|---|---|
| Playwright | `--url natgeo --posts 12` | 13/13 pages, 1 profile + 12 post rows, exit 0, ~4.7s per page |
| Playwright | `--urls-file` (2 profiles with `--posts 12`, a Reel, a dead username, a dead shortcode, `/explore/`) | 29 pages, 27 rows, both dead inputs `page_not_found`, `/explore/` skipped unrequested, exit 0 |
| Puppeteer | `--url natgeo --posts 2` | 3/3 rows, exit 0 |
| Selenium | one Reel URL | 1/1 row, exit 0 |
| Playwright, same IP, after curl was throttled | `--url natgeo --posts 5` | 6/6 rows: the limit follows the client, not only the address |
| curl (not this tool) | ~75 page loads in an hour | profile and post pages served WITHOUT their data — the fixture `instagram_throttled_*` |
| curl / in-page fetch | `/api/v1/users/web_profile_info/` | 401 `require_login` from a home IP, a residential proxy and inside a loaded page |

Then, the same day, with a 2Captcha key and an EU residential proxy:

| Engine | Run | Result |
|---|---|---|
| Playwright + `INSTAGRAM_PROXY` | `--url natgeo --posts 3` | 4/4 rows, exit 0; the log names the exit, password masked |
| Puppeteer + `INSTAGRAM_PROXY` | `--url natgeo --posts 2` | before the fix: every URL failed (`Network.setRequestInterception` wasn't found); after answering auth over CDP `Fetch`: 3/3, exit 0 |
| `--scraper-api`, own pool | 4 profiles | 0/4 — Instagram's login page each time, reported as blocked (exit 3 / partial) |
| `--scraper-api`, own pool | an image post, a Reel, a carousel | 3/3 rows |
| `--scraper-api`, invalid key | 2 URLs | exit 5 after one call |

**10-run sweep, 2026-10-04**: Playwright ×6 (3 direct, 3 via proxy),
Puppeteer ×2, Selenium ×1, Scraper API ×1, each a profile and its 2
latest posts. 9 runs complete (25 pages, one of them the private `@vogue`
profile), the Scraper API run blocked by the login page. No captcha of any
kind on any of the 26 pages; the only vendor string found, `arkose_captcha`,
is a cookie-consent list entry.

Not yet run live: a gated (age-restricted) post, a
post with a hidden like count, `--proxy-file` rotation over several exits,
`--cdp-endpoint`, the Docker image, and any run from a datacentre IP.
Each of these is handled from the page's own flags or covered by the
offline suite, but has no live evidence behind it yet.

The quickest real check:

```bash
python3 playwright_scraper.py --url natgeo --posts 3 --out /tmp/ig.json
cat /tmp/ig.json.meta.json        # status: complete, product_count: 4
```

## 1. Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env              # only for the paid paths below
python3 env_config.py             # what was picked up, secrets masked
```

## 2. One profile, and what the rows should look like

```bash
python3 playwright_scraper.py --url natgeo --posts 3 --out /tmp/ig.json --dump-html
```

- exit 0, `status: complete`, `pages_completed: 4`;
- one `category: profile` row: `follower_count` in the hundreds of
  millions, `recent_posts_json` with 12 shortcodes;
- three `category: post` rows whose `username` is `natgeo`, each with
  `posted_at`, `media_type`, `comment_count` and a `like_count` (or
  `likes_hidden: true`).

Compare a row against the page in a browser. `/tmp/ig_debug_N.html` holds
the exact bytes each row was read from.

## 3. The failure answers

```bash
python3 playwright_scraper.py --url zzqx_no_such_user_8812 --out /tmp/nf.json   # exit 4, nothing written
python3 playwright_scraper.py --url https://www.instagram.com/explore/         # exit 2, nothing requested
```

A throttled run (exit 3 or 6, `stop_reason: rate_limited`) is expected
after enough page loads from one exit — the run stops after 3 blocked
answers in a row when there is no proxy pool.

## 4. Puppeteer and Selenium

```bash
pip install -r requirements-puppeteer.txt   # in its own venv
PYPPETEER_EXECUTABLE_PATH=/path/to/chromium python3 puppeteer_scraper.py --url natgeo --posts 2

pip install -r requirements-selenium.txt    # in its own venv
python3 selenium_scraper.py --url https://www.instagram.com/reel/DdG4RIxIPyf/
```

Same rows, same exit codes.

## 5. The residential proxy (`--proxy` / `INSTAGRAM_PROXY`)

Put `INSTAGRAM_PROXY=http://login:password@host:port` in `.env` and run
section 2 again. The log names the exit (`proxy: http://***@host:port`),
never the password. For a pool, one proxy per line in a file and
`--proxy-file proxies.txt --proxy-shuffle`.

## 6. Scraper API mode (`--scraper-api`)

```bash
python3 playwright_scraper.py --scraper-api --url natgeo --posts 2 --out /tmp/sapi.json
```

Needs `TWOCAPTCHA_KEY`; no browser driver. With `INSTAGRAM_CDP_ENDPOINT`
also set, each call routes through that Scraping Browser profile. An
invalid key must end the run with exit 5 after one call.

## 7. The Scraping Browser API (`--cdp-endpoint`)

```bash
python3 playwright_scraper.py --url natgeo --posts 2   # with INSTAGRAM_CDP_ENDPOINT in .env
```

Selenium refuses a credentialled endpoint up front (exit 2): chromedriver
cannot authenticate one.

## 8. Push to GitHub and let CI do the rest

`tests.yml` runs the offline suite on Python 3.9 and 3.12, builds the
wheel and the Docker image, and runs one `engine-smoke` job per engine.
`canary.yml` needs the `INSTAGRAM_PROXY` repo secret (a residential
proxy); without it the job skips with a notice. Dispatch it once by hand
and check both branches.

## 9. What "done" looks like

Every row in the tables above has a date and a number. Add the open items
from the top of this file to the table as they are run, and update the
README's claims from the same runs.
