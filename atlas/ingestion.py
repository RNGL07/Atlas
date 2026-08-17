"""
Document ingestion for ATLAS.

Handles uploaded PLC program exports, manuals, and schematic/photo images —
with an intake gate: nothing an upload contributes is usable by the agent
until it's CONFIRMED with a line, station, and document kind. This is what
stops uploads from silently merging into the knowledge base without
knowing what they belong to.

- .txt / .csv / plain PLC exports -> read directly
- .pdf -> real text extraction via pdfplumber (works well for exported/
  text-based PLC program PDFs; a scanned/image-only PDF will extract little
  or nothing — that's a real limitation of any text extractor, not a stub)
- images (.png/.jpg/.jpeg) -> real OCR via pytesseract, PLUS a heuristic
  "what kind of document is this" guess (see analyze.py). Real visual
  UNDERSTANDING of a schematic's topology (not just its text labels)
  requires a vision-capable LLM call — that costs real tokens and isn't
  run automatically here. See analyze.py for exactly where that plugs in.

This module does NOT auto-populate `addresses` / `relationships` from
extracted text — see cross_check() for the deterministic, honest version
of "compare with existing knowledge" (regex-matching known address tokens),
which is different from inventing new relationships from OCR noise.
"""
import json
import re
import shutil
from pathlib import Path

from atlas.db import get_conn

STORAGE_DIR = Path(__file__).parent / "uploaded_docs"
STORAGE_DIR.mkdir(exist_ok=True)

# Matches PLC-style address tokens: D2311, M2320, X1000, Y1001, R3400, P3-R0440 etc.
ADDRESS_TOKEN_RE = re.compile(r"\b(?:P\d-)?[DMXYR]\d{3,4}[A-Z]?\b")

VALID_STATIONS = {"PIS-1", "ST10", "ST20", "ST30", "ST40", "ST45", "VISION-1", "N10", "N20"}
VALID_LINES = {"ORION-1", "NOVA-2"}
VALID_KINDS = {"schematic", "plc_program", "manual", "photo", "layout", "other"}


def _extract_pdf_text(path: Path) -> tuple[str, str]:
    try:
        import pdfplumber
    except ImportError:
        return "", "pending"
    text_parts = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
    text = "\n".join(text_parts).strip()
    if not text:
        return "", "pending"  # likely a scanned/image-only PDF; no text layer found
    return text, "pdf_text"


def _extract_image_text(path: Path) -> tuple[str, str]:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return "", "pending"
    try:
        text = pytesseract.image_to_string(Image.open(path)).strip()
    except Exception:
        return "", "pending"
    return text, "ocr"


def _pending_questions(line_code, station_code, document_kind, flow_of_operation=None) -> list[str]:
    questions = []
    if not line_code:
        questions.append("What line does this belong to?")
    if not station_code and document_kind != "layout":
        questions.append("What station does this belong to?")
    if not document_kind:
        questions.append("What type of document is this — schematic, PLC program, manual, layout, or photo?")
    if document_kind == "layout" and not flow_of_operation:
        questions.append("What is the flow of operation shown in this layout? "
                          "(e.g. 'ST10 -> ST20 -> ST30 -> ST40')")
    return questions


def cross_check(extracted_text: str) -> dict:
    """
    Deterministic 'compare with existing knowledge' step: scan extracted
    text for PLC-address-shaped tokens and check them against the
    addresses table. Real matches vs. unrecognized tokens are reported
    separately — this never invents a relationship, it only flags overlap
    or novelty for a human (or later, a confirmed LLM step) to review.
    """
    tokens = set(ADDRESS_TOKEN_RE.findall(extracted_text.upper()))
    if not tokens:
        return {"known_matches": [], "unrecognized_tokens": []}
    conn = get_conn()
    placeholders = ",".join("?" for _ in tokens)
    rows = conn.execute(
        f"SELECT address, description, station_code FROM addresses WHERE address IN ({placeholders})",
        tuple(tokens),
    ).fetchall()
    known = {r["address"] for r in rows}
    return {
        "known_matches": [dict(r) for r in rows],
        "unrecognized_tokens": sorted(tokens - known),
    }


