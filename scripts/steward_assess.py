"""Read-only, per-file duplicate decisions; a recommendation is never permission."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import zipfile
from steward_common import checked, identity, key, now, read_json, snapshot, within, write_json
from steward_policy import APPLICATION_PARTS, protection_reason

TEXT_SUFFIXES = {'.md', '.txt', '.json', '.jsonl', '.html', '.htm', '.xml', '.csv',
                 '.py', '.ps1', '.sh', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx',
                 '.yaml', '.yml', '.toml', '.ini', '.cfg', '.tex', '.css', '.url'}

def archive_copy(path, member, candidate):
    """Verify one unique member through EOF/CRC, without extracting the archive."""
    archive = snapshot(path)
    if protection_reason(path):
        raise ValueError('application_cache_is_not_a_durable_archive')
    name = PurePosixPath(member)
    if not member or name.is_absolute() or '..' in name.parts or '\\' in member:
        raise ValueError('unsafe_archive_member')
    h = hashlib.sha256()
    with zipfile.ZipFile(archive['path']) as z:
        members = [i for i in z.infolist() if i.filename == member]
        if len(members) != 1 or members[0].is_dir():
            raise ValueError('archive_member_missing_or_ambiguous')
        if members[0].file_size != candidate['identity']['bytes']:
            raise ValueError('archive_member_size_differs')
        with z.open(members[0]) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(block)
    if snapshot(path) != archive:
        raise ValueError('archive_changed_during_member_check')
    if h.hexdigest() != candidate['sha256']:
        raise ValueError('archive_member_content_differs')
    return {'archive': archive, 'member': member, 'member_sha256': h.hexdigest(),
            'crc_and_full_content_verified': True}

def find_references(path, roots, excluded, *, max_bytes, max_file_bytes):
    needles = {str(path).casefold(), path.as_posix().casefold(),
               str(path).replace('\\', '\\\\').casefold(), path.name.casefold()}
    found, errors, skipped = [], [], []
    read_bytes = 0
    for root in roots:
        def walk_error(exc):
            errors.append(str(exc))
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            dirs[:] = [n for n in dirs if n.casefold() not in APPLICATION_PARTS | {'node_modules', '.venv', '__pycache__'}]
            safe_dirs = []
            for name in dirs:
                try:
                    checked(Path(directory) / name)
                    safe_dirs.append(name)
                except (OSError, ValueError) as exc:
                    errors.append(str(exc))
            dirs[:] = safe_dirs
            for name in files:
                p = Path(directory) / name
                if p.suffix.casefold() not in TEXT_SUFFIXES or key(p) in excluded:
                    continue
                try:
                    checked(p, file=True)
                    before = identity(p)
                    if before['bytes'] > max_file_bytes:
                        skipped.append(str(p))
                        continue
                    if read_bytes + before['bytes'] > max_bytes:
                        return {'matches': found, 'complete': False, 'errors': errors,
                                'skipped_large_files': skipped, 'reason': 'reference_read_budget'}
                    raw = p.read_bytes()
                    read_bytes += len(raw)
                    if identity(p) != before:
                        raise ValueError('reference_file_changed: ' + str(p))
                    text = raw.decode('utf-8-sig').casefold()
                    if any(needle in text for needle in needles):
                        found.append(str(p))
                except (OSError, ValueError, UnicodeError) as exc:
                    errors.append(str(exc))
    return {'matches': found, 'complete': bool(roots) and not errors and not skipped,
            'errors': errors, 'skipped_large_files': skipped, 'bytes_read': read_bytes,
            'scope': 'UTF-8 text in declared roots, excluding application/dependency directories; dynamic and binary references require host review.'}

def assess(spec, *, max_bytes=32 * 1024**2, max_file_bytes=4 * 1024**2):
    if max_bytes < 1 or max_file_bytes < 1:
        raise ValueError('positive_reference_budgets_required')
    root = checked(spec['root'])
    if not root.is_dir():
        raise ValueError('source_directory_required')
    protected_roots = [checked(p, missing=True) for p in spec.get('protected_roots', [])]
    reference_roots = [checked(p) for p in spec.get('reference_roots', [str(root)])]
    if any(not p.is_dir() for p in reference_roots):
        raise ValueError('reference_directory_required')
    paths, objects = set(), set()
    for item in spec['items']:
        path = Path(item['path'])
        normalized = key(path)
        if normalized in paths:
            raise ValueError('duplicate_candidate_paths')
        paths.add(normalized)
        observed = None
        if within(path, root):
            try:
                ident = identity(path)
                observed = (ident['volume'], ident['file_id'])
            except (OSError, ValueError):
                pass  # Missing/unsafe paths receive their own needs_review row.
        if observed is not None:
            if observed in objects:
                raise ValueError('duplicate_candidate_file_objects')
            objects.add(observed)
    results = []
    for item in spec['items']:
        path = Path(item['path'])
        row = {'path': str(path), 'decision': 'needs_review', 'reasons': [], 'grants_execution': False}
        results.append(row)
        try:
            if not within(path, root):
                raise ValueError('candidate_outside_cleanup_root')
            path = checked(path, file=True)
            reason = protection_reason(path, protected_roots)
            if reason or item.get('role') in {'original', 'current_delivery', 'project_dependency', 'unique_history'}:
                row.update(decision='keep', reasons=[reason or 'needed_content_or_role'])
                continue
            candidate = snapshot(path)
            row['candidate_snapshot'] = candidate
            if candidate['identity']['links'] != 1:
                row.update(decision='keep', reasons=['hardlinked_file_requires_separate_review'])
                continue
            durable = []
            for value in item.get('keepers', []):
                keeper = snapshot(value)
                if key(value) == key(path) or (keeper['identity']['volume'], keeper['identity']['file_id']) == (candidate['identity']['volume'], candidate['identity']['file_id']):
                    raise ValueError('keeper_is_the_same_file_object')
                if keeper['sha256'] == candidate['sha256'] and not protection_reason(value):
                    durable.append(keeper)
            archived = archive_copy(item['archive']['path'], item['archive']['member'], candidate) if item.get('archive') else None
            row['verified_durable_keepers'] = durable
            row['verified_archive_copy'] = archived
            if not durable and not archived:
                row['reasons'].append('no_verified_durable_complete_copy')
                continue
            if item.get('role') != 'redundant_copy':
                row['reasons'].append('host_must_confirm_file_is_a_redundant_copy')
                continue
            excluded = {key(path), *(key(k['path']) for k in durable)}
            references = find_references(path, reference_roots, excluded,
                                         max_bytes=max_bytes, max_file_bytes=max_file_bytes)
            row['references'] = references
            if references['matches']:
                row.update(decision='keep', reasons=['path_or_filename_referenced_requires_review'])
            elif not references['complete']:
                row['reasons'].append('reference_coverage_incomplete')
            else:
                row.update(decision='propose_cleanup', reasons=['verified_complete_copy_and_no_declared_text_reference'])
            if snapshot(path) != candidate or any(snapshot(k['path']) != k for k in durable):
                raise ValueError('candidate_or_keeper_changed_during_assessment')
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as exc:
            row.update(decision='needs_review', reasons=[str(exc)])
    # A and B cannot both be disposable on the assumption that the other stays.
    proposed = {key(row['path']) for row in results if row['decision'] == 'propose_cleanup'}
    protected = {}
    for row in results:
        if row['decision'] != 'propose_cleanup':
            continue
        safe_keepers = [k for k in row['verified_durable_keepers'] if key(k['path']) not in proposed]
        archive = row.get('verified_archive_copy')
        safe_archive = archive and key(archive['archive']['path']) not in proposed
        if not safe_keepers and not safe_archive:
            row.update(decision='needs_review', reasons=['keeper_is_also_a_cleanup_candidate'])
            continue
        row['required_protected_files'] = safe_keepers + ([archive['archive']] if safe_archive else [])
        for keeper in row['required_protected_files']:
            protected[key(keeper['path'])] = keeper
    return {'schema_version': 1, 'generated_at': now(), 'root': str(root), 'items': results,
            'required_protected_files': list(protected.values()),
            'counts': dict(Counter(r['decision'] for r in results)), 'grants_execution': False,
            'note': 'Host confirms actual roles, binary/dynamic dependencies and live user authorization before making an exact execution plan. Cache copies alone are insufficient.'}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--reference-mib', type=int, default=32)
    parser.add_argument('--max-reference-file-mib', type=int, default=4)
    args = parser.parse_args()
    result = assess(read_json(args.review), max_bytes=args.reference_mib * 1024**2,
                    max_file_bytes=args.max_reference_file_mib * 1024**2)
    output = checked(args.output, missing=True)
    if within(output, result['root']):
        raise ValueError('assessment_output_must_be_outside_source')
    write_json(output, result, new=True)
    print(json.dumps({'counts': result['counts'], 'grants_execution': False}, ensure_ascii=False))

if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'refused', 'reason': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
