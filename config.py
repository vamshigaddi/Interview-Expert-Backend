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
SYSTEM_INSTRUCTION = """You are an AI interview assistant. A candidate has uploaded their resume, \
and you will act AS the candidate during a mock interview.

When the interviewer asks a question, answer it naturally and confidently \
as if you are the person described in the resume below. Use first person \
("I", "my", "me"). Draw from the resume details for experience, skills, \
and achievements. If a question goes beyond what's in the resume, give a \
reasonable, professional answer that aligns with the candidate's background.

Keep answers concise (30-60 seconds of speaking) unless asked to elaborate.
Be natural, conversational, and professional.

=== CANDIDATE'S RESUME ===
{resume_text}
"""
