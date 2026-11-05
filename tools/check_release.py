"""Check the exact source export; prints paths/reasons, never matched secrets."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys


def missing_modules(root, names):
    """Check imports even when an omitted module also vanished from the manifest."""
    modules = {name[:-3].replace('/', '.').removesuffix('.__init__')
               for name in names if name.endswith('.py')}
    errors = []
    for name in sorted(names):
        if not name.endswith('.py') or not (root / name).is_file():
            continue
        for node in ast.walk(ast.parse((root / name).read_text(), filename=name)):
            imports = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                       else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
            for module in imports:
                if (module == 'plex_bridge' or module.startswith('plex_bridge.')) and module not in modules:
                    errors.append('missing imported module: ' + module + ' (from ' + name + ')')
    return errors


def check(root):
    manifest = json.loads((root / 'release-files.json').read_text())
    expected = set(manifest['files']) | {'release-files.json', 'SHA256SUMS'}
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()
              and '__pycache__' not in p.parts and '.git' not in p.parts}
    errors = ['unexpected file: ' + p for p in sorted(actual - expected)]
    errors += ['missing file: ' + p for p in sorted(set(manifest['files']) - actual)]
    errors += missing_modules(root, set(manifest['files']))
    patterns = {
        'private key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        'GitHub credential': rb'\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}',
        'AWS credential': rb'\bAKIA[A-Z0-9]{16}\b',
        'user filesystem path': rb'/(?:Users|home)/[A-Za-z][A-Za-z0-9_-]+/',
    }
    for name in sorted(actual & set(manifest['files'])):
        p = root / name
        if p.is_symlink():
            errors.append('symlink: ' + name)
            continue
        data = p.read_bytes()
        for label, pattern in patterns.items():
            if re.search(pattern, data):
                errors.append(label + ': ' + name)
    covered = []
    for line in (root / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ', 1)
        covered.append(name)
        if name not in expected or not (root / name).is_file() or hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            errors.append('checksum mismatch: ' + name)
    if len(covered) != len(set(covered)) or set(covered) != expected - {'SHA256SUMS'}:
        errors.append('checksum coverage differs from release manifest')
    if errors:
        raise SystemExit('\n'.join(errors))
    print('PASS exact source manifest, checksums, and targeted secret/path checks')


if __name__ == '__main__':
    check(Path(sys.argv[1] if len(sys.argv) > 1 else '.').resolve())
