import unittest
from pathlib import Path
from atlas.db import init_db
from atlas import seed as seed_mod
from atlas import tools, agent, ingestion, flow_check


class TestAgent(unittest.TestCase):
    def setUp(self):
        conn = init_db(wipe=True)
        seed_mod.seed(conn)
        tools.set_live_value("D2310", "1")
        tools.set_live_value("D2311", "1057")
        tools.set_live_value("R3400", "1056")
        tools.set_live_value("M2405", "1")
        tools.set_live_value("D2850", "403")

    def test_agent_traces_fault_by_code(self):
        result = agent.run_agent("Why is fault 403 happening at ST40?")
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].tool, "trace_fault")
        self.assertIn("D2311", str(result.steps[0].result))
        self.assertIn("Evidence gathered", result.answer)

    def test_agent_asks_for_station_when_missing(self):
        result = agent.run_agent("Why is fault 403 happening?")
        self.assertTrue(result.needs_clarification)
        self.assertEqual(result.steps, [])
        self.assertIn("station", result.answer.lower())

    def test_agent_flags_station_mismatch_instead_of_guessing(self):
        result = agent.run_agent("Why is fault 403 happening at ST45?")
        self.assertTrue(result.needs_clarification)
        self.assertIn("ST40", result.answer)

    def test_agent_surfaces_prior_incidents_with_confirmation_status(self):
        tools.record_incident(403, symptom="ST40 routing record won't clear",
                               root_cause="Loose sensor bracket", confirmed=True)
        result = agent.run_agent("Why is fault 403 happening at ST40?")
        self.assertIn("CONFIRMED root cause", result.answer)
        self.assertIn("Loose sensor bracket", result.answer)
        self.assertIn("historical cause is NOT the same as current cause", result.answer)

    def test_agent_direct_incident_history_question(self):
        tools.record_incident(403, symptom="ST40 routing record won't clear",
                               root_cause="Loose sensor bracket", confirmed=True)
        result = agent.run_agent("Has fault 403 happened before at ST40?")
        tool_names = [s.tool for s in result.steps]
        self.assertIn("get_previous_incidents", tool_names)

    def test_agent_traces_by_address(self):
        result = agent.run_agent("What writes to D2311?")
        tool_names = [s.tool for s in result.steps]
        self.assertIn("trace_address", tool_names)

    def test_agent_priority_queue_question(self):
        result = agent.run_agent("What's the current routing priority queue?")
        tool_names = [s.tool for s in result.steps]
        self.assertIn("get_priority_state", tool_names)

    def test_agent_no_hallucinated_answer_on_empty_question(self):
        result = agent.run_agent("hello")
        self.assertEqual(result.steps, [])
        self.assertIn("don't have enough information", result.answer)


