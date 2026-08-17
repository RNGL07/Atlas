-- ATLAS ORION-1 Synthetic Line — PostgreSQL schema
-- Deploy target: Railway Postgres

CREATE TABLE lines (
    code        TEXT PRIMARY KEY,      -- e.g. 'ORION-1'
    name        TEXT NOT NULL,
    description TEXT
);

CREATE TABLE stations (
    code        TEXT PRIMARY KEY,       -- e.g. 'ST10', 'PIS-1', 'VISION-1'
    line_code   TEXT REFERENCES lines(code),
    name        TEXT NOT NULL,
    description TEXT
);

CREATE TABLE addresses (
    address      TEXT PRIMARY KEY,       -- e.g. 'D2311', 'M2320', 'X1000', 'Y1000', 'R3400'
    station_code TEXT REFERENCES stations(code),
    addr_type    TEXT NOT NULL CHECK (addr_type IN ('bit','register','input','output','constant')),
    description  TEXT NOT NULL,
    fidelity     TEXT NOT NULL DEFAULT 'SYNTHETIC_CONTEXT'
                 CHECK (fidelity IN ('SYNTHETIC_CONTEXT','SYNTHETIC_TEST_STATE','ATLAS_INFERENCE'))
);

-- Every deterministic logic relationship ATLAS knows about.
-- rel_type: WRITES_TO | COMPARED_WITH | DETERMINES | PAIRED_WITH | TRIGGERS | CAUSES | MOVES_TO
CREATE TABLE relationships (
    id            SERIAL PRIMARY KEY,
    from_address  TEXT NOT NULL REFERENCES addresses(address),
    to_address    TEXT NOT NULL REFERENCES addresses(address),
    rel_type      TEXT NOT NULL,
    condition_expr TEXT,               -- e.g. 'D2302 = 1'
    description   TEXT
);

CREATE TABLE fault_codes (
    code         INTEGER PRIMARY KEY,   -- e.g. 403
    station_code TEXT REFERENCES stations(code),
    name         TEXT NOT NULL,
    description  TEXT
);

CREATE TABLE fault_addresses (
    fault_code INTEGER REFERENCES fault_codes(code),
    address    TEXT REFERENCES addresses(address),
    role       TEXT,                   -- e.g. 'trigger', 'expected', 'actual'
    PRIMARY KEY (fault_code, address)
);

-- Live/simulated register-and-bit state, used to reproduce and test fault scenarios.
CREATE TABLE live_state (
    address TEXT PRIMARY KEY REFERENCES addresses(address),
    value   TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Uploaded source documents (PDFs, schematics, PLC program exports, photos).
-- Nothing here is usable by the agent until status = 'confirmed' — see the
-- intake gate in ingestion.py. This is what stops uploads from silently
-- merging into the knowledge base without knowing which line/station/kind
-- they belong to.
CREATE TABLE documents (
    id            SERIAL PRIMARY KEY,
    filename      TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    doc_type      TEXT NOT NULL CHECK (doc_type IN ('pdf','image','text','plc_export','other')),
    document_kind TEXT CHECK (document_kind IN ('schematic','plc_program','manual','photo','layout','other')),
    line_code     TEXT REFERENCES lines(code),
    station_code  TEXT REFERENCES stations(code),
    description   TEXT,
    extracted_text TEXT,               -- OCR/text-extraction output, used for semantic search
    extraction_method TEXT,            -- 'pdf_text' | 'ocr' | 'manual' | 'pending'
    status        TEXT NOT NULL DEFAULT 'pending_review'
                  CHECK (status IN ('pending_review','confirmed','rejected')),
    pending_questions TEXT,            -- JSON list of clarifying questions still open
    fidelity      TEXT NOT NULL DEFAULT 'SOURCE_DERIVED'
                  CHECK (fidelity IN ('SOURCE_DERIVED','SYNTHETIC_CONTEXT','ATLAS_INFERENCE')),
    uploaded_at   TIMESTAMPTZ DEFAULT now(),
    confirmed_at  TIMESTAMPTZ
);

-- Links an extracted address/relationship claim back to the document it came from.
CREATE TABLE document_address_refs (
    document_id INTEGER REFERENCES documents(id),
    address     TEXT REFERENCES addresses(address),
    excerpt     TEXT,                  -- short surrounding text, for citation display
    PRIMARY KEY (document_id, address)
);

-- Declared flow-of-operation from a confirmed layout document (e.g. a
-- diagram saying "ST10 -> ST20 -> ST30 -> ST40"). This is a HUMAN/DOCUMENT
-- CLAIM about the process, kept separate from `relationships` (which is
-- PLC-logic-derived) so ATLAS can compare one against the other rather
-- than conflating "what the layout says" with "what the logic proves".
CREATE TABLE station_flows (
    id            SERIAL PRIMARY KEY,
    line_code     TEXT REFERENCES lines(code),
    from_station  TEXT REFERENCES stations(code),
    to_station    TEXT REFERENCES stations(code),
    step_order    INTEGER,
    source_document_id INTEGER REFERENCES documents(id),
    description   TEXT
);

-- Maintenance/troubleshooting incident log (for future "has this happened before" queries).
CREATE TABLE incidents (
    id           SERIAL PRIMARY KEY,
    fault_code   INTEGER REFERENCES fault_codes(code),
    occurred_at  TIMESTAMPTZ DEFAULT now(),
    symptom      TEXT,
    root_cause   TEXT,
    confirmed    BOOLEAN DEFAULT FALSE,
    reported_by  TEXT
);
