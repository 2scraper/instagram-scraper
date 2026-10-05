# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/) as closely as a CLI toolkit can: a patch
release means "fixes", not that every flag and default is frozen — a fix
that changes a default is called out at the top of its entry.

## [Unreleased]

### Added

- Add configurable profile/post metric and text diffs, numeric deltas, and JSON/CSV normalization.
- Add inclusive UTC `--since` filtering without stopping at older pinned posts.
- Add `--incremental-from` with validated snapshot hashes and accumulated seen-post IDs (posts dropped by `--since` included, so a delta does not refetch them).
- Add atomic `--checkpoint` and `--resume`, preserving successful rows and discovered queue order.

### Changed

- Count HTTP 200 login walls against proxies; stop when the proxy pool is exhausted without direct fallback.
- Report missing/malformed timelines as partial and rolling post windows as selections, not deletions.
- Read JSON script blocks independently of HTML attribute order and route whitespace.
- Exercise monitoring and interruption scenarios in CI and Docker tests; ship the new run_state module in the wheel/image.

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
