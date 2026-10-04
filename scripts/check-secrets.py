#!/usr/bin/env python3
"""Scan tracked/staged content without printing credential values."""
import re
import subprocess
import sys

PATTERNS = [
    ('Google API key', re.compile(rb'AIza[0-9A-Za-z_-]{35}')),
    ('Tavily key', re.compile(rb'tvly-(?:dev-|prod-)?[0-9A-Za-z_-]{20,}')),
    ('OpenRouter key', re.compile(rb'sk-or-v1-[0-9a-f]{32,}')),
    ('SerpAPI token', re.compile(rb'(?:api[_-]?key|serpapi[_-]?(?:api[_-]?)?key)[\x22\x27]?\s*[:=]\s*[\x22\x27]?[0-9a-f]{64}', re.I)),
    ('Database password', re.compile(rb'postgres(?:ql)?://[^\s:@]+:[^\s@]+@')),
]
staged = '--staged' in sys.argv
command = ['git','diff','--cached','--name-only','--diff-filter=ACMR','-z'] if staged else ['git','ls-files','--cached','--others','--exclude-standard','-z']
files = subprocess.check_output(command).decode().split('\0')
found = False
for path in filter(None, files):
    from pathlib import Path
    if not staged and not Path(path).is_file():
        continue
    data = subprocess.check_output(['git','show', ':'+path] if staged else ['git','show','HEAD:'+path]) if staged else open(path,'rb').read()
    for label, pattern in PATTERNS:
        if pattern.search(data.replace(b'USER:YOUR_DATABASE_PASSWORD@', b'USER@')):
            print(f'{path}: possible {label}; remove it before committing', file=sys.stderr)
            found = True
sys.exit(1 if found else 0)