def ingest_file(source_path: str, original_filename: str, doc_type: str,
                 line_code: str | None = None, station_code: str | None = None,
                 document_kind: str | None = None, description: str | None = None,
                 flow_of_operation: str | None = None) -> dict:
    """
    Copies the file into storage, extracts text, cross-checks it against
    known addresses, and creates a document record. If line/station/kind
    weren't supplied, the document is stored as 'pending_review' with
    explicit clarifying questions — it is NOT usable by the agent until
    confirm_document() is called. This is the gate that stops uploads from
    blurring together.

    document_kind='layout' additionally requires flow_of_operation (a
    string like 'ST10 -> ST20 -> ST30 -> ST40') before it can be confirmed —
    that claim gets checked against known PLC-logic relationships via
    flow_check.check_flow_consistency() rather than trusted blindly.
    """
    dest = STORAGE_DIR / original_filename
    shutil.copy(source_path, dest)

    extracted_text, method = "", "pending"
    ext = dest.suffix.lower()

    if doc_type == "text" or ext in (".txt", ".csv", ".md"):
        extracted_text = dest.read_text(errors="ignore")
        method = "manual"
    elif doc_type == "pdf" or ext == ".pdf":
        extracted_text, method = _extract_pdf_text(dest)
    elif doc_type == "image" or ext in (".png", ".jpg", ".jpeg"):
        extracted_text, method = _extract_image_text(dest)

    check = cross_check(extracted_text)
    from atlas.analyze import analyze
    suggestion = analyze(extracted_text, original_filename)
    questions = _pending_questions(line_code, station_code, document_kind, flow_of_operation)
    status = "pending_review" if questions else "confirmed"

    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO documents
           (filename, stored_path, doc_type, document_kind, line_code, station_code,
            description, extracted_text, extraction_method, status, pending_questions)
           VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
        (original_filename, str(dest), doc_type, document_kind, line_code, station_code,
         description, extracted_text, method, status, json.dumps(questions)),
    )
    doc_id = cur.fetchone()["id"]

    for match in check["known_matches"]:
        conn.execute(
            """INSERT INTO document_address_refs (document_id, address, excerpt) VALUES (?,?,?)
               ON CONFLICT (document_id, address) DO NOTHING""",
            (doc_id, match["address"], _excerpt(extracted_text, match["address"])),
        )
    conn.commit()

    flow_result = None
    if status == "confirmed" and document_kind == "layout" and flow_of_operation:
        flow_result = _store_and_check_flow(doc_id, line_code, flow_of_operation)

    return {
        "id": doc_id,
        "filename": original_filename,
        "doc_type": doc_type,
        "extraction_method": method,
        "extracted_text_preview": extracted_text[:500],
        "extracted_text_length": len(extracted_text),
        "status": status,
        "pending_questions": questions,
        "known_address_matches": [m["address"] for m in check["known_matches"]],
        "unrecognized_tokens": check["unrecognized_tokens"],
        "suggestion": suggestion,
        "flow_consistency_check": flow_result,
    }


def _store_and_check_flow(doc_id: int, line_code: str, flow_of_operation: str) -> dict:
    from atlas.flow_check import parse_flow_string, check_flow_consistency
    sequence = parse_flow_string(flow_of_operation)
    conn = get_conn()
    for i, (a, b) in enumerate(zip(sequence, sequence[1:])):
        conn.execute(
            """INSERT INTO station_flows (line_code, from_station, to_station, step_order, source_document_id)
               VALUES (?,?,?,?,?)""",
            (line_code, a, b, i, doc_id),
        )
    conn.commit()
    return check_flow_consistency(sequence)


def _excerpt(text: str, token: str, context: int = 40) -> str:
    i = text.upper().find(token)
    if i == -1:
        return ""
    start, end = max(0, i - context), min(len(text), i + len(token) + context)
    return text[start:end]


