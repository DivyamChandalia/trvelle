# Oracle VM deployment

The current deployment serves both packages at **https://trvelle.com** on
Ubuntu 24.04 ARM64. Caddy terminates HTTPS and proxies the production Next.js
server; the website authenticates requests before proxying to the Python API.

## Layout

| Location | Purpose |
| --- | --- |
| `/opt/trvelle/current` | Symlink to the active release |
| `/opt/trvelle/releases/20261005-connections` | Current source, Python environment and production website build |
| `/opt/trvelle/releases/20261005-latest` | Previous release before the remote connection improvements |
| `/opt/trvelle/releases/20261005` | Previous release, retained for rollback |
| `/etc/trvelle/backend.env` | Backend provider keys, database URI and service token |
| `/etc/trvelle/website.env` | Website database URI, service token, auth secret and public origin |
| `/var/lib/trvelle` | Persistent model credentials, cooldowns and worker heartbeat |
| `/var/backups/trvelle` | Private database backups |
| `/etc/caddy/Caddyfile` | Public reverse proxy and ACME challenge route |

Secrets stay outside the release directories and repository. The database is
PostgreSQL on the VM. This deployment preserves the existing VM database; it
does not import the developer laptop's database or model credentials.

## Services and networking

`trvelle-website`, `trvelle-api`, `trvelle-tools` and `trvelle-worker` are systemd
services running as the dedicated `trvelle` user, with restart-on-failure and
startup on boot. PostgreSQL and Caddy also start on boot.

Only Caddy's TCP ports 80 and 443 are public. Next.js (3000), API (8001), MCP
tools (8000) and PostgreSQL (5432) listen on loopback. Oracle's subnet Security
List and any attached Network Security Group must permit inbound TCP 80 and
443 from `0.0.0.0/0`, with **all source ports**. The host's matching firewall
rules are persisted with `netfilter-persistent`.

`trvelle.com` is the canonical origin. DNS uses `A @ → 144.24.127.147` and
`CNAME www → trvelle.com`. Caddy automatically issues and renews trusted
certificates for both domain names. HTTP, HTTPS `www` and the legacy IP redirect
to `https://trvelle.com`, preserving paths and queries. The IP's HTTP
`/.well-known/acme-challenge/` route remains reachable for its certificate renewal.
Caddy's `default_sni` remains the public IP for legacy clients without SNI.

The versioned reverse-proxy configuration is [deploy/Caddyfile](../deploy/Caddyfile).
To apply a change, copy it to the VM, validate it with
`sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile`, then run
`sudo systemctl reload caddy`. Keep the IP certificate files in place.

## HTTPS renewal

Certbot 5.8 is installed in `/opt/trvelle-certbot`. Let's Encrypt issued a trusted
public IP certificate using the `shortlived` profile. These certificates need
frequent renewal; see the [official IP certificate instructions](https://letsencrypt.org/2026/03/11/shorter-certs-certbot).

`trvelle-certbot.timer` checks for renewal twice daily. The deploy hook at
`/etc/letsencrypt/renewal-hooks/deploy/trvelle-caddy` copies renewed certificates
to `/etc/caddy/certs` with root ownership and read access for the Caddy group,
then reloads Caddy. Do not copy private keys into source control.

```sh
ssh ubuntu@144.24.127.147
sudo systemctl list-timers trvelle-certbot.timer
sudo /opt/trvelle-certbot/bin/certbot renew --dry-run
```

## Operations

```sh
curl https://trvelle.com/api/backend/health
ssh ubuntu@144.24.127.147
sudo systemctl status trvelle-website trvelle-api trvelle-tools trvelle-worker
sudo journalctl -u trvelle-api -u trvelle-worker --since '10 minutes ago'
```

Provider keys are already configured. Password recovery is disabled until
`SMTP_URL` and `EMAIL_FROM` are configured in `/etc/trvelle/website.env` and the
website is restarted. Email/password registration, login and guest access work
without SMTP.

Personal ChatGPT/Claude model connections are separate from website login.
`TRVELLE_MODEL_SSH_HOST=ubuntu@144.24.127.147` in the backend environment makes
ChatGPT sign-in show an SSH tunnel command before opening the authorization
page. Run that command on the signing-in computer and keep it open until the
callback says connected. Reopening Connections restores the pending setup.
This workflow requires SSH access to your self-hosted VM. Tokens stay encrypted
on the backend; no callback code or token needs to be pasted into the website.
Provider API keys and the configured shared models do not require that tunnel.
Google website login remains disabled.

The ChatGPT catalog may lag actual model access. Successful requests to
GPT-6.1 Sol and GPT-6 Luna record owner-scoped access evidence and add them to
that account's model picker. Refreshing the picker does not spend inference
quota. Reauthorization to the same account preserves that evidence.

Application updates run through the repositories' GitHub Actions workflows.
The receiver creates a new release directory and transfers both current source
trees, excluding `.env*`, `.git`, `.runtime`, `.venv`, `node_modules`, `.next*`,
logs and test reports. Install locked dependencies and build Next.js on ARM64
before changing `current`. Build with the website's production environment;
keep `BETTER_AUTH_URL=https://trvelle.com` consistent with the public origin.
Back up PostgreSQL before applying migrations. Apply only the initial auth
migration for a fresh database; later additive migrations can be applied to
existing databases. Feed SQL through standard input when the `postgres` user
cannot traverse the private release directory.

Switch `current` atomically, then restart all four app services. Check the public
health endpoint and perform guest access, sign-up, sign-out and sign-in checks
over HTTPS. Model catalog checks can be mocked during UI smoke tests to avoid
consuming search/provider quotas.

To roll back the application code:

```sh
sudo ln -s /opt/trvelle/releases/20261005 /opt/trvelle/current.rollback
sudo mv -Tf /opt/trvelle/current.rollback /opt/trvelle/current
sudo systemctl restart trvelle-tools trvelle-api trvelle-worker trvelle-website
```

An application rollback does not restore a database backup. Review schema
compatibility before reverting across future destructive migrations.

## GitHub CI/CD

Both repositories contain `.github/workflows/deploy.yml`. Pushes to `main` run
tests first, then stream the tested commit's Git archive over SSH to the VM.
Pull requests run tests only. Deployments from the two repositories share a
server lock, so a frontend update cannot race a backend update.

Configure these repository Actions secrets in **both** repositories:

- `ORACLE_DEPLOY_KEY`: the dedicated SSH private key generated locally at
  `.local-tools/deploy/oracle_actions_ed25519` in the combined workspace.
- `ORACLE_KNOWN_HOSTS`: contents of `.local-tools/deploy/oracle_known_hosts`.

The matching public key on the VM is restricted to
`/usr/local/bin/trvelle-actions-deploy`, which accepts only a component name
and a 40-character commit SHA. Port forwarding and interactive shells are
disabled for this key. The receiver validates the archive, excludes runtime
files/secrets, installs locked dependencies, builds Next.js, backs up the
database, switches the release, and checks public HTTPS health. A failed
post-deployment health check restores the previous application release.
Secrets remain under `/etc/trvelle`, independent of either repository.

Inspect the **Test and deploy** workflow in GitHub Actions for build and
deployment results. The deployment step fails with setup instructions until
the secrets are configured. No GitHub runner is installed on the VM. Future
changes to the root-owned deployment receiver must be installed deliberately
over the administrator SSH connection.

## Credential hardening

See [Credential security](credential-security.md) for the encrypted systemd credential bundles, website/build service accounts, owner-bound model vault, native CLI sandbox and migration procedure. Production secrets are loaded into private runtime files rather than stored in environment files.
