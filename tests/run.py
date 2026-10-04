"""Run the database tests in an isolated temporary database; no worker can claim them."""
import os
from pathlib import Path
import subprocess
import sys
import uuid
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

root = Path(__file__).resolve().parents[1]
load_dotenv(root / '.env')
url = make_url(os.environ['DB_URI']).set(drivername='postgresql+psycopg2')
name = 'trvelle_test_' + uuid.uuid4().hex
admin = create_engine(url, isolation_level='AUTOCOMMIT')
with admin.connect() as connection:
    connection.execute(text(f'CREATE DATABASE "{name}"'))
try:
    env = {**os.environ, 'DB_URI': url.set(database=name).render_as_string(hide_password=False)}
    code = subprocess.call([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q'], cwd=root, env=env)
finally:
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()
sys.exit(code)
