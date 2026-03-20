import os
import json
import time
import sqlite3
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

DB_PATH = os.environ.get("DB_PATH", "/home/mi/geminibridge/chat_history.db")
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:8011/v1/chat/completions")
BRIDGE_KEY = os.environ.get("BRIDGE_KEY", "test")

scheduler = AsyncIOScheduler()

# Gemini 2.0 Flash pricing
PRICE_INPUT  = 0.10   # $0.10 / 1M input tokens
PRICE_OUTPUT = 0.40   # $0.40 / 1M output tokens


# ── DB ──────────────────────────────────────────────────────────────────────

def init_db():
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
    con.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            prompt     TEXT    NOT NULL,
            hour       INTEGER NOT NULL,
            minute     INTEGER NOT NULL,
            days       TEXT    NOT NULL DEFAULT '*',
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at REAL    NOT NULL,
            last_run   REAL,
            last_status TEXT
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


def agents_load() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, name, prompt, hour, minute, days, enabled, created_at, last_run, last_status "
        "FROM agents ORDER BY id"
    ).fetchall()
    con.close()
    keys = ["id", "name", "prompt", "hour", "minute", "days",
            "enabled", "created_at", "last_run", "last_status"]
    return [dict(zip(keys, r)) for r in rows]


def agent_get(agent_id: int) -> dict | None:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT id, name, prompt, hour, minute, days, enabled, created_at, last_run, last_status "
        "FROM agents WHERE id=?", (agent_id,)
    ).fetchone()
    con.close()
    if not row:
        return None
    keys = ["id", "name", "prompt", "hour", "minute", "days",
            "enabled", "created_at", "last_run", "last_status"]
    return dict(zip(keys, row))


def agent_update_last(agent_id: int, status: str):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE agents SET last_run=?, last_status=? WHERE id=?",
        (time.time(), status, agent_id)
    )
    con.commit()
    con.close()


# ── Scheduler ────────────────────────────────────────────────────────────────

async def run_agent_job(agent_id: int):
    a = agent_get(agent_id)
    if not a or not a["enabled"]:
        return
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                BRIDGE_URL,
                json={"prompt": a["prompt"]},
                headers={"Authorization": f"Bearer {BRIDGE_KEY}"},
            )
            resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        db_save("user", f"[Agent: {a['name']}] {a['prompt']}")
        db_save("assistant", content)
        agent_update_last(agent_id, "ok")
    except Exception as e:
        agent_update_last(agent_id, f"error: {e}")


def schedule_agent(a: dict):
    job_id = f"agent_{a['id']}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if a["enabled"]:
        scheduler.add_job(
            run_agent_job,
            CronTrigger(hour=a["hour"], minute=a["minute"], day_of_week=a["days"]),
            args=[a["id"]],
            id=job_id,
            replace_existing=True,
        )


def load_all_agents_into_scheduler():
    for a in agents_load():
        schedule_agent(a)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_all_agents_into_scheduler()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


# ── Models ───────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    prompt: str


class AgentCreate(BaseModel):
    name: str
    prompt: str
    hour: int
    minute: int
    days: str = "*"
    enabled: bool = True


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.get("/history")
async def history(period: str = "all"):
    cutoff = _period_cutoff(period)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, role, content, stats, ts FROM messages WHERE ts >= ? ORDER BY id DESC",
        (cutoff,)
    ).fetchall()
    con.close()
    return [
        {"id": r[0], "role": r[1], "content": r[2],
         "stats": json.loads(r[3]) if r[3] else None, "ts": r[4]}
        for r in rows
    ]


def _period_cutoff(period: str) -> float:
    now = time.time()
    return {"day": now - 86400, "week": now - 7 * 86400, "month": now - 30 * 86400}.get(period, 0)


@app.get("/dashboard")
async def dashboard(period: str = "all"):
    cutoff = _period_cutoff(period)
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT role, stats, ts FROM messages WHERE ts >= ? ORDER BY ts",
        (cutoff,)
    ).fetchall()
    con.close()
    total_input_tokens = total_output_tokens = 0
    total_cost = 0.0
    sessions = 0
    for role, stats_json, _ in rows:
        if role == "assistant":
            sessions += 1
        if stats_json:
            s = json.loads(stats_json)
            total_input_tokens += s.get("input_tokens", 0)
            total_output_tokens += s.get("output_tokens", 0)
            total_cost += s.get("cost_usd", 0.0)
    return {
        "total_messages": len(rows),
        "sessions": sessions,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "total_tokens": total_input_tokens + total_output_tokens,
        "cost_usd": round(total_cost, 6),
    }


