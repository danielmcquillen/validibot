# Configuring Scheduled Tasks

Validibot uses **Celery** with **Celery Beat** for scheduled task execution. This provides a self-contained solution that works in any Docker deployment.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     Docker Compose Stack                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   Django     │  │   Worker     │  │  Beat        │          │
│  │   (Web)      │  │   (Celery)   │  │ (Scheduler)  │          │
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
- **Worker**: Celery worker processing background tasks
- **Beat**: Celery Beat scheduler triggering periodic tasks
- **Redis**: Message broker for task communication

## Scheduled Tasks

The following tasks run on schedules (configured via Django admin or data migration):

| Task | Default Schedule | Description |
|------|------------------|-------------|
| `purge_expired_submissions` | Hourly | Remove expired user submission content |
| `purge_expired_outputs` | Hourly | Remove expired validation outputs |
| `process_purge_retries` | Every 5 minutes | Retry failed purge operations |
| `cleanup_stuck_runs` | Every 10 minutes | Mark hung runs as failed |
| `cleanup_orphaned_containers` | Every 10 minutes | Remove orphaned Docker containers (Docker Compose only) |
| `cleanup_idempotency_keys` | Daily at 3 AM | Remove expired idempotency keys |
| `cleanup_callback_receipts` | Weekly (Sunday 4 AM) | Clean old callback receipts |
| `clear_sessions` | Daily at 2 AM | Remove expired Django sessions |

## Configuration

### Docker Compose Setup

The Docker Compose files include the Celery worker and Beat scheduler:

```yaml
services:
  # ... existing services ...

  celery_worker:
    # Celery worker - processes background tasks
    <<: *django
    command: ["celery", "-A", "config", "worker", "--loglevel=info"]
    ports: []
    environment:
      - APP_ROLE=celery_worker

  celery_beat:
    # Celery Beat - triggers periodic tasks
    <<: *django
    command: ["celery", "-A", "config", "beat", "--loglevel=info", "--scheduler", "django_celery_beat.schedulers:DatabaseScheduler"]
    ports: []
    environment:
      - APP_ROLE=celery_beat
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `CELERY_WORKER_CONCURRENCY` | `1` | Number of worker processes |

### Django Settings

The Celery configuration is in `config/settings/base.py`:

```python
# Celery task queue configuration
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = None  # Fire-and-forget; state in Django models
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE

# Task execution settings
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60  # 25 minutes

# Worker settings
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_CONCURRENCY = 1  # Configurable via env var

# Beat scheduler - uses database for schedule storage
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
```

## Managing Schedules

### Django Admin

The Django admin provides a UI for managing periodic tasks at `/admin/django_celery_beat/`:

- **Periodic Tasks**: View, create, edit, or disable scheduled tasks
- **Crontab Schedules**: Define cron-style schedules
- **Interval Schedules**: Define interval-based schedules (every N seconds/minutes/hours)
- **Clocked Schedules**: Define one-time execution at a specific time

### Modifying Schedules

To change when a task runs:

1. Go to Django Admin → Periodic Tasks
2. Find the task (e.g., "Purge expired submissions")
3. Click to edit
4. Change the crontab or interval schedule
5. Save

Changes take effect on the next Beat scheduler sync (typically within 5 minutes).

### Disabling Tasks

To temporarily disable a scheduled task:

1. Go to Django Admin → Periodic Tasks
2. Find the task
3. Uncheck "Enabled"
4. Save

## Monitoring

### Django Admin

View task execution history at `/admin/django_celery_beat/`:

- See registered periodic tasks
- Check when each task last ran
- Monitor enabled/disabled status

### Health Checks

```bash
# Check if worker is healthy
docker compose exec celery_worker celery -A config inspect ping

# Check active tasks
docker compose exec celery_worker celery -A config inspect active
```

### Logs

Monitor scheduler and worker logs:

```bash
# View beat scheduler logs
docker compose logs -f celery_beat

# View worker logs
docker compose logs -f celery_worker

# Expected worker output:
# [2024-01-01 12:00:00,000: INFO/MainProcess] celery@worker ready.
# [2024-01-01 12:00:05,000: INFO/MainProcess] Task validibot.purge_expired_submissions received
```

## Reliability

### Automatic Recovery

The system is designed for reliability:

1. **Container restarts**: Docker Compose `restart: unless-stopped` ensures services restart if they crash
2. **Catch-up behavior**: Management commands process all expired items, so if the scheduler was down, running tasks catches up naturally
3. **Idempotency**: All scheduled tasks are idempotent - running them multiple times is safe
4. **Late acknowledgement**: Tasks are acknowledged after completion (`acks_late=True`), preventing data loss on worker crash

### Redis Persistence

Configure Redis persistence to prevent message loss on restart:

```yaml
redis:
  image: redis:7-alpine
  command: redis-server --appendonly yes
  volumes:
    - redis_data:/data
```

### Single Beat Instance

Only run **one Beat scheduler instance**. Running multiple Beat schedulers will cause duplicate task executions. In Docker Compose, this is ensured by:

```yaml
celery_beat:
  deploy:
    replicas: 1
```

In Kubernetes, use a Deployment with `replicas: 1` or a leadership election pattern.

## Development

### Running Locally

Start the scheduler alongside other services:

```bash
# Start all services
docker compose -f docker-compose.local.yml up

# Or run specific services
docker compose -f docker-compose.local.yml up celery_worker celery_beat
```

### Manual Task Execution

You can manually trigger any scheduled task via the management command:

```bash
# Run a specific task manually
docker compose exec django python manage.py purge_expired_submissions --dry-run
docker compose exec django python manage.py cleanup_stuck_runs

# Container cleanup (Docker Compose deployments)
docker compose exec django python manage.py cleanup_containers --dry-run
docker compose exec django python manage.py cleanup_containers --all
```

Or trigger via Celery directly:

```bash
# Send a task from Django shell
docker compose exec django python manage.py shell
>>> from validibot.core.tasks.scheduled_tasks import purge_expired_submissions
>>> purge_expired_submissions.delay()
```

### Testing Scheduled Tasks

In tests, `CELERY_TASK_ALWAYS_EAGER=True` causes tasks to execute synchronously:

```python
from validibot.core.tasks.scheduled_tasks import purge_expired_submissions

# In tests, this executes immediately (no Redis needed)
result = purge_expired_submissions()
assert result["status"] == "completed"
```

## Troubleshooting

### Tasks Not Running

1. Check Beat scheduler is running: `docker compose ps celery_beat`
2. Check worker is running: `docker compose ps celery_worker`
3. Check Redis connectivity: `docker compose exec celery_worker redis-cli ping`
4. Check logs for errors: `docker compose logs celery_beat celery_worker`
5. Verify task is enabled in Django Admin

### Tasks Running Multiple Times

Only one Beat instance should run. Check for:
- Multiple Docker containers running Beat
- Kubernetes replicas > 1 for Beat deployment

### Tasks Taking Too Long

1. Adjust batch sizes via management command arguments:
   ```bash
   # Reduce batch size for slower operations
   python manage.py purge_expired_submissions --batch-size 50
   ```

2. Increase worker concurrency:
   ```yaml
   celery_worker:
     environment:
       - CELERY_WORKER_CONCURRENCY=4
   ```

### Worker Not Processing Tasks

1. Check broker connection: `docker compose exec celery_worker celery -A config inspect ping`
2. Check queue has tasks: `docker compose exec celery_worker celery -A config inspect reserved`
3. Verify CELERY_BROKER_URL is correct in settings
