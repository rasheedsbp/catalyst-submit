# Journal Formatter

Upload a manuscript, pick a journal (or upload the journal's template), download
a formatted Word file and an Overleaf-ready LaTeX project.

## Run

```bash
pip install -r requirements.txt
streamlit run app.py
```

Optional, for page-accurate previews and PDF export:

```bash
sudo apt install libreoffice poppler-utils     # Ubuntu/Debian
brew install --cask libreoffice && brew install poppler   # macOS
```

## How you use it

1. **Upload the manuscript.** `.docx`, `.tex`, a LaTeX `.zip`, or `.pdf`
   (PDF is lossy — prefer the source).
2. **Pick the journal**, or upload the publisher's template and leave the
   selector on *use my uploaded template*. The template is the more accurate
   route: it states the real page setup, fonts and columns rather than
   describing them.
3. **Press Format now**, and download.

Everything between steps 2 and 3 is optional and folded into expanders:

- **Title, authors, abstract, keywords** — correct anything read wrongly.
- **Figures, tables and equations** — reclassify or drop anything misread. A
  Word file cannot distinguish an equation pasted as a picture from a figure,
  so this is where you settle it.
- **Suggested fixes** — each detected problem is a separate tick box with a
  description of exactly what it changes. Nothing applies unless ticked.
  Reviewer comments found inside the `.docx` are listed here too; they are
  normally lost on conversion.
- **Full check report** — abstract length, keyword count, dangling citations,
  uncited figures and tables, missing sections.
- **Output options** — equation rendering, whether to keep the template's logo
  and masthead, preview fidelity.

## LaTeX output

The zip contains `main.tex`, `figures/`, and any class files you supplied.
Upload it to Overleaf, set `main.tex` as the main document, Recompile.

Three things make it compile rather than error:

- **Character sanitising.** pdflatex halts on any character it doesn't know —
  `Unicode character Ṭ (U+1E6C) not set up for use with LaTeX` — and client
  reference lists are full of them. Smart quotes, dashes, maths symbols and
  Greek are mapped to TeX equivalents; accented Latin becomes accent commands
  (`Ṭ` → `\d{T}`, `ø` → `\o{}`); control characters are stripped. Every
  `main.tex` is verified pure ASCII.
- **Honest class handling.** `main.tex` names a publisher class only when that
  class is genuinely present in a standard TeX Live / Overleaf install, or when
  you supplied it. The list is verified against a real installation — an
  optimistic entry means a failed build. Otherwise it falls back to an
  `article` preamble built from the journal's own geometry, which compiles
  anywhere.
- **No dangling includes.** A figure with no artwork emits a framed placeholder
  instead of `\includegraphics` pointing at a file that doesn't exist, which
  would otherwise halt the compile.

Verified: all 36 document × journal combinations compile with real IEEEtran and
elsarticle at zero errors, in both placement modes.

### Table widths

Column widths are computed so the total is exactly `\linewidth`. Two traps make
tables run past the margin and get cropped:

- A `p{0.13\linewidth}` column is that width **plus** `2\tabcolsep` of padding.
  Seven of them overrun the line by about 84pt. The width is therefore computed
  inside `\dimexpr` with the padding subtracted, so the arithmetic is exact at
  any column count.
- A column narrower than its longest unbreakable word overflows regardless of
  balancing, so each column's width has a floor set by its longest word, and
  long tokens like `source(host/network/both/flow` get break opportunities at
  their punctuation.

Dense tables also get tighter `\tabcolsep` and smaller type. The Word renderer
applies the same logic, rescaling column widths to fit the text width rather than
letting a per-column minimum push the total over.

Verified: across 72 LaTeX builds, zero compile errors and no overfull box wider
than 12pt; zero Word tables exceeding the page width.

### Why the PDF may not match the preview

LaTeX treats tables and figures as *floats*: it moves them to the top or bottom
of a page to avoid leaving gaps. That is how a typeset paper is supposed to look
and what reviewers expect, but it means the PDF will not match the Word preview
page for page — an object mentioned on page 4 may print on page 5.

Two controls handle this:

- **Float like a real journal** (default) — normal LaTeX behaviour, with the
  float limits relaxed so a paper with thirty-odd objects doesn't queue them all
  to the end. Wide tables use `table*` and may sit at the bottom of a page.
- **Pin in place** — every object fixed exactly where it appears in your text, so
  the PDF matches the preview. More whitespace, but nothing wanders.

Heading levels also matter here. Unnumbered ALL-CAPS headings are read as
subsections while parsing, but if they outnumber the numbered ones they were the
real sections and get promoted. Left as subsections they become "A.", "B.", "C."
under whichever numbered heading came first, and the whole paper collapses under
one section — which is what a flat, disordered two-column PDF usually indicates.

## Word output

Built inside the publisher's template when you supply one, so styles, theme
fonts, headers, footers, margins and columns are theirs. The logo, journal name
and ISSN/DOI banner are preserved — templates usually put these in the body, so
clearing the body naively throws them away.

Without a template it is built from the journal profile: page size, margins,
columns, fonts, heading numbering, caption placement. Tables with four or more
columns span the full page width rather than being crushed into a column.

### Templates with unusual styles

A publisher template only contains the styles it actually uses, so common names
like *Table Grid* are frequently absent. Setting a table style by name then
raises `KeyError` and aborts the run. Table borders are therefore written
directly into the table properties when no suitable style exists — visually
identical, and dependent on nothing. Every style access is defensive: a missing
style degrades the formatting, it does not stop the job.

If a template turns out to be malformed in some way that still breaks the build,
the Word file is produced from the journal profile instead and the app says so.
The LaTeX output is unaffected either way.

## Equations

Word stores equations as OMML, which `python-docx` ignores — so untreated,
display equations vanish and sentences lose their symbols. `core/omml.py`
converts OMML to LaTeX (fractions, sub/superscripts, radicals, n-ary operators,
delimiters, matrices, accents, functions). Numbered display equations are
detected by residual text, which is how a bare `m:oMath` beside its number is
told apart from inline math.

Word output defaults to **images**, which render identically in Word,
LibreOffice and PDF. Native OMML stays editable in Word but LibreOffice often
renders it blank, so PDFs sent to clients can come out empty.

## Adding journals

Each journal is one JSON file in `profiles/`. Copy one and edit it, or build one
from a URL or template in the app and save it — after that, just type the name.

## What it will not do

Rewrite prose, trim an abstract to a word count, or delete citations whose
sources are missing. Those need the author's judgement, and a formatter that
guesses at them quietly damages papers. They are reported precisely instead.

## In-text mentions for figures and tables

Journals require every float to be referred to in the text, and reviewers reject
papers where they are not. Manuscripts lose these constantly when content moves
around.

The **Missing in-text mentions** panel lists every figure, table and equation
with no reference, and the exact sentence it would add — built from the object's
own caption and appended to the paragraph immediately before it. You approve
each one.

The sentences state that the object exists ("Table 3 presents the statistics of
attack happened in chemical industries") and say nothing about what the data
shows. Writing "Table 3 shows that accuracy improves with depth" means reading
the data and making a claim on the author's behalf, and that claim would go into
peer review under their name. The tool will not manufacture it.

## Length

Two ways to shorten a paper; only one is safe unattended.

**Layout compaction** (Output options → *Compact layout*) tightens float
spacing, caption size, display-maths gaps, list spacing and heading gaps in the
LaTeX. It removes pages without touching a word — typically a page or two on a
20-page review.

**Text reduction** is reported, not performed. The Length panel shows words by
section, sentences repeated verbatim or near-verbatim elsewhere, and filler
phrases with their substitutions. Repeated sentences and filler can be removed
on approval, because deleting them provably loses nothing. Real compression —
deciding which of an author's sentences matters least and rewriting the rest —
is not attempted: offline, without understanding the field, that is guesswork
applied to someone's research.

On a real 10,700-word review the safe cuts came to about 1.3% of the body. That
number is the point: if a paper is over length, the text is rarely where the
slack is, and layout compaction plus the per-section word budget will get you
further than any automatic rewriting.

One safeguard worth knowing: a sentence carrying a cross-reference is never
treated as a duplicate. Deleting it would silently un-cite a figure.

## Private requests-only admin

The deployed app includes a hidden, password-protected requests console at:

```text
https://YOUR-DOMAIN/requests
```

It is deliberately not listed in the public sidebar. The page exposes only the
`requests` table: date/time, service, contact details, journal, message, status
and internal notes. Client profiles and anonymous formatting-run data are not
shown.

Set the password on the server (not in Git):

```bash
JF_ADMIN_KEY=use-a-long-random-password
JF_PUBLIC_URL=https://YOUR-DOMAIN
```

The admin can search, filter by status/date, export the filtered requests to CSV,
update request status/notes, refresh, and log out.
