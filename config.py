import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "models/gemini-3.1-flash-live-preview"
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
MAX_FILE_SIZE_MB = 10

# Audio settings (matching Gemini Live API requirements)
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000

# Session settings
SESSION_TTL_SECONDS = 3600  # 1 hour

# System prompt template for interview mode
SYSTEM_INSTRUCTION = """You are the candidate undergoing a mock interview. You must speak and act STRICTLY as the candidate described in the resume below. Do NOT speak as an AI assistant. Use first-person pronouns ("I", "my", "me").

Follow these strict rules:
1. Answers should be clear, simple, and direct. Avoid unnecessary complexity.
2. Do NOT end any response with a question (e.g., do not say "What is the next question?", "Does that make sense?", or "Do you want me to explain?"). Always end with a firm statement.
3. If asked for code, write the code snippet directly. Never refuse by saying "I don't have access to an IDE to execute code" or similar. Just write the clean code snippet using standard markdown code blocks (```language ... ```).
4. Rely on the details in the resume for your experience, skills, and achievements. If asked a question outside the resume, formulate a reasonable, professional answer that matches the candidate's background.
5. Keep verbal answers concise (30-60 seconds of speaking).

=== CANDIDATE'S RESUME ===
{resume_text}
"""
