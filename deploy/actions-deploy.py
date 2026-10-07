#!/usr/bin/python3
"""Forced SSH command for tested main-branch source bundles from GitHub Actions."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

BASE = Path('/opt/trvelle')
STATE = Path('/var/lib/trvelle-deploy')
SERVICES = ['trvelle-tools', 'trvelle-api', 'trvelle-worker', 'trvelle-website']

def command():
    supplied = sys.argv[1] if len(sys.argv) == 2 else os.environ.get('SSH_ORIGINAL_COMMAND', '')
    match = re.fullmatch(r'(backend|website) ([0-9a-f]{40})', supplied)
    if not match:
        raise ValueError('Expected backend or website followed by a commit SHA')
    return match.groups()

def unpack(archive, target):
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe archive member')
            if any(part in {'.git', '.venv', 'node_modules', '.runtime'} or part.startswith('.next')
                   or (part.startswith('.env') and part != '.env.example') for part in path.parts):
                raise ValueError('Runtime files and secrets cannot be deployed')
        bundle.extractall(target, filter='data')

def run(args, **kwargs):
    subprocess.run(args, check=True, **kwargs)

def as_app(script, cwd):
    run(['runuser', '-u', 'trvelle-build', '--', 'env', '-i',
         'HOME=/var/lib/trvelle-build','LANG=C.UTF-8',
         'PATH=/opt/node/bin:/opt/trvelle-tooling/bin:/usr/local/bin:/usr/bin:/bin',
         'bash', '-c', 'set -e; ' + script], cwd=cwd)

def switch(release):
    link = BASE / 'current.deploy'
    link.unlink(missing_ok=True)
    link.symlink_to(release)
    os.replace(link, BASE / 'current')

def main():
    component, sha = command()
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / 'lock').open('w') as lock, tempfile.TemporaryDirectory(dir=STATE) as temporary:
        fcntl.flock(lock, fcntl.LOCK_EX)
        archive = Path(temporary) / 'source.tar'
        with archive.open('wb') as output:
            count = 0
            while chunk := sys.stdin.buffer.read(65536):
                count += len(chunk)
                if count > 50 * 1024 * 1024:
                    raise ValueError('Source bundle exceeds 50 MiB')
                output.write(chunk)
        previous = (BASE / 'current').resolve()
        release = BASE / 'releases' / (time.strftime('%Y%m%d-%H%M%S') + '-' + component + '-' + sha[:12])
        release.mkdir()
        try:
            excluded = shutil.ignore_patterns('.git', '.venv', 'node_modules', '.next*', '.env', '.env.local', '.runtime', '__pycache__', 'logs', 'test-results', 'playwright-report')
            for package in ('trvelle', 'trvelle-website'):
                shutil.copytree(previous / package, release / package, ignore=excluded)
            target = release / ('trvelle' if component == 'backend' else 'trvelle-website')
            shutil.rmtree(target)
            target.mkdir()
            unpack(archive, target)
            run(['chown', '-R', 'trvelle-build:trvelle-build', str(release)])
            release.chmod(0o711)
            as_app('uv sync --locked', release / 'trvelle')
            as_app('npm ci --no-audit --no-fund; export BETTER_AUTH_URL=https://trvelle.com BETTER_AUTH_SECRET=build-only-placeholder-secret-32-characters DATABASE_URL=postgresql://USER:YOUR_DATABASE_PASSWORD@127.0.0.1/build; npm run build', release / 'trvelle-website')
            run(['chown','-R','trvelle:trvelle',str(release/'trvelle')])
            run(['chown','-R','trvelle-build:trvelle-web',str(release/'trvelle-website')])
            run(['chmod','-R','g+rX',str(release/'trvelle-website')])
            cache=release/'trvelle-website/.next/cache'
            cache.mkdir(parents=True,exist_ok=True)
            run(['chown','-R','trvelle-build:trvelle-web',str(cache)])
            run(['chmod','-R','g+rwX',str(cache)])
            backup = Path('/var/backups/trvelle') / (release.name + '.dump')
            with backup.open('wb') as output:
                run(['runuser', '-u', 'postgres', '--', 'pg_dump', '-Fc', 'trvelle'], stdout=output)
            backup.chmod(0o600)
            # Current auth migration is additive and idempotent. Future destructive
            # migrations require an explicit reviewed deployment procedure.
            migration = release / 'trvelle-website/better-auth_migrations/2026-10-05-guest-links.sql'
            with migration.open('rb') as source:
                run(['runuser', '-u', 'postgres', '--', 'psql', '-v', 'ON_ERROR_STOP=1', '-d', 'trvelle'], stdin=source)
            switch(release)
            run(['systemctl', 'restart', *SERVICES])
            deadline = time.monotonic() + 60
            while True:
                try:
                    with urllib.request.urlopen('https://144.24.127.147/api/backend/health', timeout=5) as response:
                        health = json.load(response)
                    with urllib.request.urlopen('https://144.24.127.147/login', timeout=5) as response:
                        response.read(100)
                    if health.get('database') and health.get('worker') and health.get('ai_ready'):
                        break
                except Exception:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError('Deployment health check failed')
                time.sleep(2)
            manifest = previous / 'deployment.json'
            versions = json.loads(manifest.read_text()) if manifest.exists() else {}
            versions[component] = sha
            (release / 'deployment.json').write_text(json.dumps(versions))
            print('Deployed', component, sha, 'at https://144.24.127.147', flush=True)
        except Exception:
            if (BASE / 'current').resolve() == release:
                switch(previous)
                run(['systemctl', 'restart', *SERVICES])
                print('Health check failed: restored previous release', file=sys.stderr)
            raise

if __name__ == '__main__':
    main()
