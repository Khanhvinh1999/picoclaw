"""
dispatcher.py — Cloud Psychology Agent Dispatcher
===================================================
Architecture:
  Telegram → dispatcher.py
    ├─ S1 (Gemini Flash Lite / OpenRouter) → empathetic ack   (~500ms)
    ├─ S2 (PicoClaw docker one-shot)       → tool execution   (~5-10s)
    └─ The Loop: S1 refines S2 raw output → edit original message

Flow for complex tasks:
  1. S1 ack sent immediately as Telegram message (msg_id captured)
  2. S2 picoclaw-agent runs in parallel via docker compose
  3. S1 refines S2 raw output for tone
  4. Original S1 message is EDITED with final result ("Steaming Response")

To run:
  1. Stop picoclaw-gateway + launcher: docker stop picoclaw-gateway picoclaw-launcher
  2. Run: python3 dispatcher.py

ENV vars (override via export or .env):
  TELEGRAM_TOKEN, OPENROUTER_API_KEY, S1_MODEL, WORKSPACE_PATH, DOCKER_COMPOSE_DIR
  DISPATCHER_LOG_DIR — directory for dispatcher.log and picoclaw-agent.log (default: ./logs)
"""

import asyncio
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# ── Config ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN",    "8662639337:AAEN0AGFmMZy65QqHJC8l-fZJ69L512OJ6k")
OPENROUTER_KEY     = os.getenv("OPENROUTER_API_KEY", "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")
S1_MODEL           = os.getenv("S1_MODEL",           "google/gemini-2.0-flash-lite-001")
WORKSPACE_PATH     = os.getenv("WORKSPACE_PATH",     str(Path(__file__).parent / "docker/data/workspace"))
DOCKER_COMPOSE_DIR = os.getenv("DOCKER_COMPOSE_DIR", str(Path(__file__).parent / "docker"))
VINH_CHAT_ID       = int(os.getenv("VINH_CHAT_ID",   "8441394500"))
_LOG_ROOT          = Path(__file__).resolve().parent
LOG_DIR            = Path(os.getenv("DISPATCHER_LOG_DIR", str(_LOG_ROOT / "logs")))


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Empty files so `tail -f logs/dispatcher.log` works before first log line
    for name in ("dispatcher.log", "picoclaw-agent.log"):
        (LOG_DIR / name).touch(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(LOG_DIR / "dispatcher.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


_setup_logging()
log = logging.getLogger(__name__)


def _append_agent_run_log(user_text: str, result: subprocess.CompletedProcess[str]) -> None:
    """Full docker stdout/stderr per S2 run (gateway-style visibility without long-running container)."""
    path = LOG_DIR / "picoclaw-agent.log"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sep = "=" * 72
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n{sep}\n[{ts}] returncode={result.returncode}\n")
            f.write(f"USER_MESSAGE ({len(user_text)} chars):\n{user_text}\n\n")
            f.write("--- STDOUT ---\n")
            f.write(result.stdout or "")
            f.write("\n--- STDERR ---\n")
            f.write(result.stderr or "")
            f.write("\n")
    except OSError as e:
        log.warning("Could not append %s: %s", path, e)

# ── Prompts ───────────────────────────────────────────────────────────────────

S1_ACK_SYSTEM = (
    "You are Vincent's (Vinh's) empathetic companion. Respond in Vietnamese only.\n"
    "Output exactly 1 short sentence. No quotes. No markdown. No explanation.\n"
    "Use his name naturally. Just acknowledge you heard and are working on it.\n"
    "Examples: 'Mình nghe rồi Vinh, đang xử lý cho bạn nhé...' / "
    "'Để mình check ngay Vinh, đợi một chút...'"
)

S1_REFINE_SYSTEM = (
    "You are Vincent's (Vinh's) empathetic companion. Respond in Vietnamese only.\n"
    "Rewrite the raw data below in Vinh's style: minimalist, warm, action-oriented.\n"
    "- Keep facts accurate, tone with encouragement\n"
    "- Max 4 sentences. No markdown headers. Minimal emoji.\n"
    "- If good news, be genuinely happy for him.\n"
    "- Never add filler like 'Hãy cho tôi biết nếu bạn cần gì thêm'"
)

S1_EMERGENCY_SYSTEM = (
    "You are Vincent's (Vinh's) deeply empathetic companion. Respond in Vietnamese only.\n"
    "Vinh is in distress. DO NOT suggest tasks, plans, or solutions.\n"
    "Acknowledge his feeling with genuine warmth. Ask ONE caring open question.\n"
    "Be human, not an assistant right now."
)

# ── Helpers ───────────────────────────────────────────────────────────────────

EMERGENCY_KEYWORDS = ["nản", "mệt quá", "chán", "crisis", "overwhelmed", "bỏ cuộc", "không muốn"]

# Short conversational messages that need S1 only — everything else goes to S2
SIMPLE_PATTERNS = [
    r"^(hi|hello|hey|chào|ơi|ok|okay|oke|cảm ơn|thanks|thank you|tks|👍|🙏|✅)[\s!.]*$",
    r"^(được|good|great|tốt|hiểu|rồi|xong)[\s!.]*$",
    r"^(haha|lol|:d|:p|😂|😅|🤣)[\s!.]*$",
]


def is_emergency(text: str) -> bool:
    return any(kw in text.lower() for kw in EMERGENCY_KEYWORDS)


def is_complex(text: str) -> bool:
    """Default to complex (S2) unless the message is clearly short small-talk."""
    import re
    t = text.strip()
    # Very short messages (≤3 words) that match simple greetings → not complex
    if len(t.split()) <= 3:
        for pat in SIMPLE_PATTERNS:
            if re.match(pat, t, re.IGNORECASE):
                return False
    # Everything else goes to S2 — picoclaw decides what to do with it
    return True


def read_workspace_context() -> str:
    tasks_path = Path(WORKSPACE_PATH) / "memory/pending_tasks.md"
    try:
        return tasks_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "No tasks found."


# ── S1: OpenRouter (Gemini Flash Lite) ───────────────────────────────────────

async def openrouter_s1(system: str, user_text: str) -> str:
    """Call S1 model via OpenRouter. Fast empathetic responses ~500ms."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "HTTP-Referer": "https://picoclaw.local",
                    "X-Title": "PicoClaw Dispatcher S1",
                },
                json={
                    "model": S1_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user_text},
                    ],
                    "max_tokens": 80,
                    "temperature": 0.7,
                },
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            log.error("S1 OpenRouter error %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.error("S1 exception: %s", e)
    return "Mình nghe rồi Vinh, đợi mình một chút nhé..."


# ── S2: PicoClaw agent (docker one-shot, full tool access) ───────────────────

async def picoclaw_agent(user_text: str) -> str:
    """S2: Run picoclaw agent via docker compose (full tool access: Gmail, tasks, web).
    Uses the picoclaw-agent service with workspace + credentials volumes."""
    try:
        env = os.environ.copy()
        # Load OPENROUTER_API_KEY from docker/.env if not already set
        env_file = Path(DOCKER_COMPOSE_DIR) / ".env"
        if env_file.exists() and "OPENROUTER_API_KEY" not in env:
            for line in env_file.read_text().splitlines():
                if line.startswith("OPENROUTER_API_KEY="):
                    env["OPENROUTER_API_KEY"] = line.split("=", 1)[1].strip()

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                ["docker", "compose", "--profile", "agent", "run", "--rm",
                 "picoclaw-agent", "-m", user_text],
                cwd=DOCKER_COMPOSE_DIR,
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
        )
        _append_agent_run_log(user_text, result)
        output = result.stdout.strip()
        if result.returncode != 0 or not output:
            log.error("picoclaw-agent error: %s", result.stderr[:300])
            return "Vinh ơi, mình gặp lỗi khi xử lý yêu cầu này."

        # Strip ANSI escape codes
        output = re.sub(r'\x1b\[[0-9;]*m', '', output)
        # Extract only the final agent response (skip banner + Go log lines)
        lines = output.splitlines()
        response_lines = [
            l for l in lines
            if l.strip()
            and not re.match(r'^\d{2}:\d{2}:\d{2}\s+[A-Z]+\s+', l)
            and not re.match(r'^[█╔╗╚╝║═╠╣╦╩╬─│ ]+$', l)
            and '>' not in l[:30]
        ]
        agent_response = '\n'.join(response_lines).strip()
        log.info("picoclaw-agent response (%d chars): %s", len(agent_response), agent_response[:80])
        return agent_response or "Mình đã xử lý xong Vinh, nhưng không có kết quả nào trả về."

    except subprocess.TimeoutExpired:
        log.error("picoclaw-agent timed out")
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(LOG_DIR / "picoclaw-agent.log", "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 72}\n[{ts}] TIMEOUT after 60s\nUSER_MESSAGE:\n{user_text}\n\n")
        except OSError:
            pass
        return "Xin lỗi Vinh, quá trình xử lý mất quá lâu."
    except Exception as e:
        log.error("picoclaw-agent exception: %s", e)
        return "Vinh ơi, mình gặp lỗi khi xử lý yêu cầu này."


# ── The Psychology Loop ───────────────────────────────────────────────────────

async def psychology_loop(user_text: str) -> tuple[str | None, str]:
    """
    Returns: (s1_ack, final_response)

    Emergency  → s1_ack=None, final=empathetic response (S1 only)
    Simple     → s1_ack=None, final=S1 conversational reply
    Complex    → s1_ack=S1 ack sent immediately,
                 final=S1 refinement of S2 tool result (message will be edited)
    """
    if is_emergency(user_text):
        final = await openrouter_s1(S1_EMERGENCY_SYSTEM, user_text)
        return None, final

    if not is_complex(user_text):
        final = await openrouter_s1(S1_ACK_SYSTEM, user_text)
        return None, final

    # Complex: S1 ack + S2 in parallel + The Loop
    s1_task = asyncio.create_task(openrouter_s1(S1_ACK_SYSTEM, user_text))
    s2_task = asyncio.create_task(picoclaw_agent(user_text))

    s1_ack = await s1_task   # fires fast (~500ms)
    s2_raw = await s2_task   # waits for tool result

    # The Loop: S1 refines S2 raw data for tone
    final = await openrouter_s1(
        S1_REFINE_SYSTEM,
        f"Vinh asked: {user_text}\n\nRaw S2 result:\n{s2_raw}",
    )
    return s1_ack, final


# ── Telegram Handler ──────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    chat_id   = update.effective_chat.id
    log.info("[MSG] chat=%s  text=%r", chat_id, user_text[:80])

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    s1_ack, final = await psychology_loop(user_text)

    if s1_ack:
        # Complex task: send S1 ack immediately, then EDIT it with the final result
        sent_msg = await update.message.reply_text(s1_ack)
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        # "Steaming Response" — message evolves from ack to full result
        await sent_msg.edit_text(final)
    else:
        # Simple or emergency: single message
        await update.message.reply_text(final)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("""
╔══════════════════════════════════════════════════╗
║  🧠  Psychology Agent Dispatcher (Cloud)         ║
║  S1: Gemini Flash Lite  |  S2: PicoClaw Docker  ║
╚══════════════════════════════════════════════════╝
""")
    log.info("Log directory: %s", LOG_DIR.resolve())
    log.info("S1 model: %s", S1_MODEL)
    log.info("S2 engine: picoclaw-agent (docker one-shot)")
    log.info("Workspace: %s", WORKSPACE_PATH)

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Dispatcher polling Telegram...")
    log.info("Ensure picoclaw-gateway + picoclaw-launcher are STOPPED to avoid 409 conflicts")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
