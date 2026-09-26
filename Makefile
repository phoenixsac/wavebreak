# PROFILE=lite|full  RUNTIME=container|firecracker
PROFILE ?= lite
RUNTIME ?= container
HAWKBIT_UI ?= 0
export PROFILE RUNTIME PY

ENV_FILE := $(if $(wildcard .env),.env,.env.example)
LAB_CONTROLLER_PORT ?= $(shell sed -n 's/^LAB_CONTROLLER_PORT=//p' $(ENV_FILE) | head -1)
LAB_CONTROLLER_PORT := $(if $(strip $(LAB_CONTROLLER_PORT)),$(LAB_CONTROLLER_PORT),8090)
HAWKBIT_UI_PORT ?= $(shell sed -n 's/^HAWKBIT_UI_PORT=//p' $(ENV_FILE) | head -1)
HAWKBIT_UI_PORT := $(if $(strip $(HAWKBIT_UI_PORT)),$(HAWKBIT_UI_PORT),8081)
COMPOSE_PROFILES := $(if $(filter full,$(PROFILE)),--profile full,) $(if $(filter 1,$(HAWKBIT_UI)),--profile hawkbit-ui,) $(if $(wildcard build/hawkbit-mcp/hawkbit-mcp-server.jar),--profile mcp,)
COMPOSE := docker compose --env-file $(ENV_FILE) -f platform/docker-compose.yml $(COMPOSE_PROFILES)
PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: view up down reset ps hawkbit-config bundles publish fleet fleet-down fleet-status seed lab demo-reset demo-status grafana-sa \
        e2e test lint all

view:
	python3 docs/view/generate.py
	@echo "Generated docs/view/index.html"

up:
	@test -f .env || { cp .env.example .env; echo "Created .env from .env.example"; }
	$(COMPOSE) up -d
	./scripts/wait-http.sh http://localhost:8080/v3/api-docs 300
	./scripts/hawkbit-config.sh
	@if [ "$(PROFILE)" = full ] || [ "$(HAWKBIT_UI)" = 1 ]; then \
	  ./scripts/wait-http.sh http://localhost:$(HAWKBIT_UI_PORT)/ 180; \
	fi

down:
	$(COMPOSE) --profile full --profile mcp --profile lab --profile hawkbit-ui down

reset:
	$(COMPOSE) --profile full --profile mcp --profile lab --profile hawkbit-ui down -v --remove-orphans

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
	@docker image inspect wavebreak-device:local >/dev/null 2>&1 || docker build -f sim/device/Dockerfile -t wavebreak-device:local .
	$(COMPOSE) --profile lab up -d --build lab-controller
	./scripts/wait-http.sh http://localhost:$(LAB_CONTROLLER_PORT)/healthz 120

demo-reset:
	./scripts/demo-reset.sh

grafana-sa:
	./scripts/grafana-sa.sh

demo-status:
	./scripts/demo-status.sh

e2e:
	./scripts/e2e.sh

test:
	set -e; for d in sim/ota-agent sim/inference-app wavebreak_clients $(wildcard lab/controller) $(wildcard sim/fleet/tests); do \
	  echo "== $$d"; $(PY) -m pytest -q $$d; done

lint:
	$(PY) -m ruff check .
	shellcheck scripts/*.sh scripts/host/*.sh $(wildcard sim/runtime/firecracker/*.sh) $(wildcard infra/aws/*.sh)
	$(COMPOSE) config -q

all: up publish fleet seed
