"""Version evidence and configurable classification proposals, never auto-approve."""
from __future__ import annotations
import argparse
from collections import defaultdict
import fnmatch
import html
import json
import os
from pathlib import Path
import re
from inspect_documents import inspect_file
from steward_common import checked, ensure_dir, extract_paths, identity, key, normalize_family, now, read_json, sha, within, write_json
from steward_index import connect, meta
from execute_confirmed_file_batch import build_plan
from steward_policy import protection_reason

DEFAULT_RULES = [
    {'extensions': ['.docx', '.pdf'], 'category': '文档'},
    {'extensions': ['.pptx'], 'category': '演示'},
    {'extensions': ['.xlsx', '.csv'], 'category': '数据'},
    {'extensions': ['.png', '.jpg', '.jpeg', '.mp4', '.wav'], 'category': '媒体'},
    {'extensions': ['.zip', '.7z'], 'category': '归档包'},
]
PROTECTED_EXT = {'.py', '.ps1', '.js', '.ts', '.tsx', '.cjs', '.yaml', '.yml', '.db', '.sqlite', '.sqlite3'}


def local_records(db):
    result = {}
    for row in db.execute('SELECT * FROM decisions ORDER BY id'):
        k = key(row['path'])
        prior = result.get(k, {})
        value = dict(row)
        # Retention preferences do not erase a prior acceptance of the same bytes.
        for field in ['accepted_sha', 'acceptance_evidence_ref', 'accepted_at']:
            if field in prior:
                value[field] = prior[field]
        if row['action'] == 'accept':
            value.update(accepted_sha=row['sha'], acceptance_evidence_ref=row['evidence_ref'],
                         accepted_at=row['recorded_at'])
        result[k] = value
    return result


def classify(root, state, config, output):
    db, root, state = connect(root, state)
    try:
        records = local_records(db)
        rows, targets = [], set()
        for file in db.execute('SELECT * FROM files WHERE seen=? ORDER BY path', (meta(db, 'generation', 0),)):
            path = Path(file['path'])
            relative = path.relative_to(root).as_posix()
            record = records.get(key(path))
            protected = (protection_reason(path, config.get('protected_roots', [])) or path.suffix.lower() in PROTECTED_EXT or
                         (record and record['action'] in {'keep', 'accept', 'defer'}))
            rules = config.get('classification_rules', DEFAULT_RULES)
            matching = next((r for r in rules if
                (not r.get('prefix') or relative.startswith(r['prefix'].rstrip('/') + '/')) and
                (not r.get('glob') or fnmatch.fnmatch(relative, r['glob'])) and
                (not r.get('extensions') or path.suffix.lower() in r['extensions'])), None)
            category = matching.get('category') if matching else None
            row = {'path': str(path), 'category': category or '待核对', 'protected': bool(protected),
                   'action': 'keep' if protected or not category else 'propose_move',
                   'retention_record': record, 'deletion_authorized': False}
            if category and not protected:
                # A category can be a relative hierarchy; never an escape or drive.
                cat = Path(category)
                if cat.is_absolute() or '..' in cat.parts or ':' in category or '\\' in category:
                    raise ValueError('unsafe_category')
                project = matching.get('project', '')
                if project and (Path(project).is_absolute() or '..' in Path(project).parts or ':' in project or '\\' in project):
                    raise ValueError('unsafe_project_category')
                target = root / config.get('classification_root', '分类资料') / project / cat / path.name
                if not within(target, root):
                    raise ValueError('classification_destination_outside_root')
                row['destination'] = str(target)
                row['collision'] = key(target) in targets or (target.exists() and key(target) != key(path))
                targets.add(key(target))
                if key(target) == key(path):
                    row['action'] = 'keep'
                if row['collision']:
                    row['action'] = 'collision_requires_review'
                row['reference_review_required'] = True
            rows.append(row)
        answer = {'schema_version': 2, 'root': str(root), 'metadata_complete': meta(db, 'metadata_complete', False),
                  'rows': rows, 'grants_execution': False,
                  'note': 'Proposals only. Existing links, source roles and dependencies require host review before a move plan.'}
        write_json(output, answer, new=True)
        return answer
    finally:
        db.close()


