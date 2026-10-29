import base64
import json
import os
import traceback
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from google import genai
from google.genai import types
from pydantic import BaseModel

from services.session_store import session_store

router = APIRouter()

# Live models (e.g. gemini-3.8-live) only work over the bidirectional Live API,
# so screen analysis uses a standard multimodal model with the SAME API key.
SCREEN_MODEL = os.environ.get("SCREEN_MODEL", "gemini-3.8-flash")
print(f"[Screen] Vision model: {SCREEN_MODEL}")

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    return _client


MODE_PROMPTS = {
    "coding": """Analyse this coding / DSA problem shown on screen.
Provide:
1. One spoken paragraph explaining your optimal approach and time/space complexity (e.g. O(N) time and O(N) space) in plain English ready to say aloud.
2. Clean, well-commented code in a standard fenced code block (use language visible on screen, default to Python). Keep it intuitive and easy to explain.
3. One short spoken paragraph mentioning the key edge cases handled.
Do NOT use textbook subheadings or slide titles.""",

    "system": """Analyse this system design question / architecture diagram shown on screen.
Deliver the answer in clean, natural spoken paragraphs that the candidate can speak out loud directly:
- First paragraph: Overview of the core requirements and high-level architecture.
- Second paragraph: End-to-end data flow and API/service communication.
- Third paragraph: Data storage strategy, schema choices, and caching.
- Fourth paragraph: Scalability, fault tolerance, and trade-offs.
Do NOT use textbook subheadings, markdown headers, or bullet lists.""",

    "behavioral": """Analyse the interview question shown on screen and provide a natural, confident first-person answer in 3 to 4 spoken paragraphs.
Tell a real, engaging engineering story incorporating concrete impact metrics from the candidate resume where relevant.
Do NOT use STAR subheadings or bullet points — output pure spoken speech ready to be read aloud directly.""",

    "general": """Analyse whatever is shown on screen (question, diagram, code error, or text).
Provide a direct, high-impact answer phrased as natural spoken paragraphs in first person that the candidate can speak out loud with zero hesitation.
Do NOT use textbook subheadings, markdown headers, or bullet lists.""",
}

BASE_INSTRUCTION = """You are an elite Staff-Level AI Interview Copilot assisting the candidate via their stealth HUD.
The candidate has shared a screenshot of their screen. Focus purely on the interview question, coding problem, or architecture diagram.

CRITICAL FORMATTING & READABILITY RULES:
1. SPOKEN PARAGRAPHS: The candidate speaks these answers aloud in real time. Do NOT use markdown headers (###), bold sub-headings, or robotic bullet lists. Use clean, natural spoken paragraphs separated by blank lines.
2. ABSOLUTELY NO LATEX OR DOLLAR SIGNS: NEVER use math-mode dollar signs ($) or LaTeX notation such as $\\mathcal{{O}}(n)$, $O(1)$, $\\le$, or $nums[i]$. ALWAYS write plain text: O(N), O(1), <=, >=, nums[i].
3. INTUITIVE, READABLE CODE: When code is required, prioritize simplicity and clarity over complex 'clever' tricks so the candidate can read and explain it out loud with zero confusion.

=== CANDIDATE RESUME (may be empty) ===
{resume_text}
"""


class ScreenAnalyseRequest(BaseModel):
    image_base64: str
    mime_type: str = "image/jpeg"
    mode: str = "coding"
    session_id: Optional[str] = None


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@router.post("/interview/analyse-screen")
async def analyse_screen(body: ScreenAnalyseRequest):
    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not configured on the server")

    raw = body.image_base64
    if "," in raw[:100]:  # strip "data:image/...;base64," prefix if present
        raw = raw.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image")

    resume_text = ""
    if body.session_id:
        session = session_store.get_session(body.session_id)
        if session:
            resume_text = session.get("resume_text", "") or ""

    prompt = MODE_PROMPTS.get(body.mode, MODE_PROMPTS["general"])
    config = types.GenerateContentConfig(
        system_instruction=BASE_INSTRUCTION.format(resume_text=resume_text),
        temperature=0.3,
        max_output_tokens=8192,
    )
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_bytes(data=image_bytes, mime_type=body.mime_type),
                types.Part.from_text(text=prompt),
            ],
        )
    ]

    async def event_stream():
        try:
            stream = await _get_client().aio.models.generate_content_stream(
                model=SCREEN_MODEL, contents=contents, config=config
            )
            async for chunk in stream:
                text = getattr(chunk, "text", None)
                if text:
                    yield _sse({"text": text})
            yield "data: [DONE]\n\n"
        except Exception as e:
            traceback.print_exc()
            yield _sse({"error": str(e)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
