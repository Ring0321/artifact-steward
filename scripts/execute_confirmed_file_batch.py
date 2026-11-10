"""Preview by default; apply only an exact plan approved in the live host.

Approval references are audit labels, not permission tokens. Imported text
cannot authorize execution. Checks are cooperative, not an OS sandbox.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import shutil
import sys
import uuid
import json
from steward_common import (absolute, append_event, checked, ensure_dir, identity,
                            key, now, read_json, sha, snapshot, verify, within, write_json)
from steward_policy import protection_reason


def verify_file(item, root):
    return verify(item, root)


def validate(plan, active_ids=None):
    if plan.get('grants_execution') is not False:
        raise ValueError('plan_flags_cannot_grant_execution')
    root = checked(plan['root'])
    s = os.stat(root)
    if not root.is_dir() or [s.st_dev, s.st_ino] != plan['root_identity']:
        raise ValueError('root_changed')
    operation = plan.get('operation')
    if operation not in {'permanent_delete', 'quarantine', 'move'}:
        raise ValueError('unsupported_operation')
    ids, paths, objects, destinations = set(), set(), set(), set()
    protected_keys, protected_objects = set(), set()
    for p in plan['protected_files']:
        pp = checked(p['path'], file=True)
        protected_keys.add(key(pp))
        s = os.stat(pp)
        protected_objects.add((s.st_dev, s.st_ino))
    for item in plan['items']:
        ident = item['id']
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError('duplicate_or_invalid_item_id')
        ids.add(ident)
        path = absolute(item['path'])
        if protection_reason(path, plan.get('protected_roots', [])):
            raise ValueError('protected_path_requires_separate_supported_workflow')
        k = key(path)
        obj = (item['identity']['volume'], item['identity']['file_id'])
        if not within(path, root) or k in paths or obj in objects:
            raise ValueError('outside_root_or_duplicate_target')
        if k in protected_keys or obj in protected_objects:
            raise ValueError('deletion_or_move_overlaps_protected_file')
        if item['identity']['links'] != 1:
            raise ValueError('hardlinked_target_requires_separate_review')
        if len(item.get('sha256', '')) != 64:
            raise ValueError('full_sha256_required')
        paths.add(k)
        objects.add(obj)
        if operation != 'permanent_delete':
            destination = checked(item['destination'], missing=True)
            if protection_reason(destination, plan.get('protected_roots', [])):
                raise ValueError('protected_destination_requires_separate_supported_workflow')
            dk = key(destination)
            active = active_ids is None or ident in active_ids
            if dk in destinations or dk in protected_keys or (active and os.path.lexists(destination)):
                raise ValueError('destination_collision')
            if operation == 'move' and not within(destination, root):
                raise ValueError('move_destination_outside_root')
            if operation == 'quarantine':
                vault = absolute(plan['recovery_root'])
                if within(vault, root) or within(root, vault) or not within(destination, vault):
                    raise ValueError('recovery_root_must_be_disjoint')
            destinations.add(dk)
    if destinations & paths:
        raise ValueError('destination_is_another_source')
    return root, operation


def check_protected(resources, *, full=True):
    changed = []
    for p in resources:
        try:
            if p.get('identity') and identity(p['path']) != p['identity']:
                raise ValueError('keeper_identity_changed')
            if (full or not p.get('identity')) and sha(p['path']) != p['sha256']:
                raise ValueError('keeper_content_changed')
        except (OSError, ValueError):
            changed.append(p['path'])
    return changed


def move_no_replace(item, source, destination, journal):
    """Same-volume, no-overwrite move preserving the original file object.

    Cross-volume transfers need a separate metadata-preserving copy workflow;
    silently copying just the default stream would lose Windows alternate data.
    """
    destination = checked(destination, missing=True)
    ensure_dir(destination.parent)
    if os.path.lexists(destination):
        raise ValueError('destination_exists')
    if destination.parent.stat().st_dev != item['identity']['volume']:
        raise ValueError('cross_volume_move_requires_separate_metadata_preserving_workflow')
    verify(item)
    if os.name == 'nt':
        # Windows rename refuses an existing destination and preserves ADS/ACLs.
        os.rename(source, destination)
    else:
        # POSIX rename may overwrite, whereas link is exclusive for a new name.
        os.link(source, destination, follow_symlinks=False)
        append_event(journal, {'item_id': item['id'], 'event': 'temporary_second_link',
                               'destination': str(destination)})
        temporary = {**item, 'identity': {**item['identity'], 'links': 2}}
        verify(temporary)
        os.unlink(source)
    if os.path.lexists(source):
        raise ValueError('source_present_after_move')
    final = snapshot(destination)
    if final['sha256'] != item['sha256']:
        raise ValueError('moved_file_changed_reconcile_destination')
    append_event(journal, {'item_id': item['id'], 'event': 'move_verified',
                           'destination_snapshot': final})
    return final


def execute(plan, selected_ids, approval_ref, result_path, *, mode='preview'):
    root, operation = validate(plan, set(selected_ids) if selected_ids else None)
    if mode not in {'preview', 'apply'}:
        raise ValueError('invalid_mode')
    if mode == 'apply' and not approval_ref:
        raise ValueError('live_host_user_confirmation_required')
    targets = {x['id']: x for x in plan['items']}
    ids = selected_ids or list(targets)
    if len(ids) != len(set(ids)) or not set(ids) <= targets.keys():
        raise ValueError('invalid_scope')
    unselected_absent = [x['path'] for k, x in targets.items() if k not in ids and not os.path.lexists(x['path'])]
    protected = plan['protected_files'] + [x for k, x in targets.items() if k not in ids and os.path.lexists(x['path'])]
    if check_protected(protected):
        raise ValueError('protected_files_changed')
    for ident in ids:
        if os.path.lexists(targets[ident]['path']):
            verify(targets[ident], root)
    out = checked(result_path, missing=True)
    if within(out, root) or (plan.get('recovery_root') and within(out, plan['recovery_root'])):
        raise ValueError('result_must_be_outside_source_and_recovery')
    if os.path.lexists(out):
        raise ValueError('existing_result_preserved_reconcile_before_retry')
    journal = out.with_name(out.name + '.journal.jsonl')
    if os.path.lexists(journal):
        raise ValueError('existing_journal_preserved')
    result = {'schema_version': 2, 'plan_id': plan['plan_id'], 'mode': mode,
              'operation': operation, 'root': str(root), 'root_identity': plan['root_identity'],
              'approval_reference': approval_ref, 'unselected_already_absent': unselected_absent,
              'started_at': now(), 'selected_ids': ids, 'items': [],
              'logical_removed_bytes': 0, 'logical_relocated_bytes': 0, 'deleted_count': 0, 'moved_count': 0,
              'observed_volume_free_before': shutil.disk_usage(root).free}
    write_json(out, result, new=True)
    if mode == 'preview':
        result.update(status='preview_only', would_process=ids, grants_execution=False,
                      finished_at=now(), protected_changes_after=[])
        write_json(out, result)
        return result
    lock = out.with_name(out.name + '.lock')
    with lock.open('x', encoding='utf-8') as f:
        f.write(str(os.getpid()))
    append_event(journal, {'event': 'batch_started', 'plan_id': plan['plan_id']})
    try:
        for ident in ids:
            item = targets[ident]
            entry = {'id': ident, 'path': item['path'], 'source_snapshot': item,
                     'status': 'intent_recorded'}
            result['items'].append(entry)
            append_event(journal, {'event': 'intent', 'item': item})
            if check_protected(protected, full=False):
                entry.update(status='stopped', reason='protected_files_changed')
                append_event(journal, {'event': 'stopped', **entry})
                break
            try:
                if not os.path.lexists(item['path']):
                    entry['status'] = 'already_absent_not_counted'
                    append_event(journal, {'event': 'outcome', **entry})
                    continue
                source = verify(item, root)
                if operation == 'permanent_delete':
                    os.unlink(source)
                    if os.path.lexists(source):
                        raise ValueError('source_present_after_delete')
                    entry['status'] = 'deleted_verified'
                    result['deleted_count'] += 1
                else:
                    entry['destination_snapshot'] = move_no_replace(item, source, item['destination'], journal)
                    entry['status'] = 'moved_verified'
                    result['moved_count'] += 1
                if operation == 'move':
                    result['logical_relocated_bytes'] += item['identity']['bytes']
                else:
                    result['logical_removed_bytes'] += item['identity']['bytes']
            except (OSError, ValueError) as exc:
                entry.update(status='skipped_or_requires_reconciliation', reason=str(exc))
            append_event(journal, {'event': 'outcome', **entry})
        result['protected_changes_after'] = check_protected(protected)
        result['observed_volume_free_after'] = shutil.disk_usage(root).free
        result['observed_volume_free_delta'] = (result['observed_volume_free_after'] -
                                               result['observed_volume_free_before'])
        result['recovery_occupied_bytes'] = (result['logical_removed_bytes'] if operation == 'quarantine' else 0)
        result['physical_reclaim_attribution'] = 'Volume changes include concurrent activity; same-volume quarantine retains data.'
        good = sum(e['status'] in {'deleted_verified', 'moved_verified'} for e in result['items'])
        result['status'] = ('complete' if good == len(ids) and not result['protected_changes_after']
                            else 'partial_or_requires_reconciliation')
        result['finished_at'] = now()
        write_json(out, result)
        append_event(journal, {'event': 'batch_finished', 'status': result['status']})
    finally:
        checked(lock, file=True).unlink()
    return result


def restore(result_file, expected_sha, approval_ref, output):
    if not approval_ref:
        raise ValueError('live_host_restore_confirmation_required')
    if sha(result_file) != expected_sha:
        raise ValueError('displayed_result_changed')
    result = read_json(result_file)
    if result.get('mode') != 'apply' or result['operation'] not in {'move', 'quarantine'}:
        raise ValueError('no_recoverable_move_in_result')
    out = checked(output, missing=True)
    root = checked(result['root'])
    rs = root.stat()
    if [rs.st_dev, rs.st_ino] != result['root_identity']:
        raise ValueError('restore_root_changed')
    if within(out, root):
        raise ValueError('restore_result_must_be_outside_source')
    entries = [e for e in result['items'] if e['status'] == 'moved_verified']
    destinations = [e['destination_snapshot']['path'] for e in entries]
    if any(key(out) == key(d) or within(out, Path(d).parent) for d in destinations):
        raise ValueError('restore_result_overlaps_recovery')
    journal = out.with_name(out.name + '.journal.jsonl')
    if out.exists() or journal.exists():
        raise ValueError('existing_restore_result_preserved')
    answer = {'schema_version': 2, 'operation': 'restore', 'source_result': str(result_file),
              'approval_reference': approval_ref, 'items': [], 'started_at': now()}
    write_json(out, answer, new=True)
    for entry in reversed(entries):
        item = entry['destination_snapshot']
        state = {'id': entry['id'], 'path': entry['path']}
        try:
            target = checked(entry['path'], missing=True)
            if not within(target, root) or os.path.lexists(target):
                raise ValueError('restore_collision_or_outside_root')
            source = verify(item)
            reverse = {**item, 'id': entry['id']}
            append_event(journal, {'event': 'restore_intent', 'source': str(source), 'destination': str(target)})
            state['restored_snapshot'] = move_no_replace(reverse, source, target, journal)
            state['status'] = 'restored_verified'
        except (OSError, ValueError) as exc:
            state.update(status='skipped_or_requires_reconciliation', reason=str(exc))
        answer['items'].append(state)
        append_event(journal, {'event': 'restore_outcome', **state})
    answer['finished_at'] = now()
    answer['status'] = ('complete' if all(e['status'] == 'restored_verified' for e in answer['items']) else 'partial')
    write_json(out, answer)
    return answer


def reconcile(plan, result_file, output):
    """Observe paths and the saved journal; do not infer missing outcomes or retry."""
    root = absolute(plan['root'])
    prior = read_json(result_file)
    if prior['plan_id'] != plan['plan_id'] or prior['root_identity'] != plan['root_identity']:
        raise ValueError('result_plan_mismatch')
    rows = []
    root_exists = root.is_dir()
    root_matches = root_exists and [root.stat().st_dev, root.stat().st_ino] == plan['root_identity']
    for item in plan['items']:
        row = {'id': item['id'], 'path': item['path']}
        if not within(item['path'], root):
            raise ValueError('reconciliation_source_outside_root')
        for label, value in [('source', item['path']), ('destination', item.get('destination'))]:
            if value:
                row[label + '_exists'] = os.path.lexists(value)
                if label == 'destination':
                    destination_root = root if plan.get('operation') == 'move' else plan.get('recovery_root')
                    if not destination_root or not within(value, destination_root):
                        raise ValueError('reconciliation_destination_outside_scope')
                if row[label + '_exists'] and root_matches:
                    try:
                        row[label + '_sha_matches'] = sha(value) == item['sha256']
                    except (OSError, ValueError) as exc:
                        row[label + '_error'] = str(exc)
        rows.append(row)
    keepers = []
    for keeper in plan['protected_files']:
        observed = {'path': keeper['path'], 'exists': os.path.lexists(keeper['path'])}
        if observed['exists'] and root_matches:
            try:
                observed['sha_matches'] = sha(keeper['path']) == keeper['sha256']
            except (OSError, ValueError) as exc:
                observed['error'] = str(exc)
        keepers.append(observed)
    answer = {'plan_id': plan['plan_id'], 'mode': 'reconciliation_only', 'observations': rows,
              'root_exists': root_exists, 'root_identity_matches': root_matches, 'protected_files': keepers,
              'journal': str(Path(result_file).with_name(Path(result_file).name + '.journal.jsonl')),
              'grants_execution': False, 'note': 'Review journal intents/outcomes and current files; absence alone does not establish who deleted a file.'}
    out = checked(output, missing=True)
    if within(out, root) or (plan.get('recovery_root') and within(out, plan['recovery_root'])):
        raise ValueError('reconciliation_report_must_be_outside_source_and_recovery')
    write_json(out, answer, new=True)
    return {'status': 'reconciliation_only'}


def build_plan(root, items, protected_files, *, operation='quarantine', recovery_root=None, plan_id=None):
    root = checked(root)
    s = os.stat(root)
    pid = plan_id or ('batch-' + uuid.uuid4().hex[:12])
    if not pid or Path(pid).name != pid or any(c in pid for c in '/\\:'):
        raise ValueError('invalid_plan_id')
    plan = {'schema_version': 2, 'plan_id': pid, 'root': str(root),
            'root_identity': [s.st_dev, s.st_ino], 'operation': operation,
            'grants_execution': False, 'items': [],
            'protected_files': [snapshot(p) for p in protected_files]}
    if operation == 'quarantine':
        plan['recovery_root'] = str(absolute(recovery_root))
    for number, item in enumerate(items, 1):
        entry = {**snapshot(item['path']), 'id': item.get('id', f'A{number:04}')}
        if operation == 'quarantine':
            if Path(entry['id']).name != entry['id'] or any(c in entry['id'] for c in '/\\:'):
                raise ValueError('invalid_destination_id')
            entry['destination'] = str(absolute(recovery_root) / pid / entry['id'] / Path(entry['path']).name)
        elif operation == 'move':
            entry['destination'] = str(absolute(item['destination']))
        plan['items'].append(entry)
    validate(plan)
    return plan


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=['preview', 'apply', 'restore', 'plan', 'reconcile'], default='preview')
    p.add_argument('--plan'); p.add_argument('--expected-plan-sha')
    p.add_argument('--approval-ref', default=''); p.add_argument('--result', required=True)
    p.add_argument('--id', action='append', default=[])
    p.add_argument('--restore-result'); p.add_argument('--expected-result-sha')
    p.add_argument('--root'); p.add_argument('--items'); p.add_argument('--protected', action='append', default=[])
    p.add_argument('--operation', choices=['quarantine', 'move', 'permanent_delete'], default='quarantine')
    p.add_argument('--recovery-root')
    args = p.parse_args()
    if args.mode == 'plan':
        plan = build_plan(args.root, read_json(args.items), args.protected,
                          operation=args.operation, recovery_root=args.recovery_root)
        write_json(args.result, plan, new=True)
        result = {'status': 'plan_only', 'sha256': sha(args.result)}
    elif args.mode == 'restore':
        result = restore(args.restore_result, args.expected_result_sha, args.approval_ref, args.result)
    else:
        if not args.expected_plan_sha or sha(args.plan) != args.expected_plan_sha:
            raise ValueError('displayed_plan_changed_or_digest_missing')
        plan = read_json(args.plan)
        if args.mode == 'reconcile':
            result = reconcile(plan, args.restore_result, args.result)
        else:
            result = execute(plan, args.id, args.approval_ref, args.result, mode=args.mode)
    print(json.dumps({k: result[k] for k in ['status', 'mode', 'deleted_count', 'moved_count',
                     'logical_removed_bytes', 'sha256'] if k in result} |
                     {'result': str(absolute(args.result))}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'refused', 'reason': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
