.PHONY: up down migrate revision

up:
	docker compose up --build

down:
	docker compose down

migrate:
	docker compose exec app alembic upgrade head

revision:
	@test -n "$(m)" || (echo 'Usage: make revision m="message"' && exit 1)
	docker compose exec app alembic revision --autogenerate -m "$(m)"
