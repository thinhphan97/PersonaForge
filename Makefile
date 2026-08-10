SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PYTEST ?= $(if $(wildcard .venv/bin/pytest),.venv/bin/pytest,pytest)
RUFF ?= $(if $(wildcard .venv/bin/ruff),.venv/bin/ruff,ruff)
MYPY ?= $(if $(wildcard .venv/bin/mypy),.venv/bin/mypy,mypy)
ENV_FILE ?= .env
PYTEST_ARGS ?=
SINCE ?= 5m
NAME ?= change
CONFIRM ?=

COMPOSE := docker compose --env-file $(ENV_FILE)
FULL_COMPOSE := $(COMPOSE) --profile livekit --profile agent
GPU_COMPOSE := docker compose --env-file $(ENV_FILE) -f compose.yaml -f compose.gpu.yaml --profile livekit --profile agent
DEVOPS := $(PYTHON) scripts/devops.py --env-file $(ENV_FILE)
APP_PUBLIC_URL ?= http://localhost:$(shell $(DEVOPS) value APP_HTTP_PORT 8000)

.PHONY: help doctor env-check install assets format format-check lint typecheck \
	unit contract integration test e2e check compose-config compose-config-gpu \
	build build-gpu build-chat build-agent build-control build-worker build-worker-gpu \
	up up-agent up-control up-livekit up-gpu up-remote-worker up-remote-worker-gpu \
	restart-agent restart-worker \
	recreate-chat recreate-agent recreate-control recreate-worker recreate-worker-gpu \
	recreate-remote-worker recreate-remote-worker-gpu \
	rebuild-chat rebuild-agent rebuild-control rebuild-worker rebuild-worker-gpu \
	recover-worker-gpu ps health health-full \
	wait-ready smoke-control \
	logs logs-chat logs-agent logs-control logs-worker logs-livekit logs-errors \
	worker-status capacity telemetry gpu-check migrate migration migration-check \
	db-shell redis-cli down down-all clean clean-images reset-demo enroll-worker \
	third-party-fetch third-party-artifacts third-party-verify third-party-verify-strict

help: ## Show all available targets
	@awk 'BEGIN {FS = ":.*## "; printf "PersonaForge development commands\n\n"} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf '\nOverrides: ENV_FILE=.env SINCE=10m PYTEST_ARGS="-x -vv"\n'

doctor: ## Check toolchain, Docker, ports, and GPU/NVML
	@$(DEVOPS) doctor

env-check: ## Validate environment variables and unsafe settings
	@$(DEVOPS) env-check

install: ## Install the project with development dependencies
	$(PYTHON) -m pip install -e '.[dev]'

assets: ## Generate demo videos for all personas
	bash scripts/generate_demo_videos.sh

third-party-fetch: ## Fetch pinned upstream model source repositories
	bash scripts/fetch_third_party_source.sh

third-party-artifacts: ## Download and verify pinned model artifacts into the external cache
	$(PYTHON) scripts/fetch_third_party_artifacts.py

third-party-verify: ## Verify fetched third-party source and cached artifacts (non-strict)
	$(PYTHON) scripts/verify_third_party.py

third-party-verify-strict: ## Require every pinned source and model artifact to be present
	$(PYTHON) scripts/verify_third_party.py --strict

format: ## Format source and tests with Ruff
	$(RUFF) format src tests scripts

format-check: ## Check formatting without changing files
	$(RUFF) format --check src tests scripts

lint: ## Run Ruff lint checks
	$(RUFF) check src tests scripts

typecheck: ## Run strict mypy checks on application source
	$(MYPY) src

unit: ## Run unit tests
	$(PYTEST) tests/unit tests/test_architecture.py $(PYTEST_ARGS)

contract: ## Run repository and contract tests
	$(PYTEST) tests/contract $(PYTEST_ARGS)

integration: ## Run integration tests
	$(PYTEST) tests/integration $(PYTEST_ARGS)

test: ## Run the test suite excluding browser E2E
	$(PYTEST) tests --ignore=tests/e2e $(PYTEST_ARGS)

e2e: ## Run Playwright E2E against the current stack
	E2E_BASE_URL=$$($(DEVOPS) value APP_PUBLIC_URL $(APP_PUBLIC_URL)) $(PYTEST) tests/e2e $(PYTEST_ARGS)

check: lint typecheck test ## Run the non-mutating pre-commit/CI quality gate

compose-config: ## Validate the CPU Compose configuration
	$(FULL_COMPOSE) config --quiet