def confirm_document(doc_id: int, line_code: str | None = None,
                      station_code: str | None = None, document_kind: str | None = None,
                      flow_of_operation: str | None = None) -> dict:
    """
    Answer the outstanding clarifying questions for a pending document and
    move it to 'confirmed' — the point at which the agent is allowed to use
    it. Rejects confirmation if required fields are still missing or
    reference an unknown line/station, rather than silently accepting bad
    classification.
    """
    doc = get_document(doc_id)
    if doc is None:
        return {"error": f"No document {doc_id}"}

    line_code = line_code or doc["line_code"]
    station_code = station_code or doc["station_code"]
    document_kind = document_kind or doc["document_kind"]

    errors = []
    if line_code and line_code not in VALID_LINES:
        errors.append(f"Unknown line '{line_code}'")
    if station_code and station_code not in VALID_STATIONS:
        errors.append(f"Unknown station '{station_code}'")
    if document_kind and document_kind not in VALID_KINDS:
        errors.append(f"Unknown document kind '{document_kind}'")

    questions = _pending_questions(line_code, station_code, document_kind, flow_of_operation)
    if questions or errors:
        conn = get_conn()
        conn.execute(
            "UPDATE documents SET line_code=?, station_code=?, document_kind=?, pending_questions=? WHERE id=?",
            (line_code, station_code, document_kind, json.dumps(questions), doc_id),
        )
        conn.commit()
        return {"status": "pending_review", "pending_questions": questions, "errors": errors}

    conn = get_conn()
    conn.execute(
        """UPDATE documents
           SET line_code=?, station_code=?, document_kind=?, status='confirmed',
               pending_questions=NULL, confirmed_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (line_code, station_code, document_kind, doc_id),
    )
    conn.commit()

    flow_result = None
    if document_kind == "layout" and flow_of_operation:
        flow_result = _store_and_check_flow(doc_id, line_code, flow_of_operation)

    return {"status": "confirmed", "id": doc_id, "flow_consistency_check": flow_result}


def get_document(doc_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def list_documents(station_code: str | None = None, status: str | None = None,
                    line_code: str | None = None) -> list[dict]:
    conn = get_conn()
    query = ("SELECT id, filename, doc_type, document_kind, line_code, station_code, "
              "status, extraction_method, uploaded_at FROM documents WHERE 1=1")
    params = []
    if station_code:
        query += " AND station_code = ?"
        params.append(station_code)
    if status:
        query += " AND status = ?"
        params.append(status)
    if line_code:
        query += " AND line_code = ?"
        params.append(line_code)
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def search_documents(keyword: str, confirmed_only: bool = True, line_code: str | None = None) -> list[dict]:
    """Plain-text keyword search over extracted text — the pre-vector-search
    version. Only searches CONFIRMED documents by default, so an
    unclassified upload never surfaces as if it were reliable knowledge.
    Scoped to a line when given, so a search inside one line's section
    never returns another line's documents."""
    conn = get_conn()
    query = ("SELECT id, filename, doc_type, station_code, line_code, status FROM documents "
              "WHERE extracted_text LIKE ?")
    params = [f"%{keyword}%"]
    if confirmed_only:
        query += " AND status = 'confirmed'"
    if line_code:
        query += " AND line_code = ?"
        params.append(line_code)
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_line_section(line_code: str) -> dict:
    """
    Everything filed under one line's 'section' — stations, confirmed
    documents, and known flow-of-operation. This is the 'open Line 5's
    section and see everything for Line 5' access pattern: scoped
    strictly by line_code, so nothing from another line leaks in.
    """
    conn = get_conn()
    line = conn.execute("SELECT * FROM lines WHERE code = ?", (line_code,)).fetchone()
    if not line:
        return {"error": f"Unknown line '{line_code}'"}
    stations = conn.execute("SELECT * FROM stations WHERE line_code = ?", (line_code,)).fetchall()
    docs = list_documents(line_code=line_code, status="confirmed")
    flows = conn.execute(
        "SELECT from_station, to_station, step_order FROM station_flows "
        "WHERE line_code = ? ORDER BY step_order", (line_code,)
    ).fetchall()
    return {
        "line": dict(line),
        "stations": [dict(s) for s in stations],
        "documents": docs,
        "declared_flow": [dict(f) for f in flows],
    }
