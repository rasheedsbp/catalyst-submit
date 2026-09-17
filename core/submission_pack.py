"""One-click submission package.

Given a manuscript and a target journal, this reads the journal's own author
guidelines (no API keys — plain web pages, or text pasted by the user), works out
what the journal asks for at submission, and builds every file it can:

    manuscript (formatted)        title page             cover letter
    blinded manuscript            highlights             declarations
    figures as separate files     tables as editable     suggested reviewers
    submission checklist          guidelines summary     LaTeX project

Two rules keep this honest:

  * Nothing is invented. Text comes from the manuscript or is a clearly marked
    [placeholder]. Statements an author must personally confirm ("not under
    consideration elsewhere") are marked [confirm].
  * Every requirement shows the sentence from the guidelines that triggered it
    and the page it came from, so the user can check it in seconds.
"""
from __future__ import annotations

import copy
import csv
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .model import Manuscript

# --------------------------------------------------------------------------- #
# 1. guidelines
# --------------------------------------------------------------------------- #
# Where publishers keep the author guide, relative to the journal homepage.
GUIDE_PATHS = {
    "sciencedirect.com": ["/publish/guide-for-authors"],
    "elsevier.com": ["/guide-for-authors"],
    "springer.com": ["/submission-guidelines"],
    "biomedcentral.com": ["/submission-guidelines/preparing-your-manuscript"],
    "nature.com": ["/submission-guidelines"],
    "mdpi.com": ["/instructions"],
    "wiley.com": ["/author-guidelines"],
    "tandfonline.com": ["/journal-information"],
    "sagepub.com": ["/author-instructions"],
    "frontiersin.org": ["/for-authors/author-guidelines"],
    "hindawi.com": ["/guides"],
    "ieee.org": ["/guide-for-authors"],
    "emerald.com": ["/author-guidelines"],
    "plos.org": ["/s/submission-guidelines"],
    "iopscience.iop.org": ["/page/Scope"],
}


@dataclass
class Guidelines:
    journal: str = ""
    homepage: str | None = None
    publisher: str | None = None
    pages: list[str] = field(default_factory=list)
    text: str = ""
    errors: list[str] = field(default_factory=list)


def _openalex_journal(name: str) -> dict | None:
    import requests
    try:
        r = requests.get("https://api.openalex.org/sources",
                         params={"search": name, "filter": "type:journal", "per_page": 5},
                         timeout=15)
        results = r.json().get("results", []) if r.ok else []
    except Exception:  # noqa: BLE001
        return None
    if not results:
        return None
    want = re.sub(r"[^a-z0-9]", "", name.lower())
    for s in results:                               # exact title match first
        if re.sub(r"[^a-z0-9]", "", (s.get("display_name") or "").lower()) == want:
            return s
    return results[0]


def gather_guidelines(journal: str = "", url: str | None = None,
                      pasted: str | None = None, max_pages: int = 5) -> Guidelines:
    from . import discover
    g = Guidelines(journal=journal)
    start = (url or "").strip() or None
    if not start and journal:
        src = _openalex_journal(journal)
        if src:
            g.journal = src.get("display_name") or journal
            g.homepage = src.get("homepage_url")
            g.publisher = src.get("host_organization_name")
            start = g.homepage
        else:
            g.errors.append("Journal not found on OpenAlex — paste the guidelines "
                            "URL or text instead.")
    queue = [start] if start else []
    if start:
        low = start.lower()
        for dom, paths in GUIDE_PATHS.items():
            if dom in low:
                queue = [start.rstrip("/") + p for p in paths] + queue
                break
    seen = set()
    while queue and len(g.pages) < max_pages:
        u = queue.pop(0)
        if not u or u in seen:
            continue
        seen.add(u)
        try:
            final, html = discover.fetch(u)
        except Exception as exc:  # noqa: BLE001
            g.errors.append(f"{u}: {type(exc).__name__}")
            continue
        g.pages.append(final)
        g.text += "\n" + discover.strip_html(html)
        if len(g.pages) == 1 and not g.journal:
            g.journal = discover.page_title(html)
        for link in discover.find_guideline_links(html, final, limit=4):
            if link not in seen:
                queue.append(link)
    if pasted and pasted.strip():
        g.text += "\n" + pasted.strip()
        g.pages.append("(text pasted by user)")
    return g


