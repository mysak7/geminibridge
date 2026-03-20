# Gemini Bridge — Usage Guide

**Host:** Raspberry Pi (`ras`) — bridge runs on port **8003**

---

## Start / stop

```bash
# from /home/mi/geminibridge
docker compose up -d          # start in background
docker compose down           # stop
docker compose logs -f        # tail logs
docker compose up -d --build  # rebuild after code changes
```

Or via Makefile:

```bash
make up       # start
make down     # stop
make logs     # tail logs
make rebuild  # force rebuild + restart
```

---

## Endpoints

| | |
|---|---|
| Base URL | `http://ras:8003` |
| API key | `test` (set via `API_KEY` in docker-compose.yml) |

### 1. Raw prompt (legacy)
```bash
curl -X POST http://ras:8003/v1/chat/completions \
  -H "Authorization: Bearer test" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is 2+2?"}'
```

### 2. OpenAI messages format
```bash
curl -X POST http://ras:8003/v1/chat/completions \
  -H "Authorization: Bearer test" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini", "messages": [{"role": "user", "content": "What is 2+2?"}]}'
```

### 3. Streaming (SSE)
```bash
curl -X POST http://ras:8003/v1/chat/completions \
  -H "Authorization: Bearer test" \
  -H "Content-Type: application/json" \
  -d '{"model": "gemini", "stream": true, "messages": [{"role": "user", "content": "Count to 5"}]}'
```

---

## Python — OpenAI SDK drop-in

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://ras:8003/v1",
    api_key="test",
)

# Non-streaming
resp = client.chat.completions.create(
    model="gemini",
    messages=[{"role": "user", "content": "Explain docker networking"}],
)
print(resp.choices[0].message.content)

# Streaming
for chunk in client.chat.completions.create(
    model="gemini",
    messages=[{"role": "user", "content": "Count to 10"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

## LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://ras:8003/v1",
    api_key="test",
    model="gemini",
    streaming=True,
)
llm.invoke("Summarise the TCP handshake")
```

---

## Chat UI

Run on host (not in Docker):

```bash
make ui
# or
uvicorn chat_ui:app --host 0.0.0.0 --port 8004
```

Open `http://ras:8004` in a browser.

---

## Authentication

### Option A — GEMINI_API_KEY (recommended for servers)

```bash
export GEMINI_API_KEY=your_api_key_here
docker compose up -d
```

Or add to `docker-compose.yml` environment section.

### Option B — Google OAuth (browser login)

```bash
gemini auth login          # on the host
# credentials saved to ~/.gemini/
docker compose up -d       # container mounts ~/.gemini read-only
```

---

## Architecture

```
client
  │  POST /v1/chat/completions
  ▼
FastAPI (api.py, port 8000 inside container → 8003 on host)
  │
  ├─ stream=false  →  write prompt to /workspace/input_<id>.txt
  │                   gemini -p "read file, write to output_<id>.txt" --yolo
  │                   read output file → return JSON
  │
  └─ stream=true   →  gemini -p "<prompt>" --yolo
                      pipe stdout chunks → SSE to client
```

---

## Known limits

| Limit | Detail |
|---|---|
| Multi-turn memory | Only the **last** user message is sent to Gemini; earlier conversation turns are dropped |
| Token counts | Word-based estimates, not real BPE tokens |
| Pricing | Estimated using Gemini 2.0 Flash rates ($0.10/1M in · $0.40/1M out) |
| Concurrent requests | Each request spawns a `gemini` subprocess; heavy load will be slow on Raspberry Pi |
