"""Validate distributable content and documentation without reading user data."""
from pathlib import Path
import ast
import json
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
TOP = ('SKILL.md', 'README.md', 'LICENSE', 'VERSION', 'CHANGELOG.md', '.gitignore', '.gitattributes')
DIRECTORIES = ('scripts', 'references', 'assets', 'agents', 'tests', 'tools', '.github')
PATTERNS = {
    'credential': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{30,})\b'),
    'private_windows_home': re.compile(r'(?i)\b[A-Z]:[\\/]Users[\\/](?!Public(?:[\\/]|\b))[^\s<>"\n]+'),
    'private_posix_home': re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'private_key': re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
}

def source_files():
    files = [ROOT / name for name in TOP]
    for name in DIRECTORIES:
        directory = ROOT / name
        files.extend(p for p in directory.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    return sorted(files)

def check():
    errors = []
    files = source_files()
    for p in files:
        relative = p.relative_to(ROOT).as_posix()
        if p.is_symlink() or not p.is_file():
            errors.append(relative + ': missing file or symlink')
            continue
        if p.stat().st_size > 1024**2:
            errors.append(relative + ': unexpected large distribution file')
            continue
        if p.name not in TOP and p.suffix not in {'.py', '.md', '.json', '.yaml', '.yml'}:
            errors.append(relative + ': unexpected distribution file type')
            continue
        text = p.read_text(encoding='utf-8')
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                errors.append(relative + ': ' + label)
        try:
            if p.suffix == '.py':
                ast.parse(text, filename=relative, feature_version=(3, 11))
            elif p.suffix == '.json':
                json.loads(text)
        except (ValueError, SyntaxError) as exc:
            errors.append(relative + ': invalid source: ' + str(exc))
        if p.suffix == '.md':
            for raw in re.findall(r'\[[^\]]*\]\(([^\r\n)]+)\)', text):
                target = unquote(raw.strip('<>')).split('#')[0]
                if not target or re.match(r'^[A-Za-z]+:', target) or target.startswith('/'):
                    continue
                resolved = (p.parent / target).resolve()
                if not resolved.is_relative_to(ROOT) or not resolved.exists():
                    errors.append(relative + ': broken local link: ' + target)
    skill = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
    if not re.match(r'^---\nname: artifact-steward\ndescription: .+\n---', skill):
        errors.append('SKILL.md: invalid required frontmatter')
    if not re.fullmatch(r'\d+\.\d+\.\d+\n?', (ROOT / 'VERSION').read_text(encoding='utf-8')):
        errors.append('VERSION: semantic version required')
    return {'files_checked': len(files), 'errors': errors, 'status': 'pass' if not errors else 'failed'}

if __name__ == '__main__':
    result = check()
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(bool(result['errors']))
