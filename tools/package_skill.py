"""Create an allowlisted skill ZIP, member manifest and external SHA256 sum."""
from pathlib import Path
import argparse
import hashlib
import json
import zipfile
from check_release import ROOT, check, source_files

def package(output_dir):
    review = check()
    if review['errors']:
        raise ValueError('Release check failed: ' + json.dumps(review))
    output = Path(output_dir).resolve()
    if output.is_relative_to(ROOT):
        raise ValueError('package_output_must_be_outside_source_repository')
    output.mkdir(parents=True, exist_ok=True)
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    archive = output / f'artifact-steward-{version}.zip'
    files = [p for p in source_files() if p.name in {'SKILL.md', 'README.md', 'LICENSE', 'VERSION', 'CHANGELOG.md'}
             or p.relative_to(ROOT).parts[0] in {'scripts', 'references', 'assets', 'agents'}]
    # Match Git's LF-normalized text across Windows and POSIX checkouts.
    payload = {p.relative_to(ROOT).as_posix(): p.read_text(encoding='utf-8').encode('utf-8') for p in files}
    manifest = '\n'.join(hashlib.sha256(data).hexdigest() + '  ' + name for name, data in sorted(payload.items())) + '\n'
    payload['MANIFEST.sha256'] = manifest.encode('utf-8')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo('artifact-steward/' + name, (2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, data, compresslevel=9)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with (output / 'SHA256SUMS.txt').open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(digest + '  ' + archive.name + '\n')
    return {'archive': str(archive), 'members': len(payload), 'sha256': digest}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.output_dir), ensure_ascii=False))
