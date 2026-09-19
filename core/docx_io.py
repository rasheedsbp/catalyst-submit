"""Open any Word template python-docx would otherwise reject.

python-docx only accepts the *document* content type. A .dotx/.dotm template
carries the *template* content type and raises ValueError, and a legacy .doc
is not OOXML at all. Both used to fail silently and the formatter fell back to
a generic profile (wrong font, double spacing, no masthead).
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import zipfile

_TEMPLATE_CT = (b"wordprocessingml.template.main+xml",
                b"ms-word.template.macroEnabledTemplate.main+xml",
                b"ms-word.document.macroEnabled.main+xml")
_DOC_CT = b"wordprocessingml.document.main+xml"


def normalize(data: bytes) -> bytes:
    """Return .docx bytes for .docx/.dotx/.dotm/.docm/.doc input."""
    if data[:4] == b"PK\x03\x04":
        z = zipfile.ZipFile(io.BytesIO(data))
        ct = z.read("[Content_Types].xml")
        if not any(t in ct for t in _TEMPLATE_CT):
            return data
        for t in _TEMPLATE_CT:
            ct = ct.replace(t, _DOC_CT)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:
            for n in z.namelist():
                if n == "[Content_Types].xml":
                    w.writestr(n, ct)
                elif n.lower().endswith("vbaproject.bin"):
                    continue
                else:
                    w.writestr(z.getinfo(n), z.read(n))
        return out.getvalue()
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":      # legacy .doc
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            raise ValueError("legacy .doc template — save it as .docx in Word first")
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "t.doc")
            open(src, "wb").write(data)
            subprocess.run([soffice, "--headless", "--convert-to", "docx",
                            "--outdir", td, src], check=True, timeout=120,
                           capture_output=True)
            return open(os.path.join(td, "t.docx"), "rb").read()
    raise ValueError("not a Word file")


def open_document(data: bytes):
    from docx import Document
    return Document(io.BytesIO(normalize(data)))
