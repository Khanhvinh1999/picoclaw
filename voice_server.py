"""
voice_server.py — Voice Interface Server
==========================================
Standalone FastAPI server for the voice call UI.
Runs alongside dispatcher.py (Telegram) independently.

Endpoints:
  POST /transcribe  — audio blob → Groq Whisper → text
  POST /chat        — text → psychology loop → response text
  POST /tts         — text → ElevenLabs MP3 (if ELEVENLABS_* env set)
  GET  /tts-enabled — {"enabled": bool}
  GET  /            — serve voice_ui.html

Run:
  /opt/anaconda3/bin/python3 voice_server.py
  Open: http://localhost:8080
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import ssl
import subprocess
from pathlib import Path

import httpx
import websockets
from websockets.exceptions import ConnectionClosed
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


def _load_env_files() -> None:
    """Load KEY=value from .env files if present (no python-dotenv dependency)."""
    for env_path in (
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parent / "docker" / ".env",
    ):
        if not env_path.is_file():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except OSError:
            pass


_load_env_files()

# ── Config ────────────────────────────────────────────────────────────────────

GROQ_API_KEY       = os.getenv("GROQ_API_KEY", "").strip()
OPENROUTER_KEY     = os.getenv("OPENROUTER_API_KEY", "").strip()
S1_MODEL           = os.getenv("S1_MODEL",            "google/gemini-2.0-flash-lite-001")
DOCKER_COMPOSE_DIR = os.getenv("DOCKER_COMPOSE_DIR",  str(Path(__file__).parent / "docker"))
WORKSPACE_PATH     = os.getenv("WORKSPACE_PATH",      str(Path(__file__).parent / "docker/data/workspace"))
PORT               = int(os.getenv("VOICE_PORT",      "8080"))
UI_FILE            = Path(__file__).parent / "voice_ui.html"
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
# STT: empty / auto = Whisper auto-detect (English speech → English text; response still Vietnamese via prompts)
_whisper = os.getenv("WHISPER_LANGUAGE", "").strip().lower()
WHISPER_LANGUAGE_DEFAULT = None if _whisper in ("", "auto", "detect") else _whisper

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


def _wss_ssl_context() -> ssl.SSLContext:
    """TLS for wss:// (ElevenLabs). Python on macOS often lacks a CA bundle; certifi fixes verify failures."""
    ctx = ssl.create_default_context()
    bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if bundle and Path(bundle).is_file():
        ctx.load_verify_locations(bundle)
        return ctx
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except ImportError:
        log.warning(
            "Install certifi for reliable ElevenLabs WSS: pip install certifi "
            "(or set SSL_CERT_FILE to your CA bundle)."
        )
    return ctx


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Jarvis Voice Interface")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── ElevenLabs TTS cache (credit saver) ──────────────────────────────────────

CACHE_DIR = Path(__file__).parent / ".cache" / "elevenlabs_tts"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── Prompts ───────────────────────────────────────────────────────────────────

S1_ACK_SYSTEM = (
    "You are Vincent's (Vinh's) empathetic companion. Respond in English only.\n"
    "Output exactly 1 short sentence. No quotes. No markdown.\n"
    "Use his name naturally. Just acknowledge you heard and are working on it."
)

S1_REFINE_SYSTEM = (
    "You are Vincent's (Vinh's) empathetic companion. Respond in English only.\n"
    "Rewrite the raw data in Vinh's style: minimalist, warm, action-oriented.\n"
    "- Keep facts accurate, add encouragement\n"
    "- Max 3 sentences. No markdown. No filler phrases.\n"
    "- For voice: write naturally spoken English, not text-formatted."
)

S1_EMERGENCY_SYSTEM = (
    "You are Vincent's (Vinh's) deeply empathetic companion. Respond in English only.\n"
    "Vinh is in distress. DO NOT suggest tasks or solutions.\n"
    "Acknowledge his feeling warmly. Ask ONE caring question. Be human."
)

EMERGENCY_KEYWORDS = ["nản", "mệt quá", "chán", "crisis", "overwhelmed", "bỏ cuộc", "không muốn"]

