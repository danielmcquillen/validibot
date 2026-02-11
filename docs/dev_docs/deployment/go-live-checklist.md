# Go-Live Checklist

This checklist covers tasks to complete before launching Validibot to production on Google Cloud.

## Pre-Launch Tasks

### Infrastructure Upgrades

> Note: `just gcp init-stage prod` currently provisions the smallest shared-core tier (`db-f1-micro`). Before serving real traffic, bump the prod instance to a larger tier as outlined below.

- [ ] **Upgrade Cloud SQL instance from `db-f1-micro` to `db-g1-small`**

  The `db-f1-micro` tier (0.6 GB RAM) is suitable for development but not production. Upgrade to `db-g1-small` (1.7 GB RAM) before go-live.

  ```bash
  gcloud sql instances patch validibot-db \
    --tier=db-g1-small
  ```

  **Important**: This requires a restart (~1-2 minutes downtime). Can be done on a live database, but schedule during a maintenance window.

  **Cost impact**: ~$7/month → ~$25/month

- [ ] **Consider High Availability (HA)** for production

  HA provides automatic failover with a standby replica. Doubles cost but provides SLA coverage.

  ```bash
  gcloud sql instances patch validibot-db \
    --availability-type=regional
  ```

  **Cost impact**: ~$25/month → ~$50/month (with HA)

### Security

- [ ] **Remove public IP from Cloud SQL** (if using Cloud SQL Auth Proxy or Private IP)

  ```bash
  gcloud sql instances patch validibot-db \
    --no-assign-ip
  ```

  *Note:* Current deployments use the Cloud SQL Auth Proxy (`--add-cloudsql-instances`), which authenticates via IAM and encrypts traffic. That is acceptable for dev/staging and many prod setups. If your production policy requires no public IP, you’ll need to add Private IP + Serverless VPC Access and point Cloud Run at the connector before disabling the public IP.

- [ ] **Review Secret Manager access** - Ensure only production service accounts have access

- [ ] **Enable Cloud Audit Logs** for security monitoring

- [ ] **Review IAM roles** - Apply principle of least privilege

- [ ] **Set up Cloud Armor** (optional) - WAF/DDoS protection for Cloud Run

### Monitoring & Alerting

- [ ] **Set up Cloud Monitoring alerts**:

  - Database CPU/memory usage > 80%
  - Database connections near limit
  - Cloud Run error rate spikes
  - Cloud Run latency increases

- [ ] **Configure uptime checks** in Cloud Monitoring

- [ ] **Review Sentry configuration** for production error tracking

- [ ] **Set up log-based alerts** for critical errors

### Performance

- [ ] **Review Cloud Run scaling settings**:

  - Minimum instances (consider 1 for faster cold starts)
  - Maximum instances
  - CPU/memory allocation
  - Concurrency settings

- [ ] **Enable Cloud CDN** for static assets (if not using WhiteNoise)

- [ ] **Review database connection pooling** settings

### Data & Backups

- [ ] **Verify automated backups** are configured and tested

  ```bash
  gcloud sql instances describe validibot-db --format="value(settings.backupConfiguration)"
  ```

- [ ] **Test backup restoration** procedure

- [ ] **Document data recovery procedures**

- [ ] **Configure backup retention** (default is 7 days, consider longer)

  ```bash
  gcloud sql instances patch validibot-db \
    --retained-backups-count=14
  ```

### DNS & SSL

