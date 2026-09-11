"""Resume import service — PDF/DOCX → Gemini structured extraction → JSON.

PDF: sent directly to Gemini as inline document data (Gemini reads PDFs natively).
DOCX: text extracted locally via python-docx, then sent as plain text.
Both paths converge to the same JSON schema via generate_json().
"""
from app.llm.gemini_client import generate_json, generate_text

# Gemini imports for multimodal PDF input
from google import genai
from google.genai import types as genai_types
from app.config import get_settings

EXTRACTION_PROMPT = """You are extracting structured data from a resume. Extract ONLY what is
explicitly stated in the text below. Do not infer, estimate, or invent any
detail, number, date, or skill that isn't directly present. If a field is
not present for an item, omit that field entirely rather than guessing.

Return ONLY raw JSON (no markdown fences, no commentary) matching exactly
this shape:

{
  "basic_info": {"full_name": "", "email": "", "phone": "", "location": "", "links": {"github": "", "linkedin": "", "portfolio": ""}},
  "education": [{"degree": "", "institution": "", "start_date": "", "end_date": "", "score": "", "highlights": ""}],
  "work_experience": [{"company": "", "role_title": "", "start_date": "", "end_date": "", "description": "", "tech_stack": []}],
  "projects": [{"title": "", "description": "", "tech_stack": [], "github_url": "", "live_url": "", "highlights": ""}],
  "skills": [{"name": "", "category": "language|framework|cloud|devops|ml|database|tool|soft_skill"}],
  "certifications": [{"title": "", "issuer": "", "date": "", "url": ""}]
}
"""


def extract_from_pdf_bytes(pdf_bytes: bytes) -> dict:
    """Send raw PDF bytes to Gemini for structured extraction."""
    import json

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)

    prompt_with_instruction = EXTRACTION_PROMPT + "\n\nRESUME CONTENT:\n(attached as PDF)"

    response = client.models.generate_content(
        model=settings.gemini_generation_model,
        contents=[
            genai_types.Content(
                parts=[
                    genai_types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                    genai_types.Part.from_text(text=prompt_with_instruction),
                ]
            )
        ],
    )
    raw = response.text.strip()
    cleaned = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model did not return valid JSON. Raw output:\n{raw}") from exc


def extract_from_docx_bytes(docx_bytes: bytes) -> dict:
    """Extract text from DOCX using python-docx, then send to Gemini."""
    import io
    from docx import Document

    doc = Document(io.BytesIO(docx_bytes))
    text_parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            text_parts.append(para.text.strip())
    # Also get text from tables (resumes often use tables for layout)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    text_parts.append(cell.text.strip())

    resume_text = "\n".join(text_parts)
    prompt = EXTRACTION_PROMPT + f"\n\nRESUME CONTENT:\n{resume_text}"
    return generate_json(prompt)


def extract_resume(file_bytes: bytes, filename: str) -> dict:
    """Route to PDF or DOCX extraction based on filename extension."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return extract_from_pdf_bytes(file_bytes)
    elif lower.endswith(".docx"):
        return extract_from_docx_bytes(file_bytes)
    else:
        raise ValueError(f"Unsupported file type: {filename}. Only .pdf and .docx are supported.")
