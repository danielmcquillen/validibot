# Security

This document covers security configuration for Validibot's GCP infrastructure, including network security, database access, and authentication.

## Cloud SQL Networking

### How Cloud Run Connects to Cloud SQL

Cloud Run services connect to Cloud SQL using the [Cloud SQL Auth Proxy](https://cloud.google.com/sql/docs/postgres/connect-run), which is built into Cloud Run via the `--add-cloudsql-instances` flag. This provides:

- **IAM-based authentication** - Only service accounts with `roles/cloudsql.client` can connect
- **Encrypted connections** - All traffic is encrypted in transit
- **No IP allowlisting needed** - Authentication is identity-based, not network-based

The connection uses a Unix socket path (`/cloudsql/PROJECT:REGION:INSTANCE`), not a TCP connection to an IP address.

### Public IP vs Private IP

Cloud SQL instances can have:

| Option | Description | Use Case |
|--------|-------------|----------|
| **Public IP** | Instance has an internet-routable IP | Simple setup, works with Cloud SQL Auth Proxy |
| **Private IP** | Instance only accessible within a VPC | Network-level isolation, requires VPC setup |
| **PSC** | Private Service Connect | Enterprise VPC peering alternative |

**Current setup**: Our instances use Public IP with IAM authentication only. This is secure because:

1. No IP allowlisting is configured - you can't connect just by knowing the IP
2. Connections require a valid IAM identity (service account) with `roles/cloudsql.client`
3. All traffic is encrypted via the Cloud SQL Auth Proxy
4. The database password is still required after proxy authentication

### Security Considerations

**Why Public IP + IAM auth is acceptable:**

- Cloud SQL Auth Proxy provides defense in depth - attackers need both:
  1. Valid IAM credentials for a service account with Cloud SQL Client role
  2. The database username and password
- This is the [recommended approach](https://cloud.google.com/sql/docs/postgres/connect-run) for Cloud Run
- No credentials are exposed in code - IAM auth uses the service account attached to Cloud Run

**When to upgrade to Private IP:**

Consider Private IP for production if you need:

- Network-level isolation (defense in depth beyond IAM)
- Compliance requirements mandating no public endpoints
- VPC Service Controls integration

### Upgrading to Private IP (Optional)

To disable public IP entirely, you need to enable Private IP first. This requires:

1. **VPC with Private Services Access**:
   ```bash
   # Create a VPC (or use default)
   gcloud compute networks create validibot-vpc --subnet-mode=auto

   # Allocate IP range for private services
   gcloud compute addresses create google-managed-services-validibot \
       --global \
       --purpose=VPC_PEERING \
       --prefix-length=16 \
       --network=validibot-vpc

   # Create private connection
   gcloud services vpc-peerings connect \
       --service=servicenetworking.googleapis.com \
       --ranges=google-managed-services-validibot \
       --network=validibot-vpc
   ```

2. **Enable Private IP on Cloud SQL**:
   ```bash
   gcloud sql instances patch validibot-db \
       --network=projects/PROJECT/global/networks/validibot-vpc \
       --project=PROJECT_ID
   ```

3. **Create Serverless VPC Access Connector**:
   ```bash
   gcloud compute networks vpc-access connectors create validibot-connector \
       --region=us-west1 \
       --network=validibot-vpc \
       --range=10.8.0.0/28
   ```

4. **Configure Cloud Run to use VPC**:
   ```bash
   gcloud run services update validibot-web \
       --vpc-connector=validibot-connector \
       --vpc-egress=private-ranges-only
   ```

5. **Disable Public IP**:
   ```bash
   gcloud sql instances patch validibot-db --no-assign-ip
   ```

**Cost impact**: VPC Access Connector adds ~$7-10/month per connector.

### Verifying Current Configuration

Check if an instance has public IP:

```bash
gcloud sql instances describe validibot-db-dev \
    --format="value(ipAddresses)"
```

Check authorized networks (should be empty for IAM-only auth):

```bash
gcloud sql instances describe validibot-db-dev \
    --format="value(settings.ipConfiguration.authorizedNetworks)"
```

## Database Access Security

### Password Management

- Database passwords are generated randomly by `gcp-init-stage`
- Passwords are stored in Secret Manager, not in code
- Each stage has its own database user and password
- Passwords should be rotated periodically via `gcloud sql users set-password`

### Connection Security

All database connections:

1. Go through the Cloud SQL Auth Proxy (IAM authentication)
2. Use SSL/TLS encryption
3. Require the database password (application-level auth)

### Principle of Least Privilege

- Each stage has its own service account
- Service accounts only have permissions for their own stage's resources
- Database users have access only to their stage's database

## Secret Manager

Secrets are stored in Google Cloud Secret Manager with:

- Regional replication (Australia only for data residency)
- Automatic encryption at rest
- IAM-based access control
- Version history for audit

See [Secrets Management](deployment.md#secrets-management) in the deployment guide.

## Related

- [IAM & Service Accounts](iam.md) - Service account configuration
- [Deployment](deployment.md) - Deployment security settings
