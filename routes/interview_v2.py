"""
Interview V2  -  gemini-3.5-transcribe-live (STT) + gemini-3.6-flash (LLM)
===========================================================================
Pipeline:
  1. Browser audio  ->  gemini-3.5-transcribe-live  ->  live transcript on HUD
  2. Silence detected (1.5s no new chunks) OR turn_complete
                    ->  gemini-3.6-flash (thinking)  ->  detailed answer streamed to HUD
"""
import asyncio
import json
import traceback
import os
import base64

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types
from dotenv import load_dotenv

from services.session_store import session_store
from config import (
    GEMINI_API_KEY,
    COPILOT_SYSTEM_INSTRUCTION,
    SYSTEM_INSTRUCTION,
    SEND_SAMPLE_RATE,
)

load_dotenv()

router = APIRouter()

STT_MODEL = os.environ.get("STT_MODEL", "gemini-3.5-transcribe-live")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.6-flash")
SILENCE_SECONDS = float(os.environ.get("SILENCE_SECONDS", "0.6"))

print(f"[InterviewV2] STT={STT_MODEL} | LLM={LLM_MODEL} | silence={SILENCE_SECONDS}s ({int(SILENCE_SECONDS*1000)}ms)")

client = genai.Client(
    http_options={"api_version": "v1beta"},
    api_key=GEMINI_API_KEY,
)


def _build_stt_config():
    return types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
    )


async def _generate_answer(system_prompt: str, contents: list):
    """Stream answer chunks immediately as they are generated with multi-turn history."""
    gen_config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.7,
    )
    response_stream = await client.aio.models.generate_content_stream(
        model=LLM_MODEL,
        contents=contents,
        config=gen_config,
    )
    async for chunk in response_stream:
        if chunk.text:
            yield chunk.text


