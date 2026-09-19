"""Build a journal profile from a journal's own website.

What this can do: fetch the author-guidelines page, work out the publisher,
inherit that publisher's base layout, pull out the numbers the page actually
states (abstract word limit, keyword count, font, spacing, margins, reference
style), and find downloadable template files.

What it cannot do: guarantee correctness. Guidelines pages are prose written for
humans, wildly inconsistent between publishers, and often incomplete — a page
may never state a margin or a font. Anything not found is inherited from the
publisher base, and every field is shown to you for review before the profile is
saved. Treat the output as a filled-in draft, not a verified spec.
"""
from __future__ import annotations

import html
import io
import json
import os
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import profiles, template_probe

UA = "Mozilla/5.0 (compatible; JournalFormatter/1.0)"
TIMEOUT = 25

# domain fragment -> (base profile id, publisher label)
PUBLISHER_MAP = [
    ("ieee.org", "ieee_access", "IEEE"),
    ("ieeexplore", "ieee_access", "IEEE"),
    ("sciencedirect", "elsevier_generic", "Elsevier"),
    ("elsevier", "elsevier_generic", "Elsevier"),
    ("springer", "springer_nature", "Springer"),
    ("springernature", "springer_nature", "Springer"),
    ("nature.com", "springer_nature", "Springer"),
    ("mdpi.com", "mdpi", "MDPI"),
    ("emerald", "emerald_ijoa", "Emerald"),
    ("emeraldgrouppublishing", "emerald_ijoa", "Emerald"),
    ("tandfonline", "elsevier_generic", "Taylor & Francis"),
    ("wiley", "elsevier_generic", "Wiley"),
    ("sagepub", "sage", "SAGE"),
    ("hindawi", "mdpi", "Hindawi"),
    ("frontiersin", "mdpi", "Frontiers"),
    ("acm.org", "ieee_conference", "ACM"),
    ("inass.org", "ieee_access", "INASS"),
]

# class stem -> (base profile id, publisher label, author_mode)
CLASS_BASE = {
    "sagej": ("sage", "SAGE", "sage"),
    "elsarticle": ("elsevier_generic", "Elsevier", "elsevier"),
    "ieeetran": ("ieee_conference", "IEEE", "ieee"),
    "ieeeaccess": ("ieee_access", "IEEE", "ieee"),
    "sn-jnl": ("springer_nature", "Springer", "springer"),
    "mdpi": ("mdpi", "MDPI", "plain"),
}


def _class_stem(class_file: str) -> str:
    return re.sub(r"\.cls$", "", (class_file or "").strip(), flags=re.I).lower()


def _reconcile_latex(lx: dict, publisher: str) -> None:
    """Keep documentclass, class_file, author_mode and notes describing ONE class.

    A kit usually ships a .cls but no \\documentclass line, so the class file
    was overwritten while the base profile's documentclass survived — a SAGE kit
    produced main.tex with \\documentclass{elsarticle}, Elsevier frontmatter and
    a README claiming elsarticle.cls was in the zip. Whatever class the kit
    actually supplies wins, and everything else is rebuilt around it.
    """
    stem = _class_stem(lx.get("class_file", ""))
    if not stem:
        return
    m = re.search(r"\{([^}]+)\}\s*$", lx.get("documentclass", ""))
    if m and m.group(1).lower() == stem:
        return
    info = CLASS_BASE.get(stem)
    if info:
        base = profiles.get(info[0])
        blx = base.get("latex", {})
        lx["documentclass"] = blx.get("documentclass", "\\documentclass{%s}" % stem)
        for k in ("author_mode", "bib_style", "keywords_cmd", "notes"):
            if blx.get(k):
                lx[k] = blx[k]
        return
    lx["documentclass"] = "\\documentclass{%s}" % stem
    lx["author_mode"] = "generic"
    lx.pop("bib_style", None)
    lx["notes"] = (f"Uses {stem}.cls from the uploaded template kit. Frontmatter "
                   "uses standard \\title/\\author/abstract commands — check the "
                   "kit's sample file for any class-specific fields.")


