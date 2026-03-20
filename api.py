import os
import uuid
import json
import time
import sqlite3
import asyncio
import subprocess
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

app = FastAPI()
security = HTTPBearer()

API_KEY = os.environ.get("API_KEY", "")
WORKSPACE = "/workspace"
DB_PATH = os.environ.get("DB_PATH", "/data/chat_history.db")

# Gemini 2.0 Flash pricing (per million tokens)
PRICE_INPUT  = 0.10   # $0.10 / 1M input tokens
PRICE_OUTPUT = 0.40   # $0.40 / 1M output tokens


# ── History DB ────────────────────────────────────────────────────────────────

def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            role    TEXT    NOT NULL,
            content TEXT    NOT NULL,
            stats   TEXT,
            ts      REAL    NOT NULL
        )
    """)
    con.commit()
    con.close()


def db_save(role: str, content: str, stats: dict | None = None):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO messages (role, content, stats, ts) VALUES (?, ?, ?, ?)",
        (role, content, json.dumps(stats) if stats else None, time.time()),
    )
    con.commit()
    con.close()


db_init()


# ── Request model ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    prompt: str | None = None
    messages: list[dict] | None = None
    model: str | None = None  # accepted but ignored; always routes to Gemini
    stream: bool = False

    def get_prompt(self) -> str:
        if self.prompt:
            return self.prompt
        # Extract the last user message from OpenAI-style messages array
        for m in reversed(self.messages or []):
            if m.get("role") == "user":
                content = m["content"]
                # content can be a string or a list of content parts
                if isinstance(content, list):
                    return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
                return content
        return ""


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials


# ── Streaming ─────────────────────────────────────────────────────────────────

async def _stream_response(prompt: str, model: str):
    """Run Gemini CLI, yield SSE chunks, and save the full exchange to history."""
    cmd = [
        "gemini",
        "-p", prompt,
        "--yolo",                  # auto-approve tool calls (like --dangerously-skip-permissions)
    ]

    t0 = time.time()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=WORKSPACE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    collected = []

    while True:
        chunk = await proc.stdout.read(256)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        collected.append(text)
        data = {
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(data)}\n\n"

    await proc.wait()

    # Save full exchange to history after stream completes
    content = "".join(collected)
    elapsed = round(time.time() - t0, 2)
    input_tokens = len(prompt) // 4
    output_tokens = len(content) // 4
    cost_usd = round((input_tokens * PRICE_INPUT + output_tokens * PRICE_OUTPUT) / 1_000_000, 6)
    db_save("user", prompt)
    db_save("assistant", content, {
        "prompt_chars": len(prompt), "prompt_words": len(prompt.split()),
        "response_chars": len(content), "response_words": len(content.split()),
        "elapsed_sec": elapsed,
        "chars_per_sec": round(len(content) / elapsed, 1) if elapsed > 0 else 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "source": "api",
    })

    done = {
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done)}\n\n"
    yield "data: [DONE]\n\n"


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest, token: str = Depends(verify_token)):
    prompt = request.get_prompt()
    if not prompt:
        raise HTTPException(status_code=400, detail="No prompt or user message provided.")

    model = request.model or "gemini"

    # --- Streaming path ---
    if request.stream:
        return StreamingResponse(
            _stream_response(prompt, model),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    # --- Non-streaming path: file-based ---
    task_id = str(uuid.uuid4())
    input_path = f"{WORKSPACE}/input_{task_id}.txt"
    output_path = f"{WORKSPACE}/output_{task_id}.txt"

    with open(input_path, "w") as f:
        f.write(prompt)

    system_prompt = (
        f"Read the task from {input_path}. "
        f"Execute all steps and write ONLY the final result exactly to {output_path}. "
        f"Do not write extra explanation."
    )

    cmd = [
        "gemini",
        "-p", system_prompt,
        "--yolo",                  # auto-approve tool calls
    ]

    t0 = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=WORKSPACE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Gemini CLI failed: {stderr.decode()}")

        if not os.path.exists(output_path):
            raise HTTPException(status_code=500, detail="Gemini did not produce an output file.")

        with open(output_path, "r") as f:
            content = f.read()

    finally:
        for path in (input_path, output_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    elapsed = round(time.time() - t0, 2)
    input_tokens = len(prompt) // 4
    output_tokens = len(content) // 4
    cost_usd = round((input_tokens * PRICE_INPUT + output_tokens * PRICE_OUTPUT) / 1_000_000, 6)

    # Save exchange to shared history DB
    db_save("user", prompt)
    db_save("assistant", content, {
        "prompt_chars": len(prompt), "prompt_words": len(prompt.split()),
        "response_chars": len(content), "response_words": len(content.split()),
        "elapsed_sec": elapsed,
        "chars_per_sec": round(len(content) / elapsed, 1) if elapsed > 0 else 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "source": "api",
    })

    return {
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": len(prompt.split()),
            "completion_tokens": len(content.split()),
            "total_tokens": len(prompt.split()) + len(content.split()),
        },
    }
