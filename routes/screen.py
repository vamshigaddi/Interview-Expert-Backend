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
SCREEN_MODEL = os.environ.get("SCREEN_MODEL", "gemini-2.5-flash")
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
1. **Problem Understanding** - what exactly is being asked.
2. **Optimal Approach** - best algorithm, with time and space complexity upfront.
3. **Clean Code** - production-ready, well-commented solution (use the language visible on screen, otherwise Python) in a fenced code block.
4. **Edge Cases** - 3-4 tricky edge cases the interviewer may ask about.
If there is already code on screen with a bug or error, point out the fix first.""",

    "system": """Analyse this system design question / architecture diagram shown on screen.
Provide:
1. **What is shown** - the requirements or architecture visible.
2. **Design Walkthrough** - requirements -> high-level architecture -> components -> data flow.
3. **Database & Storage** - schema, SQL vs NoSQL choice, indexing.
4. **Scalability & Trade-offs** - load balancing, caching, queues, bottlenecks.
5. **Capacity Estimates** - QPS, storage, bandwidth.""",

    "behavioral": """Analyse the interview question shown on screen and give a natural STAR-style answer
(Situation, Task, Action, Result) in first person, using the candidate's resume where relevant.
Include concrete, quantifiable results. Sound conversational, not scripted.""",

    "general": """Analyse whatever is shown on screen (coding problem, question, diagram, error, or text)
and give the most useful, concise answer or hints. Be sharp and focused.""",
}

BASE_INSTRUCTION = """You are an elite Staff-Level AI Interview Copilot. The candidate has shared a screenshot of their screen.
Ignore unrelated UI (taskbar, browser tabs, video call tiles) and focus on the question, problem, code, or diagram.
Format the answer in clean Markdown.

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
