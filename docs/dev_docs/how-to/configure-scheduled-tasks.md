# Configuring Scheduled Tasks

Validibot uses **Dramatiq** with **periodiq** for scheduled task execution. This replaces the previous GCS Cloud Scheduler approach and provides a self-contained solution that works in any Docker deployment.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     Docker Compose Stack                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   Django     │  │   Worker     │  │  Scheduler   │          │
│  │   (Web)      │  │  (Dramatiq)  │  │  (periodiq)  │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│         │                 │                  │                   │
│         └────────────┬────┴──────────────────┘                  │
│                      │                                           │
│               ┌──────┴──────┐                                   │
│               │    Redis    │                                   │
│               │   (Broker)  │                                   │
│               └─────────────┘                                   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Components

- **Web**: Django application serving HTTP requests
- **Worker**: Dramatiq worker processing background tasks
- **Scheduler**: periodiq scheduler triggering periodic tasks
- **Redis**: Message broker for task communication

## Scheduled Tasks

The following tasks run on schedules:

| Task | Schedule | Description |
|------|----------|-------------|
| `purge_expired_submissions` | Hourly | Remove expired user submission content |
| `purge_expired_outputs` | Hourly | Remove expired validation outputs |
| `process_purge_retries` | Every 5 minutes | Retry failed purge operations |
| `cleanup_stuck_runs` | Every 10 minutes | Mark hung runs as failed |
| `cleanup_orphaned_containers` | Every 10 minutes | Remove orphaned Docker containers (self-hosted only) |
| `cleanup_idempotency_keys` | Daily at 3 AM | Remove expired idempotency keys |
| `cleanup_callback_receipts` | Weekly (Sunday 4 AM) | Clean old callback receipts |
| `clearsessions` | Daily at 2 AM | Remove expired Django sessions |

## Configuration

### Docker Compose Setup

Add the scheduler service to your `docker-compose.yml`:

```yaml
services:
  # ... existing services ...

  scheduler:
    # Periodiq scheduler - triggers periodic tasks
    <<: *django  # Inherits from django service
    command: ["/start-scheduler"]
    ports: []  # No ports needed
    environment:
      - APP_ROLE=scheduler
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `DRAMATIQ_BROKER` | `redis` | Broker type (redis) |

### Django Settings

The Dramatiq configuration is in `config/settings/base.py`:

```python
# Dramatiq task queue configuration
DRAMATIQ_BROKER = {
    "BROKER": "dramatiq.brokers.redis.RedisBroker",
    "OPTIONS": {"url": REDIS_URL},
    "MIDDLEWARE": [
        "dramatiq.middleware.AgeLimit",
        "dramatiq.middleware.TimeLimit",
        "dramatiq.middleware.Retries",
        "django_dramatiq.middleware.DbConnectionsMiddleware",
        "django_dramatiq.middleware.AdminMiddleware",
    ],
}

DRAMATIQ_RESULT_BACKEND = {
    "BACKEND": "dramatiq.results.backends.redis.RedisBackend",
    "BACKEND_OPTIONS": {"url": REDIS_URL},
    "MIDDLEWARE_OPTIONS": {"result_ttl": 60000},  # 1 minute
}

# Admin monitoring options
DRAMATIQ_TASKS_DATABASE = "default"
```

## Monitoring

### Django Admin

The Django admin provides visibility into scheduled tasks at `/admin/django_dramatiq/task/`:

- View recent task executions
- See task status (pending, running, completed, failed)
- Check task arguments and results
- Monitor execution times

### Health Checks

The scheduler exposes a health check endpoint when running:

```bash
# Check if scheduler is healthy
curl http://scheduler:8002/health/
```

### Logs

Monitor scheduler logs to verify tasks are running:

```bash
# View scheduler logs
docker compose logs -f scheduler

# Expected output:
# periodiq: Registered periodic task purge_expired_submissions (cron: 0 * * * *)
# periodiq: Registered periodic task purge_expired_outputs (cron: 0 * * * *)
# ...
```

## Reliability

### Automatic Recovery

The scheduler is designed for reliability:

1. **Container restarts**: Docker Compose `restart: unless-stopped` ensures the scheduler restarts if it crashes
2. **Catch-up behavior**: Management commands process all expired items, so if the scheduler was down, running tasks catches up naturally
3. **Idempotency**: All scheduled tasks are idempotent - running them multiple times is safe

### Redis Persistence

Configure Redis persistence to prevent message loss on restart:

```yaml
redis:
  image: redis:7-alpine
  command: redis-server --appendonly yes
  volumes:
    - redis_data:/data
```

### Multiple Schedulers

Only run **one scheduler instance**. Running multiple schedulers will cause duplicate task executions. In Kubernetes, use a Deployment with `replicas: 1`.

## Development

### Running Locally

Start the scheduler alongside other services:

```bash
# Start all services
docker compose -f docker-compose.local.yml up

# Or run scheduler separately
docker compose -f docker-compose.local.yml up scheduler
```

### Manual Task Execution

You can manually trigger any scheduled task via the management command:

```bash
# Run a specific task manually
docker compose exec django python manage.py purge_expired_submissions --dry-run
docker compose exec django python manage.py cleanup_stuck_runs

# Container cleanup (self-hosted deployments)
docker compose exec django python manage.py cleanup_containers --dry-run
docker compose exec django python manage.py cleanup_containers --all
```

### Testing Scheduled Tasks

Tasks can be tested in isolation:

```python
from validibot.core.tasks import scheduled_tasks

# Test task function directly
scheduled_tasks.purge_expired_submissions()
```

## Migration from Cloud Scheduler

If migrating from GCS Cloud Scheduler:

1. Add the `scheduler` service to docker-compose.yml
2. Deploy the updated stack
3. Disable Cloud Scheduler jobs in GCP Console
4. Delete the scheduled task API endpoints (no longer needed)

The scheduled tasks are now self-contained and don't require external scheduling infrastructure.

## Troubleshooting

### Tasks Not Running

1. Check scheduler is running: `docker compose ps scheduler`
2. Check Redis connectivity: `docker compose exec scheduler redis-cli ping`
3. Check scheduler logs for errors: `docker compose logs scheduler`

### Tasks Running Multiple Times

Only one scheduler instance should run. Check for:
- Multiple Docker containers running the scheduler
- Kubernetes replicas > 1

### Tasks Taking Too Long

Adjust batch sizes and timeouts in task definitions or via management command arguments:

```bash
# Reduce batch size for slower operations
python manage.py purge_expired_submissions --batch-size 50
```
