"""A tiny PDF writer (text only), so sample resumes need no extra library.

Used by scripts/generate_resumes.py and by the tests. It writes a valid
single-page PDF with Helvetica text that pypdf can extract.
"""


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_pdf(lines: list[str]) -> bytes:
    # Content stream: start at the top-left, 14 pt line spacing.
    commands = ["BT", "/F1 11 Tf", "14 TL", "50 790 Td"]
    for line in lines:
        commands.append(f"({_escape(line.encode('latin-1', 'replace').decode('latin-1'))}) Tj T*")
    commands.append("ET")
    stream = "\n".join(commands).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def corrupt_pdf() -> bytes:
    """Looks like a PDF by name and header, but is not one."""
    return b"%PDF-1.4\nthis file was truncated during upload \x00\x01\x02"
