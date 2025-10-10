# Heroku Deployment

This guide assumes you deploy from the `main` branch to a single Heroku app that
hosts the web process, Celery worker, and Celery beat. Adjust the commands if you
operate multiple environments.

## Prerequisites

- Heroku CLI (`brew tap heroku/brew && brew install heroku`)
- Logged in via `heroku login`
- Access to the production Heroku app
- A configured AWS S3 bucket for media uploads
- Postmark (or SMTP) credentials for transactional mail

## One-Time App Bootstrap

```bash
# Create the app if it does not exist
heroku create simplevalidations --team your-team-name

# Ensure the Python buildpack is first
heroku buildpacks:set heroku/python -a simplevalidations

# Attach add-ons
heroku addons:create heroku-postgresql:standard-0 -a simplevalidations
heroku addons:create heroku-redis:premium-0 -a simplevalidations
```

Add Papertrail (or your preferred log drain) for long-term log retention.

## Configuration

Use `_envs/production/django.env` as the source of truth. Set the values with:

```bash
heroku config:set $(cat _envs/production/django.env | xargs) -a simplevalidations
```

Double-check the high-risk entries:

- `DJANGO_SECRET_KEY`
- `DJANGO_ALLOWED_HOSTS=simplevalidations.com,*.simplevalidations.com`
- `DJANGO_SECURE_SSL_REDIRECT=true`
- `DJANGO_AWS_*` (bucket name, access keys, optional custom domain)
- `POSTMARK_SERVER_TOKEN`
- `SENTRY_DSN`

Heroku sets `DATABASE_URL` and `REDIS_URL` automatically when you add the add-ons.

### Procfile

```
release: python manage.py migrate
web: gunicorn config.asgi:application -k uvicorn_worker.UvicornWorker
worker: REMAP_SIGTERM=SIGQUIT celery -A config.celery_app worker --loglevel=info
beat: REMAP_SIGTERM=SIGQUIT celery -A config.celery_app beat --loglevel=info
```

The `release` phase runs migrations automatically. For the first deploy (or after
database resets) run:

```bash
heroku run python manage.py setup_all -a simplevalidations
```

`setup_all` seeds role definitions, creates personal workspaces, and ensures the AI
Assist validator exists.

## Deploying

### Via GitHub integration

1. Connect the repo to the Heroku app.
2. Enable automatic deploys from `main` or trigger a manual deploy.

### Via CLI

```bash
git push heroku main
```

The slug build runs `bin/post_compile`, which executes `collectstatic` and
`compilemessages`. After the release stage finishes, Heroku restarts all dynos.

## Post-Deploy Verification

1. `heroku logs --tail -p web -a simplevalidations` – confirm startup is clean.
2. `heroku logs --tail -p worker -a simplevalidations` – watch Celery queue health.
3. Visit the site root and the `/accounts/login/` endpoint to ensure static/media
   assets load.
4. Trigger a waitlist signup to verify outbound email (Postmark).
5. Check Sentry for new errors.

## Routine Operations

- **Scale dynos**

  ```bash
  heroku ps:scale web=2 worker=2 beat=1 -a simplevalidations
  ```

- **Tail logs**

  ```bash
  heroku logs --tail -p web -a simplevalidations       # ASGI server
  heroku logs --tail -p worker -a simplevalidations    # Celery worker
  heroku logs --tail -p beat -a simplevalidations      # Celery beat
  ```

- **Run management commands**

  ```bash
  heroku run python manage.py <command> -a simplevalidations
  ```

- **Database backups**

  Enable Heroku Postgres automatic backups (`pg:backups schedule`). To restore a
  backup into staging:

  ```bash
  heroku pg:backups:restore b001 DATABASE_URL -a simplevalidations-staging
  ```

## Rebuilding the Database (Development Only)

Never reset production data. For disposable environments you can do:

```bash
heroku pg:reset DATABASE_URL --confirm simplevalidations-dev
heroku run python manage.py migrate -a simplevalidations-dev
heroku run python manage.py setup_all -a simplevalidations-dev
```

After a reset, re-run `setup_all` to repopulate roles, workspaces, and validators.

## Troubleshooting

- **`DisallowedHost` errors**: confirm `DJANGO_ALLOWED_HOSTS` includes the canonical
  domain and any subdomains you serve.
- **Static 500 page**: check `heroku logs -p web`; missing S3 credentials often cause
  failures during `collectstatic`.
- **Celery deadlocks**: ensure the worker and beat dynos both have access to
  `REDIS_URL` and that the add-on has not exhausted its connection quota.

See the [Deployment Overview](overview.md) for the broader release checklist.
