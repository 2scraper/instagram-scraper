# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/) as closely as a CLI toolkit can: a patch
release means "fixes", not that every flag and default is frozen — a fix
that changes a default is called out at the top of its entry.

## [Unreleased]

## [0.4.0] - 2026-11-05

> **Behaviour changes.**
> * Behind `--proxy` / `--proxy-file`, all three browser engines send one
>   `HEAD https://www.instagram.com/robots.txt` through every exit of the
>   pool before launching a browser. An exit whose gateway answers `407`
>   (credential refused, or traffic exhausted) or `402` (traffic/balance
>   exhausted) is dropped from the pool with a warning; only when every exit
>   is refused does the run end with exit 5 and `stop_reason:
>   proxy_auth_failed` in the new `<out>.latest_attempt.meta.json`. No
>   browser is started then, and `<out>` and `<out>.meta.json` are left as
>   they were. Any other answer, or a transport error, keeps the exit. Not
>   sent with `--cdp-endpoint`, `--scraper-api` or without a proxy.
> * Behind a local proxy, images, video/audio and web fonts are no longer
>   downloaded. Playwright aborts them by resource type. Puppeteer fails them
>   in the `Fetch` handler that answers the proxy's password prompt. Selenium
>   blocks them by URL extension. `--load-media` restores the old behaviour.
>   Nothing changes with `--cdp-endpoint`, `--scraper-api` or with no proxy.
>   `<out>.meta.json` records `media_blocked`.
> * Behind a local proxy, a run now stops once about 50 MB have gone through
>   it (`--proxy-budget-mb`, `0` = unlimited): the rows read so far are kept
>   and the run exits 6 (5 with none), `stop_reason: proxy_budget_exceeded`.
>   A budget crossed while the last page loads, with every page read, leaves
>   the run `complete` (exit 0, `proxy_budget_exceeded: true` in the
>   sidecar). A proxied run that collects nothing now writes
>   `<out>.latest_attempt.meta.json` with its outcome and byte count. Not
>   applied with `--cdp-endpoint`, `--scraper-api` or without a proxy.
> * Usage problems found before anything is fetched now exit 2 instead of
>   running: a `--proxy-file` that cannot be read (was a traceback, exit 1)
>   or has no proxy in it (the run went out from this machine's own address);
>   a SOCKS proxy with a login (Chromium cannot authenticate one; was exit 5
>   after the browser started); any proxy with a login on Selenium (its
>   credentials were stripped and every page got 407); `--url` together with
>   `--urls-file` (the file was ignored); an `--out` whose directory is
>   missing, that is a directory, or that is the `--urls-file`,
>   `--incremental-from`, `--proxy-file` or `--checkpoint` (the run used to
>   finish and then crash, or overwrite its own input); and a
>   `--max-results` smaller than the number of inputs.
> * `--max-results` no longer drops input profiles. It used to fill up with
>   the posts of the first profiles, so later profiles were never opened
>   while the run still said `complete`. Every input is now read; the cap
>   trims only posts found by `--posts`, each listed in `excluded_urls` as
>   `max_results` with a warning.
> * Ctrl-C and SIGTERM (`docker stop`) finish the URL in hand, write the rows
>   read so far as `partial` (`stop_reason: interrupted`) and exit 130 / 143
>   with a `--resume` hint. They used to exit 1 ("crash") silently and, without
>   `--checkpoint`, lose everything read. A second Ctrl-C stops at once.
> * `diff_runs.py` exits 2 when it cannot compare (missing file or sidecar,
>   partial run, another selection); exit 1 now only ever means "changed"
>   under `--fail-on-change`.
> * Every page is opened with `?hl=en`, so `alt_text` is English on every
>   engine and mode. It followed the visitor's language: the OS locale for
>   Puppeteer on macOS, the remote profile's over `--cdp-endpoint`, the
>   exit's country through `--scraper-api` (Dutch, Finnish...).
> * New post columns `location_name`, `location_id`, `location_lat`,
>   `location_lng` (at the end of the row, `null` without a geotag).

### Added

- `--load-media` in all three engines.
- Proxy traffic meter (`proxy_meter.py`) in all three browser engines, active
  only behind a local proxy: Playwright sums `request.sizes()`, Puppeteer
  and Selenium read CDP `Network.loadingFinished.encodedDataLength` plus the
  request sizes (Selenium turns Chrome's performance log on for this). It is
  an estimate of the tunnelled payload (TLS/`CONNECT` overhead not
  included). The sidecar records `proxy_bytes`, `proxy_budget_bytes` and
  `proxy_budget_exceeded`, and a proxied run ends with one `Proxy traffic:
  X.X MB of Y MB budget` line. New flag `--proxy-budget-mb` (default 50).
  The proxy pre-check's own `HEAD` is counted too.
- CI only: the canary passes `--proxy-budget-mb 10`, writes `**canary**:
  proxy traffic X.X MB of 10 MB` to the job summary, and fails with
  `::error::stopped: proxy budget 10 MB exceeded` when the run hit the
  budget. The engine jobs run the meter against a stub proxy in a real
  browser.

### Changed

- Puppeteer: pyppeteer's per-URL `Browser listening on...` / `terminate chrome
  process...` INFO lines are no longer logged.
- CI only: a failed canary check now always ends with an `::error::` line. A run
  through the `INSTAGRAM_PROXY` secret that exited nonzero used to end red with no
  annotation, and a missing or wrong output file showed only a traceback.
  The message notes that a proxy rejecting its credential (HTTP 407) shows
  up in Chromium as a navigation timeout (exit 5), not as a 407.
- CI only: that message now points at the proxy pre-check, which reports a
  `407` or `402` by name before any browser is started.
- CI only: the canary now runs weekly (Mondays 06:41 UTC) and on a manual dispatch, with its optional secret `INSTAGRAM_PROXY` passed on every run; the daily schedule is removed.

### Fixed

Found in a manual QA pass of `main` @ 6764b74 (2026-10-25 to 11-05); each has a
test in `tests/test_qa_fixes.py`.

- A `--proxy-file` with only blank or `#` lines silently ran from this
  machine's own address; a missing one gave a traceback (see above).
- `--max-results` silently dropped whole input profiles (see above).
- Puppeteer on Apple Silicon died at launch with no hint: pyppeteer's own
  Chromium is an x86_64 build. An installed Google Chrome or Chromium from
  `/Applications` is now used there; otherwise the error names
  `PYPPETEER_EXECUTABLE_PATH`.
- A failed navigation through a `--proxy-file` pool was retried through the
  same exit (three 45 s timeouts on an exit refusing its credential, then the
  URL was lost). With more than one exit left, the retry now goes through the
  next one and the failure counts against the exit (`--proxy-block-retries`).
- A refused first exit stopped the whole run although the pool had working
  ones (see the pre-check above).
- A UTF-8 BOM (Notepad, Excel) glued itself to the first line of
  `--urls-file` / `--proxy-file`, which was then skipped.
- A `--urls-file` with only comments said "provide --url or --urls-file";
  it now says the file has no inputs.
- `nasa/`, `m.instagram.com/...` and `instagr.am/p/...` inputs were skipped.
- `--dump-html` into a missing directory turned a page that loaded into
  exit 5; a failed dump is now a warning.
- `--fingerprint` without `--fp-tags` picked a phone's user agent about half
  the time, and Instagram then served its mobile layout, reported as "not an
  Instagram page" (exit 5). Only a desktop-Chrome profile is used now (up to
  5 tries), and the mobile layout is named as such. The applied user agent's
  Chrome version is set to the browser that actually runs (it claimed 151
  on Chromium 156), and Edge/Opera profiles are skipped.
- The "blocked" hint through `--scraper-api` suggested `--proxy-file`, which
  that mode ignores; it now suggests a browser engine or
  `INSTAGRAM_CDP_ENDPOINT`.
- `diff_runs.py` JSON against CSV reported every row as `source_changed`
  (`null` vs an empty cell), and always printed price and currency lines
  for rows that have no price.
- `python3 env_config.py` showed the `.env` value of a variable exported
  empty, although the run uses none.
- `.dockerignore` now excludes the per-engine `.venv-*/` directories.
- README: Puppeteer's `PYPPETEER_EXECUTABLE_PATH`, `--allow-different-scope`,
  `--twocaptcha-key` / `--captcha-api`, the exit-2 cases and the location
  columns are documented; the stale check count is gone.
- Proxy traffic meter: Selenium and Puppeteer no longer count requests that never leave the browser. Chrome's new-tab page loads `chrome://` scripts (about 2.8 MB, seen through a stub proxy) that were reported as finished loads and added to `proxy_bytes`, enough to bring a run noticeably closer to `--proxy-budget-mb`. Only `http(s)` requests and responses are counted now. The block sent once the budget is spent is also handed to the page's own event loop, so it works when the budget runs out on another thread.

## [0.3.0] - 2026-10-15

> **Behaviour changes.** CSV output now prefixes a leading apostrophe to any
> text cell that starts with `=`, `+`, `-`, `@`, a tab, CR or LF, so a
> spreadsheet does not run a caption or bio as a formula; the sidecar records
> how many cells were changed as `csv_cells_escaped`. JSON output is
> unchanged. Selenium and pyppeteer now reject a negative `--retries`,
> `--retry-delay`, `--delay-between-pages`, `--max-solves` or
> `--proxy-block-retries` with exit 2, as Playwright already did (Playwright
> now also rejects a negative `--proxy-block-retries`). An `INSTAGRAM_URL`
> in `.env` or the environment no longer replaces a `--urls-file` typed on
> the command line.

### Fixed

- Output, sidecar and checkpoint files are written atomically (a temporary
  file beside the target, fsync, rename). A crash or full disk during a
  write used to leave a truncated, invalid file where the previous good
  output had been. New files get the usual `0o666 & ~umask` mode, and an
  existing file keeps its own mode.
- `INSTAGRAM_URL` silently overrode a typed `--urls-file`, so the run read a
  different account than the one asked for. It is now ignored, with a log
  line, when `--urls-file` is given.
- A paid captcha solve is limited to one per URL as well as by
  `--max-solves` for the run, and both limits are checked before anything
  is bought.
- The credential scan now also reads `smoke_test.py`, untracked `.env.*`
  copies such as `.env.bak`, and flags a bare 32-hex value anywhere.
- `Bearer` tokens are masked in redacted error text.
- The Scraping Browser 401 message no longer claims the credentials last
  about a day; how long they last varies.
- The Playwright `--fp-tags` help example is a single OS tag (`Windows`), as
  in the other two engines.
- The live canary no longer skips green without the `INSTAGRAM_PROXY`
  secret: it runs direct from the GitHub runner and goes red if the site
  was not read, naming the missing secret. Its first run (2026-10-15) was
  refused from the runner with Instagram's login page, so it stays red until
  the secret is set.
- Comments describe this repo's own rules instead of pointing at internal
  notes or other projects.

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
  logged-out visitor (`location` was null on 36 of 36 posts; a geotagged
  post was found later, see 0.4.0).
