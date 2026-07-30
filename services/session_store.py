import uuid
import time
from typing import Optional
from config import SESSION_TTL_SECONDS


class SessionStore:
    """
    Simple in-memory session store for holding parsed resume text.
    Sessions auto-expire after SESSION_TTL_SECONDS (default 1 hour).
    """

    def __init__(self):
        self._sessions: dict[str, dict] = {}

    def create_session(self, resume_text: str, filename: str) -> str:
        """Create a new session with parsed resume text. Returns session_id."""
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = {
            "resume_text": resume_text,
            "filename": filename,
            "created_at": time.time(),
        }
        self._cleanup_expired()
        return session_id

    def get_session(self, session_id: str) -> Optional[dict]:
        """Retrieve session data if it exists and hasn't expired."""
        self._cleanup_expired()
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.time() - session["created_at"] > SESSION_TTL_SECONDS:
            del self._sessions[session_id]
            return None
        return session

    def delete_session(self, session_id: str) -> None:
        """Delete a session."""
        self._sessions.pop(session_id, None)

    def update_session_usage(self, session_id: str, usage_metadata) -> None:
        """Update the session's latest usage metadata."""
        session = self._sessions.get(session_id)
        if session:
            session["usage_metadata"] = usage_metadata

    def update_session_text(self, session_id: str, text_chunk: str) -> None:
        """Accumulate output text for the session."""
        session = self._sessions.get(session_id)
        if session:
            if "output_text" not in session:
                session["output_text"] = ""
            session["output_text"] += text_chunk

    def _cleanup_expired(self) -> None:
        """Remove all expired sessions."""
        now = time.time()
        expired = [
            sid for sid, data in self._sessions.items()
            if now - data["created_at"] > SESSION_TTL_SECONDS
        ]
        for sid in expired:
            del self._sessions[sid]


# Singleton instance shared across the application
session_store = SessionStore()
