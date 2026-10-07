# Credential security

Personal API keys, ChatGPT OAuth registration/access/refresh tokens, and model
choices are encrypted in `accounts.enc`. The encrypted envelope binds the data
to its user UUID and purpose, so a ciphertext copied into another user's folder
is rejected. Updates use file locks and atomic, synced writes. OAuth refresh
updates only its own connection and cannot restore a removed key or disconnected
account from an older snapshot.

Claude Code stays unmodified. Its native `.credentials.json` and `.claude.json`
are sealed in an owner-bound `claude.enc`. On production, they are decrypted only
into a private profile on `/run` (tmpfs) for the duration of a CLI operation.
Refresh changes are encrypted before cleanup, including on cancellation. Legacy
plaintext profiles migrate after the encrypted copy is verified. Guest linking
re-encrypts the profile for the destination owner and preserves existing target
connections.

Production requires Bubblewrap. The CLI gets a fresh user/PID/IPC/UTS namespace,
no capabilities, minimal devices, read-only runtime/package files and only its
current profile. It cannot see the host credential vault, encryption key,
service environment files or other profiles. Its environment is an explicit
allowlist with no provider keys, service tokens, database passwords or host
profile variables. Network access remains enabled for native authentication and
inference. The narrow AppArmor rule in `deploy/bwrap.apparmor` permits Bubblewrap
namespaces without disabling Ubuntu's global restriction.

## Server configuration

`deploy/enable-credential-hardening.py` is a root-only **post-deployment**
migration: deploy the updated backend and website bootstrap first. It enables
root-protected encrypted systemd credentials for backend/website secrets and the
model vault key. systemd decrypts them into service-private runtime files; the
model key is separate from the credential vault. The public website runs as
`trvelle-web`; builds run as `trvelle-build` with placeholder credentials and
cannot read the production vault. The website database role can access only
Better Auth tables and guest-link records, not backend itinerary tables or
PostgreSQL administration. Services restrict filesystem writes, core dumps and swapping of their private memory.
Private environment snapshots are encrypted rather than left as plaintext.

The migration expects encrypted credential bundles prepared by a root
administrator using `systemd-creds encrypt --with-key=host`. Never print secrets
or copy decrypted bundles into source control. The matching credential names
are `backend-secrets`, `website-secrets` and `model-accounts.key`. After verifying
all existing connections with the service-loaded key, remove the old vault-local
`master.key`; retain only the root-protected encrypted key. Back up encrypted
credentials and the systemd host key securely and separately from user data.

Development keeps the existing private local `.env` and vault-key workflow.
`MODEL_ACCOUNTS_KEY_FILE` supplies an external key; a missing/unsafe configured
key fails closed. Production additionally sets `TRVELLE_REQUIRE_VAULT_KEY=1`,
`TRVELLE_REQUIRE_CREDENTIALS=1`, `TRVELLE_CLAUDE_SANDBOX=1`,
`TRVELLE_REQUIRE_TMPFS=1` and a service-specific `TRVELLE_CLAUDE_RUNTIME_DIR`.

## Browser and access controls

Connection/login/chat pages never load affiliate scripts. Crossing between a
public and private page creates a new document to remove already-running
third-party handlers. Private pages use a per-request nonce CSP, restricted script
execution, same-origin network requests and frame protection. Provider
photos remain permitted. The backend proxy derives user identity from the
verified website session, blocks cross-site mutations and returns private data
with `Cache-Control: no-store`. Validation errors omit submitted secrets.
Credential input fields are cleared after submission; credentials are not kept
in browser storage or user cookies.

Credential lifecycle/access audit events include only user UUID, provider and
action. They exclude tokens, keys, codes and conversation contents. Removing a
key or disconnecting an account stops affected planning runs. ChatGPT local
access is disabled before remote revocation; failed revocations remain encrypted
for retry. API keys must be revoked at their provider if suspected exposed.

## Limits

These controls reduce accidental disclosure, cross-user file mixups, child
process access and the impact of a compromised frontend/build dependency. The
credential-handling backend must still decrypt credentials to use them. A fully
compromised backend, root administrator or complete VM image with its host
sealing key can defeat local protections. This is not a claim of protection
against total server compromise; externally managed KMS/secret brokers and an
independent security review are appropriate next steps for higher assurance.