def evidence_map(evidence, root):
    deliveries, requirements, refs = defaultdict(list), [], set()
    for task in evidence.get('threads', []):
        thread = task['thread']
        for position, message in enumerate(task['messages']):
            ref = str(thread['id']) + '/' + str(message['turn_id']) + '/' + str(message['item_id'])
            refs.add(ref)
            if message['kind'] == 'user_message':
                requirements.append({'reference': ref, 'text': message.get('text', ''), 'task_id': thread['id']})
            if message['kind'] == 'assistant_final':
                for path, purpose in extract_paths(message.get('text', ''), thread['cwd']):
                    if within(path, root) and purpose != 'source':
                        deliveries[key(path)].append({'reference': ref, 'task_id': thread['id'],
                                                       'sequence': position, 'at': message.get('at')})
    return deliveries, requirements, refs


def versions(root, state, evidence, config, output, max_files=100):
    db, root, state = connect(root, state)
    try:
        files = {key(r['path']): dict(r) for r in db.execute('SELECT * FROM files WHERE seen=?', (meta(db, 'generation', 0),))}
        records = local_records(db)
        delivered, requirements, refs = evidence_map(evidence, root)
        groups = config.get('version_groups', [])
        if not groups:
            inferred = defaultdict(list)
            for row in files.values():
                path = Path(row['path'])
                if path.suffix.lower() not in {'.docx', '.pptx', '.pdf', '.xlsx', '.zip', '.md'}:
                    continue
                family = normalize_family(path.stem)
                # Inferred groups are scoped to one directory and purpose is unknown.
                inferred[(str(path.parent), family)].append(str(path))
            for (parent, family), paths in inferred.items():
                if len(paths) > 1 or any(key(p) in delivered for p in paths):
                    groups.append({'name': family, 'purpose': '待核实用途', 'paths': paths,
                                   'lineage': 'inferred_name_and_directory_only'})
            missing = [p for p in delivered if p not in files and not Path(p).exists()]
            if missing:
                groups.append({'name': '交付路径缺失', 'purpose': '待定位改名或移动', 'paths': missing,
                               'lineage': 'missing_delivery_reference'})
        result_groups, count, omitted = [], 0, []
        for group in groups:
            members, group_omitted = [], []
            for value in group['paths']:
                path = Path(value)
                if not within(path, root):
                    raise ValueError('version_member_outside_root')
                if count >= max_files:
                    omitted.append(str(path)); group_omitted.append(str(path)); continue
                count += 1
                item = {'path': str(path), 'deliveries': delivered.get(key(path), []),
                        'requirement_match': 'unknown', 'user_accepted': False, 'deletion_authorized': False}
                try:
                    before = identity(path)
                    digest = sha(path)
                    inspection = inspect_file(path)
                    if identity(path) != before or inspection.get('file_identity') != before:
                        raise ValueError('file_changed_during_version_review')
                    item.update(sha256=digest, inspection=inspection, exists=True)
                    record = records.get(key(path))
                    if record:
                        item['retention_record'] = record
                        item['user_accepted'] = record.get('accepted_sha') == item['sha256']
                    match = group.get('requirement_checks', {}).get(str(path)) or group.get('requirement_checks', {}).get(path.as_posix())
                    if match:
                        evidence_refs = match.get('evidence_refs', [])
                        if match.get('status') not in {'met', 'unmet', 'unknown'}:
                            raise ValueError('invalid_requirement_check')
                        if match['status'] != 'unknown' and (not evidence_refs or not set(evidence_refs) <= refs):
                            raise ValueError('requirement_check_needs_real_message_references')
                        item['requirement_match'] = match['status']
                        item['requirement_evidence'] = evidence_refs
                        item['requirement_check_method'] = match.get('method', 'host_review_of_content_against_supplied_messages')
                    pages = item['inspection'].get('actual_slide_count', item['inspection'].get('actual_page_count'))
                    claimed = re.search(r'(\d+)\s*页', path.stem)
                    if pages is not None and claimed and int(claimed.group(1)) != pages:
                        item['page_count_warning'] = {'filename_claim': int(claimed.group(1)), 'actual': pages}
                except (OSError, ValueError) as exc:
                    item.update(exists=path.exists(), error=str(exc))
                members.append(item)
            met = [i for i in members if i['requirement_match'] == 'met' and i.get('exists') and not i.get('error')]
            candidate = met[0] if len(met) == 1 else None
            basis = 'content_checked_against_last_requirements' if candidate else 'no_unique_requirements_match'
            if not candidate and not met:
                possible = [(d, item) for item in members for d in item['deliveries']]
                tasks = {d['task_id'] for d, item in possible}
                if len(tasks) == 1 and possible:
                    last = max(d['sequence'] for d, item in possible)
                    latest = {item['path']: item for d, item in possible if d['sequence'] == last}
                    if len(latest) == 1:
                        proposed = next(iter(latest.values()))
                        if proposed.get('exists') and not proposed.get('error') and proposed['requirement_match'] != 'unmet':
                            candidate, basis = proposed, 'latest_delivery_candidate_requires_intent_review'
                        else:
                            basis = 'latest_delivery_missing_or_fails_last_requirements'
            if group_omitted:
                candidate, basis = None, 'incomplete_group_check_budget'
            differences = []
            if candidate:
                for old in members:
                    if old is candidate or not old.get('sha256'):
                        continue
                    a, b = old.get('inspection', {}), candidate.get('inspection', {})
                    same_text = a.get('main_text_sha256') is not None and a.get('main_text_sha256') == b.get('main_text_sha256')
                    relation = ('identical_file_bytes' if old['sha256'] == candidate['sha256'] else
                                'same_extracted_text_different_file' if same_text else 'different_or_uncompared_content')
                    changed = [field for field in ['actual_slide_count', 'actual_page_count', 'media_sha256',
                               'speaker_notes', 'slide_order', 'hidden_slide_numbers',
                               'supplemental_text', 'insertions', 'deletions', 'moved_from', 'moved_to', 'sheets', 'manifest_versions', 'manifest_version']
                               if a.get(field) != b.get(field)]
                    differences.append({'old': old['path'], 'candidate': candidate['path'], 'relation': relation,
                                        'changed_checks': changed, 'deletion_authorized': False})
            result_groups.append({'name': group['name'], 'purpose': group.get('purpose', '待确认'),
                'lineage': group.get('lineage', 'explicit_host_group'), 'members': members,
                'candidate': candidate['path'] if candidate else None, 'basis': basis,
                'omitted_files': group_omitted,
                'user_accepted': bool(candidate and candidate['user_accepted']), 'differences': differences})
        answer = {'schema_version': 2, 'generated_at': now(), 'groups': result_groups,
                  'chat_coverage': [{'task': t['thread']['id'], **t['coverage']} for t in evidence.get('threads', [])],
                  'last_user_requirements': requirements, 'omitted_files': omitted,
                  'metadata_complete': meta(db, 'metadata_complete', False), 'grants_execution': False,
                  'limitations': ['Requirements matching is a host evidence annotation, not automatic semantic proof.',
                                 'Extracted text/media/page checks do not prove layout or all dependencies.',
                                 'Inferred filename groups are clues; separate purposes require explicit groups.']}
        write_json(output, answer, new=True)
        return answer
    finally:
        db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['versions', 'classify', 'move-plan'])
    p.add_argument('--root', required=True); p.add_argument('--state', required=True)
    p.add_argument('--output', required=True); p.add_argument('--evidence'); p.add_argument('--config')
    p.add_argument('--max-files', type=int, default=100); p.add_argument('--reviewed-moves')
    args = p.parse_args()
    config = read_json(args.config) if args.config else {}
    if args.command == 'classify':
        answer = classify(args.root, args.state, config, args.output)
        result = {'proposals': len(answer['rows']), 'grants_execution': False}
    elif args.command == 'versions':
        evidence = read_json(args.evidence) if args.evidence else {'threads': []}
        answer = versions(args.root, args.state, evidence, config, args.output, args.max_files)
        result = {'groups': len(answer['groups']), 'omitted_files': len(answer['omitted_files']), 'grants_execution': False}
    else:
        # Only host-reviewed explicit moves enter a plan, never every proposal automatically.
        moves = read_json(args.reviewed_moves)
        db, root, state = connect(args.root, args.state)
        try:
            protected = [r['path'] for r in local_records(db).values() if r['action'] in {'keep', 'accept', 'defer'}]
        finally:
            db.close()
        answer = build_plan(args.root, moves, protected, operation='move')
        write_json(args.output, answer, new=True)
        result = {'status': 'plan_only', 'sha256': sha(args.output), 'items': len(answer['items']), 'grants_execution': False}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
