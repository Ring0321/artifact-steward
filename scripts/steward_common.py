"""Shared cooperative filesystem checks; no OS sandbox."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import re
import stat
from urllib.parse import unquote
import uuid

UNSAFE = 0x400 | 0x1000 | 0x40000 | 0x400000


def now():
    return datetime.now(timezone.utc).isoformat()


def absolute(value):
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts:
        raise ValueError('absolute_nontraversing_path_required')
    return Path(os.path.abspath(p))


def key(value):
    return os.path.normcase(str(absolute(value)))


def within(value, root):
    try:
        return os.path.commonpath([key(value), key(root)]) == key(root)
    except ValueError:
        return False


def checked(value, *, file=False, missing=False):
    p = absolute(value)
    for part in [*reversed(p.parents), p]:
        if not os.path.lexists(part):
            if missing:
                continue
            raise FileNotFoundError(str(part))
        s = os.lstat(part)
        if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & UNSAFE:
            raise ValueError('linked_or_offline_path: ' + str(part))
        if part != p and not stat.S_ISDIR(s.st_mode):
            raise ValueError('non_directory_ancestor')
    if file and not stat.S_ISREG(os.lstat(p).st_mode):
        raise ValueError('regular_file_required')
    return p


def identity(value):
    s = os.stat(checked(value, file=True), follow_symlinks=False)
    return dict(volume=s.st_dev, file_id=s.st_ino, bytes=s.st_size,
                mtime_ns=s.st_mtime_ns, links=s.st_nlink)


def sha(value):
    p = checked(value, file=True)
    before = identity(p)
    h = hashlib.sha256()
    with p.open('rb') as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    if identity(p) != before:
        raise ValueError('file_changed_while_hashing')
    return h.hexdigest()


def snapshot(value):
    p = checked(value, file=True)
    ident = identity(p)
    digest = sha(p)
    if identity(p) != ident:
        raise ValueError('file_changed_while_snapshotting')
    return {'path': str(p), 'identity': ident, 'sha256': digest}


def verify(item, root=None):
    p = checked(item['path'], file=True)
    if root is not None and not within(p, root):
        raise ValueError('outside_registered_root')
    if identity(p) != item['identity']:
        raise ValueError('identity_or_metadata_changed')
    if sha(p) != item['sha256']:
        raise ValueError('content_changed')
    if identity(p) != item['identity']:
        raise ValueError('changed_during_verification')
    return p


def ensure_dir(value):
    p = checked(value, missing=True)
    p.mkdir(parents=True, exist_ok=True)
    checked(p)
    if not p.is_dir():
        raise ValueError('directory_required')
    return p


def write_json(value, payload, *, new=False):
    p = checked(value, missing=True)
    ensure_dir(p.parent)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    if new:
        with p.open('xb') as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        return
    if p.exists() and identity(p)['links'] != 1:
        raise ValueError('linked_state_file')
    tmp = p.with_name('.' + p.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('xb') as f:
        f.write(encoded)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def read_json(value, limit=16 * 1024**2):
    p = checked(value, file=True)
    with p.open('rb') as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('json_read_budget')
    return json.loads(raw)


def append_event(value, event):
    p = checked(value, missing=True)
    ensure_dir(p.parent)
    if p.exists() and identity(p)['links'] != 1:
        raise ValueError('linked_journal')
    with p.open('a', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps({'at': now(), **event}, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def extract_paths(text, cwd):
    found = []
    for match in re.finditer(r':codex-file-citation\{([^}]*)\}', text):
        p = re.search(r'path="([^"]+)"', match.group(1))
        purpose = re.search(r'purpose="([^"]+)"', match.group(1))
        if p:
            found.append((p.group(1), purpose.group(1) if purpose else 'unknown'))
    found.extend((m.group(1).strip('<>'), 'link') for m in re.finditer(r'\[[^\]]*\]\((<[^>]+>|[^\r\n)]+)\)', text))
    found.extend((m.group(1), 'mention') for m in re.finditer(r'([A-Za-z]:[\\/][^\r\n"<>|*?]*?\.(?:docx|pptx|xlsx|pdf|zip|md|txt|png|jpg|csv))(?=$|[\s`）)\],，。;；"\'<>#])', text, re.I))
    result, seen = [], set()
    for raw, purpose in found:
        value = re.sub(r'(\.[A-Za-z]{2,5}):\d+$', r'\1', unquote(raw).strip())
        if re.match(r'^[a-z]+://', value, re.I) or value.startswith('#'):
            continue
        p = Path(value)
        if not p.is_absolute():
            if not value.startswith(('outputs/', 'outputs\\', 'work/', 'work\\', 'inputs/', 'inputs\\')):
                continue
            p = Path(cwd) / p
        if '..' in p.parts:
            continue
        item = (str(p.absolute()), purpose)
        if item not in seen:
            seen.add(item); result.append(item)
    return result


def normalize_family(stem):
    stem = re.sub(r'_(?:交付候选|历史版)_20\d{6}$', '', stem)
    stem = re.sub(r'(?i)(?:[_ -]v\d+(?:\.\d+)*|[_ -]20\d{6})', '', stem)
    stem = re.sub(r'[_ -](?:[^_ -]{0,18}(?:版|稿)|final|draft)$', '', stem, flags=re.I)
    return stem.strip('_ -')
