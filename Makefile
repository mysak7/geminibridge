.PHONY: help up down logs build rebuild ui test smoke

API_PORT  ?= 8011
UI_PORT   ?= 8012
API_KEY   ?= test
BRIDGE_URL ?= http://localhost:$(API_PORT)

help:
	@echo "geminibridge — Gemini CLI API bridge"
	@echo ""
	@echo "  make up        Start API container (docker compose)"
	@echo "  make down      Stop and remove container"
	@echo "  make logs      Tail container logs"
	@echo "  make build     Build Docker image"
	@echo "  make rebuild   Force-rebuild image and restart"
	@echo "  make ui        Start chat UI (host, port $(UI_PORT))"
	@echo "  make test      Run test clients"
	@echo "  make smoke     Quick curl smoke test"
	@echo ""
	@echo "  Env vars: API_PORT=$(API_PORT)  UI_PORT=$(UI_PORT)  API_KEY=$(API_KEY)"

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

build:
	docker compose build

rebuild:
	docker compose up -d --build

ui:
	@echo "Starting Gemini Bridge chat UI on port $(UI_PORT)..."
	DB_PATH=$(HOME)/geminibridge/chat_history.db \
	BRIDGE_URL=$(BRIDGE_URL)/v1/chat/completions \
	BRIDGE_KEY=$(API_KEY) \
	uvicorn chat_ui:app --host 0.0.0.0 --port $(UI_PORT) --reload

test:
	python3 test_hello.py
	python3 test_greeting.py

smoke:
	@echo "Smoke test → $(BRIDGE_URL)/v1/chat/completions"
	@curl -sf $(BRIDGE_URL)/v1/chat/completions \
	  -H "Authorization: Bearer $(API_KEY)" \
	  -H "Content-Type: application/json" \
	  -d '{"prompt": "Reply with exactly: hello from geminibridge"}' \
	  | python3 -m json.tool
