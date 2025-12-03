.PHONY: help up down logs build ps clean restart shell migrate \
        gcp-deploy gcp-build gcp-push gcp-secrets gcp-logs gcp-setup gcp-pause gcp-resume gcp-shell

# GCP Configuration
GCP_PROJECT := project-a509c806-3e21-4fbc-b19
GCP_REGION := australia-southeast1
GCP_IMAGE := australia-southeast1-docker.pkg.dev/$(GCP_PROJECT)/validibot/validibot-web
GCP_SERVICE := validibot-web
GCP_SA := validibot-cloudrun-prod@$(GCP_PROJECT).iam.gserviceaccount.com
GCP_SQL := $(GCP_PROJECT):$(GCP_REGION):validibot-db

# Default target - show help
help:
	@echo "Local Docker Development Commands:"
	@echo "  make up       - Start all containers"
	@echo "  make down     - Stop all containers"
	@echo "  make build    - Rebuild and start containers"
	@echo "  make logs     - Follow logs from all containers"
	@echo "  make ps       - Show container status"
	@echo "  make restart  - Stop and start containers"
	@echo "  make clean    - Stop containers and remove volumes (loses data!)"
	@echo "  make shell    - Open a shell in the Django container"
	@echo "  make migrate  - Run Django migrations"
	@echo ""
	@echo "GCP Cloud Run Commands:"
	@echo "  make gcp-build    - Build Docker image for Cloud Run"
	@echo "  make gcp-push     - Push image to Artifact Registry"
	@echo "  make gcp-deploy   - Deploy to Cloud Run (builds and pushes first)"
	@echo "  make gcp-secrets  - Upload .envs/.production/.django to Secret Manager"
	@echo "  make gcp-logs     - View recent Cloud Run logs"
	@echo "  make gcp-setup    - Run setup_all management command"
	@echo "  make gcp-shell CMD='...' - Run a management command on Cloud Run"
	@echo "  make gcp-pause    - Block public access to Cloud Run"
	@echo "  make gcp-resume   - Restore public access to Cloud Run"

up:
	docker compose -f docker-compose.local.yml up -d

down:
	docker compose -f docker-compose.local.yml down

build:
	docker compose -f docker-compose.local.yml up -d --build

logs:
	docker compose -f docker-compose.local.yml logs -f

ps:
	docker compose -f docker-compose.local.yml ps

restart: down up

clean:
	docker compose -f docker-compose.local.yml down -v

shell:
	docker compose -f docker-compose.local.yml exec django bash

migrate:
	docker compose -f docker-compose.local.yml exec django python manage.py migrate

# =============================================================================
# GCP Cloud Run Commands
# =============================================================================

# Get current git short hash for image tagging
GIT_SHA := $(shell git rev-parse --short HEAD)

gcp-build:
	docker build --platform linux/amd64 \
		-f compose/production/django/Dockerfile \
		-t $(GCP_IMAGE):$(GIT_SHA) \
		-t $(GCP_IMAGE):latest .

gcp-push:
	docker push $(GCP_IMAGE):$(GIT_SHA)
	docker push $(GCP_IMAGE):latest

gcp-deploy: gcp-build gcp-push
	gcloud run deploy $(GCP_SERVICE) \
		--image $(GCP_IMAGE):$(GIT_SHA) \
		--region $(GCP_REGION) \
		--service-account $(GCP_SA) \
		--add-cloudsql-instances $(GCP_SQL) \
		--set-secrets=/secrets/.env=django-env:latest \
		--min-instances 0 \
		--max-instances 4 \
		--memory 1Gi \
		--allow-unauthenticated \
		--project $(GCP_PROJECT)

gcp-secrets:
	gcloud secrets versions add django-env \
		--data-file=.envs/.production/.django \
		--project $(GCP_PROJECT)
	@echo "Secret updated. Run 'make gcp-deploy' to apply changes."

gcp-logs:
	gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$(GCP_SERVICE)" \
		--project $(GCP_PROJECT) \
		--limit 50 \
		--format="table(timestamp,severity,textPayload)"

gcp-setup:
	gcloud run jobs update validibot-setup \
		--region $(GCP_REGION) \
		--image $(GCP_IMAGE):latest \
		--args="-c,set -a && source /secrets/.env && set +a && python manage.py setup_all" \
		--project $(GCP_PROJECT)
	gcloud run jobs execute validibot-setup --region $(GCP_REGION) --wait --project $(GCP_PROJECT)

gcp-shell:
ifndef CMD
	$(error Usage: make gcp-shell CMD='your_command args')
endif
	gcloud run jobs update validibot-setup \
		--region $(GCP_REGION) \
		--args="-c,set -a && source /secrets/.env && set +a && python manage.py $(CMD)" \
		--project $(GCP_PROJECT)
	gcloud run jobs execute validibot-setup --region $(GCP_REGION) --wait --project $(GCP_PROJECT)

gcp-pause:
	gcloud run services update $(GCP_SERVICE) \
		--region $(GCP_REGION) \
		--ingress internal \
		--project $(GCP_PROJECT)
	@echo "Service paused. Public access blocked."

gcp-resume:
	gcloud run services update $(GCP_SERVICE) \
		--region $(GCP_REGION) \
		--ingress all \
		--project $(GCP_PROJECT)
	@echo "Service resumed. Public access restored."