SIMPLE_PATTERNS = [
    r"^(hi|hello|hey|chào|ơi|ok|okay|oke|cảm ơn|thanks|thank you|tks)[\s!.]*$",
    r"^(được|good|great|tốt|hiểu|rồi|xong)[\s!.]*$",
    r"^(haha|lol|😂|😅)[\s!.]*$",
]


def is_emergency(text: str) -> bool:
    return any(kw in text.lower() for kw in EMERGENCY_KEYWORDS)


def is_complex(text: str) -> bool:
    t = text.strip()
    if len(t.split()) <= 3:
        for pat in SIMPLE_PATTERNS:
            if re.match(pat, t, re.IGNORECASE):
                return False
    return True


# ── S1: OpenRouter ────────────────────────────────────────────────────────────

async def openrouter_s1(system: str, user_text: str) -> str:
    if not OPENROUTER_KEY:
        log.error("OPENROUTER_API_KEY not set")
        return "Voice assistant is not configured (missing OPENROUTER_API_KEY)."
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "HTTP-Referer": "https://picoclaw.local",
                    "X-Title": "PicoClaw Voice S1",
                },
                json={
                    "model": S1_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user_text},
                    ],
                    "max_tokens": 100,
                    "temperature": 0.7,
                },
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("S1 error: %s", e)
    return "Mình nghe rồi Vinh, đợi một chút nhé..."


async def openrouter_stream(system: str, user_text: str):
    """Yield content deltas from OpenRouter streaming API (SSE)."""
    if not OPENROUTER_KEY:
        log.error("OPENROUTER_API_KEY not set (stream)")
        return
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "HTTP-Referer": "https://picoclaw.local",
                "X-Title": "PicoClaw Voice S1 Stream",
            },
            json={
                "model": S1_MODEL,
                "stream": True,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 240,
                "temperature": 0.7,
            },
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    return
                try:
                    payload = json.loads(data)
                    delta = payload.get("choices", [{}])[0].get("delta", {})
                    chunk = delta.get("content")
                    if chunk:
                        yield chunk
                except Exception:
                    continue


# ── S2: PicoClaw docker ───────────────────────────────────────────────────────

async def picoclaw_agent(user_text: str) -> str:
    try:
        env = os.environ.copy()
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
        output = result.stdout.strip()
        if result.returncode != 0 or not output:
            return "Vinh ơi, mình gặp lỗi khi xử lý yêu cầu này."

        output = re.sub(r'\x1b\[[0-9;]*m', '', output)
        lines = output.splitlines()
        response_lines = [
            l for l in lines
            if l.strip()
            and not re.match(r'^\d{2}:\d{2}:\d{2}\s+[A-Z]+\s+', l)
            and not re.match(r'^[█╔╗╚╝║═╠╣╦╩╬─│ ]+$', l)
            and '>' not in l[:30]
        ]
        return '\n'.join(response_lines).strip() or "Xong rồi Vinh."

    except subprocess.TimeoutExpired:
        return "Xin lỗi Vinh, quá trình xử lý mất quá lâu."
    except Exception as e:
        log.error("picoclaw error: %s", e)
        return "Vinh ơi, mình gặp lỗi."


# ── Psychology Loop ───────────────────────────────────────────────────────────