@app.post("/chat")
async def chat(msg: ChatMessage):
    t0 = time.time()
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            BRIDGE_URL,
            json={"prompt": msg.prompt},
            headers={"Authorization": f"Bearer {BRIDGE_KEY}"},
        )
        resp.raise_for_status()
    elapsed = round(time.time() - t0, 2)
    content = resp.json()["choices"][0]["message"]["content"]
    input_tokens = len(msg.prompt) // 4
    output_tokens = len(content) // 4
    cost_usd = round((input_tokens * PRICE_INPUT + output_tokens * PRICE_OUTPUT) / 1_000_000, 6)
    stats = {
        "prompt_chars": len(msg.prompt),
        "prompt_words": len(msg.prompt.split()),
        "response_chars": len(content),
        "response_words": len(content.split()),
        "elapsed_sec": elapsed,
        "chars_per_sec": round(len(content) / elapsed, 1) if elapsed > 0 else 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
    }
    db_save("user", msg.prompt)
    db_save("assistant", content, stats)
    return {"content": content, "stats": stats}


@app.get("/agents")
async def get_agents():
    return agents_load()


@app.post("/agents")
async def create_agent(body: AgentCreate):
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "INSERT INTO agents (name, prompt, hour, minute, days, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (body.name, body.prompt, body.hour, body.minute,
         body.days, int(body.enabled), time.time()),
    )
    agent_id = cur.lastrowid
    con.commit()
    con.close()
    a = agent_get(agent_id)
    schedule_agent(a)
    return a


@app.put("/agents/{agent_id}")
async def update_agent(agent_id: int, body: AgentCreate):
    a = agent_get(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE agents SET name=?, prompt=?, hour=?, minute=?, days=?, enabled=? WHERE id=?",
        (body.name, body.prompt, body.hour, body.minute,
         body.days, int(body.enabled), agent_id),
    )
    con.commit()
    con.close()
    updated = agent_get(agent_id)
    schedule_agent(updated)
    return updated


@app.delete("/agents/{agent_id}")
async def delete_agent(agent_id: int):
    job_id = f"agent_{agent_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM agents WHERE id=?", (agent_id,))
    con.commit()
    con.close()
    return {"ok": True}


