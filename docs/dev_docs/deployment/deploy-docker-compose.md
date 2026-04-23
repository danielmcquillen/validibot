# Deploy with Docker Compose

This is the main self-hosted production target for Validibot. Use it when you want to run Validibot on a VPS, a single cloud VM, an on-prem server, or any host you control with Docker.

For most self-hosted customers, this is the best production path.

## When to choose this target

Choose Docker Compose if you want:

- A production-style deployment on infrastructure you control
- A simpler alternative to Kubernetes
- A good fit for DigitalOcean, Hetzner, EC2, or on-prem servers
- A deployment that can stay online for real users behind a reverse proxy

Choose [Run Validibot Locally](deploy-local.md) instead if you only want to evaluate the product on your laptop.

## What this target runs

The Docker Compose production stack uses `docker-compose.production.yml` and the `just docker-compose ...` commands.

It runs:

- `web` with Gunicorn
- `worker` for background jobs and validator execution
- `scheduler` for periodic tasks
- `postgres`
- `redis`

You provide the reverse proxy yourself. See [Reverse Proxy Setup](reverse-proxy.md).

## First-time install

1. Create the production env directory:

   ```bash
   mkdir -p .envs/.production/.docker-compose
   ```

2. Copy the env templates:

   ```bash
   cp .envs.example/.production/.docker-compose/.django .envs/.production/.docker-compose/.django
   cp .envs.example/.production/.docker-compose/.postgres .envs/.production/.docker-compose/.postgres
   ```

   Also copy the `.build` file — it holds both commercial-package
   installation vars (Pro / Enterprise) and recipe-level knobs like
   `ENABLE_MCP_SERVER`. Safe to copy for any deployment; all vars
   have sensible defaults when left empty.

   ```bash
   cp .envs.example/.production/.docker-compose/.build .envs/.production/.docker-compose/.build
   ```

3. Edit both files and replace the placeholder values.

   Make sure you set:

   - `DJANGO_SECRET_KEY`
   - `DJANGO_ALLOWED_HOSTS`
   - `SITE_URL`
   - `WORKER_API_KEY`
   - `POSTGRES_PASSWORD`
   - `SUPERUSER_PASSWORD`

   If you are installing a commercial package, edit `.envs/.production/.docker-compose/.build` too:

   ```bash
   VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro==<version>
   VALIDIBOT_PRIVATE_INDEX_URL=https://<license-credentials>@pypi.validibot.com/simple/
   ```

   Use `validibot-enterprise==<version>` instead if you purchased Enterprise.
   You can also use a quoted exact wheel URL on `pypi.validibot.com` that
   includes `#sha256=<hash>` instead of a package name and version.

   Then point Django at the Pro-activating settings module by setting
   `DJANGO_SETTINGS_MODULE` in your `.envs/.production/.docker-compose/.django`:

   ```bash
   DJANGO_SETTINGS_MODULE=config.settings.production_pro
   ```

   That settings module adds `validibot_pro` to `INSTALLED_APPS`, which
   is what Django needs in order to import the package and run its
   license-registration hook. Do not edit `config/settings/base.py`
   directly — that makes future upgrades harder; the dedicated
   settings module is the supported path.

   To also include the MCP server (Pro feature, exposes validation
   workflows to AI agents), flip this in `.envs/.production/.docker-compose/.build`:

   ```bash
   ENABLE_MCP_SERVER=true
   ```

   The `just docker-compose up` / `build` recipes source the `.build`
   file at the top and activate the `mcp` Compose profile when the
   flag is truthy. The MCP server has a runtime license check that
   requires validibot-pro (or enterprise) to be installed — it refuses
   to start on community-only deployments even when the flag is set.

4. Validate the env files and bootstrap the deployment:

   ```bash
   just docker-compose check-env
   just docker-compose bootstrap
   ```

`bootstrap` is the recommended first-run command. It:

- builds and starts the stack
- waits for the web container to come up
- applies migrations
- runs `setup_validibot`
- runs `check_validibot`

## Enable signed credentials on Docker Compose

If you purchased Pro or Enterprise and want signed credentials, the simplest
self-hosted option is the local file signing backend.

Create a private signing key on the host:

```bash
mkdir -p .envs/.production/.docker-compose/keys
openssl ecparam -name prime256v1 -genkey -noout \
  -out .envs/.production/.docker-compose/keys/credential-signing.pem
chmod 600 .envs/.production/.docker-compose/keys/credential-signing.pem
```

Then add this to `.envs/.production/.docker-compose/.django`:

```bash
SIGNING_KEY_PATH=/run/validibot-keys/credential-signing.pem
CREDENTIAL_ISSUER_URL=https://validibot.example.com
```

The production compose file mounts `.envs/.production/.docker-compose/keys`
into the web and worker containers at `/run/validibot-keys`.

If you rotate the key later, existing credentials remain valid only as long as
their verifying public key is still exposed through the instance JWKS. Plan key
rotation deliberately.

## Verify the deployment

After bootstrap completes:

```bash
just docker-compose status
just docker-compose health-check
just docker-compose manage "check_validibot"
```

At this point the app is running on port `8000` on the host. For a real deployment, put it behind a reverse proxy before exposing it publicly.

## Reverse proxy and TLS

Validibot does not ship with an always-on proxy container by default. That keeps the stack compatible with self-hosters who already have Caddy, Traefik, nginx, or Cloudflare Tunnel in place.

Use one of these guides next:

- [Reverse Proxy Setup](reverse-proxy.md)
- [Deploying to DigitalOcean](digitalocean.md)

## Updates and day-two operations

Routine operations use the same `just docker-compose ...` namespace:

```bash
just docker-compose deploy
just docker-compose update
just docker-compose logs
just docker-compose backup-db
just docker-compose restore-db backups/file.sql.gz
```

`deploy` is for starting or rebuilding the stack. `update` is the safer day-two path because it takes a database backup and runs migrations as part of the update flow.

## Security and isolation notes

There are a few important production details to understand:

- The worker is the only service that gets Docker socket access for advanced validator execution.
- The reverse proxy should terminate TLS and keep internal services private.
- Secrets belong in `.envs/`, never in the repo.
- Advanced validator images should be images you built and control yourself.

For the operator responsibilities and safe-default expectations, read [Docker Compose Deployment Responsibility](docker-compose-responsibility.md).

## Good fits for this target

Docker Compose is a good fit when:

- you want to self-host on one machine
- you are comfortable managing OS updates and backups
- you do not need GCP-specific infrastructure

It is also the easiest target to run on AWS today, because the AWS-specific deployment automation is not implemented yet.

## Related guides

- [Run Validibot Locally](deploy-local.md)
- [Environment Configuration](environment-configuration.md)
- [Justfile Guide](justfile-guide.md)
- [Reverse Proxy Setup](reverse-proxy.md)
- [Deploying to DigitalOcean](digitalocean.md)
