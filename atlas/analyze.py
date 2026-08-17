"""
Document "understanding" — classifying what an uploaded image/PDF actually
is, and suggesting where it belongs.

Honest split, same pattern as agent.py's MockLLM:

- MockVisionAnalyzer: free, deterministic, keyword/heuristic-based. It can
  guess document_kind from filename and OCR'd text patterns (e.g. text
  containing "ladder", "rung", "M2" address patterns -> likely plc_program;
  "schematic", "wiring", "P&ID" -> likely schematic). It CANNOT look at an
  image and understand circuit topology, wiring, or diagram structure — no
  local/free tool does that. This is a real capability gap, not a detail
  glossed over.

- A real vision-capable LLM call (Claude or GPT-4V with image input) is
  where actual visual understanding plugs in — same interface, swap it in
  when you're ready to spend tokens and have network/API access. That
  costs real tokens per image and isn't run automatically here.

Either way, the output is a SUGGESTION, not an auto-confirm — it still goes
through ingestion.py's intake gate (pending_review -> confirm_document), so
a wrong guess can never silently misfile a document into the wrong
station/line.
"""
import re

PLC_HINTS = re.compile(r"\b(ladder|rung|plc|fun\d{3}|wbmov|wmove)\b", re.I)
SCHEMATIC_HINTS = re.compile(r"\b(schematic|wiring|p&id|circuit|terminal|connector)\b", re.I)
MANUAL_HINTS = re.compile(r"\b(manual|procedure|instruction|troubleshooting guide)\b", re.I)

STATION_TOKEN_RE = re.compile(r"\bST\d{2}\b", re.I)


class MockVisionAnalyzer:
    """Heuristic, free, no image understanding — text-pattern guessing only."""

    def suggest_document_kind(self, extracted_text: str, filename: str) -> tuple[str | None, float]:
        text = f"{filename} {extracted_text}"
        if PLC_HINTS.search(text):
            return "plc_program", 0.6
        if SCHEMATIC_HINTS.search(text):
            return "schematic", 0.6
        if MANUAL_HINTS.search(text):
            return "manual", 0.5
        return None, 0.0

    def suggest_station(self, extracted_text: str, filename: str) -> tuple[str | None, float]:
        match = STATION_TOKEN_RE.search(f"{filename} {extracted_text}")
        if match:
            return match.group(0).upper(), 0.7
        return None, 0.0

    def describe(self, extracted_text: str, filename: str) -> str:
        return ("(Mock analysis — text-pattern based, not real visual "
                "understanding. A real vision-LLM call is needed to describe "
                "diagram content beyond the text it contains.)")


def analyze(extracted_text: str, filename: str, analyzer=None) -> dict:
    analyzer = analyzer or MockVisionAnalyzer()
    kind, kind_conf = analyzer.suggest_document_kind(extracted_text, filename)
    station, station_conf = analyzer.suggest_station(extracted_text, filename)
    return {
        "suggested_document_kind": kind,
        "document_kind_confidence": kind_conf,
        "suggested_station": station,
        "station_confidence": station_conf,
        "description": analyzer.describe(extracted_text, filename),
        "note": "Suggestions only — must be confirmed via confirm_document(), never auto-applied.",
    }