# --------------------------------------------------------------------------- #
# 2. requirements
# --------------------------------------------------------------------------- #
REQUIRED, OPTIONAL, NOT_FOUND = "Required", "Optional", "Not mentioned"


@dataclass
class Requirement:
    key: str
    label: str
    status: str = NOT_FOUND
    detail: str = ""
    evidence: str = ""
    output: str = ""           # file generated for it, if any
    action: str = ""           # what the user still has to do


def _sentence_around(text: str, m: re.Match, width: int = 220) -> str:
    a = text.rfind(".", 0, m.start()) + 1
    b = text.find(".", m.end())
    b = len(text) if b == -1 else b + 1
    s = text[a:b].strip()
    return (s[:width] + "…") if len(s) > width else s


_OPTIONAL_CUE = re.compile(r"\b(optional|may|can|encouraged|if applicable|where applicable|"
                           r"recommended|welcome)\b", re.I)
_REQUIRED_CUE = re.compile(r"\b(must|required|mandatory|should|need to|are asked to|"
                           r"compulsory|essential)\b", re.I)

RULES: list[tuple[str, str, str]] = [
    # key, label, pattern
    ("cover_letter", "Cover letter", r"cover(?:ing)? letter"),
    ("title_page", "Separate title page", r"title page|separate (?:file|page) (?:with|containing) (?:the )?author"),
    ("blinded", "Blinded (anonymised) manuscript",
     r"double[- ](?:blind|anonymi[sz]ed)|anonymi[sz]ed manuscript|blinded manuscript|"
     r"remove (?:all )?(?:author|identifying) (?:names|information)"),
    ("highlights", "Highlights", r"\bhighlights\b"),
    ("graphical_abstract", "Graphical abstract", r"graphical abstract|visual abstract|TOC graphic"),
    ("competing_interests", "Competing interests declaration",
     r"(?:declaration of )?(?:competing|conflicts?) of interests?|competing interests?"),
    ("funding", "Funding statement", r"funding (?:statement|information|sources?)|\bfunding\b"),
    ("data_availability", "Data availability statement", r"data availability|research data|data sharing statement"),
    ("ethics", "Ethics approval / consent", r"ethic(?:s|al) (?:approval|statement|committee)|informed consent|institutional review board"),
    ("author_contributions", "Author contributions (CRediT)", r"credit (?:author )?(?:statement|taxonomy|roles)|author contributions?"),
    ("ai_declaration", "Generative-AI use declaration", r"generative ai|ai[- ]assisted technologies|use of (?:ai|artificial intelligence)"),
    ("orcid", "ORCID iDs", r"\borcid\b"),
    ("reviewers", "Suggested reviewers", r"suggest(?:ed)? (?:potential )?reviewers|reviewer suggestions|potential reviewers"),
    ("figures_separate", "Figures as separate files",
     r"(?:figures?|artwork)[^.]{0,80}(?:separate(?:ly)? files?|uploaded separately|individual files?)|"
     r"separate files?[^.]{0,40}(?:figures?|artwork)"),
    ("tables_editable", "Editable tables", r"tables?[^.]{0,60}(?:editable|not as images?|as text)"),
    ("supplementary", "Supplementary material", r"supplementary (?:material|data|files?|information)"),
    ("latex_source", "LaTeX source files", r"latex (?:source|files?)|\.tex files?"),
    ("copyright", "Copyright / licence form", r"copyright (?:transfer|form|agreement)|licen[cs]e (?:to publish|agreement|form)"),
    ("reporting_checklist", "Reporting guideline checklist",
     r"\b(?:CONSORT|PRISMA|STROBE|ARRIVE|STARD|CARE|SRQR|COREQ|SPIRIT|TRIPOD)\b"),
    ("trial_registration", "Clinical trial registration", r"trial registration|clinicaltrials\.gov|registered (?:in|with) a (?:public )?trial registry"),
    ("preprint", "Preprint policy", r"preprints?"),
]


