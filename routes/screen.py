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
Provide a clean, easy-to-read response formatted as:
1. **Problem Summary** - 1 to 2 clear, simple sentences explaining what needs to be solved.
2. **Optimal Approach & Complexity** - 
   - Time Complexity: O(...) - brief reason.
   - Space Complexity: O(...) - brief reason.
   - Explain the core idea in 2-3 straightforward bullet points.
3. **Clean & Intuitive Solution** - 
   - Write clean, readable, well-commented code in a standard fenced code block (use language visible on screen, default to Python).
   - IMPORTANT: Keep the code simple and beginner-to-intermediate readable! Do NOT use overly clever, dense, compressed one-liners or obscure syntax. Use clear variable names (e.g. seen, complement, current_sum, left, right).
4. **Key Edge Cases** - 3 brief edge cases to mention out loud to the interviewer.
If existing code on screen has a bug or error, clearly highlight the fix first.""",

    "system": """Analyse this system design question / architecture diagram shown on screen.
Provide:
1. **Overview** - What requirements or architecture are shown.
2. **High-Level Design & Data Flow** - Step-by-step pipeline in clean bullet points.
3. **Database & Storage** - Schema, SQL vs NoSQL rationale, indexing.
4. **Scalability & Trade-offs** - Load balancing, caching, queues, bottlenecks.
5. **Key Numbers** - Estimated QPS, storage, bandwidth.""",

    "behavioral": """Analyse the interview question shown on screen and provide a natural, confident STAR-style answer
(Situation, Task, Action, Result) in first person, using the candidate's background where relevant.
Include concrete, realistic impact metrics. Sound conversational, professional, and authentic.""",

    "general": """Analyse whatever is shown on screen (question, diagram, code error, or text).
Provide the most useful, focused, and direct answer with clear bullet points. Keep it sharp and actionable.""",
}

BASE_INSTRUCTION = """You are an elite Staff-Level AI Interview Copilot assisting the candidate via their stealth HUD.
The candidate has shared a screenshot of their screen. Focus purely on the interview question, coding problem, or architecture diagram.

CRITICAL FORMATTING & READABILITY RULES:
1. ABSOLUTELY NO LATEX OR DOLLAR SIGNS: NEVER use math-mode dollar signs ($) or LaTeX notation such as $\\mathcal{{O}}(n)$, $O(1)$, $\\le$, or $nums[i]$. ALWAYS write plain text: O(N), O(1), <=, >=, nums[i].
2. INTUITIVE, READABLE CODE: Prioritize simplicity and clarity over complex 'clever' tricks. The candidate must be able to read and explain the code out loud with zero confusion.
3. SCANNABLE BULLET POINTS: Keep descriptions concise and formatted with clean Markdown bullet points. Avoid dense walls of text.

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
