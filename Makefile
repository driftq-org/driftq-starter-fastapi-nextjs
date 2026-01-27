.PHONY: up down logs reset

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

reset:
	docker compose down -v
	docker compose up --build
