"""Regression tests for the manual-QA findings of 2026-10-25..11-05 (one test per finding, numbered as in the QA report)."""
import argparse
import asyncio
import contextlib
import inspect
import io
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import diff_runs
import env_config
import fingerprint_client
import output_writer
import page_flow as flow
import page_parser as parser
import playwright_scraper as playwright
import proxy_pool
import puppeteer_scraper as puppeteer
import selenium_scraper as selenium

FIX = Path(__file__).parent / 'fixtures'
PROFILE = (FIX / 'instagram_profile_natgeo_live_20261004.html').read_text()
POST = (FIX / 'instagram_post_image_live_20261004.html').read_text()
LOGIN = (FIX / 'instagram_login_wall_live_20261004.html').read_text()
CODE = 'DeAZ0uAgNuA'
ENGINES = (playwright, puppeteer, selenium)
# The Docker image runs this suite without the docs (README, .dockerignore).
HAS_DOCS = (ROOT / 'README.md').is_file() and (ROOT / '.dockerignore').is_file()


def creds_url(scheme, login, password, host):
    """Assembled, so the repository's credential scanner does not read a test value as a leak."""
    return scheme + '://' + login + ':' + password + '@' + host


def run_engine(mod, argv):
    """An engine's run() with stderr captured; no browser is ever reached in these tests."""
    args = mod.build_arg_parser().parse_args(argv)
    err = io.StringIO()
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stderr(err):
            result = mod.run(args)
            if inspect.iscoroutine(result):
                result = asyncio.run(result)
    finally:
        logging.disable(logging.NOTSET)
    return result, err.getvalue()


def post_page(code):
    return POST.replace(CODE, code)


def profile_page(username, codes):
    """The natgeo capture re-keyed to `username`, its timeline cut to `codes`."""
    page = PROFILE.replace('"username":"natgeo"', '"username":"%s"' % username)
    original = parser.timeline_shortcodes(PROFILE)
    for old, new in zip(original, list(codes) + ['Unused%05d' % i for i in range(len(original))]):
        page = page.replace('"code":"%s"' % old, '"code":"%s"' % new)
    return page


class FakeEngine:
    """page_flow.Engine over a dict url -> html; `fail` maps a proxy host to an exception for its navigations."""
    name = 'qa'
    readiness_s = 0

    def __init__(self, pages, fail=None):
        self.pages, self.fail = pages, fail or {}
        self.asked, self.via = [], []

    async def open(self, proxy):
        engine = self

        class Session:
            async def goto(self, url):
                engine.asked.append(url)
                engine.via.append(proxy.host if proxy else None)
                if proxy is not None and proxy.host in engine.fail:
                    raise engine.fail[proxy.host]
                self.url = url.removesuffix('?hl=en')
                return 200

            async def content(self):
                return engine.pages[self.url]

            async def wait(self, seconds):
                pass

            async def close(self):
                pass
        return Session()

    async def sleep(self, seconds):
        pass

    async def solve_captcha(self, *a, **kw):
        return None


class QaFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(setattr, flow.STOP, 'signum', None)

    def path(self, name, text=None, data=None):
        p = self.root / name
        if text is not None:
            p.write_text(text, encoding='utf-8')
        if data is not None:
            p.write_bytes(data)
        return str(p)

    def args(self, *flags):
        return playwright.build_arg_parser().parse_args(
            ['--retries', '0', '--solve-captcha', 'off', '--delay-between-pages', '0', '--retry-delay', '0',
             '--out', str(self.root / 'out.json'), *flags])

    def flow_run(self, args, urls, engine, pool=None):
        code = asyncio.run(flow.run(engine, args, urls=urls, proxy_pool=pool, client=None, started_at=0))
        meta_path = Path(args.out + '.meta.json')
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else []
        return code, meta, rows

    # 1 -------------------------------------------------------------------
    def test_01_proxy_file_without_proxies_is_refused_not_direct(self):
        empty = self.path('px.txt', '# none\n\n   \n')
        for mod in ENGINES:
            code, err = run_engine(mod, ['--url', 'natgeo', '--proxy-file', empty, '--out', self.path('o.json')])
            self.assertEqual(code, 2, (mod.__name__, err))
            self.assertIn('has no proxy in it', err)

    # 2 -------------------------------------------------------------------
    def test_02_pyppeteer_uses_an_installed_chrome_on_apple_silicon(self):
        chrome = puppeteer._MAC_BROWSERS[0]
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(puppeteer.sys, 'platform', 'darwin'), \
                mock.patch.object(puppeteer.platform, 'machine', return_value='arm64'), \
                mock.patch.object(puppeteer.os.path, 'exists', side_effect=lambda p: p == chrome):
            self.assertEqual(puppeteer._find_executable(), chrome)
        with mock.patch.dict(os.environ, {'PYPPETEER_EXECUTABLE_PATH': '/x/chrome'}, clear=True):
            self.assertEqual(puppeteer._find_executable(), '/x/chrome')
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(puppeteer.sys, 'platform', 'linux'):
            self.assertIsNone(puppeteer._find_executable())
        if HAS_DOCS:
            self.assertIn('PYPPETEER_EXECUTABLE_PATH', (ROOT / 'README.md').read_text())

    # 3 -------------------------------------------------------------------
    def test_03_unwritable_out_is_refused_before_any_fetch(self):
        os.mkdir(self.root / 'adir')
        for out in (str(self.root / 'missing' / 'x.json'), str(self.root / 'adir')):
            for mod in ENGINES:
                code, err = run_engine(mod, ['--url', 'natgeo', '--out', out])
                self.assertEqual(code, 2, (mod.__name__, out, err))
                self.assertIn('--out', err)

    # 4 -------------------------------------------------------------------
    def test_04_missing_proxy_file_is_a_usage_error(self):
        for mod in ENGINES:
            code, err = run_engine(mod, ['--url', 'natgeo', '--proxy-file', str(self.root / 'nope.txt'),
                                         '--out', self.path('o.json')])
            self.assertEqual(code, 2, (mod.__name__, err))
            self.assertIn('could not read --proxy-file', err)
            self.assertNotIn('Traceback', err)

    # 5 -------------------------------------------------------------------
    def test_05_diff_runs_errors_exit_2_changes_exit_1(self):
        def finish(name, rows):
            out = str(self.root / name)
            output_writer.finish_run(products=rows, out_path=out, fmt='json', engine='t', url='u', pages_requested=1,
                                     pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False,
                                     allow_empty=False, started_at=0.0, extra_meta={'selection': {}})
            return out
        a = finish('a.json', [parser.parse_post(POST, shortcode=CODE)])
        changed = parser.parse_post(POST, shortcode=CODE)
        changed.like_count = (changed.like_count or 0) + 1
        b = finish('b.json', [changed])

        def cli(*argv):
            with mock.patch.object(sys, 'argv', ['diff_runs.py', *argv]), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                return diff_runs.main(), err.getvalue()
        self.assertEqual(cli(a, b, '--fail-on-change')[0], 1)
        self.assertEqual(cli(a, a, '--fail-on-change')[0], 0)
        code, err = cli(a, str(self.root / 'nope.json'), '--fail-on-change')
        self.assertEqual(code, 2)
        self.assertIn('error:', err)
        Path(b + '.meta.json').write_text(json.dumps({**json.loads(Path(b + '.meta.json').read_text()),
                                                      'status': 'partial'}))
        self.assertEqual(cli(a, b, '--fail-on-change')[0], 2)

    # 6 -------------------------------------------------------------------
    def test_06_interrupt_writes_what_was_read_and_exits_130(self):
        urls = [parser.post_url(c) for c in ('Aaaaa11111a', 'Bbbbb22222b', 'Ccccc33333c')]
        class Pages(dict):
            def __getitem__(self, url):
                if url == urls[1]:
                    flow.STOP.signum = signal.SIGINT  # Ctrl-C while the 2nd URL is being read
                return dict.__getitem__(self, url)
        engine = FakeEngine(Pages({u: post_page(u.split('/')[-2]) for u in urls}))
        args = self.args('--url', 'x', '--checkpoint', str(self.root / 'cp.json'))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code, meta, rows = self.flow_run(args, urls, engine)
        self.assertEqual(code, 130)
        self.assertEqual(len(rows), 2)  # the URL in hand is finished, the rest is not started
        self.assertEqual((meta['status'], meta['stop_reason']), ('partial', 'interrupted'))
        self.assertEqual([f['reason'] for f in meta['failed_urls']], ['interrupted'])
        self.assertIn('--resume', err.getvalue())
        flow.STOP.signum = signal.SIGTERM
        self.assertEqual(flow.STOP.exit_code(), 143)
        for mod in ENGINES:
            self.assertIn('page_flow.STOP.install()', inspect.getsource(mod.main))
            self.assertIn('interrupted_exit', inspect.getsource(mod.main))

    # 7 -------------------------------------------------------------------
    def test_07_json_vs_csv_diff_has_no_false_source_changes_or_price_lines(self):
        row = parser.parse_post(POST, shortcode=CODE)
        outs = []
        for fmt in ('json', 'csv'):
            out = str(self.root / ('r.' + fmt))
            output_writer.finish_run(products=[row], out_path=out, fmt=fmt, engine='t', url='u', pages_requested=1,
                                     pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False,
                                     allow_empty=False, started_at=0.0, extra_meta={'selection': {}})
            outs.append(out)
        result = diff_runs.diff(*outs)
        self.assertEqual((result['source_changed'], result['changed'], result['field_changes']), ([], [], []))
        with mock.patch.object(sys, 'argv', ['diff_runs.py', *outs]), contextlib.redirect_stdout(io.StringIO()) as out:
            diff_runs.main()
        self.assertNotIn('price', out.getvalue())

    # 8 -------------------------------------------------------------------
    def test_08_env_report_shows_an_exported_empty_value_as_unset(self):
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        variable = 'INSTAGRAM_PROXY'
        Path('.env').write_text(variable + '=' + creds_url('http', 'u', 'p', 'h:1') + '\n')
        with mock.patch.dict(os.environ, {'INSTAGRAM_PROXY': ''}), contextlib.redirect_stdout(io.StringIO()) as out:
            env_config._report()
        line = next(l for l in out.getvalue().splitlines() if 'INSTAGRAM_PROXY' in l)
        self.assertIn('exported empty', line)
        self.assertNotIn('[.env]', line)

    # 9, 10, 20 -----------------------------------------------------------
    def test_09_10_20_input_list_errors_and_bom(self):
        comments = self.path('c.txt', '# only comments\n\n')
        code, err = run_engine(playwright, ['--urls-file', comments])
        self.assertEqual(code, 2)
        self.assertIn('has no inputs', err)
        both = self.path('b.txt', 'nasa\n')
        code, err = run_engine(playwright, ['--urls-file', both, '--url', 'natgeo'])
        self.assertEqual(code, 2)
        self.assertIn('one of --url / --urls-file', err)
        bom = self.path('bom.txt', data=b'\xef\xbb\xbfnatgeo\r\nnasa\r\n')
        ns = argparse.Namespace(url=None, urls_file=bom)
        self.assertEqual(flow.resolve_urls(ns), ([parser.profile_url('natgeo'), parser.profile_url('nasa')], 0))
        proxies = self.path('px.txt', data=b'\xef\xbb\xbfhost:1234\n')
        self.assertEqual(proxy_pool.load_proxies(None, proxies)[0].host, 'host')

    # 11 ------------------------------------------------------------------
    def test_11_socks_proxy_with_login_is_a_usage_error(self):
        for mod in (playwright, puppeteer):
            code, err = run_engine(mod, ['--url', 'natgeo', '--proxy', creds_url('socks5', 'u', 'p', '1.2.3.4:1080'),
                                         '--out', self.path('o.json')])
            self.assertEqual(code, 2, (mod.__name__, err))
            self.assertIn('SOCKS', err)

    # 12 ------------------------------------------------------------------
    def test_12_more_input_shapes(self):
        nasa = parser.profile_url('nasa')
        for text in ('nasa/', '@nasa/', 'https://m.instagram.com/nasa/', 'm.instagram.com/nasa'):
            self.assertEqual(parser.normalize_input(text), nasa, text)
        for text in ('https://instagr.am/p/%s/' % CODE, 'instagr.am/p/%s' % CODE):
            self.assertEqual(parser.normalize_input(text), parser.post_url(CODE), text)
        for text in ('explore/', 'https://evil.com/nasa/', 'nasa/extra/'):
            self.assertIsNone(parser.normalize_input(text), text)

    # 13, 14 --------------------------------------------------------------
    @unittest.skipUnless(HAS_DOCS, 'docs are not copied into the Docker image')
    def test_13_14_docs_and_docker_context(self):
        readme = (ROOT / 'README.md').read_text()
        for needle in ('--allow-different-scope', '--twocaptcha-key', '--captcha-api'):
            self.assertIn(needle, readme)
        self.assertNotIn('# 63 offline checks', readme)
        self.assertIn('.venv-*/', (ROOT / '.dockerignore').read_text().splitlines())

    # 15 is checked in smoke_test (finish_run with every page read).

    # 16 ------------------------------------------------------------------
    def test_16_a_failing_exit_hands_the_retry_to_the_next_one_and_is_dropped(self):
        urls = [parser.post_url(c) for c in ('Aaaaa11111a', 'Bbbbb22222b', 'Ccccc33333c')]
        pages = {u: post_page(u.split('/')[-2]) for u in urls}
        pool = proxy_pool.ProxyPool([proxy_pool.parse_proxy_line('good:1'), proxy_pool.parse_proxy_line('bad:2')],
                                    block_retries=1)
        engine = FakeEngine(pages, fail={'bad': RuntimeError('Page.goto: Timeout 45000ms exceeded')})
        code, meta, rows = self.flow_run(self.args('--url', 'x', '--retries', '2'), urls, engine, pool)
        self.assertEqual((code, len(rows)), (0, 3))
        self.assertEqual(engine.via.count('bad'), 1)  # tried once, then out of the pool
        self.assertEqual(pool.live_count(), 1)

    # 17 ------------------------------------------------------------------
    def test_17_precheck_drops_a_refused_exit_and_keeps_the_rest(self):
        pool = proxy_pool.ProxyPool([proxy_pool.parse_proxy_line(creds_url('http', 'u', 'bad', 'a:1')),
                                     proxy_pool.parse_proxy_line(creds_url('http', 'u', 'ok', 'b:2'))])

        def precheck(proxy_url, probe, timeout):
            if ':bad@' in proxy_url:
                raise proxy_pool.ProxyPrecheckError(407)
            return 200
        args = argparse.Namespace(out=str(self.root / 'o.json'), cdp_endpoint=None, scraper_api=False)
        with mock.patch.object(proxy_pool, 'precheck_proxy', side_effect=precheck):
            stop = flow.proxy_precheck(args, pool, urls=[parser.profile_url('natgeo')], engine_name='t', started_at=0)
        self.assertIsNone(stop)
        self.assertEqual([p.host for p in pool.live()], ['b'])
        every = proxy_pool.ProxyPool([proxy_pool.parse_proxy_line(creds_url('http', 'u', 'bad', 'a:1'))])
        with mock.patch.object(proxy_pool, 'precheck_proxy', side_effect=precheck), \
                contextlib.redirect_stderr(io.StringIO()):
            stop = flow.proxy_precheck(args, every, urls=[parser.profile_url('natgeo')], engine_name='t', started_at=0)
        self.assertEqual(stop, output_writer.EXIT_REMOTE_API_ERROR)

    # 18 ------------------------------------------------------------------
    def test_18_selenium_refuses_a_proxy_with_a_login(self):
        code, err = run_engine(selenium, ['--url', 'natgeo', '--proxy', creds_url('http', 'u', 'p', '1.2.3.4:8080'),
                                          '--out', self.path('o.json')])
        self.assertEqual(code, 2)
        self.assertIn('cannot authenticate', err)
        self.assertNotIn(':p@', err)

    # 19 ------------------------------------------------------------------
    def test_19_max_results_never_drops_an_input_profile(self):
        names = ['natgeo', 'nasa', 'bbcnews']
        urls = [parser.profile_url(n) for n in names]
        code, err = run_engine(playwright, ['--urls-file', self.path('five.txt', '\n'.join(names)),
                                            '--posts', '3', '--max-results', '2', '--out', self.path('o.json')])
        self.assertEqual(code, 2)
        self.assertIn('--max-results 2 is smaller than the 3 inputs', err)
        pages = {}
        for n, name in enumerate(names):
            codes = ['Post%d%06d' % (n, i) for i in range(3)]
            pages[urls[n]] = profile_page(name, codes)
            for c in codes:
                pages[parser.post_url(c)] = post_page(c)
        engine = FakeEngine(pages)
        code, meta, rows = self.flow_run(self.args('--url', 'x', '--posts', '3', '--max-results', '5'), urls, engine)
        fetched = [u.removesuffix('?hl=en') for u in engine.asked]
        self.assertTrue(all(u in fetched for u in urls), fetched)  # every input profile is read
        self.assertEqual(len(fetched), 5)
        trimmed = [e for e in meta['excluded_urls'] if e['reason'] == 'max_results']
        self.assertEqual(len(trimmed), 7)  # 9 posts found, room for 2

    # 21 ------------------------------------------------------------------
    def test_21_out_may_not_overwrite_an_input(self):
        inp = self.path('in.txt', 'natgeo\n')
        code, err = run_engine(playwright, ['--urls-file', inp, '--out', inp, '--format', 'csv'])
        self.assertEqual(code, 2)
        self.assertIn('would overwrite --urls-file', err)
        self.assertEqual(Path(inp).read_text(), 'natgeo\n')

    # 22 ------------------------------------------------------------------
    def test_22_location_is_read(self):
        page = POST.replace('"location":null', '"location":{"__typename":"XDTLocationDict","pk":471367639,'
                            '"lat":33.2,"lng":-87.5,"name":"Bryant-Denny Stadium","profile_pic_url":null}', 1)
        row = parser.parse_post(page, shortcode=CODE)
        self.assertEqual((row.location_name, row.location_id, row.location_lat, row.location_lng),
                         ('Bryant-Denny Stadium', '471367639', 33.2, -87.5))
        plain = parser.parse_post(POST, shortcode=CODE)
        self.assertIsNone(plain.location_name)
        self.assertIn('location_name', output_writer.PRODUCT_FIELD_NAMES)

    # 23, 26, 29 ----------------------------------------------------------
    def test_23_26_29_every_page_is_asked_in_english(self):
        url = parser.post_url(CODE)
        engine = FakeEngine({url: POST})
        self.flow_run(self.args('--url', 'x'), [url], engine)
        self.assertEqual(engine.asked, [url + '?hl=en'])

    # 24 ------------------------------------------------------------------
    def test_24_a_failed_html_dump_does_not_fail_the_page(self):
        url = parser.post_url(CODE)
        args = self.args('--url', 'x', '--dump-html')
        with mock.patch.object(flow._Path, 'write_text', side_effect=OSError('disk full')):
            code, meta, rows = self.flow_run(args, [url], FakeEngine({url: POST}))
        self.assertEqual((code, len(rows)), (0, 1))

    # 25 ------------------------------------------------------------------
    def test_25_mobile_layout_is_named_and_phone_profiles_are_skipped(self):
        url = parser.post_url(CODE)
        mobile = POST.replace('comet.igweb.PolarisLoggedOutDesktopWWWPostRoute',
                              'comet.instagramwebloggedout.PolarisLoggedOutMobilePostRoute')
        outcome = flow.decide(url=url, http_status=200, html=mobile)
        self.assertEqual(outcome.failure, 'fetch_error')
        self.assertIn('MOBILE layout', outcome.warnings[0])
        self.assertNotIn('not an Instagram page', outcome.warnings[0])
        android = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Mobile Safari/537.36'
        windows = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36'
        client = mock.Mock()
        client.random_fingerprint.side_effect = [{'userAgent': {'userAgent': android}},
                                                 {'userAgent': {'userAgent': windows}}]
        self.assertEqual(fingerprint_client.choose_user_agent(client), windows)
        client.random_fingerprint.side_effect = None
        client.random_fingerprint.return_value = {'userAgent': {'userAgent': android}}
        self.assertIsNone(fingerprint_client.choose_user_agent(client, attempts=3))

    # 27 ------------------------------------------------------------------
    def test_27_user_agent_claims_the_running_chrome(self):
        edge = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0')
        self.assertIsNotNone(fingerprint_client.unsuitable_reason(edge))
        ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36'
        self.assertIn('Chrome/156.0.0.0 ', fingerprint_client.align_user_agent(ua, '156.0.7258.5'))
        self.assertIn('Chrome/155.0.0.0 ', fingerprint_client.align_user_agent(ua, 'HeadlessChrome/155.0.1.2'))
        self.assertEqual(fingerprint_client.align_user_agent(ua, None), ua)

    # 28 ------------------------------------------------------------------
    def test_28_scraper_api_block_hint_does_not_suggest_a_proxy(self):
        url = parser.profile_url('natgeo')
        via_api = flow.decide(url=url, http_status=200, html=LOGIN, scraper_api=True).warnings[0]
        self.assertNotIn('--proxy-file', via_api)
        self.assertIn('INSTAGRAM_CDP_ENDPOINT', via_api)
        self.assertIn('--proxy-file', flow.decide(url=url, http_status=200, html=LOGIN).warnings[0])


if __name__ == '__main__':
    unittest.main()
