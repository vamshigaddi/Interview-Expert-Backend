import asyncio
import json
import traceback
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types
from dotenv import load_dotenv

from services.session_store import session_store
from config import GEMINI_MODEL, SYSTEM_INSTRUCTION

load_dotenv()

router = APIRouter()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
print(f"[Interview] API key loaded: {'YES (' + GEMINI_API_KEY[:8] + '...)' if GEMINI_API_KEY else 'NO — check your .env file!'}")

# Initialize the Gemini client
client = genai.Client(
    http_options={"api_version": "v1beta"},
    api_key=GEMINI_API_KEY,
)


def _build_live_config(resume_text: str) -> types.LiveConnectConfig:
    """Build Gemini Live API config with resume context as system instruction."""
    system_prompt = SYSTEM_INSTRUCTION.format(resume_text=resume_text)

    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(
            parts=[types.Part(text=system_prompt)]
        ),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Zephyr")
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


def calculate_session_cost(usage_metadata, elapsed_seconds: int, output_text: str = "") -> dict:
    """
    Calculate the estimated cost of a Gemini session in USD and Rupees.
    Uses exact token counts from UsageMetadata if available, otherwise falls back to duration estimation.
    """
    input_text_tokens = 0
    input_audio_tokens = 0
    output_text_tokens = 0
    output_audio_tokens = 0

    if usage_metadata:
        # Prompt tokens (input)
        input_text_tokens = usage_metadata.prompt_token_count or 0
        prompt_details = getattr(usage_metadata, "prompt_tokens_details", None) or []
        for detail in prompt_details:
            modality = str(getattr(detail, "modality", "")).upper()
            if "AUDIO" in modality:
                input_audio_tokens = detail.token_count or 0
                # Subtract audio tokens from total prompt to get clean text token count
                input_text_tokens = max(0, input_text_tokens - input_audio_tokens)

        # Response tokens (output)
        output_text_tokens = usage_metadata.response_token_count or 0
        response_details = getattr(usage_metadata, "response_tokens_details", None) or []
        for detail in response_details:
            modality = str(getattr(detail, "modality", "")).upper()
            if "AUDIO" in modality:
                output_audio_tokens = detail.token_count or 0
                # Subtract audio tokens from total response to get clean text token count
                output_text_tokens = max(0, output_text_tokens - output_audio_tokens)

    # If output_text_tokens is 0 but we have generated transcript text, estimate text tokens
    if output_text_tokens == 0 and output_text:
        word_count = len(output_text.split())
        output_text_tokens = int(word_count * 1.33)

    # Pricing per 1M tokens (USD)
    input_text_rate = 0.75 / 1_000_000
    input_audio_rate = 3.00 / 1_000_000
    output_text_rate = 4.50 / 1_000_000
    output_audio_rate = 12.00 / 1_000_000

    # Calculate token costs
    cost_input_text = input_text_tokens * input_text_rate
    cost_input_audio = input_audio_tokens * input_audio_rate
    cost_output_text = output_text_tokens * output_text_rate
    cost_output_audio = output_audio_tokens * output_audio_rate

    total_usd = cost_input_text + cost_input_audio + cost_output_text + cost_output_audio

    # If no tokens were recorded (e.g. WebSocket disconnected quickly before usage_metadata arrived),
    # fallback to duration-based estimates
    if total_usd == 0 and elapsed_seconds > 0:
        # Input Audio: $0.005 / min
        # Output Audio: $0.018 / min
        # Assume 80% of elapsed time was active mic input, 30% was AI speaking output
        est_input_sec = elapsed_seconds * 0.8
        est_output_sec = elapsed_seconds * 0.3
        
        cost_input_audio = est_input_sec * (0.005 / 60)
        cost_output_audio = est_output_sec * (0.018 / 60)
        total_usd = cost_input_audio + cost_output_audio

    total_rupees = total_usd * 96.0

    return {
        "input_text_tokens": input_text_tokens,
        "input_audio_tokens": input_audio_tokens,
        "output_text_tokens": output_text_tokens,
        "output_audio_tokens": output_audio_tokens,
        
        "cost_input_text_usd": f"${cost_input_text:.6f}",
        "cost_input_audio_usd": f"${cost_input_audio:.6f}",
        "cost_output_text_usd": f"${cost_output_text:.6f}",
        "cost_output_audio_usd": f"${cost_output_audio:.6f}",
        
        "total_usd": f"{total_usd:.6f}",
        "total_rupees": f"{total_rupees:.4f}"
    }