def extract_requirements(text: str) -> list[Requirement]:
    out: list[Requirement] = []
    t = re.sub(r"\s+", " ", text or "")
    for key, label, pat in RULES:
        req = Requirement(key=key, label=label)
        ms_ = list(re.finditer(pat, t, re.I))
        if ms_:
            sents = [_sentence_around(t, m) for m in ms_[:6]]
            strong = [s for s in sents if _REQUIRED_CUE.search(s)]
            soft = [s for s in sents if _OPTIONAL_CUE.search(s)]
            req.status = REQUIRED if strong else (OPTIONAL if soft else REQUIRED)
            req.evidence = (strong or soft or sents)[0]
        out.append(req)

    by = {r.key: r for r in out}
    # quantities that change what gets generated
    m = re.search(r"highlights[^.]{0,160}?(\d)\s*(?:to|-|–)\s*(\d)\s*bullet", t, re.I) or \
        re.search(r"(\d)\s*(?:to|-|–)\s*(\d)\s*bullet points", t, re.I)
    chars = re.search(r"(\d{2,3})\s*characters?[^.]{0,40}(?:including spaces|per (?:bullet|highlight))", t, re.I) \
        or re.search(r"highlights?[^.]{0,120}?(\d{2,3})\s*characters", t, re.I)
    if m:
        by["highlights"].detail = f"{m.group(1)}–{m.group(2)} bullet points"
    if chars:
        by["highlights"].detail += (", " if by["highlights"].detail else "") + \
            f"max {chars.group(1)} characters each"
    m = re.search(r"(?:suggest|provide|nominate)[^.]{0,40}?(\d{1,2}|three|four|five|six)\s*"
                  r"(?:potential |suitable )?reviewers", t, re.I)
    if m:
        words = {"three": "3", "four": "4", "five": "5", "six": "6"}
        by["reviewers"].detail = f"{words.get(m.group(1).lower(), m.group(1))} reviewers"
    fmts = sorted(set(f.upper() for f in re.findall(r"\b(TIFF?|EPS|PDF|JPE?G|PNG)\b", t)))
    dpi = re.search(r"(\d{3,4})\s*dpi", t, re.I)
    if fmts or dpi:
        by["figures_separate"].detail = ", ".join(
            x for x in [("formats: " + "/".join(fmts)) if fmts else "",
                        f"min {dpi.group(1)} dpi" if dpi else ""] if x)
    wl = re.search(r"(?:manuscript|article|paper|main text)[^.]{0,80}?(?:not exceed|maximum of|"
                   r"no more than|limit(?:ed)? (?:of|to))\s*([\d,]{3,6})\s*words", t, re.I)
    if wl:
        out.append(Requirement("word_limit", "Word limit", REQUIRED,
                               f"{wl.group(1)} words", _sentence_around(t, wl)))
    return out


def requirement_value(reqs: list[Requirement], key: str) -> Requirement | None:
    return next((r for r in reqs if r.key == key), None)


# --------------------------------------------------------------------------- #
# 3. what the manuscript already contains
# --------------------------------------------------------------------------- #
SECTION_KEYS = {
    "competing_interests": r"conflicts? of interest|competing interest|declaration of interest",
    "funding": r"\bfunding\b|financial support|grant",
    "data_availability": r"data (?:and code )?availab|data sharing|availability of data",
    "ethics": r"ethic|informed consent|institutional review",
    "author_contributions": r"author(?:s')? contributions?|credit",
    "acknowledgements": r"acknowledg",
    "ai_declaration": r"generative ai|use of ai|ai[- ]assisted",
}


def manuscript_statements(ms: Manuscript) -> dict[str, str]:
    found: dict[str, str] = {}
    for sec in ms.sections:
        title = (sec.title or "").lower()
        for key, pat in SECTION_KEYS.items():
            if key not in found and re.search(pat, title):
                txt = " ".join(b.text for b in sec.blocks if b.kind == "para").strip()
                if txt:
                    found[key] = re.sub("\u27eaMATH\\d+\u27eb", "", txt)
    for key, attr in (("funding", "funding"), ("competing_interests", "conflicts"),
                      ("data_availability", "data_availability")):
        if key not in found and getattr(ms, attr, ""):
            found[key] = getattr(ms, attr)
    return found


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text or "") if s.strip()]