@router.websocket("/interview/v2/{session_id}")
async def interview_v2_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    print(f"[InterviewV2] WebSocket accepted - {session_id}")

    session = session_store.get_session(session_id)
    if session is None:
        print(f"[InterviewV2] Session {session_id} not in memory (Cloud Run scale or cold start). Recovering with default resume...")
        default_resume_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads", "default_resume.txt")
        resume_text = ""
        if os.path.exists(default_resume_path):
            try:
                with open(default_resume_path, "r", encoding="utf-8") as f:
                    resume_text = f.read().strip()
            except Exception as e:
                print(f"[InterviewV2] Error reading default resume: {e}")
        if not resume_text:
            resume_text = "Target Role: AI & ML / Software Engineer."
        session_store.create_session(resume_text, "cloud_run_recovered_session", is_copilot=True)
        session = session_store.get_session(session_id) or {
            "resume_text": resume_text,
            "filename": "cloud_run_recovered_session",
            "is_copilot": True,
        }

    resume_text = session["resume_text"]
    is_copilot = session.get("is_copilot", True)
    system_prompt = (
        COPILOT_SYSTEM_INSTRUCTION.format(resume_text=resume_text)
        if is_copilot
        else SYSTEM_INSTRUCTION.format(resume_text=resume_text)
    )

    shutdown_event = asyncio.Event()
    llm_lock = asyncio.Lock()
    ws_lock = asyncio.Lock()
    audio_queue = asyncio.Queue(maxsize=200)

    async def send_ws(payload: dict):
        async with ws_lock:
            try:
                await websocket.send_text(json.dumps(payload))
            except Exception:
                pass

    # Mutable state shared between coroutines
    state = {
        "transcript": "",       # accumulates STT chunks for the current turn
        "timer": None,          # asyncio Task for the silence countdown
        "history": [],          # list[types.Content] for full multi-turn conversational context
    }

    async def _run_llm(transcript: str, image_part: types.Part | None = None):
        """Send transcript and optional screen capture image to LLM and stream the answer to HUD."""
        async with llm_lock:
            try:
                print(f"[InterviewV2] LLM <- {transcript[:80]}..." + (" [WITH SCREEN IMAGE]" if image_part else ""))
                await send_ws({"type": "status", "status": "thinking"})

                # Build message parts (image prepended if present)
                parts = []
                if image_part:
                    parts.append(image_part)
                parts.append(types.Part.from_text(text=transcript))

                # Append user turn to history
                state["history"].append(
                    types.Content(role="user", parts=parts)
                )

                first_chunk = True
                full = ""
                async for chunk in _generate_answer(system_prompt, state["history"]):
                    if first_chunk:
                        await send_ws({"type": "status", "status": "speaking"})
                        first_chunk = False
                    full += chunk
                    await send_ws({
                        "type": "transcript_chunk",
                        "role": "model",
                        "content": chunk,
                    })

                # Append model response to history
                if full:
                    state["history"].append(
                        types.Content(role="model", parts=[types.Part.from_text(text=full)])
                    )

                session_store.update_session_text(session_id, full)
                await send_ws({"type": "audio_end"})
                await send_ws({"type": "status", "status": "listening"})
                print(f"[InterviewV2] LLM done - {len(full.split())} words")
                print(f"\n{'='*30} [LLM ANSWER] {'='*30}\n{full.strip()}\n{'='*74}\n")

            except WebSocketDisconnect:
                shutdown_event.set()
            except Exception as exc:
                print(f"[InterviewV2] LLM error: {exc}")
                traceback.print_exc()
                await send_ws({"type": "error", "message": str(exc)})

    def _reset_silence_timer():
        """Cancel existing timer and start a fresh one only if SILENCE_SECONDS > 0."""
        if SILENCE_SECONDS <= 0:
            return
        old = state["timer"]
        if old and not old.done():
            old.cancel()
        state["timer"] = asyncio.create_task(_silence_countdown())

    async def _silence_countdown():
        """After SILENCE_SECONDS with no new STT chunks, fire the LLM."""
        await asyncio.sleep(SILENCE_SECONDS)
        transcript = state["transcript"].strip()
        if transcript:
            state["transcript"] = ""
            print(f"[InterviewV2] Silence timeout -> LLM: {transcript[:80]}")
            asyncio.create_task(_run_llm(transcript))

    def _got_transcript_chunk(text: str):
        """Called whenever a STT chunk arrives."""
        state["transcript"] += text
        _reset_silence_timer()

    async def browser_receiver():
        """Receives audio chunks and control messages from Desktop HUD."""
        try:
            while not shutdown_event.is_set():
                try:
                    message = await websocket.receive()
                except WebSocketDisconnect:
                    shutdown_event.set()
                    return
                if message.get("type") == "websocket.disconnect":
                    shutdown_event.set()
                    return

                # Audio binary from client mic/system
                if "bytes" in message and message["bytes"]:
                    try:
                        audio_queue.put_nowait(message["bytes"])
                    except asyncio.QueueFull:
                        pass

                # Text or control commands from client
                elif "text" in message and message["text"]:
                    try:
                        msg = json.loads(message["text"])
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")
                    if msg_type == "stop_and_generate":
                        if state["timer"] and not state["timer"].done():
                            state["timer"].cancel()
                        question_text = (msg.get("content") or state["transcript"]).strip()
                        state["transcript"] = ""
                        if question_text:
                            print(f"[InterviewV2] Manual Stop & Generate -> LLM: {question_text[:80]}")
                            asyncio.create_task(_run_llm(question_text))

                    elif msg_type == "text" and msg.get("content"):
                        if state["timer"] and not state["timer"].done():
                            state["timer"].cancel()
                        state["transcript"] = ""
                        txt = msg["content"].strip()
                        await send_ws({
                            "type": "transcript", "role": "user", "content": txt
                        })
                        asyncio.create_task(_run_llm(txt))

                    elif msg_type == "screen_analysis":
                        if state["timer"] and not state["timer"].done():
                            state["timer"].cancel()
                        state["transcript"] = ""

                        image_b64 = msg.get("image", "")
                        mime_type = msg.get("mime_type", "image/jpeg")
                        custom_prompt = (msg.get("prompt") or "").strip()

                        prompt_text = (
                            custom_prompt
                            or "Please analyze this coding problem/question shown on screen:\n"
                               "1. **Approach & Algorithm**: 2-3 sentences explaining the optimal algorithm (e.g., Two Pointers, Dynamic Programming, Monotonic Stack, BFS/DFS).\n"
                               "2. **Optimal Code**: Clean, well-commented code solution matching the language visible on screen (or Python by default).\n"
                               "3. **Complexity Analysis**: Time complexity and Space complexity with brief explanation.\n"
                               "4. **Key Edge Cases**: Constraints or corner cases handled."
                        )

                        await send_ws({
                            "type": "transcript",
                            "role": "user",
                            "content": f"📸 [Screen Analysis: {custom_prompt or 'Coding Problem on Screen'}]",
                        })

                        try:
                            image_bytes = base64.b64decode(image_b64)
                            image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
                            asyncio.create_task(_run_llm(prompt_text, image_part))
                        except Exception as img_err:
                            print(f"[InterviewV2] Image decode error: {img_err}")
                            await send_ws({
                                "type": "error",
                                "message": f"Failed to process screen capture: {img_err}",
                            })

                    elif msg_type == "clear":
                        state["transcript"] = ""
                        if state["timer"] and not state["timer"].done():
                            state["timer"].cancel()

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not shutdown_event.is_set():
                print(f"[InterviewV2] browser_receiver error: {exc}")
            shutdown_event.set()

    async def stt_runner():
        """
        Maintains an auto-reconnecting STT connection to Google.
        If Google drops the live connection (e.g. 10-15m timeout or code 1008),
        this runner reconnects automatically without disconnecting the HUD.
        """
        stt_config = _build_stt_config()
        reconnect_delay = 1.0

        while not shutdown_event.is_set():
            try:
                print(f"[InterviewV2] Connecting to {STT_MODEL}...")
                async with client.aio.live.connect(model=STT_MODEL, config=stt_config) as stt_session:
                    print("[InterviewV2] STT connected and ready")
                    await send_ws({"type": "status", "status": "ready"})
                    reconnect_delay = 1.0  # reset backoff on successful connect

                    async def forward_audio():
                        while not shutdown_event.is_set():
                            data = await audio_queue.get()
                            if data is None:
                                return
                            try:
                                await stt_session.send_realtime_input(
                                    audio={"data": data, "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"}
                                )
                            except Exception:
                                return

                    async def receive_transcripts():
                        async for response in stt_session.receive():
                            if shutdown_event.is_set():
                                return

                            server_content = getattr(response, "server_content", None)
                            tx = None
                            if server_content:
                                inp = getattr(server_content, "input_transcription", None)
                                if inp:
                                    tx = getattr(inp, "text", None)
                                if not tx:
                                    out = getattr(server_content, "output_transcription", None)
                                    if out:
                                        tx = getattr(out, "text", None)
                                if not tx:
                                    mt = getattr(server_content, "model_turn", None)
                                    if mt and getattr(mt, "parts", None):
                                        for p in mt.parts:
                                            if getattr(p, "text", None):
                                                tx = p.text
                                                break
                            if not tx:
                                tx = getattr(response, "text", None)

                            if tx:
                                _got_transcript_chunk(tx)
                                print(f"[InterviewV2] STT chunk: {tx[:60]}")
                                await send_ws({
                                    "type": "transcript",
                                    "role": "user",
                                    "content": tx,
                                })

                            sc = server_content
                            turn_done = (
                                (sc and getattr(sc, "turn_complete", False))
                                or getattr(response, "turn_complete", False)
                            )
                            if turn_done and SILENCE_SECONDS > 0:
                                t = state["transcript"].strip()
                                state["transcript"] = ""
                                if state["timer"] and not state["timer"].done():
                                    state["timer"].cancel()
                                print(f"[InterviewV2] turn_complete -> LLM: {t[:80]}")
                                if t:
                                    asyncio.create_task(_run_llm(t))

                    subtasks = [
                        asyncio.create_task(forward_audio()),
                        asyncio.create_task(receive_transcripts()),
                    ]
                    # Wait until forwarder or receiver completes (e.g. Google dropped connection)
                    done, pending = await asyncio.wait(subtasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

            except WebSocketDisconnect:
                shutdown_event.set()
                return
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if shutdown_event.is_set():
                    return
                print(f"[InterviewV2] STT session dropped ({exc}). Auto-reconnecting in {reconnect_delay:.1f}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, 5.0)

    try:
        tasks = [
            asyncio.create_task(browser_receiver()),
            asyncio.create_task(stt_runner()),
        ]
        await asyncio.gather(*tasks)
    except WebSocketDisconnect:
        print(f"[InterviewV2] Disconnected - {session_id}")
    except Exception as exc:
        print(f"[InterviewV2] SESSION ERROR: {exc}")
        traceback.print_exc()
        await send_ws({"type": "error", "message": str(exc)})
    finally:
        shutdown_event.set()
        # Drain audio queue with sentinel
        try:
            audio_queue.put_nowait(None)
        except Exception:
            pass
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print(f"[InterviewV2] Session {session_id} ended")
