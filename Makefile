.PHONY: up down migrate revision test test-js lint format

up:
	docker compose up --build

down:
	docker compose down

migrate:
	docker compose exec app alembic upgrade head

revision:
	@test -n "$(m)" || (echo 'Usage: make revision m="message"' && exit 1)
	docker compose exec app alembic revision --autogenerate -m "$(m)"

test:
	docker compose exec app pytest $(args)

# На хосте: в контейнере app только Python. Нужен Node 22+
test-js:
	node --test "tests/js/*.test.js"

lint:
	docker compose exec app ruff check .
	docker compose exec app ruff format --check .
	docker compose exec app mypy

format:
	docker compose exec app ruff check --fix .
	docker compose exec app ruff format .