async def psychology_loop(user_text: str) -> tuple[str | None, str]:
    """Returns (s1_ack, final_response). s1_ack is None for simple/emergency."""
    if is_emergency(user_text):
        return None, await openrouter_s1(S1_EMERGENCY_SYSTEM, user_text)

    if not is_complex(user_text):
        return None, await openrouter_s1(S1_ACK_SYSTEM, user_text)

    # Complex: S1 ack + S2 in parallel + The Loop
    s1_task = asyncio.create_task(openrouter_s1(S1_ACK_SYSTEM, user_text))
    s2_task = asyncio.create_task(picoclaw_agent(user_text))

    s1_ack = await s1_task
    s2_raw = await s2_task

    final = await openrouter_s1(
        S1_REFINE_SYSTEM,
        f"Vinh asked: {user_text}\n\nRaw S2 result:\n{s2_raw}",
    )
    return s1_ack, final


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_ui():
    if not UI_FILE.exists():
        return JSONResponse({"error": "voice_ui.html not found"}, status_code=404)
    return FileResponse(UI_FILE, media_type="text/html")


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    stt_language: str = Form(default=""),
):
    """Receive audio blob, send to Groq Whisper, return transcription.

    stt_language: '' / auto = auto-detect; vi / en = force language (ISO 639-1).
    Falls back to WHISPER_LANGUAGE env if form field empty.
    """
    try:
        if not GROQ_API_KEY:
            return JSONResponse(
                {"error": "GROQ_API_KEY not set — add it to .env (see .env.example)"},
                status_code=503,
            )

        audio_bytes = await audio.read()
        log.info("Transcribing audio: %d bytes, content_type=%s", len(audio_bytes), audio.content_type)

        # Determine file extension from content-type (Groq needs matching filename + type)
        ct = (audio.content_type or "audio/webm").split(";")[0].strip()
        ext_map = {"audio/mp4": "mp4", "audio/x-m4a": "m4a", "audio/mpeg": "mp3",
                   "audio/ogg": "ogg", "audio/wav": "wav", "audio/webm": "webm"}
        ext = ext_map.get(ct, "webm")
        filename = f"recording.{ext}"
        log.info("Sending to Groq: filename=%s content_type=%s size=%d", filename, ct, len(audio_bytes))

        raw_lang = (stt_language or "").strip().lower()
        if raw_lang in ("", "auto", "detect"):
            lang = WHISPER_LANGUAGE_DEFAULT
        else:
            lang = raw_lang if len(raw_lang) == 2 else None

        form: dict[str, str] = {"model": "whisper-large-v3", "response_format": "json"}
        if lang:
            form["language"] = lang
            log.info("Groq STT language forced: %s", lang)
        else:
            log.info("Groq STT language: auto-detect")

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (filename, audio_bytes, ct)},
                data=form,
            )
            if resp.status_code == 200:
                text = resp.json().get("text", "").strip()
                log.info("Transcribed: %r", text)
                return {"text": text}
            log.error("Groq error %s: %s", resp.status_code, resp.text[:200])
            return JSONResponse({"error": "Transcription failed"}, status_code=500)

    except Exception as e:
        log.error("Transcribe exception: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


class ChatRequest(BaseModel):
    text: str


@app.post("/chat")
async def chat(req: ChatRequest):
    """Run psychology loop, return s1_ack and final response."""
    log.info("[VOICE CHAT] text=%r", req.text[:80])
    s1_ack, final = await psychology_loop(req.text)
    # TTS priority rule:
    # - emergency and S2-driven requests: speak (high)
    # - small-talk / short confirmations: do not speak (low)
    if is_emergency(req.text):
        tts_priority = "high"
    elif is_complex(req.text):
        tts_priority = "high"
    else:
        tts_priority = "low"

    return {"s1_ack": s1_ack, "response": final, "tts_priority": tts_priority}


@app.get("/tts-enabled")
async def tts_enabled():
    return {"enabled": bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)}


class TTSRequest(BaseModel):
    text: str


