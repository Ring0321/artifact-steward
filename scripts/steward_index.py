"""Resumable metadata passes, full-file hash cache and local retention records."""
from __future__ import annotations
import argparse
import csv
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from steward_common import (UNSAFE, absolute, checked, ensure_dir, identity, key, now,
                            read_json, sha, snapshot, within, write_json)

EXCLUDED = {'.git', '.codex', '.agents'}


def connect(root, state):
    root, state = checked(root), checked(state, missing=True)
    if within(state, root) or within(root, state):
        raise ValueError('state_and_source_must_be_disjoint')
    ensure_dir(state)
    marker = state / '.artifact-steward-index.json'
    rs = root.stat()
    owner = {'tool': 'artifact-steward-index', 'schema': 2, 'root': key(root),
             'root_identity': [rs.st_dev, rs.st_ino]}
    if marker.exists():
        if read_json(marker) != owner:
            raise ValueError('state_root_or_owner_mismatch')
    else:
        if any(state.iterdir()):
            raise ValueError('refuse_unowned_state_directory')
        write_json(marker, owner, new=True)
    dbpath = state / 'catalog.sqlite3'
    checked(dbpath, missing=True)
    if dbpath.exists() and identity(dbpath)['links'] != 1:
        raise ValueError('linked_catalog')
    db = sqlite3.connect(dbpath, timeout=2)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA journal_mode=DELETE;
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS dirs(path TEXT PRIMARY KEY, offset INTEGER, stamp INTEGER, done INTEGER);
    CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, seen INTEGER, bytes INTEGER,
      ident TEXT, sha TEXT, hash_state TEXT, error TEXT);
    CREATE INDEX IF NOT EXISTS size_idx ON files(bytes);
    CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, path TEXT, sha TEXT,
      object_id TEXT, action TEXT, reason TEXT, evidence_ref TEXT, recorded_at TEXT);
    ''')
    return db, root, state


def meta(db, name, default=None):
    row = db.execute('SELECT value FROM meta WHERE k=?', (name,)).fetchone()
    return json.loads(row[0]) if row else default


def setmeta(db, name, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (name, json.dumps(value)))


def scan(root, state, *, entries=20000, seconds=45, hash_bytes=512 * 1024**2,
         refresh=False, hash_all=False):
    if entries < 1 or seconds <= 0 or hash_bytes < 0:
        raise ValueError('positive_scan_budgets_required')
    db, root, state = connect(root, state)
    lock = state / 'scan.lock'
    with lock.open('x', encoding='utf-8') as f:
        f.write(str(os.getpid()))
    start = time.monotonic()
    try:
        generation = meta(db, 'generation', 0)
        if not generation or refresh:
            generation += 1
            db.execute('DELETE FROM dirs')
            db.execute('INSERT INTO dirs VALUES (?,?,?,0)', (str(root), 0, root.stat().st_mtime_ns))
            setmeta(db, 'generation', generation)
            setmeta(db, 'metadata_complete', False)
            setmeta(db, 'started_at', now())
            setmeta(db, 'errors', [])
            setmeta(db, 'exclusions', [])
            db.commit()
        processed = 0
        errors = meta(db, 'errors', [])
        exclusions = meta(db, 'exclusions', [])
        while processed < entries and time.monotonic() - start < seconds:
            row = db.execute('SELECT * FROM dirs WHERE done=0 ORDER BY path LIMIT 1').fetchone()
            if not row:
                break
            try:
                directory = checked(row['path'])
                stamp = directory.stat().st_mtime_ns
            except (OSError, ValueError) as exc:
                if len(errors) < 100:
                    errors.append({'path': row['path'], 'reason': str(exc)})
                db.execute('UPDATE dirs SET done=1 WHERE path=?', (row['path'],))
                db.commit()
                continue
            offset = row['offset'] if stamp == row['stamp'] else 0
            count, exhausted = 0, True
            try:
                with os.scandir(directory) as listing:
                    for count, item in enumerate(listing, 1):
                        if count <= offset:
                            if time.monotonic() - start >= seconds:
                                exhausted = False
                                count = offset
                                break
                            continue
                        if processed >= entries or time.monotonic() - start >= seconds:
                            exhausted = False
                            count -= 1
                            break
                        processed += 1
                        try:
                            # Windows DirEntry.stat can report zero file IDs/link counts.
                            # Use os.stat for persistent identities and cache invalidation.
                            s = os.stat(item.path, follow_symlinks=False)
                            if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & UNSAFE:
                                if len(exclusions) < 100:
                                    exclusions.append({'path': item.path, 'reason': 'link_or_offline'})
                                continue
                            if item.name in EXCLUDED and stat.S_ISDIR(s.st_mode):
                                if len(exclusions) < 100:
                                    exclusions.append({'path': item.path, 'reason': 'protected_application_or_git_directory'})
                                continue
                            if stat.S_ISDIR(s.st_mode):
                                db.execute('INSERT OR IGNORE INTO dirs VALUES (?,?,?,0)',
                                           (item.path, 0, s.st_mtime_ns))
                            elif stat.S_ISREG(s.st_mode):
                                ident = json.dumps(dict(volume=s.st_dev, file_id=s.st_ino, bytes=s.st_size,
                                                        mtime_ns=s.st_mtime_ns, links=s.st_nlink), sort_keys=True)
                                old = db.execute('SELECT ident,sha,hash_state FROM files WHERE path=?', (item.path,)).fetchone()
                                unchanged = old and old['ident'] == ident
                                digest = old['sha'] if unchanged else None
                                status = old['hash_state'] if unchanged else 'pending'
                                db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,NULL)',
                                           (item.path, generation, s.st_size, ident, digest, status))
                        except (OSError, ValueError) as exc:
                            if len(errors) < 100:
                                errors.append({'path': item.path, 'reason': str(exc)})
            except OSError as exc:
                if len(errors) < 100:
                    errors.append({'path': str(directory), 'reason': str(exc)})
                exhausted = True
            db.execute('UPDATE dirs SET offset=?,stamp=?,done=? WHERE path=?',
                       (count, stamp, int(exhausted), str(directory)))
            setmeta(db, 'errors', errors)
            setmeta(db, 'exclusions', exclusions)
            db.commit()
        pending_dirs = db.execute('SELECT COUNT(*) FROM dirs WHERE done=0').fetchone()[0]
        setmeta(db, 'metadata_complete', pending_dirs == 0)
        setmeta(db, 'observed_at', now())
        db.commit()
        consumed, hashed = 0, 0
        # File hashes are always complete SHA256, never a prefix or chunk digest.
        # A single file can exceed the soft per-run byte budget; its result is saved.
        where = '' if hash_all else ' AND bytes IN (SELECT bytes FROM files WHERE seen=? GROUP BY bytes HAVING COUNT(*)>1)'
        params = (generation,) if hash_all else (generation, generation)
        query = 'SELECT * FROM files WHERE seen=? AND sha IS NULL' + where + ' ORDER BY bytes,path'
        rows = db.execute(query, params)
        for row in rows:
            if hash_bytes == 0 or consumed >= hash_bytes or time.monotonic() - start >= seconds:
                break
            try:
                if identity(row['path']) != json.loads(row['ident']):
                    raise ValueError('changed_since_metadata_pass_refresh_required')
                digest = sha(row['path'])
                if identity(row['path']) != json.loads(row['ident']):
                    raise ValueError('changed_during_hashing')
                db.execute('UPDATE files SET sha=?,hash_state=?,error=NULL WHERE path=?',
                           (digest, 'full_sha256', row['path']))
                consumed += row['bytes']; hashed += 1
            except (OSError, ValueError) as exc:
                db.execute('UPDATE files SET hash_state=?,error=? WHERE path=?',
                           ('requires_refresh', str(exc), row['path']))
            db.commit()
        count = db.execute('SELECT COUNT(*) FROM files WHERE seen=?', (generation,)).fetchone()[0]
        remaining = db.execute('SELECT COUNT(*) FROM files WHERE seen=? AND sha IS NULL', (generation,)).fetchone()[0]
        report = {'schema_version': 2, 'root': str(root), 'generation': generation,
                  'metadata_complete': pending_dirs == 0, 'pending_directories': pending_dirs,
                  'files_observed_this_pass': count, 'entries_processed_this_run': processed,
                  'hashed_this_run': hashed, 'hash_bytes_this_run': consumed,
                  'files_without_full_hash': remaining, 'errors': errors, 'exclusions': exclusions,
                  'observed_at': meta(db, 'observed_at'), 'elapsed_seconds': round(time.monotonic() - start, 3),
                  'coverage': 'complete_metadata_for_registered_scope' if not pending_dirs and not errors else 'partial',
                  'limitations': ['Not an atomic filesystem snapshot.', 'Cached hashes are performance hints, never execution authorization.',
                                  'Hash/time budgets are soft per file; metadata resume re-enumerates the directory prefix.',
                                  'Refresh is required to discover changes after a completed pass.', 'Default hashing selects equal-sized candidates; use hash-all for other files.']}
        write_json(state / 'latest-scan.json', report)
        return report
    finally:
        db.close()
        checked(lock, file=True).unlink()


def duplicates(root, state, output):
    db, root, state = connect(root, state)
    try:
        generation = meta(db, 'generation', 0)
        groups = []
        for row in db.execute('SELECT sha,bytes,COUNT(*) AS count FROM files WHERE seen=? AND sha IS NOT NULL GROUP BY sha,bytes HAVING COUNT(*)>1', (generation,)):
            files = [dict(r) for r in db.execute('SELECT path,ident FROM files WHERE seen=? AND sha=? ORDER BY path', (generation, row['sha']))]
            groups.append({'sha256': row['sha'], 'bytes_each': row['bytes'], 'count': row['count'],
                           'files': [{'path': r['path'], 'identity': json.loads(r['ident'])} for r in files],
                           'distinct_physical_files': len({(json.loads(r['ident'])['volume'], json.loads(r['ident'])['file_id']) for r in files}),
                           'recommendation': 'candidate_only_verify_references_and_keepers', 'deletion_authorized': False})
        result = {'groups': groups, 'source_metadata_complete': meta(db, 'metadata_complete', False),
                  'source_scan_errors': meta(db, 'errors', []),
                  'source_scan_exclusions': meta(db, 'exclusions', []),
                  'files_without_full_hash': db.execute('SELECT COUNT(*) FROM files WHERE seen=? AND sha IS NULL', (generation,)).fetchone()[0],
                  'generation': generation, 'grants_execution': False}
        write_json(output, result, new=True)
        return result
    finally:
        db.close()


def remember(root, state, path, action, reason, evidence_ref):
    if action not in {'keep', 'accept', 'defer', 'retire'} or not reason or not evidence_ref:
        raise ValueError('explicit_action_reason_and_real_evidence_reference_required')
    db, root, state = connect(root, state)
    try:
        file = snapshot(path)
        if not within(path, root):
            raise ValueError('decision_outside_root')
        obj = json.dumps([file['identity']['volume'], file['identity']['file_id']])
        db.execute('INSERT INTO decisions(path,sha,object_id,action,reason,evidence_ref,recorded_at) VALUES (?,?,?,?,?,?,?)',
                   (file['path'], file['sha256'], obj, action, reason, evidence_ref, now()))
        db.commit()
        return {'status': 'retention_record_saved', 'action': action, 'grants_execution': False}
    finally:
        db.close()


def decisions(root, state, output):
    db, root, state = connect(root, state)
    try:
        result = []
        for row in db.execute('SELECT * FROM decisions ORDER BY id'):
            value = dict(row)
            try:
                value['current_content_matches'] = sha(row['path']) == row['sha']
            except (OSError, ValueError):
                value['current_content_matches'] = False
            value['grants_execution'] = False
            result.append(value)
        write_json(output, {'decisions': result, 'grants_execution': False}, new=True)
        return {'records': len(result), 'grants_execution': False}
    finally:
        db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['scan', 'duplicates', 'remember', 'decisions'])
    p.add_argument('--root', required=True); p.add_argument('--state', required=True)
    p.add_argument('--entries', type=int, default=20000); p.add_argument('--seconds', type=float, default=45)
    p.add_argument('--hash-mib', type=int, default=512); p.add_argument('--refresh', action='store_true')
    p.add_argument('--hash-all', action='store_true'); p.add_argument('--output')
    p.add_argument('--file'); p.add_argument('--action'); p.add_argument('--reason'); p.add_argument('--evidence-ref')
    args = p.parse_args()
    if args.command == 'scan':
        result = scan(args.root, args.state, entries=args.entries, seconds=args.seconds,
                      hash_bytes=args.hash_mib * 1024**2, refresh=args.refresh, hash_all=args.hash_all)
    elif args.command == 'duplicates':
        result = duplicates(args.root, args.state, args.output)
        result = {'groups': len(result['groups']), 'grants_execution': False}
    elif args.command == 'remember':
        result = remember(args.root, args.state, args.file, args.action, args.reason, args.evidence_ref)
    else:
        result = decisions(args.root, args.state, args.output)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
