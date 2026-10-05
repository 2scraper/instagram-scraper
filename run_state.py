"""Incremental selection and atomic checkpoints; never stores credentials or HTML."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile

from output_writer import Product

VERSION = 1


def utc_date(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        raise ValueError('use an ISO date or timestamp, e.g. 2026-10-01 or 2026-10-01T12:00:00Z') from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def add_arguments(parser):
    parser.add_argument('--since', help='Include posts at or after this ISO date/time (UTC if no offset); profiles are retained')
    parser.add_argument('--incremental-from', metavar='JSON_OR_CSV', help='Skip already collected posts from a complete, matching snapshot; profiles are refreshed')
    parser.add_argument('--checkpoint', metavar='PATH', help='Atomically save progress after each URL; use a separate file from output')
    parser.add_argument('--resume', action='store_true', help='Continue --checkpoint, retrying failed URLs and retaining successful rows')


def selection(args, urls):
    return {'mode': 'urls', 'urls': urls, 'posts': int(getattr(args, 'posts', 0) or 0),
            'max_results': args.max_results, 'since': getattr(args, '_since', None),
            'incremental_sha256': getattr(args, '_incremental_digest', None)}


def atomic_json(path, data):
    target = Path(path)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix=target.name + '.', suffix='.tmp', delete=False) as stream:
            temp = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def prepare(args, urls):
    """Validate before starting a browser or paid transport. Returns a checkpoint or None."""
    import page_parser as pp
    from diff_runs import _load_rows

    args._since = utc_date(args.since).isoformat() if getattr(args, 'since', None) else None
    args._known_posts = set()
    args._incremental_digest = None
    baseline = getattr(args, 'incremental_from', None)
    if baseline:
        path = Path(baseline)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        meta = json.loads(Path(str(path) + '.meta.json').read_text())
        if not isinstance(meta, dict):
            raise ValueError('invalid baseline metadata')
        if meta.get('status') != 'complete' or meta.get('output_sha256') != digest:
            raise ValueError('incremental baseline must be complete and have a matching output_sha256')
        old = meta.get('selection') or {}
        if not isinstance(old, dict):
            raise ValueError('invalid baseline selection')
        for key in ('urls', 'posts', 'max_results'):
            if old.get(key) != selection(args, urls)[key]:
                raise ValueError('incremental baseline has a different selection: ' + key)
        if old.get('since') != args._since:
            raise ValueError('incremental baseline has a different date filter')
        rows = _load_rows(str(path))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('invalid baseline rows')
        history = meta.get('seen_post_skus', [])
        if not isinstance(history, list) or any(not isinstance(s, str) or not s.startswith('instagram-post-') for s in history):
            raise ValueError('invalid incremental post history')
        args._known_posts = set(history) | {row['sku'] for row in rows if row.get('category') == 'post' and row.get('sku')}
        args._incremental_digest = digest
    checkpoint = getattr(args, 'checkpoint', None)
    if getattr(args, 'resume', False) and not checkpoint:
        raise ValueError('--resume requires --checkpoint PATH')
    if not checkpoint:
        return None
    path = Path(checkpoint).resolve()
    output = Path(args.out or ('instagram_results.' + args.format)).resolve()
    protected = {output, Path(str(output) + '.meta.json')}
    for value in (baseline, getattr(args, 'urls_file', None)):
        if value:
            protected.add(Path(value).resolve())
    if baseline:
        protected.add(Path(baseline + '.meta.json').resolve())
    if path in protected:
        raise ValueError('checkpoint must be separate from input, baseline, output and metadata')
    if not path.parent.is_dir():
        raise ValueError('checkpoint parent directory does not exist')
    if not getattr(args, 'resume', False):
        if path.exists():
            raise ValueError('checkpoint already exists; use --resume or choose a new path')
        return None
    state = json.loads(path.read_text())
    if not isinstance(state, dict):
        raise ValueError('invalid checkpoint object')
    if state.get('version') != VERSION or state.get('selection') != selection(args, urls):
        raise ValueError('checkpoint version or selection does not match this run')
    queue, records = state.get('queue'), state.get('records')
    if (not isinstance(queue, list) or not isinstance(records, dict)
            or any(not isinstance(u, str) or pp.normalize_input(u) != u for u in queue)
            or len(set(queue)) != len(queue) or any(u not in queue for u in records)
            or any(u not in queue for u in urls)):
        raise ValueError('invalid checkpoint queue')
    for record in records.values():
        if not isinstance(record, dict) or not isinstance(record.get('done'), bool):
            raise ValueError('invalid checkpoint record')
        if record.get('product') is not None:
            Product(**record['product'])  # validate row schema before launching
    return state


def save(args, urls, queue, records):
    if not getattr(args, 'checkpoint', None):
        return
    atomic_json(args.checkpoint, {'version': VERSION, 'selection': selection(args, urls),
                                 'queue': queue, 'records': records})