# --------------------------------------------------------------------------- #
# 4. documents
# --------------------------------------------------------------------------- #
PH = "[{}]"


def _doc():
    from docx import Document
    from docx.shared import Pt
    d = Document()
    st = d.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(12)
    return d


def _bytes(d) -> bytes:
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def title_page(ms: Manuscript, journal: str) -> bytes:
    d = _doc()
    d.add_heading("Title Page", level=1)
    d.add_paragraph().add_run("Title: ").bold = True
    d.paragraphs[-1].add_run(ms.title or PH.format("title"))
    d.add_paragraph().add_run("Article type: ").bold = True
    d.paragraphs[-1].add_run(PH.format("e.g. Original Research Article"))
    d.add_heading("Authors", level=2)
    if ms.authors:
        for i, a in enumerate(ms.authors, 1):
            p = d.add_paragraph(f"{i}. {a.name}")
            p.add_run(f" — {a.affiliation or PH.format('department, institution, city, country')}")
            p.add_run(f" · Email: {a.email or PH.format('email')}")
            p.add_run(f" · ORCID: {a.orcid or PH.format('0000-0000-0000-0000')}")
    else:
        d.add_paragraph(PH.format("Author 1 name — affiliation — email — ORCID"))
    corr = next((a for a in ms.authors if a.corresponding), ms.authors[0] if ms.authors else None)
    d.add_heading("Corresponding author", level=2)
    d.add_paragraph(f"{corr.name if corr else PH.format('name')}, "
                    f"{(corr.affiliation if corr and corr.affiliation else PH.format('full postal address'))}, "
                    f"{(corr.email if corr and corr.email else PH.format('email'))}")
    from . import compact
    wc = compact.word_count(ms)
    d.add_heading("Manuscript details", level=2)
    for line in (f"Word count (body): {wc['body']:,}", f"Abstract words: {wc['abstract']}",
                 f"Figures: {len(ms.figures)}", f"Tables: {len(ms.tables)}",
                 f"References: {len(ms.references)}",
                 "Keywords: " + (", ".join(ms.keywords) or PH.format("keywords"))):
        d.add_paragraph(line, style="List Bullet")
    return _bytes(d)


def cover_letter(ms: Manuscript, journal: str, publisher: str | None) -> bytes:
    d = _doc()
    corr = next((a for a in ms.authors if a.corresponding), ms.authors[0] if ms.authors else None)
    d.add_paragraph(date.today().strftime("%d %B %Y"))
    d.add_paragraph(f"The Editor-in-Chief\n{journal or PH.format('journal name')}"
                    + (f"\n{publisher}" if publisher else ""))
    d.add_paragraph("Dear Editor,")
    d.add_paragraph(
        f"We are pleased to submit our manuscript entitled “{ms.title or PH.format('title')}” "
        f"for consideration as {PH.format('article type, e.g. an Original Research Article')} "
        f"in {journal or PH.format('journal name')}.")
    abs_s = _sentences(ms.abstract)
    if abs_s:
        # the authors' own abstract, uncut when short — trimming mid-abstract
        # leaves sentences like "It improves ..." with nothing to refer to
        summary = abs_s if len(abs_s) <= 5 else abs_s[:2] + abs_s[-1:]
        d.add_paragraph("In brief: " + " ".join(summary))
    d.add_paragraph(PH.format("One or two sentences on why this work suits the journal's "
                              "scope and readership — written by the authors."))
    d.add_paragraph(
        "[confirm] This manuscript is original, has not been published previously, and is "
        "not under consideration for publication elsewhere. All authors have approved the "
        "manuscript and agree with its submission. The authors declare no competing "
        "interests other than those stated in the manuscript.")
    d.add_paragraph("Thank you for considering our work.")
    d.add_paragraph("Sincerely,")
    d.add_paragraph(f"{corr.name if corr else PH.format('corresponding author')}\n"
                    f"{(corr.affiliation if corr and corr.affiliation else PH.format('affiliation'))}\n"
                    f"{(corr.email if corr and corr.email else PH.format('email'))}")
    return _bytes(d)