compose-config-gpu: ## Validate the GPU Compose override
	$(GPU_COMPOSE) config --quiet

build: ## Build all CPU images explicitly
	$(FULL_COMPOSE) build

build-gpu: ## Build all images including the NVIDIA Worker explicitly (slow)
	$(GPU_COMPOSE) build

build-chat: ## Build only the Avatar Chat/Core API image
	$(COMPOSE) build avatar-chat

build-agent: ## Build only the Agent Runtime image
	$(FULL_COMPOSE) build personaforge-agent

build-control: ## Build only the Control Plane image
	$(FULL_COMPOSE) build personaforge-control

build-worker: ## Build only the CPU Worker image
	$(FULL_COMPOSE) build avatar-worker

build-worker-gpu: ## Build only the NVIDIA Worker image (slow)
	$(GPU_COMPOSE) build avatar-worker

up: ## Start the basic aiortc demo using existing images (builds only if missing)
	MEDIA_PROVIDER=aiortc AGENT_RUNTIME_PROVIDER=local $(COMPOSE) up -d postgres redis avatar-chat

up-agent: ## Start the gRPC Agent demo using existing images (builds only if missing)
	MEDIA_PROVIDER=aiortc AGENT_RUNTIME_PROVIDER=grpc $(COMPOSE) --profile agent up -d postgres redis personaforge-agent avatar-chat

up-control: ## Start Control/Agent/LiveKit using existing images (builds only if missing)
	$(FULL_COMPOSE) up -d postgres redis livekit personaforge-control personaforge-agent
	@$(DEVOPS) wait-ready --control-only

enroll-worker: ## Issue a one-time worker token and update the env file
	@$(DEVOPS) enroll-worker

up-livekit: ## Start the full CPU LiveKit pipeline using existing images
	MEDIA_PROVIDER=livekit AGENT_RUNTIME_PROVIDER=grpc $(FULL_COMPOSE) up -d
	@$(DEVOPS) wait-ready --full

up-gpu: ## Start the full NVIDIA LiveKit pipeline using existing images
	MEDIA_PROVIDER=livekit AGENT_RUNTIME_PROVIDER=grpc $(GPU_COMPOSE) up -d
	@$(DEVOPS) wait-ready --full

up-remote-worker: ## Start only the CPU Worker against an external Control/Redis/LiveKit host
	$(FULL_COMPOSE) up -d --no-deps avatar-worker
	@$(FULL_COMPOSE) ps avatar-worker

up-remote-worker-gpu: ## Start only the NVIDIA Worker against external services
	$(GPU_COMPOSE) up -d --no-deps avatar-worker
	@$(GPU_COMPOSE) ps avatar-worker

restart-agent: ## Restart the Agent Runtime
	$(FULL_COMPOSE) restart personaforge-agent

restart-worker: ## Restart Worker without reloading environment values
	$(FULL_COMPOSE) restart avatar-worker

recreate-chat: ## Recreate Avatar Chat and reload Compose/.env without building
	$(COMPOSE) up -d --force-recreate --no-deps avatar-chat

recreate-agent: ## Recreate Agent Runtime and reload Compose/.env without building
	$(FULL_COMPOSE) up -d --force-recreate --no-deps personaforge-agent

recreate-control: ## Recreate Control Plane and reload Compose/.env without building
	$(FULL_COMPOSE) up -d --force-recreate --no-deps personaforge-control
	@$(DEVOPS) wait-ready --control-only

recreate-worker: ## Recreate the CPU Worker and reload the env file
	$(FULL_COMPOSE) up -d --force-recreate --no-deps avatar-worker
	@$(DEVOPS) wait-ready --worker-only

recreate-worker-gpu: ## Recreate the NVIDIA Worker and reload Compose/.env without building
	$(GPU_COMPOSE) up -d --force-recreate --no-deps avatar-worker
	@$(DEVOPS) wait-ready --worker-only

recreate-remote-worker: ## Recreate only a remote CPU Worker and reload its env
	$(FULL_COMPOSE) up -d --force-recreate --no-deps avatar-worker
	@$(FULL_COMPOSE) ps avatar-worker

recreate-remote-worker-gpu: ## Recreate only a remote NVIDIA Worker and reload its env
	$(GPU_COMPOSE) up -d --force-recreate --no-deps avatar-worker
	@$(GPU_COMPOSE) ps avatar-worker

rebuild-chat: build-chat recreate-chat ## Build and recreate Avatar Chat after source/dependency changes

rebuild-agent: build-agent recreate-agent ## Build and recreate Agent Runtime after source/dependency changes

