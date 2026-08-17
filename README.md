# ATLAS — ORION-1 MVP

Deterministic fault-tracing over the synthetic ORION-1 line. This is the
single-station-class MVP from the roadmap: prove that "trace this fault"
returns a correct, cited, non-hallucinated answer before adding the LLM
conversation layer, multi-tenant security, or PLC auto-parsing.

## What's here

- `schema_postgres.sql` — canonical Postgres schema (Railway deployment target)
- `atlas/db.py` — SQLite mirror of that schema, for local dev with zero setup
- `atlas/seed.py` — the full ORION-1 dataset (stations, addresses, relationships,
  fault codes, normal running state) transcribed from the canonical spec
- `atlas/tools.py` — the deterministic query functions (`get_address`,
  `find_writers`, `find_readers`, `trace_address`, `trace_fault`, etc.) —
  **no LLM involved**. This is what stops ATLAS from inventing a rung or
  register that doesn't exist.
- `atlas/test_atlas.py` — proves the tools work, including the exact
  ST40 fault-403 scenario from the spec end to end
- `atlas/api.py` — FastAPI layer exposing the tools as HTTP endpoints

## Run it

```bash
cd atlas
python3 -m atlas.seed          # builds atlas.db and seeds ORION-1
python3 -m unittest atlas.test_atlas -v   # proves the traces work
```

To run the API locally (requires `pip install fastapi uvicorn`):
```bash
uvicorn atlas.api:app --reload
curl http://localhost:8000/fault/403
curl http://localhost:8000/priority-state
```

## Agent orchestrator (`atlas/agent.py`)

Costs zero tokens right now. `MockLLM` is a rule-based stand-in for a real
LLM's tool-calling decisions — it proves the orchestrator loop (parse
question -> call tool(s) -> chain results -> assemble answer) works
correctly. It does NOT prove reasoning quality on ambiguous phrasing — only
a real model call does that. Swap in a real Claude/GPT client later; same
orchestrator code, same `TOOL_REGISTRY`, zero rewrite.

```bash
python3 -c "from atlas import agent; r = agent.run_agent('Why is fault 403 happening at ST40?'); print(r.answer)"
```

## Document ingestion (`atlas/ingestion.py`)

Real, not stubbed:
- PDFs: real text extraction via `pdfplumber`. Works well on exported/
  text-based PDFs. A scanned/image-only PDF will extract little or nothing
  — a genuine limitation of any text extractor, not a shortcut taken here.
- Images (schematics, photos, screenshots): real OCR via `pytesseract`.
  Reads text labels reasonably well; does NOT understand circuit topology,
  symbols, or wiring diagrams as structured data — see "Vision understanding" below.
- Uploaded documents do **not** automatically populate the `addresses` /
  `relationships` tables — turning extracted text into structured facts is
  a separate, harder problem, deliberately left out so OCR noise never
  silently becomes an invented PLC relationship.

## Intake gate — the "don't dump and blur" system

Every upload goes through `ingestion.ingest_file()`, which:
1. Extracts text (real PDF extraction / real OCR, per above).
2. Cross-checks extracted text against known addresses (`cross_check()`) —
   reports genuine matches vs. genuinely unrecognized tokens, never
   inventing either.
3. Runs a heuristic suggestion pass (`analyze.py`) for document kind/station
   — advisory only, never auto-applied.
4. If line, station, or document kind weren't supplied, the document is
   stored with `status = 'pending_review'` and explicit `pending_questions`
   — e.g. *"What line does this belong to?"*, *"What station does this
   belong to?"*, *"What type of document is this?"*
5. `search_documents()` only searches `status = 'confirmed'` documents by
   default — an unclassified upload can never surface as if it were
   reliable knowledge.
6. `confirm_document(doc_id, line_code, station_code, document_kind)`
   answers the outstanding questions and flips status to `confirmed` — and
   rejects confirmation outright if given an unknown line/station/kind,
   rather than silently accepting bad classification.

Same discipline is enforced in the agent (`agent.py`): a fault/alarm
question with no station mentioned gets a clarifying question back, not a
guessed trace — and if the station given doesn't match the fault's actual
station in the data, the agent flags the mismatch instead of tracing the
wrong thing. This is the literal "ask for station before answering" rule
from the original TRACE spec, now enforced in code and tested.

## Line sections — "open Line 5's section" access pattern

`GET /lines/{line_code}` returns everything filed under one line: its
stations, confirmed documents, and declared flow-of-operation — strictly
scoped by `line_code`, tested to prove no leakage (`TestLineSections`).
`search_documents()` and `list_documents()` both accept an optional
`line_code` filter for the same reason. Two lines exist in the seed data —
`ORION-1` (the full dataset) and a minimal `NOVA-2` — specifically so
isolation between sections is a tested fact, not a claim.

## Flow-of-operation consistency check (`atlas/flow_check.py`)

