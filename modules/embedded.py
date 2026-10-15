"""Discover and install app-owned MySQL binaries from Oracle's current GA pages."""
import hashlib
import json
import os
import platform as host_platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def version(value):
    found = re.search(r'\b(\d+\.\d+\.\d+)\b', value)
    if not found:
        raise ValueError('Could not determine MySQL binary version')
    return found.group(1)


def release_packages(html):
    pattern = r'\((mysql(?:-shell)?-\d+\.\d+\.\d+-[\w.\-]+\.(?:tar\.xz|tar\.gz))\).*?class="md5">([a-f0-9]{32})'
    return [{'filename': m[0], 'md5': m[1], 'version': version(m[0])}
            for m in re.findall(pattern, html, re.S)]


def discover(component, platform, machine=None):
    machine = machine or host_platform.machine()
    arm = machine in {'arm64', 'aarch64'}
    if machine not in {'arm64','aarch64','x86_64','amd64'}:
        raise ValueError('Supported architectures are ARM64 and x86_64')
    product = 'mysql' if component == 'server' else 'shell'
    os_id = '33' if platform == 'macos' else '2'
    url = f'https://dev.mysql.com/downloads/{product}/?os={os_id}'
    with urllib.request.urlopen(url, timeout=30) as response:
        html = response.read().decode()
    packages = release_packages(html)
    def compatible(package):
        name = package['filename']
        if component == 'server' and name.startswith('mysql-shell'):
            return False
        if platform == 'macos':
            return 'macos' in name and ('arm64' in name if arm else ('x86_64' in name or 'x86-64bit' in name))
        return 'linux-glibc2.28' in name and (('aarch64' in name or 'arm-64bit' in name) if arm else ('x86_64' in name or 'x86-64bit' in name))
    packages = [p for p in packages if compatible(p)]
    if not packages:
        raise ValueError(f'No compatible current {component} archive found; supply an official URL and SHA256 override')
    # Prefer minimal server builds at the latest version; Shell uses the generic tarball.
    latest = max(tuple(map(int, p['version'].split('.'))) for p in packages)
    candidates = [p for p in packages if tuple(map(int, p['version'].split('.'))) == latest]
    selected = sorted(candidates, key=lambda p: ('minimal' not in p['filename'], p['filename']))[0]
    directory = 'MySQL-' if component == 'server' else 'MySQL-Shell-'
    series = '.'.join(selected['version'].split('.')[:2])
    selected['url'] = f"https://dev.mysql.com/get/Downloads/{directory}{series}/{selected['filename']}"
    selected['source'] = url
    return selected


def binary_environment():
    env = dict(os.environ)
    shell_config = ROOT / '.data/mysqlsh'
    shell_config.mkdir(parents=True, exist_ok=True, mode=0o700)
    env['MYSQLSH_USER_CONFIG_HOME'] = str(shell_config)
    compatibility = ROOT / '.embedded/mysql-server/compat/lib'
    env['LD_LIBRARY_PATH'] = str(compatibility) + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    return env


def install(component, platform):
    prefix = 'BLUEGREEN_MYSQL_SERVER' if component == 'server' else 'BLUEGREEN_MYSQLSH'
    override = os.environ.get(prefix + '_URL')
    if override:
        parsed = urllib.parse.urlparse(override)
        if parsed.scheme != 'https' or parsed.hostname not in {'dev.mysql.com','cdn.mysql.com','downloads.mysql.com'}:
            raise ValueError('Package overrides must use an official MySQL HTTPS download URL')
        digest = os.environ.get(prefix + '_SHA256')
        if not digest or not re.fullmatch('[a-fA-F0-9]{64}', digest):
            raise ValueError('A SHA256 checksum is required with a package URL override')
        package = {'url': override, 'filename': Path(parsed.path).name, 'sha256': digest.lower(),
                   'version': version(Path(parsed.path).name), 'source': override}
    else:
        package = discover(component, platform)
    pinned = os.environ.get(prefix + '_VERSION')
    if pinned and pinned != package['version']:
        raise ValueError('Pinned version differs from discovered release; supply its official URL and SHA256')
    target = ROOT / '.embedded' / ('mysql-server' if component == 'server' else 'mysql-shell')
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    executable = 'mysqld' if component == 'server' else 'mysqlsh'
    current = target / 'current'
    if (current / 'bin' / executable).exists():
        installed = subprocess.check_output([str(current/'bin'/executable),'--version'], env=binary_environment(), text=True)
        if version(installed) == package['version']:
            return package
        if tuple(map(int, version(installed).split('.'))) > tuple(map(int, package['version'].split('.'))):
            raise ValueError('Refusing to downgrade the embedded MySQL installation')
    downloads = ROOT / 'downloads'
    downloads.mkdir(mode=0o700, exist_ok=True)
    archive = downloads / package['filename']
    algorithm = 'sha256' if package.get('sha256') else 'md5'
    expected = package[algorithm]
    def checksum(path):
        result = hashlib.new(algorithm)
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024*1024), b''):
                result.update(block)
        return result.hexdigest()
    if not archive.exists() or checksum(archive) != expected:
        temporary = archive.with_suffix(archive.suffix + '.part')
        print(f"Downloading embedded MySQL {component} {package['version']}", flush=True)
        with urllib.request.urlopen(package['url'], timeout=90) as response, temporary.open('wb') as handle:
            shutil.copyfileobj(response, handle, 1024*1024)
        if checksum(temporary) != expected:
            temporary.unlink(missing_ok=True)
            raise ValueError('MySQL archive checksum mismatch')
        temporary.replace(archive)
    with tempfile.TemporaryDirectory(dir=target, prefix='extract-') as stage:
        with tarfile.open(archive) as tar:
            tar.extractall(stage, filter='data')
        roots = [p for p in Path(stage).iterdir() if p.is_dir() and (p/'bin'/executable).exists()]
        if len(roots) != 1:
            raise ValueError('Archive does not contain the expected embedded MySQL executable')
        new_root = target / roots[0].name
        if not new_root.exists():
            shutil.move(str(roots[0]), new_root)
        # A retry can validate an already extracted directory after a launch interruption.
    output = subprocess.check_output([str(new_root/'bin'/executable),'--version'], env=binary_environment(), text=True)
    if version(output) != package['version']:
        raise ValueError('Installed binary does not match the discovered release')
    link = target/'next'
    link.unlink(missing_ok=True)
    link.symlink_to(new_root.name, target_is_directory=True)
    link.replace(current)
    (target/'release.json').write_text(json.dumps(package, indent=2) + '\n')
    return package
