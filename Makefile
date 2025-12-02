.PHONY: help up down logs build ps clean restart shell migrate

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
