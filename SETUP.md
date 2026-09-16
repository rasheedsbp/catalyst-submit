# Journal Formatter — free tool + services funnel

Three pages:

- **Journal Formatter** (`app.py`) — the free tool. No login, no wall.
- **Our Services** (`pages/1_Services.py`) — service cards; clicking one opens
  a request form.
- **Admin** (`pages/9_Admin.py`) — every request, with status and CSV export.
  Protected by `JF_ADMIN_KEY`; hidden entirely if that isn't set.

## Run

```bash
pip install -r requirements.txt
streamlit run app.py
```

Server:

```bash
cp .env.example .env
nano .env          # JF_DOMAIN, JF_EMAIL, JF_WHATSAPP, JF_ADMIN_KEY
docker compose up -d --build
```

## Set these before launch

| Variable | Why |
|---|---|
| `JF_EMAIL` | Shown in the sidebar and footer as a mailto link |
| `JF_WHATSAPP` | Becomes a wa.me link — the fastest channel for most scholars |
| `JF_ADMIN_KEY` | Long random string. Without it the admin page refuses to open |
| `JF_DOMAIN` | Suggested: `formatter.cssoftwaresolutions.in` |

## What gets captured

The request form collects name, email, phone, institution, country, role,
research area, target journal, timeline and their message. Every field lands in
`data/leads.db` and appears in Admin immediately.

Two entry points, both tagged so you can tell them apart:

- `services page` — someone browsing services
- `formatter tool` — someone who just formatted a document and asked for help

Each request carries a status (New → Contacted → Quoted → In progress → Won →
Closed) and an internal notes field, so this doubles as a light CRM. Nothing
falls between the click and the invoice.

## How the funnel works

**The tool never asks for anything before the download.** Files come first,
always. A wall in front of a free tool turns a would-be client into someone who
tells colleagues it's bait.

**Offers are tied to what the checker actually found**, so the pitch is specific
rather than a banner:

| Detected | Offered |
|---|---|
| Figures with no artwork | Figures & Tables |
| Citations with no reference entry | Manuscript Preparation |
| Abstract over the limit, structured abstract required | Manuscript Preparation |
| Reviewer comments left in the file | Reviewer Response Support |
| Eight or more results tables | Statistical Analysis |
| Equations present | Python & MATLAB Implementation |

A clean manuscript gets no pitch. That restraint is what keeps the tool worth
recommending.

## Design

Colours are sampled from the logo itself: navy `#08238A`, sky `#50B4EF`, with
the pink/red/orange/green accent bar echoing the petal mark. The logo sits in
the header and sidebar of every page.

To swap the logo, replace `assets/logo.png` (square, 320px or larger).

## Privacy — say it and keep it true

Uploads are processed in memory and temporary files, deleted when the session
ends. Only anonymous counts (journal, figure/table totals, word count) and
details people type into the form themselves are stored.

Researchers hand this tool unpublished manuscripts. If they suspect otherwise,
it stops being useful as an introduction. There is deliberately no tracking
beyond what someone submits.

## Back up

`data/leads.db` holds every enquiry.

```bash
crontab -e
0 3 * * * /opt/journal-formatter/backup.sh
```

---

# Journal Finder

The Journal Finder is integrated as a second tool, sharing the same navigation,
theme and client profile. The pipeline itself is unchanged from the original
project — only the chrome differs.

**Datasets are not bundled** (they run to ~41 MB). On first use, open the
sidebar on that page and press **Fetch data**; they download into `data/` and
stay there. Do this once after deploying, or the Scopus requirement returns
nothing.

Because the finder holds datasets in `data/` alongside `leads.db`, keep that
volume mounted — `docker-compose.yml` already does.

---

# Top navigation

Every page carries the same bar: Journal Formatter · Journal Finder · Our
Services, plus a button through to cssoftwaresolutions.in. Streamlit's own
sidebar page list is hidden, because it shows raw filenames and collapses on
mobile — the bar sits where a visitor arriving from a search actually looks.

