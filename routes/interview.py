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
                                        await websocket.send_text(json.dumps({
                                            "type": "transcript",
                                            "role": "model",
                                            "content": tx_text,
                                        }))

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
