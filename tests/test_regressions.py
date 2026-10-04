"""Failure and recovery scenarios: exercise the shared flow without network or paid tasks."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import page_flow as flow
import page_parser as parser
import playwright_scraper as playwright
import puppeteer_scraper as puppeteer
import selenium_scraper as selenium
import diff_runs

FIX = Path(__file__).parent / 'fixtures'
POST = (FIX / 'instagram_post_image_live_20261004.html').read_text()
NOT_FOUND = (FIX / 'instagram_not_found_live_20261004.html').read_text()
LOGIN = (FIX / 'instagram_login_wall_live_20261004.html').read_text()
THROTTLED = (FIX / 'instagram_throttled_post_live_20261004.html').read_text()
CODE = 'DeAZ0uAgNuA'


def url(n):
    return parser.post_url(code(n))


def code(n):
    return CODE if n == 1 else 'Zz' + str(n).zfill(9)


def page_for(n):
    """The real image-post capture, re-keyed to post n's shortcode."""
    return POST.replace(CODE, code(n))


class Session:
    def __init__(self, engine):
        self.engine, self.url = engine, ''

    async def goto(self, target):
        self.url = target
        e = self.engine
        e.gotos += 1
        if e.mode == 'navigation_error':
            raise RuntimeError('navigation failed')
        if e.mode == 'recover_once' and e.gotos == 1:
            raise RuntimeError('net::ERR_TIMED_OUT')
        return 200

    async def content(self):
        e = self.engine
        n = 2 if self.url == url(2) else 1
        if e.mode == 'content_error' and n == 2:
            raise RuntimeError('navigation raced with content')
        if e.mode == 'not_found':
            return NOT_FOUND
        if e.mode == 'login_wall' and n == 2:
            return LOGIN
        if e.mode == 'throttled' and n == 2:
            return THROTTLED
        if e.mode == 'bad_post' and n == 2:
            return page_for(2).replace('"taken_at"', '"taken_at":0,"x_taken_at"').replace('"code":"%s"' % code(2), '"code":"other"')
        return page_for(n)

    async def wait(self, seconds):
        pass

    async def close(self):
        self.engine.closed += 1
        if self.engine.mode == 'close_error':
            raise RuntimeError('cleanup failed')


class Engine:
    name = 'regression'
    readiness_s = 0

    def __init__(self, mode):
        self.mode = mode
        self.gotos = self.closed = 0

    async def open(self, proxy):
        return Session(self)

    async def sleep(self, seconds):
        pass

    async def solve_captcha(self, *args, **kwargs):
        raise AssertionError('no paid solving in regression tests')


class Regressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_flow(self, mode, *, numbers=(1, 2), name='result', retries='0'):
        out = str(Path(self.tmp.name) / (name + '.json'))
        args = playwright.build_arg_parser().parse_args([
            '--url', url(1), '--out', out, '--max-results', '5',
            '--solve-captcha', 'off', '--retries', retries, '--allow-empty'])
        engine = Engine(mode)
        code_ = asyncio.run(flow.run(engine, args, urls=[url(n) for n in numbers],
                                     proxy_pool=None, client=None, started_at=0))
        return code_, json.loads(Path(out + '.meta.json').read_text()), json.loads(Path(out).read_text()), engine, out

    def test_success_and_not_found(self):
        code_, meta, rows, engine, _ = self.run_flow('good')
        self.assertEqual((code_, meta['status'], len(rows)), (0, 'complete', 2))
        self.assertEqual([r['shortcode'] for r in rows], [code(1), code(2)])
        self.assertEqual(engine.closed, 2)
        code_, meta, rows, _, _ = self.run_flow('not_found')
        self.assertEqual((code_, len(meta['not_found_urls'])), (4, 2))

    def test_partial_failures_keep_good_rows(self):
        for mode, reason in (('bad_post', 'parse_error'), ('content_error', 'fetch_error'), ('login_wall', 'blocked'),
                             ('throttled', 'rate_limited')):
            with self.subTest(mode=mode):
                code_, meta, rows, _, _ = self.run_flow(mode)
                self.assertEqual((code_, meta['status'], len(rows)), (6, 'partial', 1))
                self.assertEqual(meta['failed_pages'], [2])
                self.assertEqual(meta['failed_urls'][0], {'url': url(2), 'reason': reason})

    def test_unread_is_not_empty(self):
        self.assertEqual(self.run_flow('navigation_error')[0], 5)

    def test_navigation_retry_recovers(self):
        code_, _, rows, engine, _ = self.run_flow('recover_once', numbers=(1,), retries='1')
        self.assertEqual((code_, len(rows), engine.gotos), (0, 1, 2))

    def test_rate_limit_reason(self):
        code_, meta, rows, _, _ = self.run_flow('throttled')
        self.assertEqual((code_, meta['stop_reason'], len(rows)), (6, 'rate_limited', 1))

    def test_cleanup_failure_preserves_rows(self):
        self.assertEqual(self.run_flow('close_error')[0], 0)

    def test_batch_scope(self):
        a = self.run_flow('good', numbers=(1, 2), name='a')[-1]
        b = self.run_flow('good', numbers=(1, 3), name='b')[-1]
        with self.assertRaises(SystemExit):
            diff_runs.diff(a, b)
        self.assertEqual(diff_runs.diff(a, a)['removed'], [])

    def test_partial_run_cannot_be_diffed(self):
        out = self.run_flow('throttled', name='p')[-1]
        with self.assertRaises(SystemExit):
            diff_runs.diff(out, out)

    def test_url_validation(self):
        for value in ('https://evilinstagram.com/natgeo', 'file:///natgeo', '/natgeo',
                      'https://user:pass@www.instagram.com/natgeo/', 'https://www.instagram.com:8080/natgeo/',
                      'https://www.instagram.com/explore/tags/x/', 'https://www.instagram.com/p/x/'):
            self.assertIsNone(parser.normalize_input(value), value)
        self.assertEqual(parser.normalize_input(url(1)), url(1))

    def test_local_solver_never_buys_undeliverable_token(self):
        client = Mock()
        for engine in (playwright, puppeteer):
            result = asyncio.run(engine._maybe_solve_captcha(html='<div class="cf-turnstile" data-sitekey="x"></div>',
                                 url=url(1), client=client, policy='when-blocked'))
            self.assertEqual(result['action'], 'unsupported_delivery')
        result = selenium._maybe_solve_captcha(html='challenge', url=url(1), client=client, policy='always')
        self.assertEqual(result['action'], 'unsupported_delivery')
        self.assertEqual(client.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