@app.post("/tts")
async def elevenlabs_tts(req: TTSRequest):
    """Proxy to ElevenLabs — key never sent to browser."""
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        return JSONResponse({"error": "ElevenLabs not configured"}, status_code=503)

    text = (req.text or "").strip()[:5000]
    if not text:
        return JSONResponse({"error": "Empty text"}, status_code=400)

    text = (req.text or "").strip()[:5000]
    # Cache lookup first to avoid repeated ElevenLabs calls
    cache_hash = hashlib.sha256(
        f"{ELEVENLABS_VOICE_ID}|{ELEVENLABS_MODEL_ID}|{text}".encode("utf-8")
    ).hexdigest()
    cache_file = CACHE_DIR / f"{cache_hash}.mp3"
    if cache_file.is_file() and cache_file.stat().st_size > 0:
        log.info("ElevenLabs cache hit (%d bytes)", cache_file.stat().st_size)
        audio_bytes = cache_file.read_bytes()
        return Response(content=audio_bytes, media_type="audio/mpeg")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                url,
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json={
                    "text": text,
                    "model_id": ELEVENLABS_MODEL_ID,
                },
            )
        if resp.status_code != 200:
            log.error("ElevenLabs %s: %s", resp.status_code, resp.text[:400])
            return JSONResponse({"error": "ElevenLabs TTS failed"}, status_code=502)

        # Save to cache after successful call
        cache_file.write_bytes(resp.content)
        log.info("ElevenLabs TTS ok: %d bytes (cached)", len(resp.content))
        return Response(content=resp.content, media_type="audio/mpeg")
    except Exception as e:
        log.error("ElevenLabs exception: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def elevenlabs_stream_mp3(text: str):
    """Stream MP3 bytes from ElevenLabs HTTP streaming endpoint."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            url,
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": ELEVENLABS_MODEL_ID,
                "output_format": "mp3_44100_128",
                "optimize_streaming_latency": 2,
            },
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(f"ElevenLabs stream failed {resp.status_code}: {body[:200]!r}")
            async for chunk in resp.aiter_bytes():
                if chunk:
                    yield chunk


async def elevenlabs_ws_stream(text_queue: "asyncio.Queue[str | None]"):
    """Bidirectional WS: send text chunks, receive base64 audio chunks."""
    if not (ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID):
        return

    # Recommended low-latency model for realtime; fall back to configured model if needed
    model_id = ELEVENLABS_MODEL_ID
    uri = f"wss://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream-input?model_id={model_id}"

    async with websockets.connect(
        uri,
        additional_headers={"xi-api-key": ELEVENLABS_API_KEY},
        ssl=_wss_ssl_context(),
    ) as elws:
        send_lock = asyncio.Lock()
        # Initialize connection (space text keeps socket open)
        init_msg = {
            "text": " ",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8, "use_speaker_boost": False},
            "generation_config": {"chunk_length_schedule": [50, 120, 160, 290]},
        }
        async with send_lock:
            await elws.send(json.dumps(init_msg))
        stop_event = asyncio.Event()

        async def sender():
            while True:
                t = await text_queue.get()
                if t is None:
                    stop_event.set()
                    # flush remaining buffer and close
                    async with send_lock:
                        await elws.send(json.dumps({"text": "", "flush": True}))
                    return
                if not t:
                    continue
                async with send_lock:
                    await elws.send(json.dumps({"text": t}))

        async def keepalive():
            """ElevenLabs stream-input closes after ~20s of no new text.
            Keep the socket alive by sending a single space periodically.
            """
            # Send slightly faster than the 20s policy.
            interval_s = 5
            while not stop_event.is_set():
                try:
                    await asyncio.sleep(interval_s)
                    if stop_event.is_set():
                        break
                    async with send_lock:
                        await elws.send(json.dumps({"text": " "}))
                except Exception:
                    # If WS is closing or network flaky, just stop keepalive.
                    break

        async def receiver():
            while True:
                try:
                    msg = await elws.recv()
                except ConnectionClosed as e:
                    log.warning(
                        "ElevenLabs WS closed: code=%s reason=%s",
                        getattr(e, "code", None),
                        getattr(e, "reason", None) or str(e),
                    )
                    stop_event.set()
                    return
                data = json.loads(msg)
                if data.get("audio"):
                    yield base64.b64decode(data["audio"])
                if data.get("isFinal"):
                    stop_event.set()
                    return

        sender_task = asyncio.create_task(sender())
        keepalive_task = asyncio.create_task(keepalive())
        try:
            async for audio_bytes in receiver():
                yield audio_bytes
        finally:
            keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await keepalive_task
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender_task


def _sentence_splitter():
    """Coroutine that yields complete sentences from a stream of text."""
    buf = ""
    end_re = re.compile(r"([.!?]+)(\s+|$)")
    while True:
        chunk = (yield)
        if chunk is None:
            tail = buf.strip()
            if tail:
                yield tail
            return
        buf += chunk
        while True:
            m = end_re.search(buf)
            if not m:
                break
            cut = m.end()
            sent = buf[:cut].strip()
            buf = buf[cut:]
            if sent:
                yield sent


def split_sentences_incremental(state: dict, delta: str) -> list[str]:
    """Incrementally split sentences from streamed text.

    Keeps a buffer in `state["buf"]` and returns any complete sentences found.
    """
    buf = state.get("buf", "") + (delta or "")
    out: list[str] = []
    end_re = state.setdefault("end_re", re.compile(r"([.!?]+)(\s+|$)"))

    while True:
        m = end_re.search(buf)
        if not m:
            break
        cut = m.end()
        sent = buf[:cut].strip()
        buf = buf[cut:]
        if sent:
            out.append(sent)

    state["buf"] = buf
    return out


def flush_sentence_buffer(state: dict) -> str | None:
    tail = (state.get("buf") or "").strip()
    state["buf"] = ""
    return tail or None


@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket):
    """Streaming pipeline: stream OpenRouter tokens + stream ElevenLabs MP3 back to browser.

    Client sends JSON: {"text": "..."}
    Server sends JSON text frames (type=text_start/text_delta/text_end/audio_start/audio_end)
    and binary audio frames (audio/mpeg chunks).
    """
    await ws.accept()
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            user_text = (msg.get("text") or "").strip()
            if not user_text:
                await ws.send_json({"type": "error", "message": "empty text"})
                continue

            tts_priority = "high" if (is_emergency(user_text) or is_complex(user_text)) else "low"
            await ws.send_json({"type": "tts_priority", "value": tts_priority})

            # Stage 1: streaming ACK (UI updates as soon as first token arrives)
            await ws.send_json({"type": "text_start", "phase": "ack"})
            async for delta in openrouter_stream(S1_ACK_SYSTEM, user_text):
                await ws.send_json({"type": "text_delta", "phase": "ack", "delta": delta})
            await ws.send_json({"type": "text_end", "phase": "ack"})

            if tts_priority == "low":
                # no S2, no audio
                continue

            # Stage 2: tool execution
            s2_raw = await picoclaw_agent(user_text)

            # Stage 3: stream final + sentence-level TTS
            await ws.send_json({"type": "text_start", "phase": "final"})
            audio_started = False

            # WS TTS stream-input: keep one continuous audio stream
            tts_queue: asyncio.Queue[str | None] = asyncio.Queue()
            el_configured = bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)

            async def tts_forwarder():
                nonlocal audio_started
                if not el_configured:
                    return
                if not audio_started:
                    audio_started = True
                    await ws.send_json({"type": "audio_start", "format": "audio/mpeg"})
                try:
                    async for a in elevenlabs_ws_stream(tts_queue):
                        await ws.send_bytes(a)
                except Exception as e:
                    log.warning("ElevenLabs stream forwarder stopped: %s", e)

            tts_task = asyncio.create_task(tts_forwarder())

            try:
                try:
                    async for delta in openrouter_stream(
                        S1_REFINE_SYSTEM,
                        f"Vinh asked: {user_text}\n\nRaw S2 result:\n{s2_raw}",
                    ):
                        await ws.send_json({"type": "text_delta", "phase": "final", "delta": delta})
                        if el_configured:
                            await tts_queue.put(delta)
                finally:
                    if el_configured:
                        await tts_queue.put(None)
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await tts_task
            finally:
                try:
                    await ws.send_json({"type": "text_end", "phase": "final"})
                except Exception:
                    pass
                if audio_started:
                    try:
                        await ws.send_json({"type": "audio_end"})
                    except Exception:
                        pass

    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════╗
║  🎙  Jarvis Voice Interface                      ║
║  STT: Groq  |  TTS: ElevenLabs or Browser       ║
╚══════════════════════════════════════════════════╝
""")
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY empty — /transcribe will fail (set in repo .env or docker/.env)")
    if not OPENROUTER_KEY:
        log.warning("OPENROUTER_API_KEY empty — chat/WebSocket LLM will fail")
    log.info("Starting voice server on http://localhost:%d", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