@router.get("/interview/summary/{session_id}")
async def get_session_summary(session_id: str, duration_seconds: int = 0):
    """
    Get the billing summary for a finished interview session and release its memory.
    """
    session = session_store.get_session(session_id)
    if session is None:
        return {
            "input_text_tokens": 0,
            "input_audio_tokens": 0,
            "output_text_tokens": 0,
            "output_audio_tokens": 0,
            "cost_input_text_usd": "$0.000000",
            "cost_input_audio_usd": "$0.000000",
            "cost_output_text_usd": "$0.000000",
            "cost_output_audio_usd": "$0.000000",
            "total_usd": "0.000000",
            "total_rupees": "0.0000"
        }

    usage_metadata = session.get("usage_metadata")
    output_text = session.get("output_text", "")
    summary = calculate_session_cost(usage_metadata, duration_seconds, output_text)

    # Clean up the session data
    session_store.delete_session(session_id)
    print(f"[Interview] Summary calculated and session {session_id} deleted")

    return summary


@router.websocket("/interview/{session_id}")
async def interview_websocket(websocket: WebSocket, session_id: str):
    """
    WebSocket endpoint for real-time interview audio streaming.
    """
    # Validate the session exists
    session = session_store.get_session(session_id)
    if session is None:
        print(f"[Interview] Session {session_id} not found or expired")
        await websocket.close(code=4004, reason="Session not found or expired")
        return

    await websocket.accept()
    print(f"[Interview] WebSocket accepted for session {session_id}")

    resume_text = session["resume_text"]
    config = _build_live_config(resume_text)

    # Flag to coordinate shutdown
    shutdown_event = asyncio.Event()

    try:
        # Try connecting to Gemini — this is where most failures happen
        print(f"[Interview] Connecting to Gemini Live API ({GEMINI_MODEL})...")
        async with client.aio.live.connect(model=GEMINI_MODEL, config=config) as gemini_session:
            print(f"[Interview] ✅ Gemini session connected!")

            # Notify frontend that the session is ready
            await websocket.send_text(json.dumps({
                "type": "status",
                "status": "ready"
            }))

            # Queue for audio coming back from Gemini to be sent to browser
            audio_out_queue: asyncio.Queue[bytes] = asyncio.Queue()

            async def browser_to_gemini():
                """Receive audio/text from browser and forward to Gemini."""
                try:
                    while not shutdown_event.is_set():
                        try:
                            message = await websocket.receive()
                        except WebSocketDisconnect:
                            print("[Interview] Browser disconnected (browser_to_gemini)")
                            shutdown_event.set()
                            return

                        msg_type = message.get("type", "")

                        if msg_type == "websocket.disconnect":
                            print("[Interview] Browser sent disconnect frame")
                            shutdown_event.set()
                            return

                        if "bytes" in message and message["bytes"]:
                            # Binary frame = raw PCM audio from microphone
                            audio_data = message["bytes"]
                            await gemini_session.send_realtime_input(
                                audio={"data": audio_data, "mime_type": "audio/pcm"},
                            )

                        elif "text" in message and message["text"]:
                            # Text frame = JSON message (text chat or control)
                            try:
                                msg = json.loads(message["text"])
                            except json.JSONDecodeError:
                                continue

                            if msg.get("type") == "text" and msg.get("content"):
                                print(f"[Interview] Text chat from user: {msg['content'][:80]}...")
                                await gemini_session.send(
                                    input=msg["content"],
                                    end_of_turn=True,
                                )
                                # Echo user's text message back as transcript
                                await websocket.send_text(json.dumps({
                                    "type": "transcript",
                                    "role": "user",
                                    "content": msg["content"],
                                }))

                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    print(f"[Interview] ❌ browser_to_gemini error: {e}")
                    traceback.print_exc()
                    shutdown_event.set()

            async def gemini_to_browser():
                """Receive responses from Gemini and forward audio/text to browser."""
                try:
                    while not shutdown_event.is_set():
                        turn = gemini_session.receive()
                        async for response in turn:
                            if shutdown_event.is_set():
                                return

                            # Check for audio data
                            if data := response.data:
                                audio_out_queue.put_nowait(data)

                            # Check for model text/transcript
                            if text := response.text:
                                print(f"[Interview] Model text: {text[:80]}...")
                                session_store.update_session_text(session_id, text)
                                await websocket.send_text(json.dumps({
                                    "type": "transcript",
                                    "role": "model",
                                    "content": text,
                                }))

                            # Check for transcriptions from server_content
                            server_content = getattr(response, "server_content", None)
                            if server_content:
                                # Input transcription (what user said via audio)
                                input_tx = getattr(server_content, "input_transcription", None)
                                if input_tx:
                                    tx_text = getattr(input_tx, "text", None)
                                    if tx_text:
                                        print(f"[Interview] User said (transcribed): {tx_text[:80]}...")
                                        await websocket.send_text(json.dumps({
                                            "type": "transcript",
                                            "role": "user",
                                            "content": tx_text,
                                        }))

                                # Output transcription (what model said)
                                output_tx = getattr(server_content, "output_transcription", None)
                                if output_tx:
                                    tx_text = getattr(output_tx, "text", None)
                                    if tx_text:
                                        print(f"[Interview] Model said (transcribed): {tx_text[:80]}...")
                                        session_store.update_session_text(session_id, tx_text)
                                        await websocket.send_text(json.dumps({
                                            "type": "transcript",
                                            "role": "model",
                                            "content": tx_text,
                                        }))

                            # Capture and update latest usage metadata
                            usage = getattr(response, "usage_metadata", None)
                            if usage:
                                session_store.update_session_usage(session_id, usage)

                        # Turn complete — signal the frontend
                        print("[Interview] Turn complete")
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "audio_end"
                            }))
                        except Exception:
                            pass

                except asyncio.CancelledError:
                    pass
                except WebSocketDisconnect:
                    print("[Interview] Browser disconnected (gemini_to_browser)")
                    shutdown_event.set()
                except Exception as e:
                    print(f"[Interview] ❌ gemini_to_browser error: {e}")
                    traceback.print_exc()
                    shutdown_event.set()

            async def send_audio_to_browser():
                """Stream queued audio chunks from Gemini back to the browser."""
                try:
                    while not shutdown_event.is_set():
                        try:
                            audio_bytes = await asyncio.wait_for(
                                audio_out_queue.get(), timeout=1.0
                            )
                            await websocket.send_bytes(audio_bytes)
                        except asyncio.TimeoutError:
                            # No audio in queue, just loop and check shutdown
                            continue
                except asyncio.CancelledError:
                    pass
                except WebSocketDisconnect:
                    print("[Interview] Browser disconnected (send_audio)")
                    shutdown_event.set()
                except Exception as e:
                    print(f"[Interview] ❌ send_audio_to_browser error: {e}")
                    traceback.print_exc()
                    shutdown_event.set()

            # Run all three tasks concurrently using gather instead of TaskGroup
            # (TaskGroup raises ExceptionGroup which is harder to handle)
            tasks = [
                asyncio.create_task(browser_to_gemini()),
                asyncio.create_task(gemini_to_browser()),
                asyncio.create_task(send_audio_to_browser()),
            ]

            # Wait for shutdown signal or any task to complete
            try:
                await asyncio.gather(*tasks)
            except Exception as e:
                print(f"[Interview] Task error: {e}")
                traceback.print_exc()
            finally:
                # Cancel any remaining tasks
                for task in tasks:
                    if not task.done():
                        task.cancel()
                # Wait for cancellations to complete
                await asyncio.gather(*tasks, return_exceptions=True)

    except WebSocketDisconnect:
        print(f"[Interview] Client disconnected from session {session_id}")
    except Exception as e:
        print(f"[Interview] ❌ SESSION ERROR: {e}")
        traceback.print_exc()
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": f"Interview session error: {str(e)}"
            }))
            await websocket.close()
        except Exception:
            pass
    finally:
        print(f"[Interview] Session {session_id} ended")
