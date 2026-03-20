# geminibridge

A production-ready FastAPI wrapper around the **Google Gemini CLI** that exposes an OpenAI-compatible REST API. Drop-in replacement for [claudebridge](../claudebridge), using `gemini` instead of `claude`.

---

## What it does

- Exposes `POST /v1/chat/completions` — identical to the OpenAI API surface
- Works with the **OpenAI Python SDK**, **LangChain**, **curl**, or any HTTP client
- Supports **streaming** (SSE) and **non-streaming** responses
- Protects the endpoint with a Bearer token (`API_KEY`)
- Stores every exchange in a **SQLite history database**
- Includes a **web chat UI** (Chat, History, Dashboard, Agents tabs)
- Runs **scheduled agents** (cron-based prompts sent automatically)
- Runs the API inside **Docker** with Gemini CLI pre-installed

---

## Differences from claudebridge

| Feature | claudebridge | geminibridge |
|---|---|---|
| Backend CLI | `claude` (`@anthropic-ai/claude-code`) | `gemini` (`@google/gemini-cli`) |
| CLI flag for auto-approve | `--dangerously-skip-permissions --no-session-persistence` | `--yolo` |
| Default API port | `8001` | `8011` |
| Default UI port | `8002` | `8012` |
| Auth credentials | `~/.claude` (browser OAuth) | `GEMINI_API_KEY` or `~/.gemini` (Google OAuth) |
| Pricing estimate | Claude Sonnet ($3/$15 per 1M tokens) | Gemini 2.0 Flash ($0.10/$0.40 per 1M tokens) |
| Model name in responses | `claude-code` | `gemini` |
| UI accent color | Blue (#58a6ff) | Google Blue (#4285f4) |

The external API surface (`/v1/chat/completions`, request/response format, streaming SSE) is **identical** to claudebridge. To switch, just change the base URL.

---

## Installation

### Prerequisites

- Linux (Ubuntu/Debian recommended)
- Docker + Docker Compose
- Node.js (for Gemini CLI installation)
- A Gemini API key **or** a Google account for OAuth

### Quick install

```bash
git clone https://github.com/mysak7/geminibridge
cd geminibridge
sudo bash install.sh
```

The script installs Node.js, the Gemini CLI, Docker, builds the image, and starts the container.

### Manual setup

```bash
# Install Gemini CLI
npm install -g @google/gemini-cli

# Authenticate (choose one):
export GEMINI_API_KEY=your_key_here          # Option A: API key
gemini auth login                             # Option B: browser OAuth

# Set bridge API key and start
echo "API_KEY=mysecretkey" > .env
echo "GEMINI_API_KEY=${GEMINI_API_KEY}" >> .env
docker compose up -d
```

---

## Usage

### API endpoint

```
POST http://localhost:8011/v1/chat/completions
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

### Raw prompt
```bash
curl -X POST http://localhost:8011/v1/chat/completions \
  -H "Authorization: Bearer test" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is 2+2?"}'
```

### OpenAI messages format
```bash
curl -X POST http://localhost:8011/v1/chat/completions \
  -H "Authorization: Bearer test" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini", "messages": [{"role": "user", "content": "Hello!"}]}'
```

### Streaming
```bash
curl -X POST http://localhost:8011/v1/chat/completions \
  -H "Authorization: Bearer test" \
  -H "Content-Type: application/json" \
  -d '{"stream": true, "messages": [{"role": "user", "content": "Count to 5"}]}'
```

### Python (OpenAI SDK)
```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8011/v1", api_key="test")
resp = client.chat.completions.create(
    model="gemini",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

### LangChain
```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(base_url="http://localhost:8011/v1", api_key="test", model="gemini")
llm.invoke("Explain Gemini in one sentence")
```

---

## Chat UI

```bash
make ui
# or
uvicorn chat_ui:app --host 0.0.0.0 --port 8012
```

Open `http://localhost:8012` — provides Chat, History, Dashboard, and Agents tabs.

---

## Makefile commands

```bash
make up       # docker compose up -d
make down     # docker compose down
make logs     # tail container logs
make build    # build Docker image
make rebuild  # force rebuild + restart
make ui       # start chat UI on port 8012
make test     # run test_hello.py + test_greeting.py
make smoke    # quick curl test
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | `test` | Bearer token for the bridge endpoint |
| `GEMINI_API_KEY` | *(empty)* | Google AI API key (alternative to OAuth) |
| `DB_PATH` | `/data/chat_history.db` | SQLite database path inside container |
| `BRIDGE_URL` | `http://localhost:8011/v1/chat/completions` | URL chat_ui.py uses to reach the API |
| `BRIDGE_KEY` | `test` | API key chat_ui.py sends to the bridge |

---

## Architecture

```
client
  │  POST /v1/chat/completions
  ▼
FastAPI  api.py  (Docker, port 8000 → 8011 on host)
  │
  ├─ stream=false  →  write prompt to /workspace/input_<uuid>.txt
  │                   gemini -p "read + write result to output file" --yolo
  │                   read output file → return OpenAI JSON
  │
  └─ stream=true   →  gemini -p "<prompt>" --yolo
                      pipe stdout chunks as SSE → client

chat_ui.py  (host, port 8012)  →  calls localhost:8011  →  Gemini CLI
SQLite DB  (shared volume /home/mi/geminibridge/)
```

---

## Migrating from claudebridge

Change only the base URL:

```python
# Before (claudebridge)
client = OpenAI(base_url="http://ras:8001/v1", api_key="test")

# After (geminibridge)
client = OpenAI(base_url="http://ras:8011/v1", api_key="test")
```

Everything else — request format, response format, streaming, LangChain integration — is identical.