class TestIngestion(unittest.TestCase):
    def setUp(self):
        conn = init_db(wipe=True)
        seed_mod.seed(conn)

    def test_ingest_text_file_fully_classified_is_confirmed(self):
        doc = ingestion.ingest_file("/tmp/test_notes.txt", "test_notes.txt", "text",
                                     line_code="ORION-1", station_code="ST30",
                                     document_kind="manual", description="Test notes")
        self.assertEqual(doc["extraction_method"], "manual")
        self.assertIn("M2320", doc["extracted_text_preview"])
        self.assertEqual(doc["status"], "confirmed")
        self.assertEqual(doc["pending_questions"], [])

    def test_ingest_without_classification_is_gated_pending(self):
        """This is the core ask: dumping a file with no line/station/kind must
        NOT silently become usable knowledge — it has to sit pending with
        explicit questions until confirmed."""
        doc = ingestion.ingest_file("/tmp/test_manual.pdf", "test_manual.pdf", "pdf")
        self.assertEqual(doc["status"], "pending_review")
        self.assertIn("What line does this belong to?", doc["pending_questions"])
        self.assertIn("What station does this belong to?", doc["pending_questions"])
        self.assertIn("What type of document is this — schematic, PLC program, manual, layout, or photo?",
                       doc["pending_questions"])
        # Unclassified doc must not show up in confirmed-only search
        results = ingestion.search_documents("Sequence Mismatch")
        self.assertEqual(results, [])

    def test_confirm_document_completes_intake(self):
        doc = ingestion.ingest_file("/tmp/test_manual.pdf", "test_manual.pdf", "pdf")
        self.assertEqual(doc["status"], "pending_review")
        result = ingestion.confirm_document(doc["id"], line_code="ORION-1",
                                             station_code="ST40", document_kind="manual")
        self.assertEqual(result["status"], "confirmed")
        results = ingestion.search_documents("Sequence Mismatch")
        self.assertTrue(any(r["id"] == doc["id"] for r in results))

    def test_confirm_document_rejects_unknown_station(self):
        doc = ingestion.ingest_file("/tmp/test_notes.txt", "test_notes.txt", "text")
        result = ingestion.confirm_document(doc["id"], line_code="ORION-1",
                                             station_code="ST99", document_kind="manual")
        self.assertIn("Unknown station 'ST99'", result["errors"])

    def test_ingest_real_pdf(self):
        doc = ingestion.ingest_file("/tmp/test_manual.pdf", "test_manual.pdf", "pdf",
                                     line_code="ORION-1", station_code="ST40",
                                     document_kind="manual", description="ST40 manual excerpt")
        self.assertEqual(doc["extraction_method"], "pdf_text")
        self.assertIn("Fault 403", doc["extracted_text_preview"])
        self.assertIn("R3400", doc["extracted_text_preview"])
        # Cross-check against known knowledge base should catch R3400
        self.assertIn("R3400", doc["known_address_matches"])

    def test_ingest_real_image_ocr(self):
        doc = ingestion.ingest_file("/tmp/test_schematic.png", "test_schematic.png", "image",
                                     line_code="ORION-1", station_code="ST40",
                                     document_kind="photo", description="Fault label photo")
        self.assertEqual(doc["extraction_method"], "ocr")
        self.assertIn("F403", doc["extracted_text_preview"].upper())

    def test_cross_check_flags_unrecognized_token(self):
        doc = ingestion.ingest_file("/tmp/test_manual.pdf", "test_manual.pdf", "pdf",
                                     line_code="ORION-1", station_code="ST40", document_kind="manual")
        # R3400 and D2311 are real known addresses; nothing fabricated should
        # be reported as "known" — only genuine matches.
        self.assertIn("R3400", doc["known_address_matches"])
        self.assertNotIn("D9999", doc["known_address_matches"])

    def test_search_documents_by_keyword(self):
        ingestion.ingest_file("/tmp/test_manual.pdf", "test_manual.pdf", "pdf",
                               line_code="ORION-1", station_code="ST40", document_kind="manual")
        results = ingestion.search_documents("Sequence Mismatch")
        self.assertTrue(any(r["filename"] == "test_manual.pdf" for r in results))

    def test_list_documents_by_station(self):
        ingestion.ingest_file("/tmp/test_notes.txt", "test_notes.txt", "text",
                               line_code="ORION-1", station_code="ST30", document_kind="manual")
        docs = ingestion.list_documents(station_code="ST30")
        self.assertEqual(len(docs), 1)

    def test_vision_suggestion_is_advisory_not_auto_applied(self):
        """Uploading a PLC-flavored text file should get a suggested kind,
        but it must stay pending until a human confirms it — the suggestion
        alone must not classify the document."""
        doc = ingestion.ingest_file("/tmp/test_manual.pdf", "test_manual.pdf", "pdf")
        self.assertIn("suggestion", doc)
        self.assertEqual(doc["status"], "pending_review")


