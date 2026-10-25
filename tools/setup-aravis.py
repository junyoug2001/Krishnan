"""Install a workspace-local Aravis runtime from official MSYS2 packages.

No registry, drivers, global PATH, package hooks, or device calls are used.
Downloaded packages are verified against their official repository SHA256.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import struct
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1] / "native" / "aravis"
DOWNLOADS = ROOT / 'downloads'
RUNTIME = ROOT / 'runtime'
BASE = 'https://repo.msys2.org/mingw/ucrt64/'
PREFIX = 'mingw-w64-ucrt-x86_64-'
TAR = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'tar.exe'


def download(name: str, sha256: str | None = None) -> Path:
    if Path(name).name != name:
        raise ValueError('Invalid repository filename')
    destination = DOWNLOADS / name
    if destination.exists() and (not sha256 or hashlib.sha256(destination.read_bytes()).hexdigest() == sha256):
        return destination
    print('Downloading', name, flush=True)
    with urllib.request.urlopen(BASE + name, timeout=40) as response:
        data = response.read(128 * 1024 * 1024 + 1)
    if len(data) > 128 * 1024 * 1024:
        raise ValueError('Unexpected package size')
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError('SHA256 mismatch: ' + name)
    destination.write_bytes(data)
    return destination


def members(archive: Path) -> list[str]:
    result = subprocess.run([str(TAR), '-tf', str(archive)], check=True, capture_output=True, text=True)
    paths = result.stdout.splitlines()
    for entry in paths:
        path = PurePosixPath(entry)
        if path.is_absolute() or '..' in path.parts or ':' in entry or '\\' in entry:
            raise ValueError('Unsafe archive member: ' + entry)
    return paths


def unpack(archive: Path, destination: Path, selected: list[str] | None = None):
    paths = members(archive)
    destination.mkdir(parents=True, exist_ok=True)
    command = [str(TAR), '-xf', str(archive), '-C', str(destination), '--no-same-owner', '--no-same-permissions']
    if selected is not None:
        command += [path for path in selected if path in paths or any(x.startswith(path.rstrip('/') + '/') for x in paths)]
    subprocess.run(command, check=True, capture_output=True, text=True)


def fields(text: str) -> dict[str, list[str]]:
    result, key = {}, None
    for line in text.splitlines():
        if line.startswith('%') and line.endswith('%'):
            key = line.strip('%')
            result[key] = []
        elif key and line:
            result[key].append(line)
    return result


def pe_imports(path: Path) -> list[str]:
    b = path.read_bytes()
    if b[:2] != b'MZ':
        return []
    pe = struct.unpack_from('<I', b, 0x3c)[0]
    if b[pe:pe + 4] != b'PE\0\0':
        raise ValueError('Invalid PE: ' + str(path))
    count = struct.unpack_from('<H', b, pe + 6)[0]
    optional_size = struct.unpack_from('<H', b, pe + 20)[0]
    optional = pe + 24
    directories = optional + (112 if struct.unpack_from('<H', b, optional)[0] == 0x20b else 96)
    rva, _ = struct.unpack_from('<II', b, directories + 8)
    sections = []
    for i in range(count):
        off = optional + optional_size + 40 * i
        size, address, raw_size, raw = struct.unpack_from('<IIII', b, off + 8)
        sections.append((address, max(size, raw_size), raw))
    def offset(address):
        for start, size, raw in sections:
            if start <= address < start + size:
                return raw + address - start
        raise ValueError('Unmapped RVA')
    if not rva:
        return []
    cursor, names = offset(rva), []
    while any(b[cursor:cursor + 20]):
        name = offset(struct.unpack_from('<I', b, cursor + 12)[0])
        names.append(b[name:b.index(b'\0', name)].decode('ascii').lower())
        cursor += 20
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-metadata', action='store_true')
    args = parser.parse_args()
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    for name in ['ucrt64.db', 'ucrt64.files']:
        if args.refresh_metadata:
            destination = DOWNLOADS / name
            # Replace only these two fixed, local metadata files.
            if destination.exists():
                destination.unlink()
        metadata = ROOT / ('metadata-' + name.split('.')[-1])
        if args.refresh_metadata or not any(metadata.glob('*/desc')):
            unpack(download(name), metadata)
    database = {}
    for path in (ROOT / 'metadata-db').glob('*/desc'):
        entry = fields(path.read_text(encoding='utf-8'))
        database[entry['NAME'][0]] = entry
    providers = {}
    for path in (ROOT / 'metadata-files').glob('*/files'):
        desc = fields((path.parent / 'desc').read_text(encoding='utf-8'))
        package = desc['NAME'][0]
        for entry in fields(path.read_text(encoding='utf-8')).get('FILES', []):
            if entry.startswith('ucrt64/bin/') and entry.lower().endswith('.dll'):
                providers.setdefault(PurePosixPath(entry).name.lower(), []).append(package)
    installed = {}
    def install(package):
        if package in installed:
            return
        info = database[package]
        filename, digest = info['FILENAME'][0], info['SHA256SUM'][0]
        archive = download(filename, digest)
        unpack(archive, RUNTIME, ['ucrt64/bin/', 'ucrt64/include/aravis-0.8/', 'ucrt64/share/licenses/'])
        installed[package] = {'version': info['VERSION'][0], 'filename': filename, 'sha256': digest, 'url': BASE + filename}
        print('Installed locally:', package, info['VERSION'][0], flush=True)
    install(PREFIX + 'aravis')
    binary_dir = RUNTIME / 'ucrt64' / 'bin'
    system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
    checked = set()
    while True:
        pending = [p for p in binary_dir.iterdir() if p.suffix.lower() in ['.dll', '.exe'] and p.name not in checked]
        if not pending:
            break
        for binary in pending:
            checked.add(binary.name)
            for dll in pe_imports(binary):
                if (binary_dir / dll).exists() or dll.startswith(('api-ms-win-', 'ext-ms-win-')):
                    continue
                candidates = [p for p in providers.get(dll, []) if p.startswith(PREFIX) and p in database]
                if dll == 'zlib1.dll' and PREFIX + 'zlib' in candidates:
                    candidates = [PREFIX + 'zlib']
                # Prefer a verified local third-party DLL even if another
                # application previously placed a copy in System32 (libusb
                # on this workstation). Windows system DLLs have no provider.
                if not candidates and (system / dll).exists():
                    continue
                if len(candidates) != 1:
                    raise RuntimeError(f'Cannot uniquely resolve {dll}: {candidates}')
                install(candidates[0])
    manifest = {'source': BASE, 'root': str(ROOT), 'architecture': 'x64/UCRT', 'method': 'local package extraction; no install scripts or drivers', 'packages': installed}
    # Include packages already extracted by an interrupted earlier setup run.
    for package, info in database.items():
        archive = DOWNLOADS / info['FILENAME'][0]
        if package.startswith(PREFIX) and archive.exists():
            digest = info['SHA256SUM'][0]
            if hashlib.sha256(archive.read_bytes()).hexdigest() == digest:
                manifest['packages'][package] = {'version': info['VERSION'][0], 'filename': info['FILENAME'][0], 'sha256': digest, 'url': BASE + info['FILENAME'][0]}
    (ROOT / 'runtime-manifest.json').write_text(json.dumps(manifest, indent=2))
    print('Ready:', binary_dir)


if __name__ == '__main__':
    main()
