.PHONY: view up down fleet reset

view:
	python3 docs/view/generate.py
	@echo "Generated docs/view/index.html"

up:
	docker compose -f platform/docker-compose.yml up -d

down:
	docker compose -f platform/docker-compose.yml down

fleet:
	@echo "TODO: spawn simulated device containers"

reset:
	docker compose -f platform/docker-compose.yml down -v --remove-orphans
