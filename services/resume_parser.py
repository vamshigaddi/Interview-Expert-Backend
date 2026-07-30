import os
import io
import pdfplumber
from docx import Document


def parse_resume(file_bytes: bytes, filename: str) -> str:
    """
    Extract text content from a resume file.
    Supports PDF, DOCX, and TXT formats.
    """
    ext = os.path.splitext(filename)[1].lower()

    if ext == ".pdf":
        return _parse_pdf(file_bytes)
    elif ext == ".docx":
        return _parse_docx(file_bytes)
    elif ext == ".txt":
        return _parse_txt(file_bytes)
    else:
        raise ValueError(f"Unsupported file format: {ext}. Please upload a PDF, DOCX, or TXT file.")


def _parse_pdf(file_bytes: bytes) -> str:
    """Extract text from a PDF file using pdfplumber for layout-aware extraction."""
    text_parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    
    full_text = "\n\n".join(text_parts)
    if not full_text.strip():
        raise ValueError("Could not extract text from the PDF. It may be a scanned document or image-based PDF.")
    return full_text.strip()


def _parse_docx(file_bytes: bytes) -> str:
    """Extract text from a DOCX file using python-docx."""
    doc = Document(io.BytesIO(file_bytes))
    text_parts = []
    for paragraph in doc.paragraphs:
        if paragraph.text.strip():
            text_parts.append(paragraph.text.strip())
    
    full_text = "\n".join(text_parts)
    if not full_text.strip():
        raise ValueError("Could not extract text from the DOCX file. The document appears to be empty.")
    return full_text.strip()


def _parse_txt(file_bytes: bytes) -> str:
    """Extract text from a plain text file."""
    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = file_bytes.decode("latin-1")
    
    if not text.strip():
        raise ValueError("The text file appears to be empty.")
    return text.strip()