class TestFlowConsistency(unittest.TestCase):
    def setUp(self):
        conn = init_db(wipe=True)
        seed_mod.seed(conn)

    def test_layout_upload_without_flow_is_gated(self):
        doc = ingestion.ingest_file("/tmp/test_notes.txt", "layout.txt", "text",
                                     line_code="ORION-1", document_kind="layout")
        self.assertEqual(doc["status"], "pending_review")
        self.assertTrue(any("flow of operation" in q for q in doc["pending_questions"]))

    def test_correct_flow_is_confirmed_where_data_backs_it(self):
        """
        ST10->ST20 and ST20->ST30 are real, data-backed handshakes (R3100/
        R3200 WRITES_TO relationships). ST30->ST40 is NOT backed by an
        explicit data relationship in this dataset — ST30 only sets a
        routing *decision* bit (M2306/M2307) and routes physically via the
        diverter; there's no explicit product-data write to ST40. This is
        exactly the kind of gap the checker should surface, not paper over.
        """
        doc = ingestion.ingest_file(
            "/tmp/test_notes.txt", "layout.txt", "text",
            line_code="ORION-1", document_kind="layout",
            flow_of_operation="ST10 -> ST20 -> ST30 -> ST40",
        )
        self.assertEqual(doc["status"], "confirmed")
        check = doc["flow_consistency_check"]
        self.assertEqual(check["summary"], "2/3 hops confirmed by known PLC-logic relationships")
        statuses = {(h["from"], h["to"]): h["status"] for h in check["hops"]}
        self.assertEqual(statuses[("ST10", "ST20")], "CONFIRMED")
        self.assertEqual(statuses[("ST20", "ST30")], "CONFIRMED")
        self.assertEqual(statuses[("ST30", "ST40")], "UNCONFIRMED_BY_LOGIC")

    def test_bogus_flow_is_flagged_not_trusted(self):
        """ST10 -> ST45 skips the real path (through ST20/ST30) — there is
        no direct data relationship, so this must be flagged, not accepted."""
        result = flow_check.check_flow_consistency(["ST10", "ST45"])
        self.assertEqual(result["hops"][0]["status"], "UNCONFIRMED_BY_LOGIC")
        self.assertIn("0/1", result["summary"])

    def test_flow_from_layout_is_persisted(self):
        doc = ingestion.ingest_file(
            "/tmp/test_notes.txt", "layout.txt", "text",
            line_code="ORION-1", document_kind="layout",
            flow_of_operation="ST10 -> ST20",
        )
        section = ingestion.get_line_section("ORION-1")
        pairs = {(f["from_station"], f["to_station"]) for f in section["declared_flow"]}
        self.assertIn(("ST10", "ST20"), pairs)


class TestLineSections(unittest.TestCase):
    def setUp(self):
        conn = init_db(wipe=True)
        seed_mod.seed(conn)
        ingestion.ingest_file("/tmp/test_manual.pdf", "orion_manual.pdf", "pdf",
                               line_code="ORION-1", station_code="ST40", document_kind="manual")
        ingestion.ingest_file("/tmp/test_notes.txt", "nova_notes.txt", "text",
                               line_code="NOVA-2", station_code="N10", document_kind="manual")

    def test_line_section_only_returns_its_own_documents(self):
        orion = ingestion.get_line_section("ORION-1")
        nova = ingestion.get_line_section("NOVA-2")
        orion_files = {d["filename"] for d in orion["documents"]}
        nova_files = {d["filename"] for d in nova["documents"]}
        self.assertIn("orion_manual.pdf", orion_files)
        self.assertNotIn("nova_notes.txt", orion_files)
        self.assertIn("nova_notes.txt", nova_files)
        self.assertNotIn("orion_manual.pdf", nova_files)

    def test_search_scoped_to_line_does_not_leak(self):
        results = ingestion.search_documents("Sequence Mismatch", line_code="NOVA-2")
        self.assertEqual(results, [])  # that phrase only exists in the ORION-1 manual
        results2 = ingestion.search_documents("Sequence Mismatch", line_code="ORION-1")
        self.assertTrue(any(r["filename"] == "orion_manual.pdf" for r in results2))

    def test_unknown_line_section_errors_cleanly(self):
        result = ingestion.get_line_section("DOES-NOT-EXIST")
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