Uploading a `document_kind='layout'` document requires a stated flow (e.g.
`"ST10 -> ST20 -> ST30 -> ST40"`). That claim gets checked, hop by hop,
against real relationship data: does a WRITES_TO relationship actually
exist from one station's addresses into the next station's addresses? Real
finding from testing this against ORION-1: **ST10->ST20 and ST20->ST30 are
data-backed; ST30->ST40 is NOT** — ST30 only sets a routing *decision* bit,
it doesn't have a modeled product-data write to ST40. The checker correctly
flags that gap (`UNCONFIRMED_BY_LOGIC`) instead of rubber-stamping the
layout's claim. This is the deliberate point: a diagram saying two stations
talk to each other is not proof they do, and now that's enforced in code.

## Vision "understanding" — what's real vs. what needs tokens

`analyze.py`'s `MockVisionAnalyzer` is free and real in the sense that it
actually runs — but it's text-pattern matching (filename + OCR output), not
visual understanding. It can plausibly guess "this looks like a PLC
program" from words like "ladder"/"rung" in the OCR text. It cannot look at
a wiring diagram and understand which terminal connects to which — that
needs an actual vision-capable LLM call (Claude or GPT with image input),
which costs real tokens per image and isn't run automatically here. The
interface is built so swapping in a real vision analyzer later is a small,
isolated change — same pattern as the agent's MockLLM -> real LLM swap.

## Maintenance history / "has this happened before" (`tools.record_incident` etc.)

`trace_fault()` now always includes `previous_incidents` — real records from
the `incidents` table (which existed in the schema from day one but wasn't
wired to anything until now). Enforces the exact distinction from the
original vision doc: **historical cause ≠ current cause**. An incident's
`root_cause` is stored as `confirmed=False` (a technician's working theory)
until explicitly promoted via `confirm_incident_root_cause()`. The mock
agent's summary always labels which is which — `[CONFIRMED root cause]` vs
`[UNCONFIRMED (reported, not verified)]` — and prepends a standing warning
that a past cause doesn't prove the current one, exactly matching the
spec's own example dialogue. "Has this happened before at ST40?" is a
first-class agent question — station-gated the same as any fault question,
tested directly.

## What's deliberately NOT here yet

- No auth, no multi-tenancy — not needed until there's a real second user
  or a pilot customer.
- No real LLM wired into the agent — the mock proves the plumbing for free;
  wiring a real Claude/GPT client is a small, well-isolated next step once
  you're ready to spend tokens.
- No automatic structured extraction from uploaded documents into the
  knowledge graph — extraction text is stored and searchable, but turning
  it into `addresses`/`relationships` records is still a human-in-the-loop
  or Phase 2+ step.

## Deploying to Railway (real, click-around version)

1. **Push this folder to a GitHub repo** (Railway deploys from GitHub). Include `requirements.txt`, `Procfile`, `nixpacks.toml`, and the `atlas/` package.
2. **Create a Railway project** → New Project → Deploy from GitHub repo → select the repo.
3. **Add a Postgres database**: in the same project, "+ New" → Database → PostgreSQL. Railway sets a `DATABASE_URL` env var automatically and shares it with your service if they're linked (Railway does this by default within a project).
4. **Run the schema once** against that Postgres instance before first use: open Railway's Postgres "Query" tab (or connect via `psql` using the connection string from the Postgres service's Variables tab) and run the contents of `schema_postgres.sql`.
5. **Seed it**: the app's `atlas/seed.py` now works against Postgres too (it goes through the same `conn.executemany()` wrapper in `db.py`, no raw cursor calls) — run it once with `DATABASE_URL` set, e.g. `DATABASE_URL=<your-url> python3 -m atlas.seed` locally against the Railway Postgres, or add a one-off Railway job that runs it.
6. **Deploy**: Railway builds automatically on push using `nixpacks.toml` (installs the `tesseract-ocr` system binary, required for real OCR — `pytesseract` alone isn't enough) and `requirements.txt`, then starts via `Procfile`.
7. **Visit the Railway-assigned URL** — the root path (`/`) now serves a real frontend (`atlas/static/index.html`) that calls the live API. This is the actual click-around app: a Trace tab, a Log incident tab, and an Upload doc tab, all hitting real endpoints — not a local mock.

### Known deployment wrinkles, stated honestly
- The Postgres connection layer (`db.py`'s `_PGConn`) is written carefully against psycopg2's documented behavior, including `RETURNING id` for the one place that needed `lastrowid`, but **has not been run against a live Postgres instance in this environment** — this sandbox has no network access to test it. Verify `/upload` and `/ask` work end-to-end on your first real deploy; if something breaks, it's most likely in that connection wrapper.
- OCR requires the `tesseract-ocr` system package, not just the `pytesseract` Python wrapper — `nixpacks.toml` handles this, but if you use a different deploy target (Docker, etc.), remember to install it there too.
- File storage (`atlas/ingestion.py`'s `STORAGE_DIR`) currently writes to local disk — fine for a single Railway instance, but Railway's filesystem is ephemeral on redeploy. For anything beyond a demo/pilot, swap this for real object storage (S3-compatible) before customer files depend on it.
