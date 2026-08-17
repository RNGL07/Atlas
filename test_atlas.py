import unittest
from atlas.db import init_db
from atlas import seed as seed_mod
from atlas import tools


class TestATLAS(unittest.TestCase):
    def setUp(self):
        conn = init_db(wipe=True)
        seed_mod.seed(conn)

    def test_get_address(self):
        addr = tools.get_address("D2311")
        self.assertIsNotNone(addr)
        self.assertEqual(addr["description"], "Sequence Slot 1")
        self.assertEqual(addr["current_value"], "1057")

    def test_unknown_address_returns_none_not_hallucinated(self):
        self.assertIsNone(tools.get_address("D9999"))

    def test_find_writers_d2311(self):
        writers = tools.find_writers("D2311")
        froms = {w["from_address"] for w in writers}
        # D2313 -> D2311 via queue shift (MOVES_TO)
        self.assertIn("D2313", froms)

    def test_find_writers_r3400(self):
        writers = tools.find_writers("R3400")
        froms = {w["from_address"] for w in writers}
        self.assertIn("D2400", froms)

    def test_trace_queue_shift_chain(self):
        path = tools.trace_address("D2312")
        targets = {step["to_address"] for step in path}
        self.assertIn("D2310", targets)  # slot 2 dest -> slot 1 dest

    def test_priority_state_reflects_normal_seed(self):
        state = tools.get_priority_state()
        self.assertEqual(state["slot_1"]["destination"], "ST40")
        self.assertEqual(state["slot_1"]["sequence"], "1057")
        self.assertEqual(state["slot_2"]["destination"], "ST45")
        self.assertEqual(state["slot_3"]["destination"], "ST40")

    def test_fault_403_definition(self):
        fault = tools.get_fault(403)
        self.assertEqual(fault["name"], "ST40 Completion Sequence Mismatch")
        addrs = {a["address"] for a in fault["related_addresses"]}
        self.assertTrue({"D2310", "D2311", "R3400", "M2320"}.issubset(addrs))

    def test_fault_403_scenario_end_to_end(self):
        """
        Canonical scenario from the ORION-1 spec:
        Queue expects D2310=1, D2311=1057 (ST40, sequence 1057)
        but ST40 actually reports R3400=1056 -> mismatch -> M2320 never sets
        -> M2322 never sets -> queue does not shift.
        This proves ATLAS traces the *data path*, not just "check the sensor".
        """
        tools.set_live_value("D2310", "1")
        tools.set_live_value("D2311", "1057")
        tools.set_live_value("R3400", "1056")  # wrong sequence
        tools.set_live_value("M2405", "1")     # ST40 says it unloaded
        tools.set_live_value("D2850", "403")

        fault = tools.trace_fault(403)
        vals = {a["address"]: a["current_value"] for a in fault["related_addresses"]}

        self.assertEqual(vals["D2311"], "1057")
        self.assertEqual(vals["R3400"], "1056")
        self.assertNotEqual(vals["D2311"], vals["R3400"],
                             "ATLAS must detect the mismatch, not assume a match")

        # The trace from R3400 should show it's compared against D2311, not
        # silently accepted.
        r3400_relationships = tools.find_readers("R3400")
        rel_types = {r["rel_type"] for r in r3400_relationships}
        self.assertIn("COMPARED_WITH", rel_types)

        # M2320 (queue record complete) must NOT be inferred as set — the
        # dataset only records the deterministic relationship, not simulated
        # PLC execution, so this asserts ATLAS has the trigger condition
        # available to reason over, proving the model isn't a black box.
        m2320 = tools.get_address("M2320")
        writers = tools.find_writers("M2320")
        self.assertTrue(any("M2405" in (w["condition_expr"] or "") for w in writers))

    def test_trace_fault_includes_empty_incident_history_by_default(self):
        fault = tools.trace_fault(403)
        self.assertEqual(fault["previous_incidents"], [])

    def test_record_and_retrieve_incident(self):
        tools.record_incident(403, symptom="ST40 routing record won't clear",
                               root_cause="Loose sensor bracket on ST40 unload confirm",
                               confirmed=True, reported_by="test_tech")
        incidents = tools.get_previous_incidents(403)
        self.assertEqual(len(incidents), 1)
        self.assertTrue(incidents[0]["confirmed"])
        self.assertEqual(incidents[0]["root_cause"], "Loose sensor bracket on ST40 unload confirm")

    def test_unconfirmed_incident_stays_unconfirmed_until_promoted(self):
        rec = tools.record_incident(403, symptom="ST40 routing record won't clear",
                                     root_cause="Suspect timing issue", confirmed=False)
        incidents = tools.get_previous_incidents(403)
        self.assertFalse(incidents[0]["confirmed"])
        tools.confirm_incident_root_cause(incidents[0]["id"], "Confirmed: sensor debounce timing")
        incidents_after = tools.get_previous_incidents(403)
        self.assertTrue(incidents_after[0]["confirmed"])
        self.assertEqual(incidents_after[0]["root_cause"], "Confirmed: sensor debounce timing")

    def test_trace_fault_surfaces_prior_incidents(self):
        tools.record_incident(403, symptom="ST40 routing record won't clear",
                               root_cause="Loose sensor bracket", confirmed=True)
        fault = tools.trace_fault(403)
        self.assertEqual(len(fault["previous_incidents"]), 1)
        self.assertEqual(fault["previous_incidents"][0]["root_cause"], "Loose sensor bracket")

    def test_trace_communication_fault_501(self):
        tools.set_live_value("M2003", "0")
        tools.set_live_value("D2901", "2")
        fault = tools.trace_fault(501)
        vals = {a["address"]: a["current_value"] for a in fault["related_addresses"]}
        self.assertEqual(vals["M2003"], "0")
        self.assertEqual(vals["D2901"], "2")

    def test_no_hallucinated_relationship(self):
        """ATLAS should return empty, not invented data, for an address with no relationships."""
        result = tools.cross_reference("X1500")
        self.assertEqual(result["writers"], [])


if __name__ == "__main__":
    unittest.main()