def highlights(ms: Manuscript, req: Requirement | None) -> tuple[bytes, list[dict]]:
    n_max, c_max = 5, 85
    if req and req.detail:
        m = re.search(r"(\d)\D+(\d) bullet", req.detail)
        if m:
            n_max = int(m.group(2))
        m = re.search(r"max (\d{2,3}) characters", req.detail)
        if m:
            c_max = int(m.group(1))
    pool = _sentences(ms.abstract)
    for sec in ms.sections:
        if re.search(r"conclu", sec.title or "", re.I):
            pool += [s for b in sec.blocks if b.kind == "para" for s in _sentences(b.text)]
    pool = [re.sub("\u27eaMATH\\d+\u27eb", "", s) for s in pool]
    claim = re.compile(r"\b(propos|improv|achiev|outperform|reduc|increas|novel|"
                       r"develop|introduc|demonstrat|show|enabl|detect|predict|"
                       r"accura|faster|efficien|robust)", re.I)
    rows = []
    for i, s_ in enumerate(dict.fromkeys(pool)):
        score = (2 if re.search(r"\d", s_) else 0) + (1 if claim.search(s_) else 0) \
            + (1 if len(s_) <= c_max else 0) - (1 if len(s_.split()) < 4 else 0)
        rows.append({"text": s_, "chars": len(s_), "fits": len(s_) <= c_max,
                     "score": score, "pos": i})
    # the most result-like sentences, kept in the order the paper tells them
    rows = sorted(sorted(rows, key=lambda r: -r["score"])[:max(n_max, 3)],
                  key=lambda r: r["pos"])
    d = _doc()
    d.add_heading("Highlights", level=1)
    d.add_paragraph(f"DRAFT — taken from your abstract and conclusion. The journal allows "
                    f"up to {n_max} bullets of at most {c_max} characters including spaces; "
                    "rewrite any marked [shorten].")
    for r in rows:
        tag = "" if r["fits"] else f"  [shorten: {r['chars']} chars]"
        d.add_paragraph(r["text"] + tag, style="List Bullet")
    return _bytes(d), rows


CREDIT_ROLES = ["Conceptualization", "Data curation", "Formal analysis",
                "Funding acquisition", "Investigation", "Methodology",
                "Project administration", "Resources", "Software", "Supervision",
                "Validation", "Visualization", "Writing – original draft",
                "Writing – review & editing"]


def declarations(ms: Manuscript, reqs: list[Requirement], found: dict[str, str]) -> bytes:
    d = _doc()
    d.add_heading("Declarations", level=1)
    wanted = [("competing_interests", "Declaration of competing interests",
               "[confirm] The authors declare that they have no known competing financial "
               "interests or personal relationships that could have appeared to influence "
               "the work reported in this paper."),
              ("funding", "Funding", PH.format("Funder name, grant number — or: This research "
                                               "received no specific grant from any funding agency.")),
              ("data_availability", "Data availability",
               PH.format("Where the data can be accessed, or: Data will be made available on request.")),
              ("ethics", "Ethics approval and consent",
               PH.format("Committee name and approval number, or: Not applicable.")),
              ("ai_declaration", "Declaration of generative AI use",
               PH.format("State any AI tool used, for what, and that the authors reviewed the "
                         "output — or: No generative AI tools were used."))]
    status = {r.key: r.status for r in reqs}
    for key, heading, placeholder in wanted:
        d.add_heading(heading, level=2)
        if key in found:
            d.add_paragraph(found[key])
            d.add_paragraph().add_run("(taken from your manuscript)").italic = True
        else:
            d.add_paragraph(placeholder)
        if status.get(key) == NOT_FOUND:
            d.add_paragraph("Not mentioned in the guidelines read — include if the "
                            "submission system asks for it.")
    d.add_heading("Author contributions (CRediT)", level=2)
    if "author_contributions" in found:
        d.add_paragraph(found["author_contributions"])
    names = [a.name for a in ms.authors] or ["Author 1", "Author 2"]
    table = d.add_table(rows=1, cols=len(names) + 1)
    table.style = "Table Grid"
    table.rows[0].cells[0].text = "Role"
    for i, n in enumerate(names, 1):
        table.rows[0].cells[i].text = n
    for role in CREDIT_ROLES:
        cells = table.add_row().cells
        cells[0].text = role
    d.add_paragraph("Mark each author's roles with an X.")
    return _bytes(d)


