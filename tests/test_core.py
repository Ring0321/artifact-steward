"""Behavior checks use only synthetic files in an isolated temporary directory."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZIP_DEFLATED

BASE = Path(__file__).resolve().parent.parent
SCRIPTS = BASE / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import execute_confirmed_file_batch as actions
import inspect_documents as docs
import steward_index as index
import steward_review as review
import steward_assess as assessment
from steward_common import sha, snapshot, write_json, read_json

import atexit
import shutil
import uuid
FIXTURE_BASE = BASE / '.test-artifacts'
FIXTURE = FIXTURE_BASE / uuid.uuid4().hex
FIXTURE.mkdir(parents=True)

def cleanup_fixtures():
    resolved = FIXTURE.resolve()
    if not resolved.is_relative_to(FIXTURE_BASE.resolve()) or resolved == FIXTURE_BASE.resolve():
        raise RuntimeError('test cleanup escaped fixture directory')
    shutil.rmtree(resolved)

atexit.register(cleanup_fixtures)



class Tests(unittest.TestCase):
    def setUp(self):
        self.home = FIXTURE / self._testMethodName
        self.root = self.home / 'inputs'
        self.root.mkdir(parents=True)
        self.state = self.home / 'state'
        self.a = self.root / 'a.txt'; self.a.write_text('alpha', encoding='utf-8')
        self.b = self.root / 'b.txt'; self.b.write_text('bravo', encoding='utf-8')

    def plan(self, operation='quarantine'):
        return actions.build_plan(self.root, [{'path': str(self.a)}], [self.b], operation=operation,
                                  recovery_root=self.home / 'recovery')

    def scan_all(self):
        for n in range(30):
            result = index.scan(self.root, self.state, entries=2, seconds=10, hash_all=True)
            if result['metadata_complete'] and result['files_without_full_hash'] == 0:
                return result
        self.fail('scan did not complete')

    def test_protected_overlap(self):
        plan = self.plan()
        plan['protected_files'].append(snapshot(self.a))
        with self.assertRaisesRegex(ValueError, 'overlaps_protected'):
            actions.execute(plan, [], 'test-only', self.home / 'result.json', mode='apply')
        self.assertEqual(self.a.read_text(), 'alpha')

    def test_optimized_python_cannot_skip_plan_digest(self):
        p = self.home / 'plan.json'; write_json(p, self.plan(), new=True)
        run = subprocess.run([sys.executable, '-B', '-O', str(SCRIPTS / 'execute_confirmed_file_batch.py'),
            '--mode', 'apply', '--plan', str(p), '--expected-plan-sha', '0' * 64,
            '--approval-ref', 'test-only', '--result', str(self.home / 'result.json')], capture_output=True)
        self.assertEqual(run.returncode, 2)
        self.assertTrue(self.a.exists())
        self.assertFalse((self.home / 'result.json').exists())

    def test_default_preview(self):
        result = actions.execute(self.plan(), [], '', self.home / 'preview.json')
        self.assertEqual(result['status'], 'preview_only')
        self.assertTrue(self.a.exists())
        self.assertFalse((self.home / 'recovery').exists())

    def test_quarantine_and_restore(self):
        before = sha(self.a); keeper = sha(self.b)
        out = self.home / 'result.json'
        result = actions.execute(self.plan(), [], 'synthetic-fixture-test', out, mode='apply')
        self.assertEqual(result['status'], 'complete')
        self.assertFalse(self.a.exists()); self.assertEqual(sha(self.b), keeper)
        restored = actions.restore(out, sha(out), 'synthetic-restore-test', self.home / 'restore.json')
        self.assertEqual(restored['status'], 'complete'); self.assertEqual(sha(self.a), before)

    def test_restore_does_not_overwrite_new_content(self):
        out = self.home / 'result.json'
        result = actions.execute(self.plan(), [], 'synthetic-test', out, mode='apply')
        self.a.write_text('new-user-content')
        restored = actions.restore(out, sha(out), 'synthetic-test', self.home / 'restore.json')
        self.assertEqual(restored['status'], 'partial')
        self.assertEqual(self.a.read_text(), 'new-user-content')
        self.assertTrue(Path(result['items'][0]['destination_snapshot']['path']).exists())

    def test_changed_target_refused(self):
        plan = self.plan(); self.a.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            actions.execute(plan, [], 'synthetic-test', self.home / 'result.json', mode='apply')
        self.assertEqual(self.a.read_text(), 'changed')

    def test_changed_keeper_refused(self):
        plan = self.plan(); self.b.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'protected_files_changed'):
            actions.execute(plan, [], 'synthetic-test', self.home / 'result.json', mode='apply')
        self.assertTrue(self.a.exists())

    def test_duplicate_ids_refused(self):
        plan = self.plan(); plan['items'].append(dict(plan['items'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            actions.validate(plan)

    def test_only_exact_fixture_deleted(self):
        plan = self.plan('permanent_delete')
        result = actions.execute(plan, [], 'synthetic-fixture-test', self.home / 'delete.json', mode='apply')
        self.assertEqual(result['deleted_count'], 1)
        self.assertFalse(self.a.exists()); self.assertEqual(self.b.read_text(), 'bravo')

    def test_move_and_undo(self):
        destination = self.root / '分类' / 'a.txt'
        plan = actions.build_plan(self.root, [{'path': str(self.a), 'destination': str(destination)}], [self.b], operation='move')
        out = self.home / 'move.json'
        result = actions.execute(plan, [], 'synthetic-test', out, mode='apply')
        self.assertEqual(result['moved_count'], 1); self.assertTrue(destination.exists())
        actions.restore(out, sha(out), 'synthetic-test', self.home / 'undo.json')
        self.assertEqual(self.a.read_text(), 'alpha')

    def test_existing_destination_refused(self):
        with self.assertRaisesRegex(ValueError, 'destination_collision'):
            actions.build_plan(self.root, [{'path': str(self.a), 'destination': str(self.b)}], [], operation='move')

    def test_approved_subsets_can_continue_with_same_plan(self):
        plan = actions.build_plan(self.root, [{'id': 'A', 'path': str(self.a)}, {'id': 'B', 'path': str(self.b)}], [],
                                  operation='quarantine', recovery_root=self.home / 'recovery')
        first = actions.execute(plan, ['A'], 'synthetic-approved-batch', self.home / 'chunk1.json', mode='apply')
        second = actions.execute(plan, ['B'], 'synthetic-approved-batch', self.home / 'chunk2.json', mode='apply')
        self.assertEqual(first['moved_count'], 1); self.assertEqual(second['moved_count'], 1)
        self.assertEqual(second['unselected_already_absent'], [str(self.a)])

    def test_already_absent_is_not_counted_as_deleted(self):
        plan = self.plan('permanent_delete'); self.a.unlink()
        result = actions.execute(plan, [], 'synthetic-approved-batch', self.home / 'missing.json', mode='apply')
        self.assertEqual(result['deleted_count'], 0)
        self.assertEqual(result['logical_removed_bytes'], 0)
        self.assertEqual(result['items'][0]['status'], 'already_absent_not_counted')

    def test_resume_and_global_duplicate_groups(self):
        (self.root / 'nested').mkdir()
        (self.root / 'nested' / 'copy.txt').write_bytes(self.a.read_bytes())
        result = self.scan_all()
        self.assertEqual(result['files_observed_this_pass'], 3)
        groups = index.duplicates(self.root, self.state, self.home / 'duplicates.json')
        self.assertEqual(len(groups['groups']), 1)
        self.assertEqual(groups['groups'][0]['count'], 2)

    def test_refresh_invalidates_changed_hash_and_discovers_new_file(self):
        self.scan_all()
        self.a.write_text('changed-after-scan')
        (self.root / 'new.txt').write_text('new')
        result = index.scan(self.root, self.state, entries=100, seconds=10, refresh=True, hash_all=True)
        self.assertEqual(result['files_observed_this_pass'], 3)
        db, _, _ = index.connect(self.root, self.state)
        try:
            value = db.execute('SELECT sha FROM files WHERE path=?', (str(self.a),)).fetchone()[0]
            self.assertEqual(value, sha(self.a))
        finally:
            db.close()

    def test_large_file_over_64mib_fully_hashed(self):
        large_root = self.home / 'large-input'; large_root.mkdir()
        path = large_root / 'large.bin'
        with path.open('wb') as f:
            f.truncate(65 * 1024**2 + 17)
        result = index.scan(large_root, self.home / 'large-state', hash_bytes=1, seconds=20, hash_all=True)
        self.assertEqual(result['hashed_this_run'], 1)
        self.assertEqual(result['hash_bytes_this_run'], 65 * 1024**2 + 17)
        db, _, _ = index.connect(large_root, self.home / 'large-state')
        try:
            value = db.execute('SELECT sha FROM files').fetchone()[0]
            self.assertEqual(value, sha(path))
        finally:
            db.close()
        # Exact generated file only; no recursive cleanup or user file removal.
        self.assertTrue(path.resolve().is_relative_to(FIXTURE.resolve()))
        path.unlink()

    def test_retention_survives_reopen_and_is_not_execution_permission(self):
        self.scan_all()
        index.remember(self.root, self.state, self.a, 'keep', 'synthetic reason', 'synthetic/user-message')
        result = review.classify(self.root, self.state, {'classification_rules': [{'extensions': ['.txt'], 'category': '材料'}]}, self.home / 'classify.json')
        row = next(r for r in result['rows'] if r['path'] == str(self.a))
        self.assertTrue(row['protected']); self.assertEqual(row['action'], 'keep')
        index.decisions(self.root, self.state, self.home / 'decisions.json')
        self.assertFalse(read_json(self.home / 'decisions.json')['grants_execution'])

    def test_classification_collision_reported(self):
        sub = self.root / 'nested'; sub.mkdir()
        (sub / 'a.txt').write_text('different content')
        self.scan_all()
        answer = review.classify(self.root, self.state, {'classification_rules': [{'extensions': ['.txt'], 'category': '材料'}]}, self.home / 'classify.json')
        self.assertTrue(any(r.get('collision') for r in answer['rows']))
        self.assertTrue(self.a.exists())

    def test_docx_checks_comments_revisions_and_media(self):
        path = self.root / 'sample.docx'
        with ZipFile(path, 'w', ZIP_DEFLATED) as z:
            z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Text</w:t></w:r><w:ins><w:r><w:t>new</w:t></w:r></w:ins><w:del><w:r><w:delText>old</w:delText></w:r></w:del></w:p></w:body></w:document>')
            z.writestr('word/comments.xml', '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment><w:p><w:r><w:t>Keep this evidence</w:t></w:r></w:p></w:comment></w:comments>')
            z.writestr('word/media/image1.png', b'SYNTHETIC-MEDIA-BYTES')
        before = sha(path); result = docs.inspect_file(path)
        self.assertEqual(result['insertions'], 1); self.assertEqual(result['deletions'], 1)
        self.assertIn('word/comments.xml', result['supplemental_text'])
        self.assertEqual(len(result['media_sha256']), 1); self.assertEqual(sha(path), before)

    def test_ppt_counts_only_referenced_slides(self):
        path = self.root / '报告_3页.pptx'
        p = 'http://schemas.openxmlformats.org/presentationml/2006/main'
        r = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        with ZipFile(path, 'w', ZIP_DEFLATED) as z:
            z.writestr('ppt/presentation.xml', f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst><p:sldId id="256" r:id="s1"/><p:sldId id="257" r:id="s2"/></p:sldIdLst></p:presentation>')
            z.writestr('ppt/_rels/presentation.xml.rels', '<Relationships><Relationship Id="s1" Target="slides/slide1.xml"/><Relationship Id="s2" Target="slides/slide2.xml"/></Relationships>')
            z.writestr('ppt/slides/slide1.xml', f'<p:sld xmlns:p="{p}"/>')
            z.writestr('ppt/slides/slide2.xml', f'<p:sld xmlns:p="{p}"/>')
            z.writestr('ppt/slides/slide99.xml', '<orphan/>')
        before = sha(path); result = docs.inspect_file(path)
        self.assertEqual(result['actual_slide_count'], 2)
        self.assertEqual(sha(path), before)

    def test_pdf_actual_pages_and_no_ocr_claim(self):
        path = self.root / 'sample.pdf'
        objects = ['<< /Type /Catalog /Pages 2 0 R >>',
                   '<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 >>']
        objects.extend(['<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << >> >>'] * 3)
        raw, offsets = b'%PDF-1.4\n', [0]
        for number, body in enumerate(objects, 1):
            offsets.append(len(raw))
            raw += f'{number} 0 obj\n{body}\nendobj\n'.encode('ascii')
        xref = len(raw)
        raw += b'xref\n0 6\n0000000000 65535 f \n'
        raw += ''.join(f'{offset:010d} 00000 n \n' for offset in offsets[1:]).encode('ascii')
        raw += f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode('ascii')
        path.write_bytes(raw)
        before = sha(path); result = docs.inspect_file(path)
        if result['coverage'] == 'dependency_missing':
            self.assertFalse(result['checks'])
            self.assertEqual(sha(path), before)
            return
        self.assertEqual(result['actual_page_count'], 3)
        self.assertFalse(result['ocr_performed']); self.assertEqual(sha(path), before)

    def test_xlsx_reads_formulas_without_recalculation_or_resave(self):
        path = self.root / 'sample.xlsx'
        s = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        r = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        with ZipFile(path, 'w', ZIP_DEFLATED) as z:
            z.writestr('xl/workbook.xml', f'<workbook xmlns="{s}" xmlns:r="{r}"><sheets><sheet name="Sheet1" sheetId="1" r:id="s1"/></sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="s1" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="{s}"><sheetData><row r="1"><c r="A1"><v>2</v></c></row><row r="2"><c r="A2"><f>A1*3</f><v>6</v></c></row></sheetData></worksheet>')
        before = sha(path); result = docs.inspect_file(path)
        cells = result['sheets'][0]['cells']
        self.assertEqual(next(c['formula'] for c in cells if c['ref'] == 'A2'), 'A1*3')
        self.assertFalse(result['formulas_recalculated']); self.assertEqual(sha(path), before)

    def test_later_user_reverts_to_older_file(self):
        old = self.root / '说明_v1.md'; old.write_text('Original small revision')
        new = self.root / '说明_v2.md'; new.write_text('Later broad rewrite')
        self.scan_all()
        def m(kind, i, text):
            return {'kind': kind, 'turn_id': str(i), 'item_id': str(i), 'at': 1000 + i, 'text': text}
        evidence = {'threads': [{'thread': {'id': 'synthetic', 'title': 'SYNTHETIC', 'cwd': str(self.root)},
            'coverage': {'hasMore': False, 'complete': True}, 'messages': [
            m('assistant_final', 1, '[old](<' + old.as_posix() + '>)'),
            m('assistant_final', 2, '[new](<' + new.as_posix() + '>)'),
            m('user_message', 3, 'Synthetic instruction: return to the original and only change the heading.')]}]}
        config = {'version_groups': [{'name': '说明', 'purpose': '原稿局部修订', 'paths': [str(old), str(new)],
                   'requirement_checks': {str(old): {'status': 'met', 'evidence_refs': ['synthetic/3/3']},
                                          str(new): {'status': 'unmet', 'evidence_refs': ['synthetic/3/3']}}}]}
        result = review.versions(self.root, self.state, evidence, config, self.home / 'versions.json')
        self.assertEqual(result['groups'][0]['candidate'], str(old))
        self.assertFalse(result['groups'][0]['user_accepted'])

    def test_no_chat_does_not_invent_final_version(self):
        (self.root / '说明_v1.md').write_text('First')
        (self.root / '说明_v2.md').write_text('Second')
        self.scan_all()
        result = review.versions(self.root, self.state, {'threads': []}, {}, self.home / 'versions.json')
        self.assertTrue(result['groups'])
        self.assertTrue(all(g['candidate'] is None for g in result['groups']))

    def test_zip_release_manifest_is_not_source_version(self):
        path = self.root / 'release_0.100.14.zip'
        with ZipFile(path, 'w', ZIP_DEFLATED) as z:
            z.writestr('release/package.json', json.dumps({'name': 'synthetic', 'version': '0.100.14'}))
        source = self.root / 'package.json'
        source.write_text(json.dumps({'name': 'synthetic', 'version': '0.100.17'}))
        release = docs.inspect_file(path); current = docs.inspect_file(source)
        self.assertEqual(release['manifest_versions'][0]['version'], '0.100.14')
        self.assertEqual(current['manifest_version'], '0.100.17')
        self.assertFalse(release['all_members_content_verified'])

    def test_changed_accepted_content_loses_acceptance_but_keeps_protection(self):
        self.scan_all()
        index.remember(self.root, self.state, self.a, 'accept', 'synthetic acceptance', 'synthetic/user')
        self.a.write_text('changed after acceptance')
        index.decisions(self.root, self.state, self.home / 'decisions.json')
        record = read_json(self.home / 'decisions.json')['decisions'][0]
        self.assertFalse(record['current_content_matches'])
        self.assertFalse(record['grants_execution'])
        result = review.classify(self.root, self.state, {}, self.home / 'classify.json')
        self.assertTrue(next(r for r in result['rows'] if r['path'] == str(self.a))['protected'])

    def test_missing_operation_never_defaults_to_permanent_delete(self):
        plan = self.plan()
        plan.pop('operation')
        with self.assertRaisesRegex(ValueError, 'unsupported_operation'):
            actions.execute(plan, [], 'synthetic-only', self.home / 'result.json', mode='apply')
        self.assertTrue(self.a.exists())

    def test_application_attachment_refused_even_in_imported_plan(self):
        path = self.root / 'xwechat_files' / 'attachment.txt'
        path.parent.mkdir()
        path.write_text('synthetic attachment')
        plan = self.plan('permanent_delete')
        plan['items'][0] = {**snapshot(path), 'id': 'synthetic-app'}
        with self.assertRaisesRegex(ValueError, 'protected_path'):
            actions.execute(plan, [], 'synthetic-only', self.home / 'result.json', mode='apply')
        self.assertTrue(path.exists())

    def test_application_file_is_protected_in_classification(self):
        path = self.root / 'WPSDrive' / 'reading.pdf'
        path.parent.mkdir()
        path.write_bytes(b'synthetic data')
        self.scan_all()
        report = review.classify(self.root, self.state, {}, self.home / 'classify.json')
        row = next(r for r in report['rows'] if r['path'] == str(path))
        self.assertEqual(row['action'], 'keep')
        self.assertTrue(row['protected'])

    def test_restore_parent_conflict_does_not_block_other_items(self):
        directory = self.root / 'old-parent'
        directory.mkdir()
        nested = directory / 'nested.txt'
        nested.write_text('nested synthetic content')
        plan = actions.build_plan(self.root, [{'path': str(self.a)}, {'path': str(nested)}],
                                  [self.b], recovery_root=self.home / 'recovery')
        out = self.home / 'applied.json'
        actions.execute(plan, [], 'synthetic-only', out, mode='apply')
        directory.rmdir()
        directory.write_text('new unrelated file at old directory path')
        report = actions.restore(out, sha(out), 'synthetic-only', self.home / 'restore.json')
        self.assertEqual(report['status'], 'partial')
        self.assertTrue(self.a.exists())
        self.assertEqual(report['items'][0]['status'], 'skipped_or_requires_reconciliation')
        self.assertEqual(report['items'][1]['status'], 'restored_verified')
        self.assertEqual(directory.read_text(), 'new unrelated file at old directory path')

    def test_save_between_hash_and_inspection_invalidates_acceptance(self):
        self.scan_all()
        index.remember(self.root, self.state, self.a, 'accept', 'synthetic acceptance', 'synthetic/user')
        def edit_then_inspect(path):
            path.write_text('new editor save after digest')
            return docs.inspect_file(path)
        config = {'version_groups': [{'name': 'synthetic', 'paths': [str(self.a)]}]}
        with patch.object(review, 'inspect_file', side_effect=edit_then_inspect):
            report = review.versions(self.root, self.state, {'threads': []}, config, self.home / 'versions.json')
        member = report['groups'][0]['members'][0]
        self.assertEqual(member['error'], 'file_changed_during_version_review')
        self.assertFalse(member['user_accepted'])
        self.assertNotIn('sha256', member)

    def assessment_spec(self):
        keeper = self.home / 'durable-copy.txt'
        keeper.write_bytes(self.a.read_bytes())
        return {'root': str(self.root), 'items': [{'path': str(self.a), 'role': 'redundant_copy', 'keepers': [str(keeper)]}]}

    def test_verified_duplicate_gets_a_reason_without_permission(self):
        report = assessment.assess(self.assessment_spec())
        self.assertEqual(report['items'][0]['decision'], 'propose_cleanup')
        self.assertFalse(report['grants_execution'])
        self.assertTrue(self.a.exists())

    def test_filename_mentioned_by_project_is_retained(self):
        spec = self.assessment_spec()
        (self.root / 'viewer.py').write_text('document = ' + repr(self.a.name))
        row = assessment.assess(spec)['items'][0]
        self.assertEqual(row['decision'], 'keep')
        self.assertTrue(row['references']['matches'])

    def test_application_cache_is_not_the_only_backup(self):
        cache = self.home / 'WPS Cloud Files'
        cache.mkdir()
        keeper = cache / 'same.txt'
        keeper.write_bytes(self.a.read_bytes())
        spec = {'root': str(self.root), 'items': [{'path': str(self.a), 'role': 'redundant_copy', 'keepers': [str(keeper)]}]}
        row = assessment.assess(spec)['items'][0]
        self.assertEqual(row['decision'], 'needs_review')
        self.assertIn('no_verified_durable_complete_copy', row['reasons'])

    def test_same_file_object_cannot_be_its_own_keeper(self):
        spec = {'root': str(self.root), 'items': [{'path': str(self.a), 'role': 'redundant_copy', 'keepers': [str(self.a)]}]}
        self.assertEqual(assessment.assess(spec)['items'][0]['decision'], 'needs_review')

    def test_unknown_role_is_not_automatically_disposable(self):
        spec = self.assessment_spec()
        spec['items'][0].pop('role')
        self.assertEqual(assessment.assess(spec)['items'][0]['decision'], 'needs_review')

    def test_current_delivery_is_retained_even_if_identical(self):
        spec = self.assessment_spec()
        spec['items'][0]['role'] = 'current_delivery'
        self.assertEqual(assessment.assess(spec)['items'][0]['decision'], 'keep')

    def test_incomplete_reference_scan_does_not_propose_cleanup(self):
        spec = self.assessment_spec()
        row = assessment.assess(spec, max_bytes=1)['items'][0]
        self.assertEqual(row['decision'], 'needs_review')
        self.assertIn('reference_coverage_incomplete', row['reasons'])

    def test_exact_archive_member_can_preserve_a_redundant_loose_file(self):
        archive = self.home / 'delivery.zip'
        with ZipFile(archive, 'w', ZIP_DEFLATED) as z:
            z.writestr('deliverable/a.txt', self.a.read_bytes())
        spec = {'root': str(self.root), 'items': [{'path': str(self.a), 'role': 'redundant_copy',
                'archive': {'path': str(archive), 'member': 'deliverable/a.txt'}}]}
        row = assessment.assess(spec)['items'][0]
        self.assertEqual(row['decision'], 'propose_cleanup')
        self.assertTrue(row['verified_archive_copy']['crc_and_full_content_verified'])

    def test_same_size_different_archive_content_is_rejected(self):
        archive = self.home / 'delivery.zip'
        with ZipFile(archive, 'w', ZIP_DEFLATED) as z:
            z.writestr('a.txt', b'delta')
        spec = {'root': str(self.root), 'items': [{'path': str(self.a), 'role': 'redundant_copy',
                'archive': {'path': str(archive), 'member': 'a.txt'}}]}
        row = assessment.assess(spec)['items'][0]
        self.assertEqual(row['decision'], 'needs_review')
        self.assertIn('archive_member_content_differs', row['reasons'])

    def test_reciprocal_keepers_cannot_propose_deleting_every_copy(self):
        self.b.write_bytes(self.a.read_bytes())
        spec = {'root': str(self.root), 'items': [
            {'path': str(self.a), 'role': 'redundant_copy', 'keepers': [str(self.b)]},
            {'path': str(self.b), 'role': 'redundant_copy', 'keepers': [str(self.a)]}]}
        rows = assessment.assess(spec)['items']
        self.assertTrue(all(r['decision'] == 'needs_review' for r in rows))
        self.assertTrue(all('keeper_is_also_a_cleanup_candidate' in r['reasons'] for r in rows))

    def test_joint_cleanup_preserves_an_external_canonical_copy(self):
        self.b.write_bytes(self.a.read_bytes())
        spec = self.assessment_spec()
        keeper = spec['items'][0]['keepers'][0]
        spec['items'].append({'path': str(self.b), 'role': 'redundant_copy', 'keepers': [keeper]})
        report = assessment.assess(spec)
        self.assertTrue(all(r['decision'] == 'propose_cleanup' for r in report['items']))
        self.assertEqual(len(report['required_protected_files']), 1)

    def test_conflicting_roles_for_one_path_refuse_the_batch(self):
        spec = self.assessment_spec()
        spec['items'].insert(0, {'path': str(self.a), 'role': 'original'})
        with self.assertRaisesRegex(ValueError, 'duplicate_candidate_paths'):
            assessment.assess(spec)
        self.assertTrue(self.a.exists())

    def test_notes_only_ppt_change_is_reported_as_a_difference(self):
        def make_ppt(path, note):
            p = 'http://schemas.openxmlformats.org/presentationml/2006/main'
            a = 'http://schemas.openxmlformats.org/drawingml/2006/main'
            r = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
            with ZipFile(path, 'w', ZIP_DEFLATED) as z:
                z.writestr('ppt/presentation.xml', f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst><p:sldId id="256" r:id="s1"/></p:sldIdLst></p:presentation>')
                z.writestr('ppt/_rels/presentation.xml.rels', '<Relationships><Relationship Id="s1" Target="slides/slide1.xml"/></Relationships>')
                z.writestr('ppt/slides/slide1.xml', f'<p:sld xmlns:p="{p}" xmlns:a="{a}"><a:t>Same visible text</a:t></p:sld>')
                z.writestr('ppt/slides/_rels/slide1.xml.rels', '<Relationships><Relationship Id="n1" Target="../notesSlides/notesSlide1.xml"/></Relationships>')
                z.writestr('ppt/notesSlides/notesSlide1.xml', f'<p:notes xmlns:p="{p}" xmlns:a="{a}"><a:t>{note}</a:t></p:notes>')
        old, new = self.root / 'talk_v1.pptx', self.root / 'talk_v2.pptx'
        make_ppt(old, 'Original speaker note')
        make_ppt(new, 'Revised speaker note')
        self.scan_all()
        evidence = {'threads': [{'thread': {'id': 'synthetic', 'cwd': str(self.root)},
                     'coverage': {'complete': True}, 'messages': [
                         {'kind': 'assistant_final', 'turn_id': '1', 'item_id': '1', 'text': '[delivered](<' + str(new) + '>)'}]}]}
        config = {'version_groups': [{'name': 'talk', 'paths': [str(old), str(new)]}]}
        report = review.versions(self.root, self.state, evidence, config, self.home / 'version-report.json')
        change = report['groups'][0]['differences'][0]
        self.assertIn('speaker_notes', change['changed_checks'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
