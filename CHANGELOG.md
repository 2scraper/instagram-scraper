# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/) as closely as a CLI toolkit can: a patch
release means "fixes", not that every flag and default is frozen — a fix
that changes a default is called out at the top of its entry.

## [Unreleased]

## [0.2.0] - 2026-10-05

Monitoring and repeat runs. Everything new here is tested offline, on
pages captured live on 2026-10-04; none of it has been run against live
Instagram yet.

Changes an existing user will notice:

- `diff_runs.py --fail-on-change` now exits 1 on monitored field changes
  (followers, likes, captions and the rest) and on rows added, removed or
  leaving the post window, not only on price changes.
- With `--posts`, a post missing from the next run is `left_selection`, not
  `removed`; a row whose URL the new run reports as not found is `removed`.
- An exhausted `--proxy-file` / `--proxy` pool stops the run
  (`stop_reason: proxy_pool_exhausted`) instead of continuing direct, and a
  login wall under HTTP 200 now counts against the proxy.
- Exit 4 also covers runs where every post was filtered by `--since` or
  already collected by `--incremental-from`.

### Added

- `diff_runs.py` compares the fields that matter on Instagram (followers,
  following, likes, comments, caption, biography, full name, verified,
  private, hidden likes) with old and new values and a `delta` for
  numbers; `--fields` picks which. JSON and CSV runs can be compared.
- `--since DATE`: keep only posts from that date or time on (inclusive,
  UTC by default). Profiles are always kept, and an older pinned post does
  not hide newer ones.
- `--incremental-from RUN`: refresh profiles and open only the posts an
  earlier complete run did not have. Each run records the posts seen
  before it (`seen_post_skus`), including those dropped by `--since`, so
  runs can be chained.
- `--checkpoint PATH` / `--resume`: save progress after each URL and finish
  an interrupted or partial run without reading the finished URLs again.

### Changed

- A `--posts` run whose profile page has no readable timeline is now
  partial (`parse_error`) instead of returning the profile without posts.
  A single timeline item without a post code is skipped with a warning.

### Fixed

- The page's JSON blocks are found whatever the order of the `<script>`
  tag's attributes, and the page route is recognised with whitespace in
  its JSON.

## [0.1.0] - 2026-10-04

First release.

### Added

- Public Instagram profiles, posts and Reels, read logged out from the data
  Instagram embeds in the page (`page_parser.py`), with three engines
  (Playwright, Puppeteer, Selenium) on one shared fetch loop
  (`page_flow.py`) and a browserless `--scraper-api` mode.
- Input as usernames, `@username`, profile URLs, post and Reel URLs
  (`--url`, `--urls-file`); `--posts N` adds a profile's N most recent
  posts (0-12, what a logged-out profile page embeds).
- One row per profile (`category: profile`) or post (`category: post`) in
  the family's shared row layout, JSON or CSV, with a `.meta.json`
  sidecar listing failed and not-found URLs.
- Four answers told apart, each from a real capture in `tests/fixtures/`:
  a row; Instagram's "page isn't available" page under HTTP 200
  (`page_not_found`, exit 4 when nothing else was read); the login page
  (blocked); and the page served without its data (`rate_limited`).
- Without a proxy pool, a run stops after 3 blocked answers in a row and
  reports the rest as `not_attempted` instead of asking a throttled
  address again.
- Daily canary through a residential proxy (`INSTAGRAM_PROXY` secret);
  skips with a notice without it.

### Fixed (before release)

- Puppeteer through a password proxy: pyppeteer's `page.authenticate()`
  needs `Network.setRequestInterception`, which current Chromium no longer
  has, so every proxied URL failed. The engine now answers the proxy's
  auth challenge over CDP `Fetch` (live: 3/3 through a residential proxy).
  The same call is in the other repos' `puppeteer_scraper.py`.

- `--fingerprint` was a silent no-op in all three engines: the client read
  the user agent from `userAgent.value` (the documentation's /random
  example), while every live response carries it as
  `userAgent.userAgent`. Both are read now, the applied user agent is
  logged, and a profile without one is reported. Live: the page saw the
  fingerprint's Windows user agent instead of the local `HeadlessChrome`.
  The same reader is in the other repos' `fingerprint_client.py`.

### Measured, not shipped

- `--scraper-api` on the Scraper API's own pool reads posts and Reels
  (3/3) but gets the login page for profiles (0/4).

- `web_profile_info` answers 401 `require_login` logged out (2026-10-04),
  so it is not used.
- No `location`, post-count or play-count column: none is served to a
  logged-out visitor (`location` was null on 36 of 36 posts).
