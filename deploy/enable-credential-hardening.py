#!/usr/bin/python3
"""Root-only post-deployment migration. Never prints credentials or raw errors.
Run with the backend virtualenv after deploying the credential loader/bootstrap.
"""
import json,os,secrets,subprocess,pwd
from pathlib import Path
import psycopg2
from psycopg2 import sql
from urllib.parse import urlsplit,urlunsplit,quote

STORE=Path('/etc/credstore.encrypted')

def run(args,**kwargs):return subprocess.run(args,check=True,**kwargs)

def decrypt(name,filename):
    return subprocess.check_output(['systemd-creds','decrypt','--name='+name,str(STORE/filename),'-'],stderr=subprocess.DEVNULL)

def encrypt(name,data,filename):
    run(['systemd-creds','encrypt','--with-key=host','--name='+name,'-',str(STORE/filename)],input=data,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    os.chmod(STORE/filename,0o600)

def website_database(backend,website):
    # A dedicated role can access auth tables, not itinerary/provider tables or
    # PostgreSQL administration. Ownership/migrations stay with the admin role.
    password=secrets.token_urlsafe(40)
    with psycopg2.connect(backend['DB_URI'].replace('postgresql+psycopg2://','postgresql://',1)) as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname='trvelle_web'")
            if cursor.fetchone():cursor.execute(sql.SQL('ALTER ROLE trvelle_web LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION').format(sql.Literal(password)))
            else:cursor.execute(sql.SQL('CREATE ROLE trvelle_web LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION').format(sql.Literal(password)))
            cursor.execute('GRANT USAGE ON SCHEMA public TO trvelle_web')
            for table in ('user','account','session','verification','trvelle_guest_links'):
                cursor.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO trvelle_web').format(sql.Identifier(table)))
    previous=urlsplit(website['DATABASE_URL'])
    host=previous.hostname
    if ':' in host:host='['+host+']'
    netloc='trvelle_web:'+quote(password,safe='')+'@'+host+(':'+str(previous.port) if previous.port else '')
    website['DATABASE_URL']=urlunsplit((previous.scheme,netloc,previous.path,previous.query,previous.fragment))

def dropin(service,lines):
    directory=Path('/etc/systemd/system')/(service+'.service.d')
    directory.mkdir(exist_ok=True)
    (directory/'credential-security.conf').write_text('[Service]\n'+'\n'.join(lines)+'\n')

def strip_secrets(path,names):
    original=path.read_text()
    encrypt('archived-environment',original.encode(),'archived-'+path.name+'-'+secrets.token_hex(5))
    lines=[line for line in original.splitlines() if line.split('=',1)[0].removeprefix('export ').strip() not in names]
    path.write_text('\n'.join(lines)+'\n')


def main():
    website_boot=Path('/opt/trvelle/current/trvelle-website/scripts/secure-start.mjs')
    backend_code=Path('/opt/trvelle/current/trvelle/trvelle/orchestrator/credential_security.py')
    if not website_boot.exists() or not backend_code.exists():raise RuntimeError('Deploy security code before enabling service credentials')
    backend=json.loads(decrypt('backend-secrets','trvelle-backend-secrets'))
    website=json.loads(decrypt('website-secrets','trvelle-website-secrets'))
    website_database(backend,website)
    encrypt('website-secrets',json.dumps(website).encode(),'trvelle-website-secrets')
    common=['LimitCORE=0','MemorySwapMax=0','ProtectSystem=strict','ProtectKernelTunables=true','ProtectKernelModules=true','ProtectControlGroups=true',
            'ProtectProc=invisible','ProcSubset=pid','PrivateDevices=true','RestrictSUIDSGID=true','LockPersonality=true','RemoveIPC=true','KeyringMode=private']
    for service in ('trvelle-api','trvelle-worker','trvelle-tools'):
        Path('/opt/trvelle/current/trvelle/logs').mkdir(exist_ok=True)
        Path('/opt/trvelle/current/trvelle/.runtime').mkdir(exist_ok=True)
        user=pwd.getpwnam('trvelle')
        for path in ('/opt/trvelle/current/trvelle/logs','/opt/trvelle/current/trvelle/.runtime'):
            os.chown(path,user.pw_uid,user.pw_gid)
        specific=[*common,'RuntimeDirectory='+service,'RuntimeDirectoryMode=0700',
            'ReadWritePaths=/var/lib/trvelle /opt/trvelle/current/trvelle/logs /opt/trvelle/current/trvelle/.runtime',
            'LoadCredentialEncrypted=backend-secrets:/etc/credstore.encrypted/trvelle-backend-secrets',
            'Environment=TRVELLE_REQUIRE_CREDENTIALS=1 PYTHONDONTWRITEBYTECODE=1']
        if service!='trvelle-tools':specific += ['LoadCredentialEncrypted=model-accounts.key:/etc/credstore.encrypted/trvelle-model-accounts-key',
            'Environment=TRVELLE_REQUIRE_VAULT_KEY=1 TRVELLE_CLAUDE_SANDBOX=1 TRVELLE_REQUIRE_TMPFS=1 TRVELLE_CLAUDE_RUNTIME_DIR=/run/'+service+'/claude']
        dropin(service,specific)
    dropin('trvelle-website',[*common,'User=trvelle-web','Group=trvelle-web','RuntimeDirectory=trvelle-website','RuntimeDirectoryMode=0700',
        'ReadWritePaths=-/opt/trvelle/current/trvelle-website/.next/cache',
        'LoadCredentialEncrypted=website-secrets:/etc/credstore.encrypted/trvelle-website-secrets',
        'Environment=TRVELLE_REQUIRE_CREDENTIALS=1','ExecStart=',
        'ExecStart=/opt/node/bin/node scripts/secure-start.mjs start --hostname 127.0.0.1 --port 3000'])
    strip_secrets(Path('/etc/trvelle/backend.env'),set(backend))
    strip_secrets(Path('/etc/trvelle/website.env'),set(website))
    # Previous private env snapshots must not leave plaintext copies behind.
    for backup in Path('/etc/trvelle').glob('*.env.before-*'):
        encrypt('archived-environment',backup.read_bytes(),'archived-'+backup.name)
        backup.unlink()
    run(['systemctl','daemon-reload'])
    run(['systemctl','restart','trvelle-tools','trvelle-api','trvelle-worker','trvelle-website'])
    print('Enabled encrypted service credentials, isolated website role and private runtime profiles')

if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Hardening configuration failed:',type(error).__name__)
        raise SystemExit(1) from None