TEMPLATE_EXT = (".docx", ".dotx", ".doc", ".zip", ".cls", ".tex", ".latex", ".sty")


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #
def fetch(url: str) -> tuple[str, str]:
    """Return (final_url, html). Raises on network failure."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
        final = r.geturl()
    for enc in ("utf-8", "latin-1"):
        try:
            return final, raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return final, raw.decode("utf-8", errors="ignore")


GUIDE_HINTS = ("author", "guideline", "instruction", "submission", "submit",
               "prepare", "template", "for-authors", "authorinfo")


def find_guideline_links(page_html: str, base_url: str, limit: int = 6) -> list[str]:
    """Follow-up pages worth reading when the given URL is a journal home page."""
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         page_html, re.I | re.S):
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2)).lower()
        blob = (href + " " + text).lower()
        if not any(h in blob for h in GUIDE_HINTS):
            continue
        full = urllib.parse.urljoin(base_url, href)
        if full in seen or full.startswith("mailto:"):
            continue
        seen.add(full)
        out.append(full)
        if len(out) >= limit:
            break
    return out


def find_template_links(page_html: str, base_url: str) -> list[str]:
    out, seen = [], set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', page_html, re.I):
        href = m.group(1)
        if not href.lower().split("?")[0].endswith(TEMPLATE_EXT):
            continue
        full = urllib.parse.urljoin(base_url, href)
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


def strip_html(page_html: str) -> str:
    t = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", page_html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def page_title(page_html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", page_html)
    if not m:
        return ""
    t = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    # "Author guidelines | Journal of X | Publisher" -> longest meaningful chunk
    parts = [p.strip() for p in re.split(r"[|\u2013\u2014-]{1,2}", t) if p.strip()]
    parts = [p for p in parts
             if not any(h in p.lower() for h in
                        ("author", "guideline", "instruction", "submission", "home"))]
    return max(parts, key=len) if parts else t


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def detect_publisher(url: str, text: str) -> tuple[str, str]:
    low = (url + " " + text[:4000]).lower()
    for frag, base, label in PUBLISHER_MAP:
        if frag in low:
            return base, label
    return "elsevier_generic", "Unknown"


def _first_int(patterns: list[str], text: str, lo: int, hi: int) -> int | None:
    for pat in patterns:
        for m in re.finditer(pat, text, re.I):
            for g in m.groups():
                if g and g.isdigit() and lo <= int(g) <= hi:
                    return int(g)
    return None


def extract_spec(text: str) -> dict:
    """Heuristic pass over the guidelines prose. Every value may be None."""
    spec: dict = {}

    spec["abstract_max_words"] = _first_int([
        r"abstract[^.]{0,120}?(?:not exceed|no more than|maximum of|maximum|limited to|within|up to)\s*(\d{2,4})\s*words",
        r"abstract[^.]{0,120}?(\d{2,4})\s*words",
        r"(\d{2,4})\s*word[s]?\s*abstract",
    ], text, 50, 1000)

    spec["structured_abstract"] = bool(re.search(
        r"structured abstract|purpose[\s/,-]{0,3}design/methodology|"
        r"abstract must (?:be )?structured", text, re.I))

    kmax = _first_int([
        r"keywords?[^.]{0,120}?(?:maximum of|no more than|not exceed|up to|between \d+ and)\s*(\d{1,2})",
        r"(\d{1,2})\s*keywords",
        r"keywords?[^.]{0,60}?(\d{1,2})\s*(?:to|-|\u2013)\s*(\d{1,2})",
    ], text, 2, 15)
    kmin = _first_int([r"keywords?[^.]{0,60}?(?:at least|minimum of)\s*(\d{1,2})",
                       r"(\d{1,2})\s*(?:to|-|\u2013)\s*\d{1,2}\s*keywords"], text, 1, 10)
    spec["keywords_max"] = kmax
    spec["keywords_min"] = kmin

    m = re.search(r"\b(Times New Roman|Arial|Calibri|Helvetica|Cambria|"
                  r"Palatino(?: Linotype)?|Garamond|Bookman Old Style|Book Antiqua)\b", text, re.I)
    spec["font"] = m.group(1) if m else None

    spec["font_size"] = _first_int([
        r"(\d{1,2})\s*(?:-|\s)?(?:pt|point)\b",
        r"font size[^.]{0,30}?(\d{1,2})",
    ], text, 8, 14)

    if re.search(r"double[- ]spac", text, re.I):
        spec["line_spacing"] = 2.0
    elif re.search(r"1\.5\s*(?:line\s*)?spac", text, re.I):
        spec["line_spacing"] = 1.5
    elif re.search(r"single[- ]spac", text, re.I):
        spec["line_spacing"] = 1.0
    else:
        spec["line_spacing"] = None

    if re.search(r"two[- ]column|double[- ]column|2[- ]column", text, re.I):
        spec["columns"] = 2
    elif re.search(r"single[- ]column|one[- ]column", text, re.I):
        spec["columns"] = 1
    else:
        spec["columns"] = None

    if re.search(r"\bA4\b", text):
        spec["page_size"] = "a4"
    elif re.search(r"\bUS Letter\b|\bletter[- ]size", text, re.I):
        spec["page_size"] = "letter"
    else:
        spec["page_size"] = None

    # the number can sit either side of the word "margin"
    spec["margin_in"] = None
    unit_pats = [
        (r"(\d(?:\.\d+)?)\s*(?:in(?:ch(?:es)?)?|\")\s*margins?", "in"),
        (r"margins?[^.]{0,60}?(\d(?:\.\d+)?)\s*(?:in(?:ch(?:es)?)?|\")", "in"),
        (r"(\d{1,3}(?:\.\d+)?)\s*(mm|cm)\s*margins?", None),
        (r"margins?[^.]{0,60}?(\d{1,3}(?:\.\d+)?)\s*(mm|cm)\b", None),
    ]
    for pat, unit in unit_pats:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        v = float(m.group(1))
        u = unit or m.group(2).lower()
        val = v if u == "in" else (v / 25.4 if u == "mm" else v / 2.54)
        if 0.3 <= val <= 2.5:
            spec["margin_in"] = round(val, 2)
            break

    styles = {
        "ieee": r"\bIEEE\s*(?:style|format|referenc)",
        "apa": r"\bAPA\b",
        "harvard-cite-them-right": r"\bHarvard\b",
        "vancouver": r"\bVancouver\b",
        "chicago-author-date": r"\bChicago\b",
        "elsevier-numeric": r"numbered references|numeric(?:al)? (?:style|referenc)",
    }
    spec["reference_style"] = next(
        (k for k, pat in styles.items() if re.search(pat, text, re.I)), None)

    spec["numbered_sections"] = bool(re.search(
        r"number(?:ed)? (?:the )?(?:section|heading)|sections? should be numbered", text, re.I))

    return spec


# --------------------------------------------------------------------------- #
# profile assembly
# --------------------------------------------------------------------------- #
def guess_base_from_template(filename: str, spec: dict, sample_text: str = "") -> str:
    """Pick the closest publisher base for a template we know nothing else about."""
    blob = (filename + " " + sample_text[:4000]).lower()
    for frag, base, _label in PUBLISHER_MAP:
        if frag.split(".")[0] in blob:
            return base
    # the class file inside a kit is the most reliable signal of all
    cls_base = CLASS_BASE.get(_class_stem(spec.get("class_file", "")))
    if cls_base:
        return cls_base[0]
    for kw, base in (("sagej", "sage"), ("sage", "sage"),
                     ("ijies", "ieee_access"), ("inass", "ieee_access"),
                     ("ieee", "ieee_conference"), ("elsevier", "elsevier_generic"),
                     ("springer", "springer_nature"), ("mdpi", "mdpi"),
                     ("emerald", "emerald_ijoa")):
        if kw in blob:
            return base
    # fall back on the geometry the template itself reports
    if spec.get("columns") == 2:
        return "ieee_conference"
    if spec.get("line_spacing") and float(spec["line_spacing"]) >= 1.9:
        return "elsevier_generic"
    return "elsevier_generic"


def profile_from_template(filename: str, data: bytes,
                          name: str | None = None) -> tuple[dict, dict]:
    """Build a usable profile from a template alone. Returns (profile, spec)."""
    from . import template_probe
    spec = template_probe.probe(filename, data)
    clean = {k: v for k, v in spec.items() if not k.startswith("_")}
    sample = ""
    if filename.lower().endswith((".docx", ".dotx")):
        try:
            import io
            from docx import Document
            from .docx_io import open_document
            d = open_document(data)
            sample = "\n".join(p.text for p in d.paragraphs[:80])
        except Exception:
            sample = ""
    base_id = guess_base_from_template(filename, clean, sample)
    base = profiles.get(base_id)
    cls_info = CLASS_BASE.get(_class_stem(clean.get("class_file", "")))
    publisher = cls_info[1] if cls_info else base["publisher"]
    label = name or re.sub(r"[_\-]+", " ",
                           re.sub(r"\.(docx|dotx|zip|cls)$", "", filename,
                                  flags=re.I)).strip() or "Uploaded template"
    prof = build_profile(label, filename, clean, base_id, publisher, [])
    return prof, clean


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:48] or "custom_journal"


def build_profile(name: str, url: str, spec: dict, base_id: str,
                  publisher: str, templates: list[str]) -> dict:
    base = dict(profiles.get(base_id))
    base.pop("_file", None)
    prof = json.loads(json.dumps(base))          # deep copy
    prof["id"] = slugify(name)
    prof["name"] = name
    prof["publisher"] = publisher
    prof["source_url"] = url
    prof["derived_from"] = base_id
    prof["template_links"] = templates[:8]
    prof["aliases"] = sorted({name.lower(), slugify(name).replace("_", " ")})

    w = prof["word"]
    if spec.get("font"):
        w["body_font"] = spec["font"]
    if spec.get("font_size"):
        w["body_size"] = spec["font_size"]
    if spec.get("line_spacing"):
        w["line_spacing"] = spec["line_spacing"]
    if spec.get("columns"):
        w["columns"] = spec["columns"]
    if spec.get("page_size"):
        w["page_size"] = spec["page_size"]
    if spec.get("margin_in"):
        mv = float(spec["margin_in"])
        w["margins_in"] = {"top": mv, "bottom": mv, "left": mv, "right": mv}
    if spec.get("heading_numbering"):
        w["heading_numbering"] = spec["heading_numbering"]
    elif spec.get("numbered_sections") is True:
        w["heading_numbering"] = "decimal"
    if spec.get("margins_in"):
        w["margins_in"] = spec["margins_in"]

    lx = prof.setdefault("latex", {})
    for src_key, dst_key in (("documentclass", "documentclass"),
                             ("class_file", "class_file"),
                             ("bib_style", "bib_style"),
                             ("author_mode", "author_mode")):
        if spec.get(src_key):
            lx[dst_key] = spec[src_key]
    _reconcile_latex(lx, publisher)
    stem = _class_stem(lx.get("class_file", ""))
    info = CLASS_BASE.get(stem)
    if info:
        prof["publisher"] = info[1]
    elif stem and lx.get("author_mode") == "generic":
        prof["publisher"] = "Custom template"

    ab = prof.setdefault("abstract", {})
    if spec.get("abstract_max_words"):
        ab["max_words"] = spec["abstract_max_words"]
    if spec.get("structured_abstract"):
        ab["type"] = "structured"
        ab.setdefault("sections", spec.get("structured_abstract_sections")
                      or ["Purpose", "Design/methodology/approach", "Findings",
                          "Originality/value"])
    elif spec.get("structured_abstract") is False:
        ab["type"] = "plain"
        ab.pop("sections", None)

    kw = prof.setdefault("keywords", {})
    if spec.get("keywords_min"):
        kw["min"] = spec["keywords_min"]
    if spec.get("keywords_max"):
        kw["max"] = spec["keywords_max"]
    if kw.get("min", 0) > kw.get("max", 99):
        kw["min"], kw["max"] = kw["max"], kw["min"]

    if spec.get("reference_style"):
        prof["reference_style"] = spec["reference_style"]
    for k in ("figure_caption_position", "table_caption_position"):
        if spec.get(k):
            prof[k] = spec[k]
    if spec.get("notes"):
        prof.setdefault("latex", {})["notes"] = \
            (prof.get("latex", {}).get("notes", "") + " " + spec["notes"]).strip()
    return prof


def discover(url: str, follow: bool = True, probe_templates: bool = True,
             max_templates: int = 3) -> dict:
    """Full pipeline, entirely local. Returns a report dict — writes nothing.

    Precedence, strongest first:
      1. the journal's own template file (.docx / .cls / .zip) — hard values
      2. explicit statements in the guidelines prose
      3. the publisher's base profile
    """
    report: dict = {"url": url, "pages_read": [], "errors": [], "templates": [],
                    "spec": {}, "template_specs": [], "probed": []}

    final, page = fetch(url)
    report["pages_read"].append(final)
    text = strip_html(page)
    templates = find_template_links(page, final)

    if follow:
        thin = not re.search(r"abstract[^.]{0,120}words|keywords?", text, re.I)
        if thin or not templates:
            for link in find_guideline_links(page, final, limit=3):
                try:
                    f2, p2 = fetch(link)
                except Exception as exc:
                    report["errors"].append(f"{link}: {exc}")
                    continue
                report["pages_read"].append(f2)
                text += "\n" + strip_html(p2)
                templates += find_template_links(p2, f2)

    report["templates"] = list(dict.fromkeys(templates))[:8]
    report["text_chars"] = len(text)

    # ---- 2. prose ---------------------------------------------------------
    spec = {k: v for k, v in extract_spec(text).items() if v is not None}
    prose_keys = set(spec)

    # ---- 1. templates (override the prose) --------------------------------
    if probe_templates:
        # a Word template tells us the most; try those first
        ordered = sorted(report["templates"],
                         key=lambda u: (0 if u.lower().split("?")[0]
                                        .endswith((".docx", ".dotx")) else
                                        1 if u.lower().split("?")[0].endswith(".zip")
                                        else 2))
        for turl in ordered[:max_templates]:
            got = download_template(turl)
            if not got:
                report["errors"].append(f"could not download {turl}")
                continue
            fname, data = got
            tspec = template_probe.probe(fname or turl, data)
            report["template_specs"].append({"url": turl, "spec": tspec})
            if tspec.get("_error"):
                report["errors"].append(f"{fname}: {tspec['_error']}")
                continue
            report["probed"].append(fname or turl)
            for k, v in tspec.items():
                if not k.startswith("_") and v is not None:
                    spec[k] = v          # template wins
    report["spec"] = spec
    report["from_template"] = sorted(
        k for k in spec
        if any(k in ts["spec"] for ts in report["template_specs"]))
    report["from_prose"] = sorted(prose_keys - set(report["from_template"]))

    base_id, publisher = detect_publisher(final, text)
    report["base_id"] = base_id
    report["publisher"] = publisher
    report["name"] = page_title(page) or url
    report["profile"] = build_profile(report["name"], final, spec,
                                      base_id, publisher, report["templates"])
    report["found"] = sorted(spec)
    ALL = ["abstract_max_words", "keywords_min", "keywords_max", "font",
           "font_size", "line_spacing", "columns", "page_size", "margin_in",
           "reference_style", "numbered_sections"]
    report["missing"] = [k for k in ALL if k not in spec]
    return report


def save_profile(prof: dict) -> str:
    prof = {k: v for k, v in prof.items() if not k.startswith("_")}
    path = profiles.PROFILE_DIR / f"{prof['id']}.json"
    path.write_text(json.dumps(prof, indent=2))
    return str(path)


def download_template(url: str) -> tuple[str, bytes] | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return os.path.basename(urllib.parse.urlparse(url).path), r.read()
    except Exception:
        return None
