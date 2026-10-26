import os
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, UploadFile, File, HTTPException
from services.resume_parser import parse_resume
from services.session_store import session_store
from config import MAX_FILE_SIZE_MB

router = APIRouter()

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}


@router.post("/upload")
async def upload_resume(file: UploadFile = File(...)):
    """
    Upload a resume file (PDF, DOCX, or TXT).
    Returns a session_id and a preview of the extracted text.
    """
    # Validate file extension
    filename = file.filename or "unknown"
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {ext}. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
        )

    # Read file bytes
    file_bytes = await file.read()

    # Validate file size
    size_mb = len(file_bytes) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({size_mb:.1f} MB). Maximum allowed: {MAX_FILE_SIZE_MB} MB."
        )

    # Parse the resume
    try:
        resume_text = parse_resume(file_bytes, filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to parse the resume. Please try a different file.")

    # Create a session
    session_id = session_store.create_session(resume_text, filename)

    # Return session info with a text preview
    preview = resume_text[:500] + ("..." if len(resume_text) > 500 else "")

    return {
        "session_id": session_id,
        "filename": filename,
        "preview": preview,
        "full_text": resume_text,
        "text_length": len(resume_text),
    }


class DirectSessionRequest(BaseModel):
    resume_text: Optional[str] = ""
    role: Optional[str] = "Software Engineer"
    mode: Optional[str] = "coding"


@router.post("/session/direct")
async def create_direct_session(body: DirectSessionRequest = DirectSessionRequest()):
    """
    Create a session directly for desktop copilot without file upload.
    If no resume is provided, automatically loads the default candidate resume.
    """
    resume_text = body.resume_text.strip() if body.resume_text else ""

    # If no custom resume provided or just a short fallback string, load default resume if available
    if len(resume_text) < 150:
        default_resume_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads", "default_resume.txt")
        if os.path.exists(default_resume_path):
            try:
                with open(default_resume_path, "r", encoding="utf-8") as f:
                    loaded = f.read().strip()
                    if loaded:
                        resume_text = loaded
                        print(f"[Upload] Loaded default candidate resume ({len(resume_text)} chars)")
            except Exception as e:
                print(f"[Upload] Error loading default resume: {e}")

    if not resume_text:
        resume_text = f"Target Role: {body.role}. Focus mode: {body.mode}."

    session_id = session_store.create_session(resume_text, "desktop_direct_session", is_copilot=True)
    return {
        "session_id": session_id,
        "filename": "desktop_direct_session",
        "preview": resume_text[:200],
    }


