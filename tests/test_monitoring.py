"""Monitoring, incremental and interruption controls using captured public HTML."""
import asyncio
import dataclasses
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import page_flow as flow
import page_parser as parser
import playwright_scraper as playwright
import puppeteer_scraper as puppeteer
import selenium_scraper as selenium
import diff_runs
from proxy_pool import ProxyPool, parse_proxy_line
import run_state

FIX = Path(__file__).parent / 'fixtures'
PROFILE = (FIX / 'instagram_profile_natgeo_live_20261004.html').read_text()
POST = (FIX / 'instagram_post_image_live_20261004.html').read_text()
LOGIN = (FIX / 'instagram_login_wall_live_20261004.html').read_text()
CODE = 'DeAZ0uAgNuA'
USER = parser.profile_url('natgeo')
P1 = parser.post_url(CODE)
P2 = parser.post_url('Abcdef12345')


class Engine:
    name = 'test'
    readiness_s = 0
    def __init__(self, pages=None, interrupt=None, statuses=None):
        self.pages = pages or {USER: PROFILE, P1: POST, P2: POST.replace(CODE, 'Abcdef12345')}
        self.interrupt = interrupt
        self.statuses = statuses or {}
        self.asked, self.proxies = [], []
    async def open(self, proxy):
        self.proxies.append(proxy)
        engine = self
        class Session:
            async def goto(self, url):
                self.url = url
                engine.asked.append(url)
                if url == engine.interrupt:
                    raise asyncio.CancelledError()
                return engine.statuses.get(url, 200)
            async def content(self):
                return engine.pages[self.url]
            async def wait(self, seconds): pass
            async def close(self): pass
        return Session()
    async def sleep(self, seconds): pass
    async def solve_captcha(self, *a, **kw): return None


class Monitoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
    def args(self, *flags, name='out'):
        return playwright.build_arg_parser().parse_args(['--url', 'natgeo', '--retries', '0', '--solve-captcha', 'off',
                    '--delay-between-pages', '0', '--out', str(self.root / (name + '.json')), '--allow-empty', *flags])
    def run_flow(self, args, urls, engine=None, pool=None):
        engine = engine or Engine()
        code = asyncio.run(flow.run(engine, args, urls=urls, proxy_pool=pool, client=None, started_at=0))
        meta = json.loads(Path(args.out + '.meta.json').read_text()) if Path(args.out + '.meta.json').exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else None
        return code, meta, rows, engine
    def test_proxy_never_falls_back_direct_and_200_counts(self):
        for status in (200, 403):
            pool = ProxyPool([parse_proxy_line('http://127.0.0.1:8888')], block_retries=1)
            eng = Engine({USER: LOGIN, P1: LOGIN}, statuses={USER: status})
            code, meta, _, _ = self.run_flow(self.args(), [USER, P1], eng, pool)
            self.assertEqual(code, 3)
            self.assertEqual(len(eng.proxies), 1)
            self.assertIsNotNone(eng.proxies[0])
            self.assertIn('proxy_pool_exhausted', [f['reason'] for f in meta['failed_urls']])
    def test_missing_timeline_is_partial_and_keeps_profile(self):
        eng = Engine({USER: PROFILE.replace('polaris_ordered_timeline_connection', 'unknown_timeline')})
        code, meta, rows, _ = self.run_flow(self.args('--posts', '2'), [USER], eng)
        self.assertEqual((code, meta['stop_reason'], len(rows)), (6, 'parse_error', 1))
    def test_attribute_order_and_route_whitespace(self):
        body = PROFILE.replace('<script type="application/json"', '<script data-x="1" type="application/json"')
        body = body.replace('"canonicalRouteName":', '"canonicalRouteName" : ')
        self.assertEqual(len(parser.parse_page(body, url=USER).products), 1)
    def test_date_filter_does_not_drop_profiles(self):
        code, meta, rows, eng = self.run_flow(self.args('--since', '2099-01-01'), [USER, P1])
        self.assertEqual((code, len(rows), rows[0]['category']), (0, 1, 'profile'))
        self.assertEqual(meta['excluded_urls'], [{'url': P1, 'reason': 'before_since'}])
        self.assertEqual(eng.asked, [USER, P1])
    def test_since_boundary_inclusive_and_timezone(self):
        row = parser.parse_page(POST, url=P1).products[0]
        self.assertEqual(self.run_flow(self.args('--since', row.posted_at), [P1])[0], 0)
        self.assertEqual(run_state.utc_date('2026-10-01T05:00:00+05:00'), run_state.utc_date('2026-10-01'))
    def test_unknown_timestamp_is_partial(self):
        body = POST.replace('"taken_at":', '"unknown_taken_at":')
        code, _, _, _ = self.run_flow(self.args('--since', '2020-01-01'), [P1], Engine({P1: body}))
        self.assertEqual(code, 6)
    def test_incremental_refreshes_profile_and_skips_known_post(self):
        base = self.args(name='base')
        self.run_flow(base, [USER, P1])
        args = self.args('--incremental-from', base.out)
        code, meta, rows, eng = self.run_flow(args, [USER, P1])
        self.assertEqual((code, len(rows), eng.asked), (0, 1, [USER]))
        self.assertTrue(meta['incremental'])
        self.assertEqual(meta['excluded_urls'][0]['reason'], 'already_seen')
        with self.assertRaises(SystemExit):
            diff_runs.diff(args.out, args.out)
    def test_incremental_does_not_refetch_posts_before_since(self):
        base = self.args('--since', '2099-01-01', name='base'); self.run_flow(base, [USER, P1])
        delta = self.args('--since', '2099-01-01', '--incremental-from', base.out, name='delta')
        code, meta, _, engine = self.run_flow(delta, [USER, P1])
        self.assertEqual((code, engine.asked), (0, [USER]))
        self.assertIn('instagram-post-' + CODE, meta['seen_post_skus'])
    def test_stop_does_not_report_known_posts_as_unread(self):
        # P2 is not found in the baseline, so only P1 is a known post.
        base = self.args(name='base')
        self.run_flow(base, [USER, P2, P1], Engine({USER: PROFILE, P2: (FIX / 'instagram_not_found_live_20261004.html').read_text(), P1: POST}))
        args = self.args('--incremental-from', base.out)
        pool = ProxyPool([parse_proxy_line('http://127.0.0.1:8888')], block_retries=1)
        # USER kills the only exit, P2 finds the pool exhausted, P1 is still queued.
        _, meta, _, _ = self.run_flow(args, [USER, P2, P1], Engine({USER: LOGIN}), pool=pool)
        self.assertEqual([f['url'] for f in meta['failed_urls']], [USER, P2])
        self.assertEqual(meta['excluded_urls'], [{'url': P1, 'reason': 'already_seen'}])
    def test_incremental_chain_remembers_prior_posts(self):
        base = self.args(name='base'); self.run_flow(base, [USER, P1])
        delta = self.args('--incremental-from', base.out, name='delta')
        self.run_flow(delta, [USER, P1])
        next_delta = self.args('--incremental-from', delta.out, name='next')
        code, meta, rows, engine = self.run_flow(next_delta, [USER, P1])
        self.assertEqual((code, len(rows), engine.asked), (0, 1, [USER]))
        self.assertIn('instagram-post-' + CODE, meta['seen_post_skus'])

    def test_resume_retries_failure_without_refetching_success(self):
        cp = str(self.root/'state.json')
        args = self.args('--checkpoint', cp)
        self.assertEqual(self.run_flow(args, [USER, P1], Engine({USER: PROFILE, P1: LOGIN}))[0], 6)
        resume = self.args('--checkpoint', cp, '--resume')
        code, meta, rows, eng = self.run_flow(resume, [USER, P1])
        self.assertEqual((code, len(rows), eng.asked), (0, 2, [P1]))
        self.assertEqual(meta['failed_urls'], [])
    def test_cancelled_run_restores_successful_rows(self):
        cp = str(self.root/'cancel.json')
        args = self.args('--checkpoint', cp)
        with self.assertRaises(asyncio.CancelledError):
            self.run_flow(args, [USER, P1], Engine(interrupt=P1))
        self.assertTrue(json.loads(Path(cp).read_text())['records'][USER]['done'])
        code, _, rows, eng = self.run_flow(self.args('--checkpoint', cp, '--resume'), [USER, P1])
        self.assertEqual((code, len(rows), eng.asked), (0, 2, [P1]))
    def test_resume_preserves_dynamic_queue(self):
        codes = json.loads(parser.parse_page(PROFILE, url=USER).products[0].recent_posts_json)[:2]
        urls = [parser.post_url(c) for c in codes]
        pages = {USER: PROFILE, **{u: POST.replace(CODE, c) for u,c in zip(urls,codes)}}
        cp = str(self.root/'dynamic.json')
        with self.assertRaises(asyncio.CancelledError):
            self.run_flow(self.args('--checkpoint', cp, '--posts', '2'), [USER], Engine(pages, interrupt=urls[1]))
        code, meta, rows, eng = self.run_flow(self.args('--checkpoint', cp, '--resume', '--posts', '2'), [USER], Engine(pages))
        self.assertEqual((code,len(rows),eng.asked),(0,3,[urls[1]]))
        self.assertTrue(meta['capped'])
    def test_invalid_configuration_before_fetch(self):
        cp = self.root/'bad.json'
        cp.write_text('{}')
        for flags in [('--since','nonsense'),('--resume',),('--checkpoint',str(cp)),('--checkpoint',str(cp),'--resume')]:
            code, _, _, eng = self.run_flow(self.args(*flags), [USER])
            self.assertEqual((code,eng.asked),(2,[]))
    def test_tampered_baseline_and_changed_resume_selection(self):
        base = self.args(name='base'); self.run_flow(base, [USER, P1])
        Path(base.out).write_text('[]')
        args = self.args('--incremental-from', base.out)
        self.assertEqual(self.run_flow(args, [USER, P1])[0], 2)
        cp = str(self.root/'selection.json')
        self.run_flow(self.args('--checkpoint', cp), [USER])
        self.assertEqual(self.run_flow(self.args('--checkpoint', cp, '--resume', '--since', '2026-01-01'), [USER])[0], 2)
        Path(cp).write_text('[]')
        self.assertEqual(self.run_flow(self.args('--checkpoint', cp, '--resume'), [USER])[0], 2)

    def test_checkpoint_cannot_overwrite_output(self):
        args = self.args()
        args.checkpoint = args.out
        self.assertEqual(self.run_flow(args,[USER])[0],2)
    def test_atomic_checkpoint_keeps_previous_on_replace_error(self):
        path = self.root/'atomic.json'
        run_state.atomic_json(path, {'old': True})
        with patch.object(run_state.os,'replace',side_effect=OSError('disk error')):
            with self.assertRaises(OSError): run_state.atomic_json(path, {'new': True})
        self.assertEqual(json.loads(path.read_text()), {'old': True})
        self.assertEqual(list(self.root.glob('*.tmp')), [])
    def test_metrics_and_text_diff_csv_json(self):
        a = self.args(name='before');self.run_flow(a,[USER,P1])
        # Modify the parsed row directly to keep the test independent of fixture counts.
        row = parser.parse_page(PROFILE,url=USER).products[0]
        b = self.args(name='after');b.format='csv';b.out=str(self.root/'after.csv')
        flow.finish(b,products=[dataclasses.replace(row,follower_count=row.follower_count+7,biography='Updated bio'),
                               parser.parse_page(POST,url=P1).products[0]],blocked=False,remote_api_error=False,
                    engine_name='test',urls=[USER,P1],started_at=0,pages_completed=2,failed_pages=[])
        result=diff_runs.diff(a.out,b.out)
        changes=result['field_changes'][0]['fields']
        self.assertEqual(changes['follower_count']['delta'],7)
        self.assertEqual(changes['biography']['new'],'Updated bio')
        proc=subprocess.run([sys.executable,str(Path(diff_runs.__file__)),a.out,b.out,'--fail-on-change'],capture_output=True)
        self.assertEqual(proc.returncode,1)
        proc=subprocess.run([sys.executable,str(Path(diff_runs.__file__)),a.out,b.out,'--fields','follower_count, caption'],capture_output=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
    def test_post_window_reports_left_selection(self):
        a=self.args('--posts','1',name='a');b=self.args('--posts','1',name='b')
        for args,target in [(a,P1),(b,P2)]:
            row=parser.parse_page(POST.replace(CODE,parser.classify_url(target)[1]),url=target).products[0]
            flow.finish(args,products=[row],blocked=False,remote_api_error=False,engine_name='test',urls=[USER],
                        started_at=0,pages_completed=1,failed_pages=[])
        result=diff_runs.diff(a.out,b.out)
        self.assertEqual(result['removed'],[])
        self.assertEqual(result['left_selection'],['instagram-post-'+CODE])
    def test_flags_on_all_engines(self):
        for engine in (playwright,puppeteer,selenium):
            args=engine.build_arg_parser().parse_args(['--url','natgeo','--since','2026-10-01','--checkpoint','state.json','--resume'])
            self.assertEqual(args.since,'2026-10-01')
            self.assertTrue(args.resume)


if __name__ == '__main__': unittest.main()