---

# Personalised experience

A visitor can set up a profile once, from the sidebar on any page. After that:

- Forms across all three tools come prefilled
- Their usual target journal is pre-selected in the formatter
- Pages greet them by name
- Service requests top the profile up rather than asking again

Each profile has a short code (`CS-XXXXXX`) that carries it to another device.

**For clients you already work with**, create the profile yourself: Admin →
Client profiles → Create. You get back a link like
`https://formatter.cssoftwaresolutions.in/?c=CS-EJERGB`. Send that to the
client and the tools open already knowing who they are and which journals they
publish in. That is the difference between a generic free tool and something
that feels like part of the service they are paying for.

Three deliberate limits:

- **Optional.** Every tool works fully without a profile, and nothing is gated
  behind creating one.
- **Chosen, not observed.** A profile holds only what someone typed into it.
  There is no fingerprinting and no tracking cookie. Researchers upload
  unpublished manuscripts here; quiet observation would betray that.
- **Not an account.** No password, so a code is a convenience, not a
  credential. Don't put anything in `notes` you wouldn't want the client to see
  if they guessed a code — treat it as a bookmark, not a vault.

---

# Performance notes

Measured on a 22-page, 10,700-word review with 15 figures and 18 tables:

| | Before | Now |
|---|---|---|
| Upload → files ready | 36.8s | **6.3s** |
| Each interaction | 2.1s | **1.3s** |
| Document analysis | 22.2s | **0.03s** |

Four things were responsible:

- **A cache that silently failed.** The analysis was cached with `st.cache_data`,
  but the proposal objects carry callables, which cannot be pickled. Every run
  raised and retried. Proposals are milliseconds to compute, so they are simply
  recomputed and only picklable results are cached.
- **LibreOffice ran twice on every format** — once for page images, once for the
  PDF — about 16 seconds nobody had asked for. Both are now produced on request,
  and the exact-page preview renders three pages first with a button for the
  rest.
- **Duplicate detection was O(n²)** across every sentence pair. It now blocks
  candidates on rare-word overlap first: 22.2s → 1.3s, identical results. It is
  also deferred until the Length panel is opened.
- **The logo was re-encoded on every rerun**, twice per page. Cached, and the
  file is smaller.



Streamlit re-runs the whole script on every interaction, so anything that scans
the manuscript must be memoised or the page feels frozen on a long paper.

Two fixes matter:

- **Duplicate detection was O(n²)** over every sentence pair — 22 seconds on a
  10,000-word review, recomputed on every keystroke. It now blocks candidates
  on rare-word overlap before running the expensive comparison: 1.2 seconds,
  identical results.
- **The whole analysis is cached** against a key covering the file, journal,
  title, abstract and keywords, so editing any of those recomputes and nothing
  else does.

Measured on the 22-page review paper: first load ~6s (Streamlit boot), parse and
analysis ~2s, and about 2s per interaction thereafter.

If it still feels slow:

- **Give the server 2 GB.** LibreOffice conversion for the PDF preview is the
  heaviest step; on a small box it swaps or gets killed.
- **Untick "High-fidelity page preview"** in Output options while you work, and
  turn it on for the final render. That skips LibreOffice entirely.
- **Windows with Python 3.14** is ahead of many wheels. If installation is slow
  or fails, use Python 3.12 — that is what the Docker image runs.

# Ease of use

The common path is now two actions: upload, download.

- **Formatting starts automatically** once there is a manuscript and a journal.
  No button to find. The button remains for re-running after changes.
- **Downloads sit directly under the heading**, before the preview, because
  that is what people came for.
- **Everything optional is collapsed** — title and author edits, figure and
  table corrections, missing in-text mentions, length analysis, the check
  report, output options. Each is labelled with a count so you know whether it
  is worth opening.
- **Slow work is opt-in.** The instant HTML preview shows immediately; exact
  page images and PDF export are buttons, each taking about four seconds.
