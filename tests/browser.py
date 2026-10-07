"""Run website/browser acceptance tests with disposable DB, accounts and SMTP.

Requires the sibling trvelle-website checkout and Playwright Chromium installed.
No production sessions, credentials, worker or paid search calls are used.
"""
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT.parent / 'trvelle-website'
load_dotenv(ROOT / '.env')
url = make_url(os.environ['DB_URI']).set(drivername='postgresql+psycopg2')
name = 'trvelle_browser_' + uuid.uuid4().hex
admin = create_engine(url, isolation_level='AUTOCOMMIT')
with admin.connect() as connection:
    connection.execute(text(f'CREATE DATABASE "{name}"'))
processes = []
server = None
try:
    with tempfile.TemporaryDirectory(prefix='trvelle-browser-') as temporary:
        path = Path(temporary)
        db_url = url.set(database=name).render_as_string(hide_password=False)
        engine = create_engine(db_url)
        with engine.begin() as connection:
            for migration in ('2025-05-31T11-17-16.158Z.sql', '2026-10-02-anonymous.sql', '2026-10-05-guest-links.sql'):
                connection.execute(text((SITE / 'better-auth_migrations' / migration).read_text()))
        engine.dispose()
        mailbox = path / 'mail.json'
        mailbox.write_text('[]'); mailbox.chmod(0o600)
        class SMTP(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b'220 Local acceptance mail\r\n')
                while True:
                    line = self.rfile.readline()
                    if not line: break
                    verb = line.decode().split()[0].upper()
                    if verb in ('EHLO', 'HELO'): self.wfile.write(b'250-localhost\r\n250 8BITMIME\r\n')
                    elif verb == 'DATA':
                        self.wfile.write(b'354 End with dot\r\n')
                        body = []
                        while (part := self.rfile.readline()) not in (b'.\r\n', b''):
                            body.append(part)
                        messages = json.loads(mailbox.read_text()); messages.append(b''.join(body).decode())
                        mailbox.write_text(json.dumps(messages)); self.wfile.write(b'250 Accepted\r\n')
                    elif verb == 'QUIT': self.wfile.write(b'221 Bye\r\n'); break
                    else: self.wfile.write(b'250 OK\r\n')
        server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), SMTP)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {**os.environ, 'DB_URI': db_url,
               'DATABASE_URL': url.set(database=name, drivername='postgresql').render_as_string(hide_password=False),
               'BACKEND_URL': 'http://127.0.0.1:18001', 'BACKEND_API_TOKEN': secrets.token_urlsafe(32),
               'BETTER_AUTH_URL': 'http://127.0.0.1:13000', 'BETTER_AUTH_SECRET': secrets.token_urlsafe(32),
               'SMTP_URL': f'smtp://127.0.0.1:{server.server_address[1]}', 'EMAIL_FROM': 'Trvelle Test <test@example.com>',
               'TRVELLE_BUILD_DIR': '.next-e2e', 'MODEL_ACCOUNTS_DIR': str(path / 'accounts'),
               'TRVELLE_SEARCH_MODE': 'replay', 'TRVELLE_REPLAY_DIR': str(ROOT / 'tests/fixtures/search'),
               'E2E_BASE_URL': 'http://127.0.0.1:13000', 'E2E_DB_URI': url.set(database=name, drivername='postgresql').render_as_string(hide_password=False),
               'E2E_MAILBOX': str(mailbox), 'E2E_BACKEND_ROOT': str(ROOT)}
        for key in ('GOOGLE_API_KEY', 'OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'BRAVE_API_KEY', 'SERPAPI_API_KEY', 'TAVILY_API_KEY', 'TRAVELPAYOUTS_API_TOKEN'):
            env[key] = ''
        # The locally installed Node is optional; any supported Node on PATH works.
        node = ROOT.parent / '.local-tools/node-v22.23.3-linux-x64/bin'
        if node.exists(): env['PATH'] = str(node) + ':' + env.get('PATH', '')
        def launch(command, cwd, label):
            output = (path / (label + '.log')).open('wb')
            processes.append(subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=output, start_new_session=True))
            output.close()
        launch([sys.executable, '-m', 'uvicorn', 'trvelle.orchestrator.api_wrapper:app', '--host', '127.0.0.1', '--port', '18001'], ROOT, 'backend')
        launch(['node', 'node_modules/next/dist/bin/next', 'dev', '--webpack', '--hostname', '127.0.0.1', '--port', '13000'], SITE, 'website')
        for address in ('http://127.0.0.1:18001/health', 'http://127.0.0.1:13000/login'):
            deadline = time.monotonic() + 120
            while True:
                try:
                    urllib.request.urlopen(address, timeout=2).close(); break
                except Exception:
                    if time.monotonic() > deadline: raise RuntimeError('Isolated test service did not start') from None
                    time.sleep(.3)
        print('Acceptance stack ready: disposable database, local mailbox, recorded searches.', flush=True)
        code = subprocess.call(['node', 'node_modules/@playwright/test/cli.js', 'test'], cwd=SITE, env=env)
finally:
    for process in processes:
        if process.poll() is None: os.killpg(process.pid, signal.SIGTERM)
    for process in processes:
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: os.killpg(process.pid, signal.SIGKILL)
    if server: server.shutdown(); server.server_close()
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()
sys.exit(code)