def tables_doc(ms: Manuscript) -> bytes:
    from . import inline_math
    d = _doc()
    d.add_heading("Tables", level=1)
    for i, t in enumerate(ms.tables, 1):
        d.add_paragraph().add_run(f"Table {i}. {inline_math.to_plain(t.caption)}").bold = True
        if not t.rows:
            continue
        ncol = max(len(r) for r in t.rows)
        tb = d.add_table(rows=0, cols=ncol)
        tb.style = "Table Grid"
        for r in t.rows:
            cells = tb.add_row().cells
            for c in range(ncol):
                cells[c].text = inline_math.to_plain(r[c]) if c < len(r) else ""
        d.add_paragraph()
    return _bytes(d)


def figure_files(ms: Manuscript, req: Requirement | None) -> tuple[dict[str, bytes], list[str]]:
    """Each figure as its own file, in a journal-accepted format where stated."""
    from PIL import Image
    want_tiff = bool(req and req.detail and "TIF" in req.detail and
                     not re.search(r"PNG|JPE?G", req.detail))
    dpi_m = re.search(r"min (\d{3,4}) dpi", req.detail) if req and req.detail else None
    dpi = int(dpi_m.group(1)) if dpi_m else 300
    files, notes = {}, []
    for i, f in enumerate(ms.figures, 1):
        if not f.path or not os.path.exists(f.path):
            notes.append(f"Figure {i}: no image in the source file — supply the artwork.")
            continue
        ext = Path(f.path).suffix.lower() or ".png"
        try:
            im = Image.open(f.path)
            width_in = im.width / dpi
            if width_in < 3.3:          # narrower than a single journal column at this dpi
                notes.append(f"Figure {i}: {im.width}px wide ≈ {width_in:.1f} in at {dpi} dpi — "
                             "below single-column width; supply a higher-resolution original.")
            if want_tiff:
                buf = io.BytesIO()
                (im.convert("RGB") if im.mode not in ("RGB", "L", "CMYK") else im) \
                    .save(buf, format="TIFF", dpi=(dpi, dpi), compression="tiff_lzw")
                files[f"Figure_{i}.tif"] = buf.getvalue()
                continue
        except Exception:  # noqa: BLE001
            pass
        files[f"Figure_{i}{ext}"] = Path(f.path).read_bytes()
    return files, notes


def reviewers_csv(req: Requirement | None) -> bytes:
    n = 5
    if req and req.detail:
        m = re.search(r"(\d{1,2})", req.detail)
        n = int(m.group(1)) if m else n
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Affiliation", "Country", "Email", "ORCID",
                "Expertise relevant to the paper", "Why suitable (no conflict of interest)"])
    for _ in range(n):
        w.writerow([""] * 7)
    return buf.getvalue().encode()


def blinded(ms: Manuscript) -> Manuscript:
    """Copy with author names, affiliations and identifying sections removed."""
    b = copy.deepcopy(ms)
    b.authors = []
    b.sections = [s for s in b.sections if not re.search(
        r"acknowledg|funding|author(?:s')? contributions?|credit|conflicts? of interest|"
        r"competing interest", s.title or "", re.I)]
    return b


# --------------------------------------------------------------------------- #
# 5. package
# --------------------------------------------------------------------------- #
@dataclass
class FormatOptions:
    """The formatter's own settings, applied to every manuscript in the pack."""
    template_bytes: bytes | None = None      # journal's Word template (.docx/.dotx)
    tex_extras: dict = field(default_factory=dict)   # .cls/.sty/.bst from a kit
    equation_mode: str = "native"            # "native" (editable) or "image"
    keep_masthead: bool = True
    float_mode: str = "journal"              # LaTeX: "journal" floats or "inline"
    compact: bool = False
    add_missing_mentions: bool = True        # "as shown in Fig. 3" for uncited objects
    include_latex: bool = True
    include_pdf: bool = True


