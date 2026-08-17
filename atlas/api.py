"""
ATLAS API — thin FastAPI layer over the deterministic tools in tools.py.

Run locally:    uvicorn atlas.api:app --reload
Deploy target:  Railway (see README.md)

Note: this repo's local dev/test DB is SQLite (db.py). For Railway, point
DB_PATH / connection logic at Postgres using schema_postgres.sql — the SQL
in tools.py is simple enough to run unchanged against Postgres; only the
connection layer in db.py needs swapping for psycopg/SQLAlchemy against
DATABASE_URL.
"""
import shutil
import tempfile
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from atlas import tools, agent, ingestion

app = FastAPI(title="ATLAS — ORION-1", version="0.1")

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
def root():
    """Serves the real frontend — clicking around this hits the live API below."""
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/address/{address}")
def address(address: str):
    result = tools.get_address(address)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No address {address} in dataset")
    return result


@app.get("/cross-reference/{address}")
def cross_reference(address: str):
    return tools.cross_reference(address)


@app.get("/trace/{address}")
def trace(address: str):
    path = tools.trace_address(address)
    return {"address": address, "path": path}


@app.get("/trace-bit/{bit}")
def trace_bit(bit: str):
    return tools.trace_internal_bit(bit)


@app.get("/priority-state")
def priority_state():
    return tools.get_priority_state()


@app.get("/fault/{code}")
def fault(code: int):
    result = tools.trace_fault(code)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/fault/{code}/incidents")
def fault_incidents(code: int):
    return tools.get_previous_incidents(code)


@app.post("/incidents")
def create_incident(fault_code: int = Form(...), symptom: str = Form(...),
                     root_cause: str | None = Form(None), confirmed: bool = Form(False),
                     reported_by: str | None = Form(None)):
    return tools.record_incident(fault_code, symptom, root_cause, confirmed, reported_by)


@app.post("/incidents/{incident_id}/confirm")
def confirm_incident(incident_id: int, root_cause: str = Form(...)):
    return tools.confirm_incident_root_cause(incident_id, root_cause)


@app.post("/simulate/{address}/{value}")
def simulate(address: str, value: str):
    """Inject a live-state value to reproduce a fault scenario for testing."""
    tools.set_live_value(address, value)
    return {"address": address, "value": value}


@app.post("/ask")
def ask(question: str = Form(...)):
    """
    Natural-language question -> agent orchestrator -> deterministic tool
    calls -> answer. Uses the free MockLLM by default. Swap `llm=` for a
    real Claude/GPT client to spend tokens and get real reasoning quality.
    """
    result = agent.run_agent(question)
    return {
        "question": result.question,
        "steps": [{"tool": s.tool, "args": s.args, "result": s.result} for s in result.steps],
        "answer": result.answer,
    }


@app.post("/upload")
def upload(file: UploadFile = File(...), doc_type: str = Form(...),
           line_code: str | None = Form(None), station_code: str | None = Form(None),
           document_kind: str | None = Form(None), description: str | None = Form(None),
           flow_of_operation: str | None = Form(None)):
    """
    Upload a PLC program export, manual PDF, schematic, layout, or photo.
    doc_type: 'pdf' | 'image' | 'text' | 'plc_export' | 'other'
    document_kind='layout' requires flow_of_operation (e.g. 'ST10 -> ST20 -> ST30')
    and gets checked against known relationships in the same call.
    If required fields aren't given, the document is stored as
    pending_review with explicit clarifying questions — not usable by the
    agent until POST /documents/{id}/confirm.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    doc = ingestion.ingest_file(tmp_path, file.filename, doc_type,
                                 line_code, station_code, document_kind, description,
                                 flow_of_operation)
    return doc


@app.post("/documents/{doc_id}/confirm")
def confirm_document(doc_id: int, line_code: str | None = Form(None),
                      station_code: str | None = Form(None), document_kind: str | None = Form(None),
                      flow_of_operation: str | None = Form(None)):
    return ingestion.confirm_document(doc_id, line_code, station_code, document_kind, flow_of_operation)


@app.get("/lines/{line_code}")
def line_section(line_code: str):
    """Everything filed under one line's section — the 'open Line 5' access pattern."""
    result = ingestion.get_line_section(line_code)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/lines/{line_code}/flow-check")
def flow_check_endpoint(line_code: str, sequence: str):
    """sequence, e.g. 'ST10,ST20,ST30,ST40' — check it against known relationships."""
    from atlas.flow_check import parse_flow_string, check_flow_consistency
    return check_flow_consistency(parse_flow_string(sequence))


@app.get("/documents")
def documents(station_code: str | None = None, status: str | None = None, line_code: str | None = None):
    return ingestion.list_documents(station_code, status, line_code)


@app.get("/documents/search")
def documents_search(keyword: str):
    return ingestion.search_documents(keyword)
