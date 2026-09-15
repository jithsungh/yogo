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

EXTRACTION_PROMPT = """You are a meticulous resume parser. Your job is to extract EVERY piece of
structured data from the resume below with maximum fidelity. Extract ONLY
what is explicitly stated — do NOT infer, estimate, or invent details.

IMPORTANT RULES:
- Read the ENTIRE resume carefully. Resumes use many layouts: columns, tables,
  headers, sidebars. Do NOT skip any section.
- For DATES: Use "YYYY-MM-DD" format. If only month+year is given (e.g.
  "Jan 2022" or "01/2022"), use "YYYY-MM-01" (first of that month). If only
  a year is given (e.g. "2022"), use "YYYY-01-01". If the end date says
  "Present", "Current", "Ongoing", or similar, use the string "present".
- For DESCRIPTIONS: Capture ALL bullet points, responsibilities, and
  achievements verbatim. Join multiple bullets with newline characters (\\n).
  Do NOT summarize or truncate — include every bullet point exactly as written.
- For TECH STACK: Extract individual technology names as separate array items.
  Parse them from descriptions, bullet points, and dedicated "Technologies"
  or "Tech Stack" lines.
- If a field has no data in the resume, OMIT that field entirely from the
  output rather than using empty strings or null.

Return ONLY raw JSON (no markdown fences, no commentary) matching this schema:

{
  "basic_info": {
    "full_name": "Full name of the candidate",
    "email": "Email address",
    "phone": "Phone number (preserve original formatting)",
    "location": "City, State/Country as written",
    "summary": "Professional summary/objective paragraph if present at top of resume",
    "links": {
      "github": "GitHub profile URL",
      "linkedin": "LinkedIn profile URL",
      "portfolio": "Personal website/portfolio URL",
      "leetcode": "LeetCode profile URL",
      "twitter": "Twitter/X profile URL",
      "other": "Any other profile URLs as comma-separated string"
    }
  },
  "education": [
    {
      "degree": "Full degree name (e.g. 'Bachelor of Technology in Computer Science')",
      "institution": "University/college name",
      "start_date": "YYYY-MM-DD (see date rules above)",
      "end_date": "YYYY-MM-DD or 'present'",
      "score": "GPA, CGPA, percentage, or class as written (e.g. '3.8/4.0', '8.5/10', '85%', 'First Class Honours')",
      "highlights": "Relevant coursework, honors, thesis title, dean's list, scholarships — join with newlines"
    }
  ],
  "work_experience": [
    {
      "company": "Company/organization name",
      "role_title": "Exact job title (e.g. 'Software Engineer Intern', 'Senior Backend Developer')",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD or 'present'",
      "description": "ALL bullet points and responsibilities, joined with newlines (\\n). Capture EVERY bullet — do NOT skip or summarize any.",
      "tech_stack": ["Individual", "technology", "names", "extracted", "from", "the", "entry"]
    }
  ],
  "projects": [
    {
      "title": "Project name/title",
      "description": "Full project description — ALL bullet points joined with newlines (\\n)",
      "tech_stack": ["Individual", "technology", "names"],
      "github_url": "GitHub/source code URL if present",
      "live_url": "Deployed/live/demo URL if present",
      "highlights": "Key achievements, metrics, or impact statements from the project"
    }
  ],
  "skills": [
    {
      "name": "Individual skill name (one per entry, e.g. 'Python' not 'Python, Java')",
      "category": "One of: language|framework|cloud|devops|ml|database|tool|soft_skill"
    }
  ],
  "certifications": [
    {
      "title": "Certification name",
      "issuer": "Issuing organization (e.g. 'AWS', 'Google', 'Coursera')",
      "date": "YYYY-MM-DD when issued or earned",
      "url": "Credential verification URL if present"
    }
  ]
}

CATEGORY RULES FOR SKILLS:
- language: Programming/scripting languages (Python, Java, C++, JavaScript, SQL, etc.)
- framework: Libraries and frameworks (React, Django, Spring Boot, TensorFlow, etc.)
- cloud: Cloud platforms and services (AWS, GCP, Azure, S3, EC2, Lambda, etc.)
- devops: DevOps and infrastructure tools (Docker, Kubernetes, Jenkins, Terraform, CI/CD, etc.)
- ml: Machine learning and AI specific (PyTorch, scikit-learn, NLP, Computer Vision, etc.)
- database: Databases and data stores (PostgreSQL, MongoDB, Redis, Elasticsearch, etc.)
- tool: Developer tools, IDEs, version control (Git, VS Code, Jira, Postman, etc.)
- soft_skill: Non-technical skills (Leadership, Agile, Communication, etc.)
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
