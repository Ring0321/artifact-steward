"""Bounded read-only DOCX/PPTX/XLSX/PDF inspection; never render or resave."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from steward_common import checked, identity, write_json

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
P = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class Package:
    def __init__(self, path, budget):
        self.z = zipfile.ZipFile(path)
        if len(self.z.infolist()) > 100000:
            self.z.close()
            raise ValueError('too_many_zip_members')
        names = [i.filename for i in self.z.infolist()]
        if len(names) != len(set(names)):
            self.z.close()
            raise ValueError('duplicate_zip_member_names')
        self.names = set(names)
        self.remaining = budget
        self.omissions = []

    def read(self, name):
        info = self.z.getinfo(name)
        if info.file_size > self.remaining or info.file_size > 32 * 1024**2:
            raise ValueError('package_content_budget')
        self.remaining -= info.file_size
        with self.z.open(info) as stream:
            data = stream.read(info.file_size + 1)
        if len(data) != info.file_size:
            raise ValueError('zip_member_size_mismatch')
        return data

    def xml(self, name):
        return ET.fromstring(self.read(name))

    def relationships(self, base):
        relpath = posixpath.join(posixpath.dirname(base), '_rels', posixpath.basename(base) + '.rels')
        if relpath not in self.names:
            return {}
        result = {}
        for el in self.xml(relpath):
            if el.get('TargetMode') == 'External':
                continue
            raw = el.get('Target', '')
            target = posixpath.normpath(raw.lstrip('/') if raw.startswith('/') else
                                       posixpath.join(posixpath.dirname(base), raw))
            if target.startswith('../') or '\\' in target:
                raise ValueError('invalid_package_relationship')
            result[el.get('Id')] = target
        return result

    def media(self, prefix):
        result = {}
        for name in sorted(n for n in self.names if n.startswith(prefix) and not n.endswith('/')):
            try:
                result[name] = hashlib.sha256(self.read(name)).hexdigest()
            except ValueError as exc:
                self.omissions.append(name + ': ' + str(exc))
        return result


def inspect_ooxml(path, budget):
    package = Package(path, budget)
    result = {'format': path.suffix.lower()[1:], 'checks': [], 'omissions': package.omissions,
              'layout_verified': False, 'coverage': 'complete_for_declared_checks'}
    try:
        if path.suffix.lower() == '.docx':
            root = package.xml('word/document.xml')
            paragraphs = [''.join(p.itertext()) for p in root.iter(W + 'p')]
            text = '\n'.join(''.join(t.text or '' for t in p.iter(W + 't')) for p in root.iter(W + 'p'))
            result.update(main_text=text, main_text_sha256=text_hash(text),
                          paragraphs=len(paragraphs), tables=len(list(root.iter(W + 'tbl'))),
                          insertions=len(list(root.iter(W + 'ins'))), deletions=len(list(root.iter(W + 'del'))),
                          moved_from=len(list(root.iter(W + 'moveFrom'))),
                          moved_to=len(list(root.iter(W + 'moveTo'))))
            supplement = {}
            for name in sorted(package.names):
                if name.startswith(('word/header', 'word/footer', 'word/comments', 'word/footnotes', 'word/endnotes')) and name.endswith('.xml'):
                    el = package.xml(name)
                    supplement[name] = ''.join(t.text or '' for t in el.iter(W + 't'))
            result['supplemental_text'] = supplement
            result['media_sha256'] = package.media('word/media/')
            result['checks'] = ['main_document_text', 'tables', 'tracked_change_counts', 'comments_headers_notes', 'media_bytes']
            result['text_scope'] = 'XML text; revisions are counted, not accepted/rejected; no page/layout equivalence'
        elif path.suffix.lower() == '.pptx':
            root = package.xml('ppt/presentation.xml')
            rels = package.relationships('ppt/presentation.xml')
            ids = root.findall('./' + P + 'sldIdLst/' + P + 'sldId')
            slides = []
            for index, sid in enumerate(ids, 1):
                target = rels.get(sid.get(R + 'id'))
                if not target or target not in package.names:
                    slides.append({'number': index, 'missing': True, 'target': target})
                    package.omissions.append('missing_slide_relationship:' + str(index))
                    continue
                slide = package.xml(target)
                text = '\n'.join(t.text or '' for t in slide.iter(A + 't'))
                notes = []
                for part in package.relationships(target).values():
                    if part.startswith('ppt/notesSlides/') and part.endswith('.xml'):
                        notes.append('\n'.join(t.text or '' for t in package.xml(part).iter(A + 't')))
                slides.append({'number': index, 'target': target, 'text': text, 'notes': notes,
                               'hidden': slide.get('show') == '0'})
            result.update(actual_slide_count=len(ids), slides=slides,
                          speaker_notes=[s.get('notes', []) for s in slides],
                          slide_order=[s.get('target') for s in slides],
                          hidden_slide_numbers=[s['number'] for s in slides if s.get('hidden')],
                          main_text='\n'.join(s.get('text', '') for s in slides),
                          media_sha256=package.media('ppt/media/'))
            result['main_text_sha256'] = text_hash(result['main_text'])
            result['checks'] = ['presentation_slide_order', 'slide_count', 'slide_text', 'speaker_notes', 'media_bytes']
        elif path.suffix.lower() == '.xlsx':
            root = package.xml('xl/workbook.xml')
            rels = package.relationships('xl/workbook.xml')
            shared = []
            if 'xl/sharedStrings.xml' in package.names:
                shared = [''.join(t.text or '' for t in el.iter(S + 't'))
                          for el in package.xml('xl/sharedStrings.xml').iter(S + 'si')]
            sheets = []
            for sheet in root.findall('./' + S + 'sheets/' + S + 'sheet'):
                target = rels.get(sheet.get(R + 'id'))
                if not target or target not in package.names:
                    package.omissions.append('missing_worksheet:' + sheet.get('name', ''))
                    continue
                cells = []
                for c in package.xml(target).iter(S + 'c'):
                    v = c.find(S + 'v'); f = c.find(S + 'f')
                    value = v.text if v is not None else ''.join(t.text or '' for t in c.iter(S + 't'))
                    if c.get('t') == 's' and value is not None:
                        value = shared[int(value)]
                    cells.append({'ref': c.get('r'), 'value': value, 'formula': f.text if f is not None else None})
                sheets.append({'name': sheet.get('name'), 'state': sheet.get('state', 'visible'), 'cells': cells})
            result.update(sheets=sheets, sheet_count=len(sheets),
                          main_text=json.dumps(sheets, ensure_ascii=False, sort_keys=True))
            result['main_text_sha256'] = text_hash(result['main_text'])
            result['checks'] = ['sheet_order', 'stored_cell_values', 'formula_text']; result['formulas_recalculated'] = False
        if result['omissions']:
            result['coverage'] = 'partial'
    finally:
        package.z.close()
    return result


def inspect_pdf(path, max_pages):
    if importlib.util.find_spec('pymupdf'):
        import pymupdf
        with pymupdf.open(path) as document:
            if document.needs_pass:
                return {'format': 'pdf', 'coverage': 'unavailable_encrypted', 'checks': []}
            count = document.page_count
            texts = [document[i].get_text() for i in range(min(count, max_pages))]
        backend = 'pymupdf'
    elif importlib.util.find_spec('pypdf'):
        from pypdf import PdfReader
        document = PdfReader(str(path))
        if document.is_encrypted:
            return {'format': 'pdf', 'coverage': 'unavailable_encrypted', 'checks': []}
        count = len(document.pages)
        texts = [(document.pages[i].extract_text() or '') for i in range(min(count, max_pages))]
        backend = 'pypdf'
    else:
        return {'format': 'pdf', 'coverage': 'dependency_missing', 'checks': [],
                'omissions': ['PDF inspection needs optional pymupdf or pypdf; no automatic install']}
    text = '\n'.join(texts)
    return {'format': 'pdf', 'backend': backend, 'actual_page_count': count,
            'pages_text_checked': len(texts), 'main_text': text, 'main_text_sha256': text_hash(text),
            'checks': ['actual_page_count', 'extractable_text'], 'layout_verified': False,
            'ocr_performed': False, 'possible_scanned_pages': sum(not t.strip() for t in texts),
            'coverage': 'complete_for_declared_checks' if len(texts) == count else 'partial_page_budget'}


def inspect_file(value, *, budget=64 * 1024**2, max_pages=500):
    path = checked(value, file=True)
    before = identity(path)
    try:
        if path.suffix.lower() in {'.docx', '.pptx', '.xlsx'}:
            result = inspect_ooxml(path, budget)
        elif path.suffix.lower() == '.pdf':
            result = inspect_pdf(path, max_pages)
        elif path.suffix.lower() == '.zip':
            package = Package(path, budget)
            try:
                names = sorted(package.names)
                manifests = []
                for name in names:
                    if posixpath.basename(name).lower() in {'package.json', 'version.json', 'manifest.json'}:
                        try:
                            value = json.loads(package.read(name).decode('utf-8-sig'))
                            if isinstance(value, dict) and isinstance(value.get('version'), (str, int, float)):
                                manifests.append({'member': name, 'name': value.get('name'), 'version': value['version']})
                        except (ValueError, UnicodeError) as exc:
                            package.omissions.append(name + ': ' + str(exc))
                result = {'format': 'zip', 'member_count': len(names), 'members': names[:1000],
                          'member_names_complete': len(names) <= 1000, 'manifest_versions': manifests,
                          'omissions': package.omissions, 'checks': ['member_directory', 'named_json_manifests'],
                          'all_members_content_verified': False,
                          'coverage': 'partial' if package.omissions else 'complete_for_declared_checks'}
            finally:
                package.z.close()
        elif path.suffix.lower() in {'.txt', '.md', '.csv', '.json', '.yaml', '.yml', '.py'}:
            with path.open('rb') as f:
                raw = f.read(min(budget, 2 * 1024**2) + 1)
            if len(raw) > min(budget, 2 * 1024**2):
                result = {'format': 'text', 'coverage': 'text_budget_exceeded', 'checks': []}
            else:
                text = raw.decode('utf-8-sig')
                result = {'format': 'text', 'coverage': 'complete_for_declared_checks',
                          'main_text': text, 'main_text_sha256': text_hash(text), 'checks': ['utf8_text']}
                if path.name.lower() in {'package.json', 'version.json', 'manifest.json'}:
                    try:
                        manifest = json.loads(text)
                        if isinstance(manifest, dict):
                            result['manifest_version'] = manifest.get('version')
                    except ValueError:
                        pass
        else:
            result = {'format': path.suffix.lower(), 'coverage': 'unsupported', 'checks': []}
    except (OSError, ValueError, KeyError, IndexError, zipfile.BadZipFile, ET.ParseError, UnicodeError, RuntimeError) as exc:
        result = {'format': path.suffix.lower(), 'coverage': 'partial_or_unavailable', 'checks': [], 'reason': str(exc)}
    if identity(path) != before:
        result = {'coverage': 'changed_during_inspection', 'checks': []}
    result['path'] = str(path)
    result['file_identity'] = before
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--file', required=True); p.add_argument('--output', required=True)
    p.add_argument('--content-mib', type=int, default=64); p.add_argument('--max-pages', type=int, default=500)
    args = p.parse_args()
    if args.content_mib < 1 or args.max_pages < 1:
        p.error('positive budgets required')
    out = Path(args.output).absolute()
    if out == Path(args.file).absolute():
        p.error('output must differ from input')
    result = inspect_file(args.file, budget=args.content_mib * 1024**2, max_pages=args.max_pages)
    write_json(out, result, new=True)
    print(json.dumps({k: result[k] for k in ['coverage', 'checks', 'actual_slide_count', 'actual_page_count'] if k in result}, ensure_ascii=False))
