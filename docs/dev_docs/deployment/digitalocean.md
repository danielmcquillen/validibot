# Deploying to DigitalOcean

This guide walks through deploying Validibot to a DigitalOcean Droplet using Docker Compose. By the end, you'll have a production-ready instance with automatic SSL certificates, proper security hardening, and database backups.

## Overview

We'll deploy:

- **1x Droplet** — Ubuntu 24.04 with Docker pre-installed
- **Managed PostgreSQL** (optional) — DigitalOcean's managed database, or run Postgres in Docker
- **Caddy** — Reverse proxy with automatic Let's Encrypt SSL
- **DigitalOcean Spaces** (optional) — S3-compatible object storage for files

## Sizing Your Droplet

Choosing the right Droplet size depends on whether you'll run advanced validators (EnergyPlus, FMU) or only use built-in validators (JSON Schema, XML Schema, Basic).

### Memory Requirements

The base Validibot stack requires approximately:

| Component                    | Idle Memory | Peak Memory |
| ---------------------------- | ----------- | ----------- |
| Django (Gunicorn, 2 workers) | ~150-200MB  | ~400MB      |
| Celery Worker                | ~150MB      | ~300MB      |
| Celery Beat                  | ~80MB       | ~100MB      |
| PostgreSQL                   | ~100MB      | ~300MB      |
| Redis                        | ~50MB       | ~100MB      |
| Caddy                        | ~20MB       | ~50MB       |
| OS + Docker overhead         | ~300MB      | ~400MB      |
| **Total (base stack)**       | **~850MB**  | **~1.65GB** |

Advanced validators like EnergyPlus can consume **2-4GB RAM** per simulation depending on model complexity. Running them on an undersized Droplet will OOM-kill other services.

### Recommended Droplet Sizes

| Use Case                         | Droplet       | Monthly Cost | Notes                                |
| -------------------------------- | ------------- | ------------ | ------------------------------------ |
| Built-in validators only         | 2GB / 1 vCPU  | $12          | JSON, XML, Basic validators          |
| Occasional advanced validators   | 4GB / 2 vCPU  | $24          | Add swap; may queue during heavy use |
| Regular advanced validator usage | 8GB / 4 vCPU  | $48          | Recommended for production           |
| High-volume production           | 16GB / 8 vCPU | $96          | Multiple concurrent validations      |

!!! warning "Don't undersize for advanced validators"
If you plan to run EnergyPlus or FMU validations, start with at least a 4GB Droplet. A 2GB Droplet running the base stack leaves only ~350MB headroom—not enough for even a small EnergyPlus simulation.

### Alternative: Split Architecture

For cost efficiency with occasional advanced validator usage, consider splitting your infrastructure:

**Option A: Managed Database (offload PostgreSQL)**

- **2GB Droplet** ($12/mo) — Web, Worker, Redis, Caddy
- **Managed PostgreSQL** ($15/mo) — Offloads ~300MB from Droplet
- Frees up memory for occasional validator runs

**Option B: Separate Validator Server**

- **2GB Droplet** ($12/mo) — Web app, database, Redis
- **4GB Droplet** ($24/mo) — Dedicated validator runner (can be powered off when not in use)
- Better isolation; validators can't impact web performance

**Option C: Cloud-based validators**

- **2GB Droplet** ($12/mo) — Full web stack
- Use GCP Cloud Run Jobs or AWS Batch for validators
- Pay-per-use for validations; no idle cost
- Requires additional setup (see [Execution Backends](../overview/execution_backends.md))

## Prerequisites

Before starting:

1. A [DigitalOcean account](https://www.digitalocean.com/)
2. A domain name with DNS managed by DigitalOcean (or ability to create A records)
3. SSH key added to your DigitalOcean account
4. The Validibot repository cloned locally

## Step 1: Create the Droplet

Refer to the [sizing guide above](#sizing-your-droplet) to choose the right Droplet size for your use case.

### Using the Control Panel

1. Go to **Create → Droplets**
2. Choose **Ubuntu 24.04 (LTS) x64**
3. Select the **Docker** marketplace image (includes Docker and Docker Compose)
4. Choose a plan based on your validator needs:
   - **Basic $12/mo** (1 vCPU, 2GB RAM) — built-in validators only
   - **Basic $24/mo** (2 vCPU, 4GB RAM) — occasional advanced validators
   - **Basic $48/mo** (4 vCPU, 8GB RAM) — regular advanced validator usage (recommended)
5. Choose a datacenter region close to your users
6. **Authentication**: Select your SSH key (never use password auth)
7. **Hostname**: `your-app-prod` or similar
8. Click **Create Droplet**

### Using doctl CLI

```bash
# Install doctl if needed: https://docs.digitalocean.com/reference/doctl/how-to/install/

# For built-in validators only (2GB)
doctl compute droplet create your-app-prod \
  --image docker-20-04 \
  --size s-1vcpu-2gb \
  --region nyc1 \
  --ssh-keys $(doctl compute ssh-key list --format ID --no-header | head -1) \
  --wait

# For advanced validators (8GB recommended)
doctl compute droplet create your-app-prod \
  --image docker-20-04 \
  --size s-4vcpu-8gb \
  --region nyc1 \
  --ssh-keys $(doctl compute ssh-key list --format ID --no-header | head -1) \
  --wait
```

Note the Droplet's IP address — you'll need it for DNS.

## Step 2: Configure DNS

Point your domain to the Droplet:

1. Go to **Networking → Domains** in DigitalOcean
2. Add your domain (e.g., `validibot.example.com`)
3. Create an **A record**:
   - Hostname: `@` (or subdomain like `app`)
   - Points to: Your Droplet's IP address
   - TTL: 3600

Wait a few minutes for DNS propagation. Verify with:

```bash
dig +short validibot.example.com
```

## Step 3: Initial Server Security

SSH into your Droplet:

```bash
ssh root@YOUR_DROPLET_IP
```

### Create a non-root user

```bash
# Create user
adduser validibot
usermod -aG sudo validibot
usermod -aG docker validibot

# Copy SSH keys to new user
rsync --archive --chown=validibot:validibot ~/.ssh /home/validibot
```

### Configure the firewall

DigitalOcean Droplets come with UFW installed but not enabled. However, **Docker bypasses UFW rules** by default, which can expose containers unexpectedly.

The recommended approach is to use [DigitalOcean Cloud Firewalls](https://docs.digitalocean.com/products/networking/firewalls/) instead of UFW, since they filter traffic at the network level before it reaches your Droplet.

**Create a Cloud Firewall:**

1. Go to **Networking → Firewalls → Create Firewall**
2. Name it `your-app-firewall`
3. **Inbound Rules**:
   - SSH (TCP 22) — Your IP only, or all IPv4/IPv6 if needed
   - HTTP (TCP 80) — All IPv4, All IPv6
   - HTTPS (TCP 443) — All IPv4, All IPv6
4. **Outbound Rules**: Allow all (default)
5. **Apply to Droplets**: Select your Validibot Droplet
6. Click **Create Firewall**

### Harden SSH

Edit `/etc/ssh/sshd_config`:

```bash
# Disable password authentication (SSH keys only)
PasswordAuthentication no
PermitRootLogin prohibit-password

# Optional: Change SSH port (update firewall if you do)
# Port 2222
```

Restart SSH:

```bash
systemctl restart sshd
```

### Install Fail2Ban

Fail2Ban blocks brute-force attacks:

```bash
apt update && apt install -y fail2ban
systemctl enable fail2ban
systemctl start fail2ban
```

Now log out and reconnect as the `validibot` user:

```bash
exit
ssh validibot@YOUR_DROPLET_IP
```

## Step 4: Set Up the Application

### Clone the repository

```bash
cd ~
git clone https://github.com/danielmcquillen/validibot.git
cd validibot
```

### Configure environment files

```bash
# Create the directory structure
mkdir -p .envs/.production/.docker-compose

# Copy templates
cp .envs.example/.production/.docker-compose/.django .envs/.production/.docker-compose/.django
cp .envs.example/.production/.docker-compose/.postgres .envs/.production/.docker-compose/.postgres
```

Edit `.envs/.production/.docker-compose/.django`:

```bash
nano .envs/.production/.docker-compose/.django
```

Key settings to change:

```bash
# Generate a secret key
DJANGO_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')

# Your domain
DJANGO_ALLOWED_HOSTS=validibot.example.com
SITE_URL=https://validibot.example.com

# Disable SSL redirect (Caddy handles TLS)
DJANGO_SECURE_SSL_REDIRECT=false

# Strong superuser password
SUPERUSER_PASSWORD=your-secure-password-here
SUPERUSER_EMAIL=admin@example.com
```

Edit `.envs/.production/.docker-compose/.postgres`:

```bash
nano .envs/.production/.docker-compose/.postgres
```

Generate a strong password:

```bash
# Generate and set the password
POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
```

### Set up Caddy for SSL

Create a Caddyfile:

```bash
mkdir -p compose/production/caddy
cat > compose/production/caddy/Caddyfile << 'EOF'
{$DOMAIN:localhost} {
    reverse_proxy django:5000

    # Increase timeouts for file uploads
    request_body {
        max_size 100MB
    }
}
EOF
```

Create the Caddy compose file:

```bash
cat > docker-compose.caddy.yml << 'EOF'
services:
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"
    volumes:
      - ./compose/production/caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    environment:
      - DOMAIN=${VALIDIBOT_DOMAIN:-localhost}
    networks:
      - validibot_validibot
    depends_on:
      - django

volumes:
  caddy_data:
  caddy_config:

networks:
  validibot_validibot:
    external: true
EOF
```

## Step 5: Deploy

### Start the application

```bash
# Build and start Validibot services
docker compose -f docker-compose.production.yml up -d --build

# Wait for services to be healthy
docker compose -f docker-compose.production.yml ps

# Start Caddy (after the network is created)
VALIDIBOT_DOMAIN=validibot.example.com docker compose -f docker-compose.caddy.yml up -d
```

### First-run setup

On first startup, the web container automatically runs migrations and `setup_validibot` to configure the site. This includes:

- Database migrations
- Site domain configuration (from `VALIDIBOT_SITE_DOMAIN` env var)
- Background job schedules
- Default validators and roles
- Superuser creation (if `SUPERUSER_USERNAME` is set in `.django`)

You can verify setup completed successfully:

```bash
docker compose -f docker-compose.production.yml exec web python manage.py check_validibot
```

### Verify the deployment

```bash
# Check all containers are running
docker compose -f docker-compose.production.yml ps
docker compose -f docker-compose.caddy.yml ps

# Test the health endpoint
curl -I https://validibot.example.com/health/

# Check logs if something isn't working
docker compose -f docker-compose.production.yml logs web
docker compose -f docker-compose.caddy.yml logs caddy
```

Visit `https://validibot.example.com` — you should see the Validibot login page with a valid SSL certificate.

## Step 6: Optional Enhancements

### Use DigitalOcean Managed PostgreSQL

For production workloads, consider using DigitalOcean's managed PostgreSQL instead of running it in Docker. Benefits include automatic backups, high availability options, and easier scaling.

1. **Create the database:**
   - Go to **Databases → Create Database Cluster**
   - Choose PostgreSQL 16
   - Select the same region as your Droplet
   - Basic plan ($15/mo) is sufficient for most use cases

2. **Configure trusted sources:**
   - Add your Droplet to the trusted sources list

3. **Update environment:**

   Edit `.envs/.production/.docker-compose/.postgres`:

   ```bash
   POSTGRES_HOST=your-db-cluster-hostname.db.ondigitalocean.com
   POSTGRES_PORT=25060
   POSTGRES_DB=validibot
   POSTGRES_USER=validibot
   POSTGRES_PASSWORD=your-db-password
   # For managed databases, append sslmode
   POSTGRES_OPTIONS=?sslmode=require
   ```

4. **Remove the postgres service** from `docker-compose.production.yml` or create an override file.

### Use DigitalOcean Spaces for file storage

Spaces provides S3-compatible object storage for uploaded files:

1. **Create a Space:**
   - Go to **Spaces → Create a Space**
   - Choose a region and name (e.g., `your-app-storage`)
   - Enable CDN if desired

2. **Create access keys:**
   - Go to **API → Spaces Keys → Generate New Key**

3. **Update environment:**

   Add to `.envs/.production/.docker-compose/.django`:

   ```bash
   DATA_STORAGE_BACKEND=s3
   STORAGE_BUCKET=your-app-storage
   AWS_S3_ENDPOINT_URL=https://nyc3.digitaloceanspaces.com
   AWS_S3_REGION_NAME=nyc3
   AWS_ACCESS_KEY_ID=your-spaces-key
   AWS_SECRET_ACCESS_KEY=your-spaces-secret
   ```

### Using advanced validators

Advanced validators (EnergyPlus, FMU, etc.) run as separate Docker containers spawned by the worker. To use them:

1. **Pre-pull validator images:**

   ```bash
   # Pull the validators you need
   docker pull ghcr.io/your-org/your-app-validator-energyplus:latest
   docker pull ghcr.io/your-org/your-app-validator-fmu:latest
   ```

2. **For private registries**, configure Docker credentials on the Droplet:

   ```bash
   # Log in to your private registry
   docker login ghcr.io -u USERNAME -p TOKEN
   ```

3. **Network isolation** — By default, advanced validator containers run with no network access for security. If validators need to download external files, uncomment `VALIDATOR_NETWORK` in the compose files.

4. **Verify the setup** — The compose files already configure the Docker socket mount and storage volume. See [Execution Backends](../overview/execution_backends.md) for details on registry authentication, network isolation, and naming requirements.

### Set up automated backups

If you're running PostgreSQL in Docker, set up automated backups:

```bash
# Create a backup script
cat > ~/backup-validibot.sh << 'EOF'
#!/bin/bash
set -e
BACKUP_DIR=/home/validibot/backups
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR

# Backup database
docker compose -f /home/validibot/validibot/docker-compose.production.yml exec -T postgres \
  pg_dump -U validibot validibot | gzip > $BACKUP_DIR/validibot_db_$DATE.sql.gz

# Keep only last 7 days
find $BACKUP_DIR -name "*.sql.gz" -mtime +7 -delete

echo "Backup completed: validibot_db_$DATE.sql.gz"
EOF

chmod +x ~/backup-validibot.sh

# Add to crontab (daily at 3am)
(crontab -l 2>/dev/null; echo "0 3 * * * /home/validibot/backup-validibot.sh >> /home/validibot/backup.log 2>&1") | crontab -
```

### Enable monitoring

DigitalOcean provides free monitoring for Droplets:

1. Go to your Droplet → **Graphs**
2. Click **Install the DigitalOcean Agent** if not already installed
3. Set up alerts for CPU, memory, and disk usage

## Updating the Application

To deploy updates:

```bash
cd ~/validibot

# Pull latest code
git pull origin main

# Rebuild and restart
docker compose -f docker-compose.production.yml up -d --build

# Run migrations if needed
docker compose -f docker-compose.production.yml exec web python manage.py migrate

# Check logs
docker compose -f docker-compose.production.yml logs -f --tail=100 django
```

## Troubleshooting

### Container won't start

```bash
# Check container status
docker compose -f docker-compose.production.yml ps -a

# View logs
docker compose -f docker-compose.production.yml logs web

# Common issues:
# - Database connection failed: Check POSTGRES_* env vars
# - Permission denied on docker.sock: Ensure user is in docker group
```

### SSL certificate errors

```bash
# Check Caddy logs
docker compose -f docker-compose.caddy.yml logs caddy

# Common issues:
# - DNS not propagated: Wait and retry
# - Rate limited: Let's Encrypt has limits; wait 1 hour
# - Port 80/443 blocked: Check Cloud Firewall rules
```

### Database connection refused

```bash
# For Docker Postgres
docker compose -f docker-compose.production.yml exec postgres pg_isready

# For managed database
# 1. Verify Droplet IP is in trusted sources
# 2. Check connection string and sslmode
# 3. Test with psql:
psql "postgresql://user:pass@host:port/db?sslmode=require"
```

### Out of memory

If the Droplet runs out of memory:

```bash
# Check memory usage
free -h
docker stats --no-stream

# Add swap space (temporary fix)
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# Long-term: Resize the Droplet or reduce container memory limits
```

## Cost Summary

| Configuration                      | Components                                | Monthly Cost |
| ---------------------------------- | ----------------------------------------- | ------------ |
| **Minimal (built-in validators)**  | 2GB Droplet                               | $12          |
| **Small (occasional advanced)**    | 4GB Droplet + swap                        | $24          |
| **Recommended (regular advanced)** | 8GB Droplet                               | $48          |
| **Split architecture**             | 2GB Droplet + Managed PostgreSQL          | $27          |
| **Production**                     | 8GB Droplet + Managed PostgreSQL + Spaces | $68+         |

Optional add-ons:

| Component                         | Monthly Cost |
| --------------------------------- | ------------ |
| Managed PostgreSQL (Basic)        | $15          |
| Managed PostgreSQL (with standby) | $30          |
| Spaces (250GB + CDN)              | $5           |
| Load Balancer                     | $12          |

## Next Steps

- Set up email delivery for notifications (e.g. Mailgun, SES, or SMTP relay)
- Configure [Sentry](https://sentry.io) for error tracking
- Review the [post-deployment checklist](post-deployment-verification.md)
- Set up [external monitoring](https://uptimerobot.com) for uptime alerts