def format_manuscript(ms: Manuscript, prof: dict, opt: FormatOptions) -> dict:
    """Run the journal formatter exactly as the Formatter page does."""
    from . import render_docx, render_latex, preview, xref, checks
    out = copy.deepcopy(ms)
    applied: list[str] = []
    if opt.add_missing_mentions:
        missing = xref.find_missing(out)
        if missing:
            added = xref.apply(out, missing, {m["label"] for m in missing})
            if added:
                applied.append(f"added in-text mentions for {len(added)} object(s)")
    res = {"report": checks.run(out, prof), "applied": applied, "warning": "",
           "ms": out}

    def word(m):
        try:
            return render_docx.render(m, prof, equation_mode=opt.equation_mode,
                                      template_bytes=opt.template_bytes,
                                      keep_masthead=opt.keep_masthead)
        except Exception as exc:  # noqa: BLE001
            if not opt.template_bytes:
                raise
            res["warning"] = (f"Could not build inside the uploaded template "
                              f"({type(exc).__name__}); used the {prof['name']} profile.")
            return render_docx.render(m, prof, equation_mode=opt.equation_mode)

    res["docx"] = word(out)
    res["docx_blinded"] = word(blinded(out))
    res["zip"] = None
    if opt.include_latex:
        try:
            res["zip"] = render_latex.build_zip(out, prof, extra_files=opt.tex_extras,
                                                float_mode=opt.float_mode,
                                                compact=opt.compact)
        except Exception as exc:  # noqa: BLE001
            res["warning"] += f" LaTeX project not built ({type(exc).__name__})."
    res["pdf"] = None
    if opt.include_pdf and preview.soffice_available():
        try:
            res["pdf"] = preview.render_pdf(res["docx"])
        except Exception:  # noqa: BLE001
            pass
    res["html"] = preview.html_preview(out, prof)
    return res