Choose one of the two approaches below. See the [deployment guide](../google_cloud/deployment.md#custom-domain-setup) for full details.

**Option A: Cloud Run Domain Mappings** (simpler, but only available in [certain regions](https://cloud.google.com/run/docs/mapping-custom-domains))

- [ ] **Create a domain mapping**

  Available in `us-west1`, `us-central1`, `us-east1`, `europe-west1`, and a few others. **Not** available in `australia-southeast1`.

  ```bash
  gcloud beta run domain-mappings create \
    --service validibot-web \
    --domain validibot.com \
    --region $GCP_REGION \
    --project $GCP_PROJECT_ID
  ```

- [ ] **Add DNS records** shown in the command output to your DNS provider.

**Option B: Global Load Balancer** (works in all regions, recommended for production)

- [ ] **Set up the load balancer for `validibot.com`**

  Required for regions without domain mapping support (e.g. `australia-southeast1`). Provides a static IP, CDN integration, and full SSL control.

  ```bash
  just gcp lb-setup prod validibot.com
  ```

- [ ] **Point your DNS at the load balancer IP**

  Create an `A` record for `validibot.com` (host `@`) pointing to the IP printed by `lb-setup`.

- [ ] **Verify the Google-managed SSL cert becomes active**

  This can take 15-60 minutes after DNS propagates.

  ```bash
  gcloud compute ssl-certificates describe validibot-cert --global
  ```

- [ ] **Lock down direct `*.run.app` access (optional)**

  Once the domain works, restrict the Cloud Run web service so only the load balancer can reach it:

  ```bash
  gcloud run services update validibot-web \
    --ingress internal-and-cloud-load-balancing \
    --region us-west1
  ```

### Application

- [ ] **Verify all environment variables** are set in production

- [ ] **Set `SITE_URL` and `WORKER_URL` appropriately**

  - `SITE_URL` should be your public domain (e.g., `https://validibot.com`).
  - `WORKER_URL` should be the worker Cloud Run service `*.run.app` URL (used for internal callbacks and scheduled tasks).

- [ ] **Run Django's `check --deploy`** to identify security issues

  ```bash
  python manage.py check --deploy
  ```

- [ ] **Run Validibot's health check** to verify all components

  ```bash
  python manage.py check_validibot --verbose
  ```

  This checks database, migrations, cache, storage, site configuration, roles/permissions, validators, background tasks, Docker, email, and security settings. See [Post-Deployment Verification](./post-deployment-verification.md) for details.

- [ ] **Verify `DEBUG=False`** in production

- [ ] **Run database migrations** in production

- [ ] **Create superuser** for admin access

- [ ] **Verify static files** are served correctly

- [ ] **Test email sending** (Postmark integration)

### External Services

- [ ] **Verify Modal.com integration** for EnergyPlus workers

- [ ] **Test Cloud Tasks** integration for async jobs

- [ ] **Verify GCS media storage** is working

- [ ] **Test GitHub App integration** (if applicable)

## Post-Launch

- [ ] **Run post-deployment verification**

  ```bash
  just verify-deployment prod
  ```

  This runs smoke tests to verify critical functionality. See [Post-Deployment Verification](./post-deployment-verification.md) for details.

- [ ] **Run health check and address any warnings**

  ```bash
  python manage.py check_validibot --verbose
  ```

  Use `--fix` to automatically resolve fixable issues (like missing default roles).

- [ ] **Monitor error rates** for first 24 hours

- [ ] **Monitor database performance** - watch for slow queries

- [ ] **Verify all scheduled tasks** are running (Cloud Scheduler → see [scheduled-jobs.md](../google_cloud/scheduled-jobs.md))

- [ ] **Test user registration and login flow**

- [ ] **Verify billing alerts** are set up in GCP

---

## Quick Reference: Database Tier Comparison

| Tier               | RAM     | Monthly Cost | SLA    | Use Case            |
| ------------------ | ------- | ------------ | ------ | ------------------- |
| `db-f1-micro`      | 0.6 GB  | ~$10         | ❌ No  | Development only    |
| `db-g1-small`      | 1.7 GB  | ~$25         | ❌ No  | Small production    |
| `db-custom-1-3840` | 3.75 GB | ~$45         | ✅ Yes | Production with SLA |
| `db-custom-2-7680` | 7.5 GB  | ~$90         | ✅ Yes | Larger production   |

**Note**: Shared-core instances (`db-f1-micro`, `db-g1-small`) are NOT covered by the Cloud SQL SLA. For mission-critical workloads, use dedicated-core instances.
