# Instagram Scraper by 2scraper

**Open-source scraper for public Instagram profiles, posts and Reels — no login, three engines, JSON or CSV.**

Pull a profile's name, bio, links, follower and following counts, verified and private flags, and its 12 most recent posts — or any post or Reel by URL: date, likes, comments, caption, hashtags, mentions, co-authors, tagged accounts and every image or video URL of a carousel.

[**View source on GitHub →**](https://github.com/2scraper/instagram-scraper)

---

## Before you scrape

Instagram's `robots.txt` disallows crawlers, and Meta's terms forbid automated collection without its permission. This tool reads only what Instagram shows any logged-out visitor, never logs in, and never collects comments or other people's interactions. Whether your use is allowed depends on your jurisdiction and purpose: check first, and never use it to track individuals.

## What to expect

Every row comes from the data Instagram embeds in the page itself, not from the rounded preview text ("268M Followers"). Live-verified on 2026-10-04 from an ordinary residential IP, with no key and no proxy: a profile and its 12 latest posts, 13 of 13 pages read; a 29-page mixed run with a dead username and a dead shortcode, both reported as not found. Playwright, Puppeteer and Selenium were all run live. Instagram limits logged-out reading per client; a throttled run says so (blocked, `rate_limited`) instead of returning an empty profile. Details in the [README](https://github.com/2scraper/instagram-scraper#readme).

## What you get

- Free, open-source scraper, one script per engine — **Playwright** (recommended), **Puppeteer** (via pyppeteer) and **Selenium**, all producing the identical output schema and exit codes
- Usernames, profile URLs, post and Reel URLs — one at a time or a batch from a file; `--posts N` adds a profile's N most recent posts (up to the 12 a logged-out page shows)
- Profile fields: name, bio, bio links, followers, following, verified, private, the 12 latest post shortcodes
- Post fields: date, likes (or a flag when hidden), comments count, caption, hashtags, mentions, alt text, co-authors, tagged accounts, media type, every carousel slide's URL, video URL, size
- JSON and CSV export, with a documented `Product` schema and a `.meta.json` sidecar on every completed/partial run
- A browserless mode (`--scraper-api`) that needs no browser driver installed
- A failed URL never hides the ones that succeeded: the sidecar lists every failed URL with its reason, and dead usernames separately

## 2Captcha products, when you want them

| Product | What it's for |
|---|---|
| **Proxies — 2captcha.com/proxy** (2prx.com is the same product, different name) | The one that matters here: Instagram limits logged-out reading per client. Residential exits in `.env` or `--proxy-file`, rotated per URL with per-exit failure tracking |
| **Scraper API — 2captcha.com** | No browser at all: `--scraper-api` fetches each post or Reel page from 2Captcha's side, one HTTP call each (profiles need a browser engine) |
| **Scraping Browser API — 2captcha.com** | A remote browser session over CDP with its own proxy, fingerprint and captcha auto-solve bundled — `--cdp-endpoint` |
| **Browser fingerprints — 2captcha Fingerprint API** | Pin a specific OS/browser/country fingerprint for a locally-launched browser |

## Who this is for

Brand, market and media researchers tracking public accounts over time (`diff_runs.py` compares two runs), who want profile and post data in a script rather than a browser tab. Anything behind a login — followers lists, stories, the full timeline, comments — is out of scope.

## Get started

```bash
git clone https://github.com/2scraper/instagram-scraper.git
cd instagram-scraper
pip install -r requirements-playwright.txt && playwright install chromium

python3 playwright_scraper.py --url natgeo --posts 12 --format json --out results.json
```

Full setup, CLI reference, and configuration details in the [repository README](https://github.com/2scraper/instagram-scraper#readme).

---

**Need it running at scale, with proxies, fingerprints, and captcha solving already configured?**
[Talk to us →](https://2captcha.com/contact) · Proxies by [2captcha.com/proxy](https://2captcha.com/proxy) · Scraping Browser API & captcha solving by [2captcha.com](https://2captcha.com)