def build_pack(ms: Manuscript, prof: dict, g: Guidelines,
               reqs: list[Requirement] | None = None,
               include_latex: bool = True,
               fmt: FormatOptions | None = None,
               formatted: dict | None = None) -> tuple[bytes, list[Requirement]]:
    from . import render_docx, render_latex
    reqs = reqs if reqs is not None else extract_requirements(g.text)
    by = {r.key: r for r in reqs}
    found = manuscript_statements(ms)
    journal = g.journal or prof.get("name", "")
    files: dict[str, bytes] = {}
    notes: list[str] = []

    def mark(key, output="", action=""):
        r = by.get(key)
        if r:
            r.output = output or r.output
            r.action = action or r.action

    fmt = fmt or FormatOptions(include_latex=include_latex, include_pdf=False,
                               add_missing_mentions=False)
    formatted = formatted or format_manuscript(ms, prof, fmt)
    ms = formatted["ms"]
    if formatted.get("warning"):
        notes.append(formatted["warning"].strip())
    notes += [f"Formatter: {a}" for a in formatted.get("applied", [])]

    # always useful, whether or not the guide names them
    files["01_Manuscript.docx"] = formatted["docx"]
    if formatted.get("pdf"):
        files["01_Manuscript.pdf"] = formatted["pdf"]
    files["02_Title_page.docx"] = title_page(ms, journal)
    mark("title_page", "02_Title_page.docx", "Fill any [placeholders]; add ORCID iDs.")
    mark("orcid", "02_Title_page.docx", "Add each author's ORCID iD.")
    files["03_Cover_letter.docx"] = cover_letter(ms, journal, g.publisher)
    mark("cover_letter", "03_Cover_letter.docx",
         "Write the scope-fit sentence; confirm the [confirm] statements.")
    files["05_Declarations.docx"] = declarations(ms, reqs, found)
    for k in ("competing_interests", "funding", "data_availability", "ethics",
              "ai_declaration", "author_contributions"):
        mark(k, "05_Declarations.docx",
             "Check the statement taken from your manuscript." if k in found
             else "Replace the [placeholder] text.")

    if by["blinded"].status != NOT_FOUND:
        files["04_Manuscript_blinded.docx"] = formatted["docx_blinded"]
        mark("blinded", "04_Manuscript_blinded.docx",
             "Also remove self-identifying phrases (e.g. 'in our previous work [12]').")
    if by["highlights"].status != NOT_FOUND:
        hb, rows = highlights(ms, by["highlights"])
        files["06_Highlights.docx"] = hb
        bad = sum(1 for r in rows if not r["fits"])
        mark("highlights", "06_Highlights.docx",
             f"Draft from your text — {bad} bullet(s) need shortening." if bad
             else "Draft from your text — check the wording.")
    if ms.tables:
        files["07_Tables.docx"] = tables_doc(ms)
        mark("tables_editable", "07_Tables.docx")
    figs, fig_notes = figure_files(ms, by["figures_separate"])
    for name, data in figs.items():
        files[f"Figures/{name}"] = data
    notes += fig_notes
    if figs:
        mark("figures_separate", "Figures/", "; ".join(fig_notes[:2]))
    if by["reviewers"].status != NOT_FOUND:
        files["08_Suggested_reviewers.csv"] = reviewers_csv(by["reviewers"])
        mark("reviewers", "08_Suggested_reviewers.csv",
             "Add experts with no recent co-authorship or shared affiliation.")
    if formatted.get("zip"):
        files["09_LaTeX_project.zip"] = formatted["zip"]
        mark("latex_source", "09_LaTeX_project.zip")
    fails = [m for lvl, m in formatted.get("report", []) if lvl == "fail"]
    warns = [m for lvl, m in formatted.get("report", []) if lvl == "warn"]
    if fails or warns:
        files["00_Format_check.txt"] = ("\n".join(
            [f"FAIL  {m}" for m in fails] + [f"WARN  {m}" for m in warns])).encode()
        notes.append(f"Format check: {len(fails)} failure(s), {len(warns)} warning(s) "
                     "— see 00_Format_check.txt")

    for key, action in (("graphical_abstract", "Design a single-panel image summarising "
                                               "the work (cannot be generated automatically)."),
                        ("copyright", "Signed during/after acceptance in the submission system."),
                        ("reporting_checklist", "Download the named checklist and complete it."),
                        ("trial_registration", "Add the registry name and number to the abstract."),
                        ("supplementary", "Collect supplementary files separately.")):
        if by[key].status != NOT_FOUND:
            mark(key, "", action)

    # checks against stated limits
    from . import compact
    wc = compact.word_count(ms)
    if "word_limit" in by:
        lim = int(by["word_limit"].detail.split()[0].replace(",", ""))
        by["word_limit"].action = (f"Over by {wc['body'] - lim:,} words." if wc["body"] > lim
                                   else f"Within limit ({wc['body']:,} words).")

    files["00_Submission_checklist.md"] = checklist_md(journal, g, reqs, notes).encode()
    files["00_Submission_checklist.csv"] = checklist_csv(reqs)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        root = re.sub(r"[^A-Za-z0-9]+", "_", journal or "journal").strip("_")[:40]
        for name, data in files.items():
            z.writestr(f"Submission_{root}/{name}", data)
    return buf.getvalue(), reqs


def checklist_md(journal: str, g: Guidelines, reqs: list[Requirement], notes: list[str]) -> str:
    L = [f"# Submission checklist — {journal or 'target journal'}",
         f"Prepared {date.today().isoformat()}.", ""]
    L.append("Guidelines read: " + (", ".join(g.pages) if g.pages else "none (fetch failed)"))
    if g.errors:
        L.append("Could not read: " + "; ".join(g.errors[:5]))
    L.append("")
    order = {REQUIRED: 0, OPTIONAL: 1, NOT_FOUND: 2}
    for r in sorted(reqs, key=lambda r: order.get(r.status, 3)):
        box = "[x]" if r.output else "[ ]"
        L.append(f"- {box} **{r.label}** — {r.status}"
                 + (f" ({r.detail})" if r.detail else "")
                 + (f" → `{r.output}`" if r.output else ""))
        if r.action:
            L.append(f"    - To do: {r.action}")
        if r.evidence:
            L.append(f"    - Guidelines: “{r.evidence}”")
    if notes:
        L += ["", "## Notes"] + [f"- {n}" for n in notes]
    L += ["", "Items marked [placeholder] or [confirm] in the generated files must be "
              "completed by the authors before submitting."]
    return "\n".join(L)


def checklist_csv(reqs: list[Requirement]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Item", "Status", "Detail", "Generated file", "To do", "Guidelines evidence"])
    for r in reqs:
        w.writerow([r.label, r.status, r.detail, r.output, r.action, r.evidence])
    return buf.getvalue().encode()
