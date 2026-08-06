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
	build build-gpu up up-agent up-control up-livekit up-gpu restart-agent \
	restart-worker recreate-worker recreate-worker-gpu recover-worker-gpu ps health health-full \
	wait-ready smoke-control \
	logs logs-chat logs-agent logs-control logs-worker logs-livekit logs-errors \
	worker-status capacity telemetry gpu-check migrate migration migration-check \
	db-shell redis-cli down down-all clean clean-images reset-demo enroll-worker

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

build: ## Build all CPU images
	$(FULL_COMPOSE) build

build-gpu: ## Build all images including the NVIDIA worker
	$(GPU_COMPOSE) build

up: ## Start the basic PostgreSQL, Redis, and aiortc demo
	MEDIA_PROVIDER=aiortc AGENT_RUNTIME_PROVIDER=local $(COMPOSE) up -d --build postgres redis avatar-chat

up-agent: ## Start the Core demo with the gRPC Agent Runtime
	MEDIA_PROVIDER=aiortc AGENT_RUNTIME_PROVIDER=grpc $(COMPOSE) --profile agent up -d --build postgres redis personaforge-agent avatar-chat

up-control: ## Start storage, Agent, LiveKit, and Control without Worker
	$(FULL_COMPOSE) up -d --build postgres redis livekit personaforge-control personaforge-agent
	@$(DEVOPS) wait-ready --control-only

enroll-worker: ## Issue a one-time worker token and update the env file
	@$(DEVOPS) enroll-worker

up-livekit: ## Start the full LiveKit pipeline with a CPU worker
	MEDIA_PROVIDER=livekit AGENT_RUNTIME_PROVIDER=grpc $(FULL_COMPOSE) up -d --build
	@$(DEVOPS) wait-ready --full

up-gpu: ## Start the full LiveKit pipeline with an NVIDIA worker
	MEDIA_PROVIDER=livekit AGENT_RUNTIME_PROVIDER=grpc $(GPU_COMPOSE) up -d --build
	@$(DEVOPS) wait-ready --full

restart-agent: ## Restart the Agent Runtime
	$(FULL_COMPOSE) restart personaforge-agent

restart-worker: ## Restart Worker without reloading environment values
	$(FULL_COMPOSE) restart avatar-worker

recreate-worker: ## Recreate the CPU Worker and reload the env file
	$(FULL_COMPOSE) up -d --force-recreate --no-deps avatar-worker
	@$(DEVOPS) wait-ready --worker-only

recreate-worker-gpu: ## Rebuild and recreate the GPU Worker, then reload the env file
	$(GPU_COMPOSE) up -d --build --force-recreate --no-deps avatar-worker
	@$(DEVOPS) wait-ready --worker-only

recover-worker-gpu: enroll-worker ## Re-enroll a GPU Worker whose persisted identity is stale
	$(GPU_COMPOSE) up -d --build --force-recreate --no-deps avatar-worker
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
	$(COMPOSE) exec redis redis-cli

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
