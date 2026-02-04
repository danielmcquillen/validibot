# Reverse Proxy Setup

Validibot does not include a reverse proxy in the default Docker Compose configuration. You'll need to set up your own reverse proxy to handle TLS termination and route traffic to the application.

This follows the same approach as other self-hosted projects like [Sentry](https://develop.sentry.dev/self-hosted/production-enhancements/reverse-proxy/), [PostHog](https://posthog.com/docs/self-host/configure/running-behind-proxy), [Paperless-ngx](https://docs.paperless-ngx.com/setup/), and [Immich](https://immich.app/docs/administration/reverse-proxy).

## Why no built-in proxy?

Most self-hosters already have a reverse proxy in their infrastructure (Traefik for other services, nginx on the host, Cloudflare Tunnel, etc.). Including one by default would conflict with existing setups and add complexity for users who don't need it.

## Before you start

Make sure Validibot is running and accessible on its default port:

```bash
docker compose -f docker-compose.self-hosted.yml up -d
curl http://localhost:8000/health/
```

You should see a successful health check response before configuring the proxy.

## Configuration requirements

Whichever proxy you choose, ensure:

1. **TLS termination** — The proxy handles HTTPS; Validibot receives plain HTTP
2. **Proper headers** — Forward `X-Forwarded-For`, `X-Forwarded-Proto`, and `Host`
3. **WebSocket support** — Required for real-time updates (if using HTMx WebSocket extensions)
4. **Timeout settings** — Increase timeouts for long-running validation uploads

Update your Validibot environment variables:

```bash
# .envs/.production/.self-hosted/.django
DJANGO_ALLOWED_HOSTS=validibot.example.com
SITE_URL=https://validibot.example.com
DJANGO_SECURE_SSL_REDIRECT=False  # Proxy handles TLS
```

---

## Caddy

[Caddy](https://caddyserver.com/) automatically obtains and renews TLS certificates via Let's Encrypt. It's the simplest option for most deployments.

### Standalone (on host)

Install Caddy on your host system:

```bash
# Debian/Ubuntu
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy
```

Create `/etc/caddy/Caddyfile`:

```caddyfile
validibot.example.com {
    reverse_proxy localhost:8000
}
```

Start Caddy:

```bash
sudo systemctl enable --now caddy
```

That's it. Caddy will automatically obtain a certificate and start serving HTTPS.

### Docker container

Add Caddy as a service alongside Validibot. Create `compose/production/caddy/Caddyfile`:

```caddyfile
{$DOMAIN:localhost} {
    reverse_proxy django:5000
}
```

Create `docker-compose.caddy.yml`:

```yaml
services:
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
      - "443:443/udp"  # HTTP/3
    volumes:
      - ./compose/production/caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    environment:
      - DOMAIN=${VALIDIBOT_DOMAIN:-localhost}
    networks:
      - validibot

volumes:
  caddy_data:
  caddy_config:

networks:
  validibot:
    external: true
    name: validibot_validibot
```

Run both compose files:

```bash
# Start Validibot
docker compose -f docker-compose.self-hosted.yml up -d

# Start Caddy (after the validibot network exists)
VALIDIBOT_DOMAIN=validibot.example.com docker compose -f docker-compose.caddy.yml up -d
```

> **Important:** The `caddy_data` volume stores certificates. Without persistence, Caddy requests new certificates on every restart, which can hit Let's Encrypt rate limits.

---

## Traefik

[Traefik](https://traefik.io/) integrates natively with Docker and discovers services via labels. It's a good choice if you're already using Traefik for other services.

### Docker labels approach

The self-hosted compose file includes a commented Traefik example. To enable it:

1. Uncomment the Traefik service in `docker-compose.self-hosted.yml`
2. Add labels to the Django service

Or create a separate `docker-compose.traefik.yml`:

```yaml
services:
  traefik:
    image: traefik:v3.0
    restart: unless-stopped
    command:
      - "--api.dashboard=true"
      - "--providers.docker=true"
      - "--providers.docker.exposedbydefault=false"
      - "--entrypoints.web.address=:80"
      - "--entrypoints.websecure.address=:443"
      - "--certificatesresolvers.letsencrypt.acme.httpchallenge=true"
      - "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web"
      - "--certificatesresolvers.letsencrypt.acme.email=${ACME_EMAIL}"
      - "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json"
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - traefik_letsencrypt:/letsencrypt
    networks:
      - validibot

volumes:
  traefik_letsencrypt:

networks:
  validibot:
    external: true
    name: validibot_validibot
```

Add labels to the Django service in `docker-compose.self-hosted.yml`:

```yaml
services:
  django:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.validibot.rule=Host(`validibot.example.com`)"
      - "traefik.http.routers.validibot.entrypoints=websecure"
      - "traefik.http.routers.validibot.tls.certresolver=letsencrypt"
      - "traefik.http.services.validibot.loadbalancer.server.port=5000"
    # Remove or comment out the ports mapping
    # ports:
    #   - "8000:5000"
```

---

## nginx

[nginx](https://nginx.org/) is the most widely deployed reverse proxy. Use it if you're already familiar with nginx or need advanced configuration options.

### Standalone (on host)

Install nginx:

```bash
# Debian/Ubuntu
sudo apt install nginx
```

Create `/etc/nginx/sites-available/validibot`:

```nginx
server {
    listen 80;
    server_name validibot.example.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name validibot.example.com;

    # Certificates (use certbot or your own)
    ssl_certificate /etc/letsencrypt/live/validibot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/validibot.example.com/privkey.pem;

    # SSL settings
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;
    ssl_prefer_server_ciphers off;

    # Proxy settings
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Increase timeouts for large file uploads
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        client_max_body_size 100M;
    }

    # WebSocket support (if needed)
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

Enable the site and obtain certificates:

```bash
sudo ln -s /etc/nginx/sites-available/validibot /etc/nginx/sites-enabled/
sudo certbot --nginx -d validibot.example.com
sudo systemctl reload nginx
```

### Docker container

Create `docker-compose.nginx.yml`:

```yaml
services:
  nginx:
    image: nginx:alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./compose/production/nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./compose/production/nginx/certs:/etc/nginx/certs:ro
    networks:
      - validibot

networks:
  validibot:
    external: true
    name: validibot_validibot
```

You'll need to manage certificates separately (e.g., with certbot on the host, or a sidecar container).

---

## Cloudflare Tunnel

[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) is ideal for home servers or environments where you can't open ports. Traffic routes through Cloudflare's network, so you get DDoS protection and don't need to expose your server directly.

1. Create a tunnel in the Cloudflare Zero Trust dashboard
2. Install `cloudflared` on your host or run it as a container
3. Configure the tunnel to route to `http://localhost:8000`

```yaml
# docker-compose.cloudflared.yml
services:
  cloudflared:
    image: cloudflare/cloudflared:latest
    restart: unless-stopped
    command: tunnel run
    environment:
      - TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}
    networks:
      - validibot

networks:
  validibot:
    external: true
    name: validibot_validibot
```

---

## Verifying the setup

After configuring your proxy:

1. **Check HTTPS access:**
   ```bash
   curl -I https://validibot.example.com/health/
   ```

2. **Verify headers are forwarded:**
   ```bash
   # Should show your real IP, not 127.0.0.1
   curl https://validibot.example.com/api/v1/auth/debug/
   ```

3. **Test file uploads:**
   Submit a test validation to ensure large files work with your timeout settings.

## Troubleshooting

### CSRF verification failed

If you see "CSRF verification failed" errors after adding a proxy:

1. Ensure `SITE_URL` matches your public domain exactly
2. Check that `X-Forwarded-Proto: https` is being sent
3. Verify `DJANGO_ALLOWED_HOSTS` includes your domain

### 502 Bad Gateway

The proxy can't reach Validibot:

1. Check Validibot is running: `docker compose ps`
2. Verify the network connection: `docker network inspect validibot_validibot`
3. Ensure the proxy is targeting the correct service name and port (`django:5000`)

### Certificate errors

- **Caddy:** Check the `caddy_data` volume persists between restarts
- **Traefik:** Verify your ACME email is set and DNS points to your server
- **nginx:** Run `certbot renew --dry-run` to test renewal
