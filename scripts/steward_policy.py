"""Conservative path protections shared by proposals and ordinary file actions."""
from pathlib import Path
from steward_common import within

# Exact path components, independent of the user's drive, account, or host.
APPLICATION_PARTS = {'.git', '.codex', '.agents', '.dsh', '.codebuddy',
                     'wechat_files', 'wechat files', 'xwechat_files', 'wxwork',
                     'wpsdrive', 'wps cloud files', 'kingsoft'}
PROTECTED_NAMES = {'agents.md', 'skill.md', '.env', '.npmrc', '.netrc',
                   'credentials', 'credentials.json'}
DATABASE_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.wal', '.shm'}

def protection_reason(path, protected_roots=()):
    p = Path(path)
    if any(part.casefold() in APPLICATION_PARTS for part in p.parts):
        return 'application_or_git_managed_path'
    if p.name.casefold() in PROTECTED_NAMES or p.suffix.casefold() in DATABASE_SUFFIXES:
        return 'configuration_or_database_file'
    if p.name.casefold().endswith(('-wal', '-shm')):
        return 'application_database_sidecar'
    if any(within(p, root) for root in protected_roots):
        return 'explicitly_protected_root'
    return None
