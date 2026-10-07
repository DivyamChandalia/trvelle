"""Owner-bound encrypted storage and restricted, ephemeral native CLI profiles."""
import asyncio
import base64
import fcntl
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from cryptography.fernet import InvalidToken


class AccountError(ValueError):
    pass


def audit(owner, action, provider=None):
    logging.getLogger('trvelle.security').info('credential_event owner=%s action=%s provider=%s',owner,action,provider or 'all')


def seal(cipher, owner, purpose, data):
    return cipher.encrypt(json.dumps({'version':1,'owner':str(owner),'purpose':purpose,'data':data}).encode())


def unseal(cipher, owner, purpose, raw, legacy=False):
    try:
        value=json.loads(cipher.decrypt(raw))
    except (InvalidToken,ValueError,TypeError):
        raise AccountError('Stored credentials could not be verified.') from None
    if legacy and 'version' not in value:
        return value,True
    if value.get('version')!=1 or value.get('owner')!=str(owner) or value.get('purpose')!=purpose:
        raise AccountError('Stored credentials do not belong to this account.')
    return value['data'],False


def atomic_private_write(path, data):
    if path.is_symlink():raise AccountError('Credential storage path is not safe.')
    temporary=path.with_name('.write-'+secrets.token_hex(12))
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as output:
            output.write(data);output.flush();os.fsync(output.fileno())
        os.replace(temporary,path)
        parent_fd=os.open(path.parent,os.O_DIRECTORY)
        try:os.fsync(parent_fd)
        finally:os.close(parent_fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def account_lock(directory):
    fd=os.open(directory/'.accounts.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def minimal_cli_env():
    # Explicit allowlist: never pass service tokens, DB URLs, provider keys,
    # encrypted-vault keys, SDK token overrides or host profile variables.
    return {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','LC_ALL':'C.UTF-8','TZ':'UTC'}


def sandbox_command(command, profile, env):
    if os.getenv('TRVELLE_CLAUDE_SANDBOX')!='1':return command,env,profile
    executable=shutil.which('bwrap')
    if not executable:raise AccountError('The required Claude sandbox is unavailable.')
    cli=Path(command[0]).resolve()
    package=cli.parent.parent
    args=[executable,'--die-with-parent','--new-session','--unshare-user','--unshare-pid','--unshare-uts','--unshare-ipc','--cap-drop','ALL',
          '--ro-bind','/usr','/usr','--symlink','usr/bin','/bin','--symlink','usr/lib','/lib',
          '--proc','/proc','--dev','/dev','--tmpfs','/tmp']
    if Path('/usr/lib64').exists():args+=['--symlink','usr/lib64','/lib64']
    for path in ('/etc/ssl','/etc/resolv.conf','/etc/hosts','/etc/nsswitch.conf','/etc/localtime','/etc/passwd','/etc/group'):
        if Path(path).exists():args+=['--ro-bind',path,path]
    if Path('/opt/node').exists():args+=['--ro-bind','/opt/node','/opt/node']
    args+=['--ro-bind',str(package),'/bridge','--bind',str(profile),'/profile','--chdir','/profile']
    scoped={**env,'HOME':'/profile','CLAUDE_CONFIG_DIR':'/profile','XDG_CONFIG_HOME':'/profile/.config','XDG_CACHE_HOME':'/profile/.cache'}
    return args+['/bridge/'+str(cli.relative_to(package)),*command[1:]],scoped,Path('/profile')


class ClaudeVault:
    FILES=('.credentials.json','.claude.json')
    def __init__(self, accounts):self.accounts=accounts

    def path(self,owner):return self.accounts.directory(owner)/'claude.enc'

    def load(self,owner):
        path=self.path(owner)
        if path.is_symlink():raise AccountError('Credential storage path is not safe.')
        if not path.exists():return {'files':{},'status':None}
        return unseal(self.accounts.cipher,owner,'claude',path.read_bytes())[0]

    def save(self,owner,data):
        atomic_private_write(self.path(owner),seal(self.accounts.cipher,owner,'claude',data))

    def migrate(self,owner):
        legacy=self.accounts.directory(owner)/'claude'
        if legacy.is_symlink():raise AccountError('Credential storage path is not safe.')
        if not legacy.exists():return
        data=self.load(owner)
        for name in self.FILES:
            path=legacy/name
            if path.is_symlink():raise AccountError('Credential storage path is not safe.')
            if path.exists() and name not in data['files']:
                raw=path.read_bytes()
                if len(raw)>1024*1024:raise AccountError('Claude profile exceeds the secure storage limit.')
                data['files'][name]=base64.b64encode(raw).decode()
        self.save(owner,data)
        if self.load(owner)!=data:raise AccountError('Claude credential migration could not be verified.')
        shutil.rmtree(legacy)
        audit(owner,'plaintext_profile_migrated','claude')

    def runtime_root(self):
        root=Path(os.getenv('TRVELLE_CLAUDE_RUNTIME_DIR',tempfile.gettempdir())).resolve()
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if os.getenv('TRVELLE_REQUIRE_TMPFS')=='1':
            mounts=[]
            for line in Path('/proc/self/mountinfo').read_text().splitlines():
                left,right=line.split(' - ',1);point=Path(left.split()[4])
                if root==point or point in root.parents:mounts.append((len(str(point)),right.split()[0]))
            if not mounts or max(mounts)[1]!='tmpfs':raise AccountError('Claude credentials require memory-only runtime storage.')
        return root

    @asynccontextmanager
    async def session(self,owner):
        directory=self.accounts.directory(owner)
        fd=os.open(directory/'.claude.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        temporary=None
        try:
            deadline=time.monotonic()+30
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>deadline:raise AccountError('Claude is processing another request. Try again shortly.')
                    await asyncio.sleep(.1)
            self.migrate(owner)
            data=self.load(owner)
            temporary=tempfile.TemporaryDirectory(prefix='claude-',dir=self.runtime_root())
            profile=Path(temporary.name)
            for name,value in data['files'].items():
                if name not in self.FILES:raise AccountError('Claude profile contains an unsupported credential file.')
                atomic_private_write(profile/name,base64.b64decode(value,validate=True))
            audit(owner,'profile_opened','claude')
            state={'directory':profile,'status':data.get('status')}
            try:yield state
            finally:
                updated={name:base64.b64encode((profile/name).read_bytes()).decode() for name in self.FILES if (profile/name).exists() and not (profile/name).is_symlink()}
                self.save(owner,{'files':updated,'status':state.get('status')})
                audit(owner,'profile_sealed','claude')
        finally:
            if temporary:temporary.cleanup()
            os.close(fd)

    async def purge(self,owner):
        async with self.session(owner) as state:
            for name in self.FILES:(state['directory']/name).unlink(missing_ok=True)
            state['status']=None
        audit(owner,'connection_removed','claude')
