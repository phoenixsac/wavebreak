# PROFILE=lite|full  RUNTIME=container|firecracker
PROFILE ?= lite
RUNTIME ?= container
export PROFILE RUNTIME

ENV_FILE := $(if $(wildcard .env),.env,.env.example)
COMPOSE_PROFILES := $(if $(filter full,$(PROFILE)),--profile full,) $(if $(wildcard build/hawkbit-mcp/hawkbit-mcp-server.jar),--profile mcp,)
COMPOSE := docker compose --env-file $(ENV_FILE) -f platform/docker-compose.yml $(COMPOSE_PROFILES)
PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: view up down reset ps hawkbit-config bundles publish fleet fleet-down fleet-status seed lab \
        e2e test lint all

view:
	python3 docs/view/generate.py
	@echo "Generated docs/view/index.html"

up:
	@test -f .env || { cp .env.example .env; echo "Created .env from .env.example"; }
	$(COMPOSE) up -d
	./scripts/wait-http.sh http://localhost:8080/v3/api-docs 300
	./scripts/hawkbit-config.sh

down:
	$(COMPOSE) --profile full --profile mcp down

reset:
	$(COMPOSE) --profile full --profile mcp down -v --remove-orphans

ps:
	$(COMPOSE) ps

hawkbit-config:
	./scripts/hawkbit-config.sh

bundles:
	./scripts/build-bundles.sh

publish: bundles
	./scripts/publish-bundles.sh

fleet:
	$(PY) sim/fleet/fleetctl.py up --profile $(PROFILE) --runtime $(RUNTIME)

fleet-down:
	$(PY) sim/fleet/fleetctl.py down --profile $(PROFILE) --runtime $(RUNTIME)

fleet-status:
	$(PY) sim/fleet/fleetctl.py status --profile $(PROFILE) --runtime $(RUNTIME)

seed:
	./scripts/seed.sh

lab:
	$(COMPOSE) --profile lab up -d lab-controller

e2e:
	./scripts/e2e.sh

test:
	$(PY) -m pytest -q sim/ota-agent sim/inference-app wavebreak_clients $(wildcard lab/controller/tests) $(wildcard sim/fleet/tests)

lint:
	$(PY) -m ruff check .
	shellcheck scripts/*.sh scripts/host/*.sh $(wildcard sim/runtime/firecracker/*.sh) $(wildcard infra/aws/*.sh)
	$(COMPOSE) config -q

all: up publish fleet seed
