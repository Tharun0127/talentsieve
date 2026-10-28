"""PDF text extraction with pypdf. Any failure here is PERMANENT: the same
file will fail the same way on every retry."""
import re

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.errors import NoTextInPDF, UnreadablePDF

MAX_CHARS = 20_000  # long enough for any CV, short enough for one LLM call


def extract_text(path: str) -> str:
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise UnreadablePDF("PDF is password-protected")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except UnreadablePDF:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise UnreadablePDF(f"could not read PDF: {exc.__class__.__name__}") from exc
    text = normalize(text)
    if len(text) < 20:
        raise NoTextInPDF("PDF has no extractable text (is it a scanned image?)")
    return text[:MAX_CHARS]


def normalize(text: str) -> str:
    """Collapse whitespace so the same CV always yields the same text (and
    therefore the same cache key)."""
    return re.sub(r"\s+", " ", text).strip()