@app.post("/agents/{agent_id}/run")
async def run_agent_now(agent_id: int):
    a = agent_get(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    await run_agent_job(agent_id)
    return agent_get(agent_id)


# ── HTML ─────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gemini Bridge</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #e6edf3; height: 100vh; display: flex; flex-direction: column; }

  /* Header */
  header { padding: 0 24px; background: #161b22; border-bottom: 1px solid #30363d; display: flex; align-items: stretch; height: 52px; }
  .header-left { display: flex; align-items: center; gap: 10px; margin-right: 24px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #4285f4; box-shadow: 0 0 8px #4285f4; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .header-left h1 { font-size: 1rem; font-weight: 600; color: #f0f6fc; }
  nav { display: flex; align-items: stretch; }
  nav button { background: none; border: none; border-bottom: 2px solid transparent; border-radius: 0; color: #8b949e; cursor: pointer; font-size: .9rem; font-weight: 500; padding: 0 18px; transform: none; transition: color .15s,border-color .15s; height: 100%; }
  nav button:hover { color: #e6edf3; background: none; }
  nav button.active { color: #f0f6fc; border-bottom-color: #4285f4; font-weight: 600; }
  nav button:disabled { background: none; }
  .bridge-label { margin-left: auto; font-size: .75rem; color: #484f58; align-self: center; }

  /* Views */
  .view { display: none; flex: 1; flex-direction: column; overflow: hidden; }
  .view.active { display: flex; }

  /* Chat */
  .chat-area { flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 20px; scroll-behavior: smooth; }
  .chat-area::-webkit-scrollbar { width: 6px; }
  .chat-area::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
  .msg-row { display: flex; flex-direction: column; gap: 6px; max-width: 820px; width: 100%; }
  .msg-row.user { align-self: flex-end; align-items: flex-end; }
  .msg-row.assistant { align-self: flex-start; align-items: flex-start; }
  .bubble { padding: 12px 16px; border-radius: 12px; line-height: 1.6; font-size: .95rem; white-space: pre-wrap; word-break: break-word; max-width: 100%; }
  .user .bubble { background: #4285f4; color: #fff; border-bottom-right-radius: 4px; }
  .assistant .bubble { background: #161b22; border: 1px solid #30363d; border-bottom-left-radius: 4px; }
  .stats-bar { display: flex; gap: 10px; flex-wrap: wrap; font-size: .72rem; color: #8b949e; }
  .stat { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 3px 8px; display: flex; align-items: center; gap: 4px; }
  .stat .label { color: #6e7681; }
  .stat .value { color: #4285f4; font-weight: 600; font-family: monospace; }
  .stat.time .value { color: #34a853; }
  .stat.speed .value { color: #d2a8ff; }
  .stat.cost .value { color: #fbbc04; }
  .typing-indicator { display: none; gap: 5px; padding: 12px 16px; background: #161b22; border: 1px solid #30363d; border-radius: 12px; border-bottom-left-radius: 4px; width: fit-content; }
  .typing-indicator.visible { display: flex; }
  .typing-indicator span { width: 7px; height: 7px; border-radius: 50%; background: #4285f4; animation: bounce 1.2s infinite; }
  .typing-indicator span:nth-child(2) { animation-delay: .2s; }
  .typing-indicator span:nth-child(3) { animation-delay: .4s; }
  @keyframes bounce { 0%,60%,100%{transform:translateY(0)} 30%{transform:translateY(-6px)} }
  .input-area { padding: 16px 24px; background: #161b22; border-top: 1px solid #30363d; display: flex; gap: 12px; align-items: flex-end; }
  textarea { flex: 1; background: #0d1117; border: 1px solid #30363d; border-radius: 10px; color: #e6edf3; font-family: inherit; font-size: .95rem; padding: 12px 14px; resize: none; outline: none; line-height: 1.5; max-height: 160px; transition: border-color .2s; }
  textarea:focus { border-color: #4285f4; }
  textarea::placeholder { color: #484f58; }
  button { background: #1a73e8; border: none; border-radius: 10px; color: #fff; cursor: pointer; font-size: .9rem; font-weight: 600; padding: 12px 20px; transition: background .2s,transform .1s; white-space: nowrap; }
  button:hover { background: #4285f4; }
  button:active { transform: scale(.97); }
  button:disabled { background: #21262d; color: #484f58; cursor: not-allowed; transform: none; }
  .empty-state { margin: auto; text-align: center; color: #484f58; }
  .empty-state .icon { font-size: 3rem; margin-bottom: 12px; }
  .empty-state p { font-size: .9rem; }
  .error-bubble { background: #2d1117; border: 1px solid #f85149; color: #f85149; }

  /* Shared scroll view */
  .scroll-view { flex: 1; overflow-y: auto; padding: 24px; }
  .scroll-view::-webkit-scrollbar { width: 6px; }
  .scroll-view::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
  .toolbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; flex-wrap: wrap; gap: 10px; }
  .toolbar h2 { font-size: 1rem; font-weight: 600; color: #f0f6fc; }
  .toolbar-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .btn-sm { background: #21262d; padding: 6px 14px; font-size: .8rem; border-radius: 6px; }
  .btn-sm:hover { background: #30363d; }
  .btn-danger { background: #2d1117; color: #f85149; border: 1px solid #3d1a1a; }
  .btn-danger:hover { background: #3d1a1a; }
  .btn-run { background: #1a2f4a; color: #4285f4; border: 1px solid #1a3a72; }
  .btn-run:hover { background: #1a3a72; }

  /* Period filter pills */
  .period-pills { display: flex; gap: 4px; }
  .pill { background: #21262d; border: 1px solid #30363d; border-radius: 20px; color: #8b949e; cursor: pointer; font-size: .75rem; font-weight: 500; padding: 4px 12px; transition: background .15s,color .15s; white-space: nowrap; }
  .pill:hover { background: #30363d; color: #e6edf3; }
  .pill.active { background: #1a2f4a; border-color: #4285f4; color: #4285f4; font-weight: 600; }

  /* History table */
  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  thead th { background: #161b22; border: 1px solid #30363d; color: #8b949e; font-weight: 600; padding: 10px 12px; text-align: left; position: sticky; top: 0; z-index: 1; }
  tbody tr { border-bottom: 1px solid #21262d; transition: background .1s; }
  tbody tr:hover { background: #161b22; }
  td { padding: 10px 12px; border: 1px solid #21262d; vertical-align: middle; max-width: 0; }
  .role-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: .75rem; font-weight: 600; }
  .role-badge.user { background: #1a2f4a; color: #4285f4; }
  .role-badge.assistant { background: #1a2f1e; color: #34a853; }
  td.preview-cell { width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #8b949e; cursor: pointer; }
  td.preview-cell:hover { color: #4285f4; text-decoration: underline; }
  td.time-cell { width: 140px; color: #484f58; font-size: .78rem; font-family: monospace; }
  td.stat-cell { width: 70px; color: #8b949e; font-family: monospace; font-size: .78rem; }
  td.cost-cell { width: 80px; color: #fbbc04; font-family: monospace; font-size: .78rem; }

  /* Dashboard */
  .dash-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 16px; margin-bottom: 32px; }
  .dash-card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 20px; display: flex; flex-direction: column; gap: 6px; }
  .dash-card .card-label { font-size: .75rem; color: #8b949e; font-weight: 500; text-transform: uppercase; letter-spacing: .04em; }
  .dash-card .card-value { font-size: 1.8rem; font-weight: 700; font-family: monospace; line-height: 1.1; }
  .dash-card .card-sub { font-size: .75rem; color: #484f58; }
  .card-blue .card-value { color: #4285f4; }
  .card-green .card-value { color: #34a853; }
  .card-orange .card-value { color: #fbbc04; }
  .card-purple .card-value { color: #d2a8ff; }
  .card-yellow .card-value { color: #ea4335; }
  .dash-note { font-size: .75rem; color: #484f58; margin-top: 8px; }

  /* Agents */
  .agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; margin-top: 16px; }
  .agent-card { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px; display: flex; flex-direction: column; gap: 10px; }
  .agent-card.disabled { opacity: .5; }
  .agent-card-header { display: flex; align-items: center; gap: 8px; }
  .agent-name { font-weight: 600; color: #f0f6fc; flex: 1; }
  .agent-schedule { font-size: .8rem; color: #4285f4; font-family: monospace; }
  .agent-prompt { font-size: .82rem; color: #8b949e; line-height: 1.5; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .agent-footer { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .last-run { font-size: .75rem; color: #484f58; flex: 1; }
  .status-ok { color: #34a853; }
  .status-err { color: #ea4335; }

  /* Toggle */
  .toggle { position: relative; width: 36px; height: 20px; flex-shrink: 0; }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle-slider { position: absolute; inset: 0; background: #30363d; border-radius: 20px; cursor: pointer; transition: .2s; }
  .toggle input:checked + .toggle-slider { background: #1a73e8; }
  .toggle-slider::before { content: ''; position: absolute; width: 14px; height: 14px; left: 3px; bottom: 3px; background: #fff; border-radius: 50%; transition: .2s; }
  .toggle input:checked + .toggle-slider::before { transform: translateX(16px); }

  /* Modal */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.7); z-index: 100; align-items: center; justify-content: center; }
  .modal-overlay.open { display: flex; }
  .modal { background: #161b22; border: 1px solid #30363d; border-radius: 12px; width: min(600px, 92vw); max-height: 85vh; display: flex; flex-direction: column; overflow: hidden; }
  .modal-header { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid #30363d; }
  .modal-header h3 { font-size: .95rem; font-weight: 600; color: #f0f6fc; }
  .modal-actions { display: flex; gap: 8px; }
  .copy-btn { background: #1a73e8; padding: 6px 14px; font-size: .8rem; border-radius: 6px; }
  .copy-btn:hover { background: #4285f4; }
  .copy-btn.copied { background: #1a4731; color: #34a853; }
  .close-btn { background: #21262d; padding: 6px 12px; font-size: .8rem; border-radius: 6px; }
  .close-btn:hover { background: #30363d; }
  .modal-body { padding: 18px; overflow-y: auto; flex: 1; white-space: pre-wrap; word-break: break-word; font-size: .9rem; line-height: 1.7; color: #e6edf3; }
  .modal-body::-webkit-scrollbar { width: 6px; }
  .modal-body::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }

  /* Agent form modal */
  .form-modal { background: #161b22; border: 1px solid #30363d; border-radius: 12px; width: min(520px, 92vw); display: flex; flex-direction: column; overflow: hidden; }
  .form-body { padding: 20px; display: flex; flex-direction: column; gap: 14px; overflow-y: auto; }
  .form-group { display: flex; flex-direction: column; gap: 5px; }
  .form-group label { font-size: .8rem; color: #8b949e; font-weight: 500; }
  .form-group input, .form-group textarea, .form-group select {
    background: #0d1117; border: 1px solid #30363d; border-radius: 8px;
    color: #e6edf3; font-family: inherit; font-size: .9rem; padding: 8px 12px; outline: none;
    transition: border-color .2s;
  }
  .form-group input:focus, .form-group textarea:focus, .form-group select:focus { border-color: #4285f4; }
  .form-group textarea { resize: vertical; min-height: 80px; }
  .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .form-footer { display: flex; justify-content: flex-end; gap: 8px; padding: 14px 20px; border-top: 1px solid #30363d; }
  .btn-save { background: #1a73e8; padding: 8px 20px; font-size: .85rem; border-radius: 8px; }
  .btn-save:hover { background: #4285f4; }
</style>
</head>
<body>

<header>
  <div class="header-left">
    <div class="dot"></div>
    <h1>Gemini Bridge</h1>
  </div>
  <nav>
    <button class="active" id="tab-chat" onclick="switchTab('chat')">Chat</button>
    <button id="tab-history" onclick="switchTab('history')">History</button>
    <button id="tab-dashboard" onclick="switchTab('dashboard')">Dashboard</button>
    <button id="tab-agents" onclick="switchTab('agents')">Agents</button>
  </nav>
  <span class="bridge-label">→ localhost:8011</span>
</header>

<!-- Chat -->
<div class="view active" id="view-chat">
  <div class="chat-area" id="chat">
    <div class="empty-state" id="empty">
      <div class="icon">✨</div>
      <p>Send a message to start chatting via the bridge</p>
    </div>
  </div>
  <div class="input-area">
    <textarea id="input" rows="1" placeholder="Type a message… (Enter to send, Shift+Enter for newline)"></textarea>
    <button id="send-btn" onclick="send()">Send</button>
  </div>
</div>

<!-- History -->
<div class="view" id="view-history">
  <div class="scroll-view">
    <div class="toolbar">
      <h2>Message History</h2>
      <div class="toolbar-right">
        <div class="period-pills" id="hist-pills">
          <span class="pill active" data-period="all" onclick="setHistPeriod('all',this)">All time</span>
          <span class="pill" data-period="day" onclick="setHistPeriod('day',this)">Today</span>
          <span class="pill" data-period="week" onclick="setHistPeriod('week',this)">7 days</span>
          <span class="pill" data-period="month" onclick="setHistPeriod('month',this)">30 days</span>
        </div>
        <button class="btn-sm" onclick="loadHistoryTable()">↻ Refresh</button>
      </div>
    </div>
    <table>
      <thead>
        <tr>
          <th>#</th><th>Time</th><th>Role</th><th>Prompt / Response</th>
          <th>Tokens in</th><th>Tokens out</th><th>⏱ sec</th><th>$ Cost</th>
        </tr>
      </thead>
      <tbody id="history-body">
        <tr><td colspan="8" style="text-align:center;color:#484f58;padding:24px">Loading…</td></tr>
      </tbody>
    </table>
  </div>
</div>

<!-- Dashboard -->
<div class="view" id="view-dashboard">
  <div class="scroll-view">
    <div class="toolbar">
      <h2>Usage Dashboard</h2>
      <div class="toolbar-right">
        <div class="period-pills">
          <span class="pill active" data-period="all" onclick="setDashPeriod('all',this)">All time</span>
          <span class="pill" data-period="day" onclick="setDashPeriod('day',this)">Today</span>
          <span class="pill" data-period="week" onclick="setDashPeriod('week',this)">7 days</span>
          <span class="pill" data-period="month" onclick="setDashPeriod('month',this)">30 days</span>
        </div>
        <button class="btn-sm" onclick="loadDashboard()">↻ Refresh</button>
      </div>
    </div>
    <div class="dash-grid" id="dash-grid">
      <div style="color:#484f58;font-size:.9rem">Loading…</div>
    </div>
    <p class="dash-note">* Token counts are estimated (chars ÷ 4). Pricing based on Gemini 2.0 Flash: $0.10/1M input · $0.40/1M output.</p>
  </div>
</div>

<!-- Agents -->
<div class="view" id="view-agents">
  <div class="scroll-view">
    <div class="toolbar">
      <h2>Scheduled Agents</h2>
      <div class="toolbar-right">
        <button class="btn-sm" onclick="loadAgents()">↻ Refresh</button>
        <button class="btn-sm" style="background:#1a2f4a;color:#4285f4" onclick="openAgentForm()">+ New Agent</button>
      </div>
    </div>
    <div class="agent-grid" id="agent-grid">
      <div style="color:#484f58;font-size:.9rem">Loading…</div>
    </div>
  </div>
</div>

<!-- Content viewer modal -->
<div class="modal-overlay" id="modal" onclick="closeModal(event)">
  <div class="modal">
    <div class="modal-header">
      <h3 id="modal-title">Content</h3>
      <div class="modal-actions">
        <button class="copy-btn" id="copy-btn" onclick="copyModal()">Copy</button>
        <button class="close-btn" onclick="closeModalDirect()">✕ Close</button>
      </div>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<!-- Agent form modal -->
<div class="modal-overlay" id="agent-modal" onclick="closeAgentModal(event)">
  <div class="form-modal">
    <div class="modal-header">
      <h3 id="agent-modal-title">New Agent</h3>
      <button class="close-btn" onclick="closeAgentModalDirect()">✕</button>
    </div>
    <div class="form-body">
      <input type="hidden" id="agent-id">
      <div class="form-group">
        <label>Name</label>
        <input type="text" id="agent-name" placeholder="e.g. Daily Summary">
      </div>
      <div class="form-group">
        <label>Prompt</label>
        <textarea id="agent-prompt" placeholder="What should Gemini do?"></textarea>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Hour (0–23)</label>
          <input type="number" id="agent-hour" min="0" max="23" value="9">
        </div>
        <div class="form-group">
          <label>Minute (0–59)</label>
          <input type="number" id="agent-minute" min="0" max="59" value="0">
        </div>
      </div>
      <div class="form-group">
        <label>Days</label>
        <select id="agent-days">
          <option value="*">Every day</option>
          <option value="mon-fri">Mon – Fri</option>
          <option value="sat,sun">Sat – Sun</option>
          <option value="mon">Monday only</option>
          <option value="0">Sunday only</option>
        </select>
      </div>
      <div class="form-group" style="flex-direction:row;align-items:center;gap:10px">
        <label style="margin:0">Enabled</label>
        <label class="toggle">
          <input type="checkbox" id="agent-enabled" checked>
          <span class="toggle-slider"></span>
        </label>
      </div>
    </div>
    <div class="form-footer">
      <button class="close-btn" onclick="closeAgentModalDirect()">Cancel</button>
      <button class="btn-save" onclick="saveAgent()">Save</button>
    </div>
  </div>
</div>

<script>
  const chat = document.getElementById('chat');
  const input = document.getElementById('input');
  const btn = document.getElementById('send-btn');
  let modalContent = '';
  let _histPeriod = 'all';
  let _dashPeriod = 'all';

  // ── Tabs ──
  function switchTab(tab) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    document.getElementById('view-' + tab).classList.add('active');
    document.getElementById('tab-' + tab).classList.add('active');
    if (tab === 'history') loadHistoryTable();
    if (tab === 'dashboard') loadDashboard();
    if (tab === 'agents') loadAgents();
  }

  // ── Chat ──
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  function removeEmpty() { document.getElementById('empty')?.remove(); }

  function addMessage(role, text, stats) {
    removeEmpty();
    const row = document.createElement('div');
    row.className = `msg-row ${role}`;
    const bubble = document.createElement('div');
    bubble.className = 'bubble' + (text === null ? ' error-bubble' : '');
    bubble.textContent = text ?? '⚠ Error reaching the bridge';
    row.appendChild(bubble);
    if (stats) {
      const bar = document.createElement('div');
      bar.className = 'stats-bar';
      const tokIn = stats.input_tokens != null ? stats.input_tokens + ' tok' : stats.prompt_chars + ' ch';
      const tokOut = stats.output_tokens != null ? stats.output_tokens + ' tok' : stats.response_chars + ' ch';
      const costStr = stats.cost_usd != null ? '$' + stats.cost_usd.toFixed(5) : '';
      bar.innerHTML = `
        <div class="stat"><span class="label">in</span><span class="value">${tokIn}</span></div>
        <div class="stat"><span class="label">out</span><span class="value">${tokOut}</span></div>
        <div class="stat time"><span class="label">⏱</span><span class="value">${stats.elapsed_sec}s</span></div>
        <div class="stat speed"><span class="label">⚡</span><span class="value">${stats.chars_per_sec} ch/s</span></div>
        ${costStr ? `<div class="stat cost"><span class="label">$</span><span class="value">${stats.cost_usd.toFixed(5)}</span></div>` : ''}`;
      row.appendChild(bar);
    }
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
  }

  function showTyping() {
    const el = document.createElement('div');
    el.className = 'msg-row assistant'; el.id = 'typing-row';
    el.innerHTML = '<div class="typing-indicator visible"><span></span><span></span><span></span></div>';
    chat.appendChild(el); chat.scrollTop = chat.scrollHeight;
  }
  function removeTyping() { document.getElementById('typing-row')?.remove(); }

  async function loadHistory() {
    try {
      const res = await fetch('/history');
      if (!res.ok) return;
      for (const m of await res.json()) addMessage(m.role, m.content, m.stats);
    } catch (e) { console.warn('history load failed', e); }
  }

  async function send() {
    const text = input.value.trim();
    if (!text || btn.disabled) return;
    input.value = ''; input.style.height = 'auto'; btn.disabled = true;
    addMessage('user', text); showTyping();
    try {
      const res = await fetch('/chat', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({prompt: text}),
      });
      removeTyping();
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      addMessage('assistant', data.content, data.stats);
    } catch (err) { removeTyping(); addMessage('assistant', null); console.error(err); }
    finally { btn.disabled = false; input.focus(); }
  }

  // ── History Table ──
  function setHistPeriod(period, el) {
    _histPeriod = period;
    document.querySelectorAll('#hist-pills .pill').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
    loadHistoryTable();
  }

  async function loadHistoryTable() {
    const tbody = document.getElementById('history-body');
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#484f58;padding:24px">Loading…</td></tr>';
    try {
      const rows = await (await fetch('/history?period=' + _histPeriod)).json();
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#484f58;padding:24px">No messages in this period</td></tr>';
        return;
      }
      window._historyRows = rows;
      tbody.innerHTML = rows.map((m, i) => {
        const s = m.stats || {};
        const preview = m.content.replace(/\\n/g,' ').slice(0,80) + (m.content.length>80?'…':'');
        const ts = m.ts ? new Date(m.ts*1000).toLocaleString() : '—';
        const tokIn = s.input_tokens != null ? s.input_tokens : (s.prompt_chars != null ? '~' + Math.round(s.prompt_chars/4) : '—');
        const tokOut = s.output_tokens != null ? s.output_tokens : (s.response_chars != null ? '~' + Math.round(s.response_chars/4) : '—');
        const cost = s.cost_usd != null ? '$' + Number(s.cost_usd).toFixed(5) : '—';
        return `<tr>
          <td class="stat-cell">${m.id}</td>
          <td class="time-cell">${ts}</td>
          <td style="width:80px"><span class="role-badge ${m.role}">${m.role}</span></td>
          <td class="preview-cell" onclick="openModal('${m.role}',${i})">${escHtml(preview)}</td>
          <td class="stat-cell">${tokIn}</td>
          <td class="stat-cell">${tokOut}</td>
          <td class="stat-cell">${s.elapsed_sec??'—'}</td>
          <td class="cost-cell">${cost}</td>
        </tr>`;
      }).join('');
    } catch { tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#f85149;padding:24px">Failed to load</td></tr>'; }
  }

  function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  // ── Dashboard ──
  function setDashPeriod(period, el) {
    _dashPeriod = period;
    document.querySelectorAll('#view-dashboard .period-pills .pill').forEach(p => p.classList.remove('active'));
    el.classList.add('active');
    loadDashboard();
  }

  function fmtNum(n) {
    if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n/1e3).toFixed(1) + 'K';
    return String(n);
  }

  async function loadDashboard() {
    const grid = document.getElementById('dash-grid');
    grid.innerHTML = '<div style="color:#484f58;font-size:.9rem">Loading…</div>';
    try {
      const d = await (await fetch('/dashboard?period=' + _dashPeriod)).json();
      grid.innerHTML = `
        <div class="dash-card card-blue">
          <div class="card-label">Total Messages</div>
          <div class="card-value">${fmtNum(d.total_messages)}</div>
          <div class="card-sub">${d.sessions} assistant replies</div>
        </div>
        <div class="dash-card card-green">
          <div class="card-label">Input Tokens</div>
          <div class="card-value">${fmtNum(d.input_tokens)}</div>
          <div class="card-sub">estimated (chars ÷ 4)</div>
        </div>
        <div class="dash-card card-purple">
          <div class="card-label">Output Tokens</div>
          <div class="card-value">${fmtNum(d.output_tokens)}</div>
          <div class="card-sub">estimated (chars ÷ 4)</div>
        </div>
        <div class="dash-card card-yellow">
          <div class="card-label">Total Tokens</div>
          <div class="card-value">${fmtNum(d.total_tokens)}</div>
          <div class="card-sub">in + out</div>
        </div>
        <div class="dash-card card-orange">
          <div class="card-label">Estimated Cost</div>
          <div class="card-value">$${d.cost_usd.toFixed(4)}</div>
          <div class="card-sub">Flash pricing</div>
        </div>`;
    } catch { grid.innerHTML = '<div style="color:#f85149;font-size:.9rem">Failed to load dashboard</div>'; }
  }

  // ── Content Modal ──
  function openModal(role, idx) {
    const m = window._historyRows[idx];
    modalContent = m.content;
    document.getElementById('modal-title').textContent = role === 'user' ? 'User Prompt' : 'Assistant Response';
    document.getElementById('modal-body').textContent = m.content;
    document.getElementById('copy-btn').textContent = 'Copy';
    document.getElementById('copy-btn').classList.remove('copied');
    document.getElementById('modal').classList.add('open');
  }
  function closeModalDirect() { document.getElementById('modal').classList.remove('open'); }
  function closeModal(e) { if (e.target===document.getElementById('modal')) closeModalDirect(); }
  async function copyModal() {
    try {
      await navigator.clipboard.writeText(modalContent);
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = modalContent;
      ta.style.cssText = 'position:fixed;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    const b = document.getElementById('copy-btn');
    b.textContent = 'Copied!'; b.classList.add('copied');
    setTimeout(() => { b.textContent = 'Copy'; b.classList.remove('copied'); }, 2000);
  }

  // ── Agents ──
  let _agents = [];

  function dayLabel(d) {
    const map = {'*':'Every day','mon-fri':'Mon–Fri','sat,sun':'Sat–Sun','mon':'Monday','0':'Sunday'};
    return map[d] ?? d;
  }

  function statusHtml(a) {
    if (!a.last_run) return '<span style="color:#484f58">Never run</span>';
    const t = new Date(a.last_run*1000).toLocaleString();
    const cls = a.last_status === 'ok' ? 'status-ok' : 'status-err';
    return `<span class="${cls}">${a.last_status}</span> <span style="color:#484f58">${t}</span>`;
  }

  async function loadAgents() {
    const grid = document.getElementById('agent-grid');
    grid.innerHTML = '<div style="color:#484f58;font-size:.9rem">Loading…</div>';
    _agents = await (await fetch('/agents')).json();
    if (!_agents.length) {
      grid.innerHTML = '<div style="color:#484f58;font-size:.9rem">No agents yet. Create one!</div>';
      return;
    }
    grid.innerHTML = _agents.map((a,i) => `
      <div class="agent-card ${a.enabled?'':'disabled'}" id="acard-${a.id}">
        <div class="agent-card-header">
          <span class="agent-name">${escHtml(a.name)}</span>
          <span class="agent-schedule">${String(a.hour).padStart(2,'0')}:${String(a.minute).padStart(2,'0')} · ${dayLabel(a.days)}</span>
        </div>
        <div class="agent-prompt">${escHtml(a.prompt)}</div>
        <div class="agent-footer">
          <label class="toggle" title="Enable/disable">
            <input type="checkbox" ${a.enabled?'checked':''} onchange="toggleAgent(${a.id},this.checked)">
            <span class="toggle-slider"></span>
          </label>
          <span class="last-run">${statusHtml(a)}</span>
          <button class="btn-sm btn-run" onclick="runNow(${a.id})">▶ Run now</button>
          <button class="btn-sm" onclick="editAgent(${i})">Edit</button>
          <button class="btn-sm btn-danger" onclick="deleteAgent(${a.id})">Delete</button>
        </div>
      </div>`).join('');
  }

  async function toggleAgent(id, enabled) {
    const a = _agents.find(x => x.id === id);
    if (!a) return;
    await fetch('/agents/'+id, {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name:a.name,prompt:a.prompt,hour:a.hour,minute:a.minute,days:a.days,enabled}),
    });
    loadAgents();
  }

  async function runNow(id) {
    const res = await fetch('/agents/'+id+'/run', {method:'POST'});
    if (res.ok) { alert('Agent ran — check History tab for result.'); loadAgents(); }
    else alert('Run failed: ' + await res.text());
  }

  async function deleteAgent(id) {
    if (!confirm('Delete this agent?')) return;
    await fetch('/agents/'+id, {method:'DELETE'});
    loadAgents();
  }

  function openAgentForm() {
    document.getElementById('agent-id').value = '';
    document.getElementById('agent-modal-title').textContent = 'New Agent';
    document.getElementById('agent-name').value = '';
    document.getElementById('agent-prompt').value = '';
    document.getElementById('agent-hour').value = 9;
    document.getElementById('agent-minute').value = 0;
    document.getElementById('agent-days').value = '*';
    document.getElementById('agent-enabled').checked = true;
    document.getElementById('agent-modal').classList.add('open');
  }

  function editAgent(idx) {
    const a = _agents[idx];
    document.getElementById('agent-id').value = a.id;
    document.getElementById('agent-modal-title').textContent = 'Edit Agent';
    document.getElementById('agent-name').value = a.name;
    document.getElementById('agent-prompt').value = a.prompt;
    document.getElementById('agent-hour').value = a.hour;
    document.getElementById('agent-minute').value = a.minute;
    document.getElementById('agent-days').value = a.days;
    document.getElementById('agent-enabled').checked = !!a.enabled;
    document.getElementById('agent-modal').classList.add('open');
  }

  function closeAgentModalDirect() { document.getElementById('agent-modal').classList.remove('open'); }
  function closeAgentModal(e) { if (e.target===document.getElementById('agent-modal')) closeAgentModalDirect(); }

  async function saveAgent() {
    const id = document.getElementById('agent-id').value;
    const body = {
      name: document.getElementById('agent-name').value.trim(),
      prompt: document.getElementById('agent-prompt').value.trim(),
      hour: parseInt(document.getElementById('agent-hour').value),
      minute: parseInt(document.getElementById('agent-minute').value),
      days: document.getElementById('agent-days').value,
      enabled: document.getElementById('agent-enabled').checked,
    };
    if (!body.name || !body.prompt) { alert('Name and prompt are required.'); return; }
    const url = id ? '/agents/'+id : '/agents';
    const method = id ? 'PUT' : 'POST';
    const res = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if (res.ok) { closeAgentModalDirect(); loadAgents(); }
    else alert('Save failed: ' + await res.text());
  }

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeModalDirect(); closeAgentModalDirect(); }
  });

  loadHistory();
  input.focus();
</script>
</body>
</html>"""