rebuild-control: build-control recreate-control ## Build and recreate Control Plane after source/dependency changes

rebuild-worker: build-worker recreate-worker ## Build and recreate the CPU Worker

rebuild-worker-gpu: build-worker-gpu recreate-worker-gpu ## Build and recreate the NVIDIA Worker (slow)

recover-worker-gpu: enroll-worker ## Re-enroll a GPU Worker whose persisted identity is stale
	$(GPU_COMPOSE) up -d --force-recreate --no-deps avatar-worker
	@$(DEVOPS) wait-ready --worker-only

ps: ## Show status for all services and profiles
	$(FULL_COMPOSE) ps

health: ## Check the Core API and basic container health
	@$(DEVOPS) health

health-full: ## Check Core, Control, Agent, and Worker heartbeat
	@$(DEVOPS) health --full

wait-ready: ## Wait for the full pipeline; TIMEOUT defaults to 120 seconds
	@$(DEVOPS) wait-ready --full --timeout $${TIMEOUT:-120}

smoke-control: ## Smoke-test enrollment and Redis ACL in Compose
	$(FULL_COMPOSE) run --rm --no-deps avatar-chat python scripts/smoke_control_plane.py

logs: ## Follow logs for the complete stack
	$(FULL_COMPOSE) logs -f --since=$(SINCE)

logs-chat: ## Follow Avatar Chat logs
	$(FULL_COMPOSE) logs -f --since=$(SINCE) avatar-chat

logs-agent: ## Follow Agent Runtime logs
	$(FULL_COMPOSE) logs -f --since=$(SINCE) personaforge-agent

logs-control: ## Follow Control Plane logs
	$(FULL_COMPOSE) logs -f --since=$(SINCE) personaforge-control

logs-worker: ## Follow Render Worker logs
	$(FULL_COMPOSE) logs -f --since=$(SINCE) avatar-worker

logs-livekit: ## Follow LiveKit logs
	$(FULL_COMPOSE) logs -f --since=$(SINCE) livekit

logs-errors: ## Filter recent errors, timeouts, OOMs, and HTTP 5xx responses
	@$(FULL_COMPOSE) logs --since=$(SINCE) 2>&1 | rg -i 'error|critical|traceback|oom|out of memory|timed out|timeout| 5[0-9][0-9] ' || true

worker-status: ## Summarize Worker inventory, heartbeat, and GPUs
	@$(DEVOPS) worker-status

capacity: ## Show slot, VRAM, and compute capacity per GPU
	@$(DEVOPS) worker-status --capacity

telemetry: ## Show resource usage for each Composite Child/PID
	@$(DEVOPS) worker-status --children

gpu-check: ## Verify NVIDIA runtime, nvitop, and Worker PID mapping
	$(GPU_COMPOSE) exec -T avatar-worker python -c "from nvitop import Device; ds=Device.all(); print(f'visible_gpus={len(ds)}'); [print(d) for d in ds]"

migrate: ## Upgrade the PostgreSQL schema to Alembic head
	$(COMPOSE) run --rm avatar-chat alembic upgrade head

migration: ## Create an Alembic revision: make migration NAME=add_voice
	$(COMPOSE) run --rm avatar-chat alembic revision --autogenerate -m "$(NAME)"

migration-check: ## Show the current and head Alembic revisions
	$(COMPOSE) run --rm avatar-chat sh -c 'alembic current && alembic heads'

db-shell: ## Open psql inside the PostgreSQL container
	$(COMPOSE) exec postgres psql -U $$($(DEVOPS) value POSTGRES_USER avatar) -d $$($(DEVOPS) value POSTGRES_DB avatar)

redis-cli: ## Open Redis CLI
	$(COMPOSE) exec redis sh -c 'REDISCLI_AUTH="$${REDIS_ADMIN_PASSWORD}" exec redis-cli'

down: ## Stop the basic demo and retain volumes
	$(COMPOSE) down

down-all: ## Stop all profiles and retain volumes
	$(FULL_COMPOSE) down

clean: ## Remove Python and test caches from the repository
	find src tests scripts -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find src tests scripts -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

clean-images: ## Remove project-built images while retaining volumes
	$(FULL_COMPOSE) down --rmi local

reset-demo: ## Delete all demo volumes; requires CONFIRM=YES
	@test "$(CONFIRM)" = "YES" || (echo 'Refused: this deletes PostgreSQL, Redis, and Worker identity. Use CONFIRM=YES'; exit 1)
	$(FULL_COMPOSE) down -v --remove-orphans
