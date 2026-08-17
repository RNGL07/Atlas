"""
Deterministic ATLAS tools.

These are plain database queries — no LLM involved. The agent orchestrator
(agent.py) calls these and hands the LLM only what they return. This is what
keeps ATLAS from inventing a rung, address, or relationship that doesn't
exist in the data.
"""
from atlas.db import get_conn


def get_address(address: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM addresses WHERE address = ?", (address,)
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["current_value"] = get_live_value(address)
    return result


def get_live_value(address: str) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT value FROM live_state WHERE address = ?", (address,)
    ).fetchone()
    return row["value"] if row else None


def find_writers(address: str) -> list[dict]:
    """What writes TO / sets this address (WRITES_TO, MOVES_TO, CAUSES, TRIGGERS targeting it)."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT from_address, rel_type, condition_expr, description
           FROM relationships
           WHERE to_address = ? AND rel_type IN ('WRITES_TO','MOVES_TO','CAUSES','TRIGGERS')""",
        (address,),
    ).fetchall()
    return [dict(r) for r in rows]


def find_readers(address: str) -> list[dict]:
    """What this address writes/moves/triggers/determines/compares against."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT to_address, rel_type, condition_expr, description
           FROM relationships
           WHERE from_address = ?""",
        (address,),
    ).fetchall()
    return [dict(r) for r in rows]


def cross_reference(address: str) -> dict:
    """All relationships touching this address, either direction."""
    return {
        "address": address,
        "writers": find_writers(address),
        "outbound": find_readers(address),
    }


def trace_address(address: str, _visited: set | None = None, _depth: int = 0) -> list[dict]:
    """
    Follow the chain of relationships forward from this address
    (WRITES_TO / MOVES_TO / PAIRED_WITH / CAUSES), depth-limited to avoid
    cycles, returning an ordered path with descriptions.
    """
    if _visited is None:
        _visited = set()
    if address in _visited or _depth > 6:
        return []
    _visited.add(address)

    conn = get_conn()
    rows = conn.execute(
        """SELECT to_address, rel_type, condition_expr, description
           FROM relationships
           WHERE from_address = ? AND rel_type IN
                 ('WRITES_TO','MOVES_TO','PAIRED_WITH','CAUSES','TRIGGERS')""",
        (address,),
    ).fetchall()

    path = []
    for r in rows:
        step = dict(r)
        step["from"] = address
        path.append(step)
        path.extend(trace_address(step["to_address"], _visited, _depth + 1))
    return path


def trace_internal_bit(bit: str) -> dict:
    """For a bit like M2320: what causes it, and what it triggers downstream."""
    addr = get_address(bit)
    return {
        "address": bit,
        "info": addr,
        "inputs": find_writers(bit),   # what sets this bit
        "downstream": find_readers(bit),  # what this bit triggers/causes
    }


def get_priority_state() -> dict:
    """Current 3-slot ST30 routing queue state, read from live_state."""
    slots = {}
    for i, (dest, seq) in enumerate(
        [("D2310", "D2311"), ("D2312", "D2313"), ("D2314", "D2315")], start=1
    ):
        dest_val = get_live_value(dest)
        seq_val = get_live_value(seq)
        dest_name = {"0": "Empty", "1": "ST40", "2": "ST45", None: "Empty"}.get(dest_val, dest_val)
        slots[f"slot_{i}"] = {"destination_addr": dest, "destination": dest_name,
                               "sequence_addr": seq, "sequence": seq_val}
    return slots


def get_fault(code: int) -> dict | None:
    conn = get_conn()
    fault = conn.execute("SELECT * FROM fault_codes WHERE code = ?", (code,)).fetchone()
    if not fault:
        return None
    addrs = conn.execute(
        """SELECT fa.address, fa.role, a.description, a.addr_type
           FROM fault_addresses fa JOIN addresses a ON a.address = fa.address
           WHERE fa.fault_code = ?""",
        (code,),
    ).fetchall()
    result = dict(fault)
    result["related_addresses"] = [dict(a) for a in addrs]
    for a in result["related_addresses"]:
        a["current_value"] = get_live_value(a["address"])
    return result


def trace_fault(code: int) -> dict:
    """Full deterministic trace for a fault: definition + related addresses + their current values + trace path + prior incidents."""
    fault = get_fault(code)
    if not fault:
        return {"error": f"No fault code {code} in dataset"}
    fault["traces"] = {
        a["address"]: trace_address(a["address"])
        for a in fault["related_addresses"]
        if a["role"] in ("trigger", "expected", "actual")
    }
    fault["previous_incidents"] = get_previous_incidents(code)
    return fault


def record_incident(fault_code: int, symptom: str, root_cause: str | None = None,
                     confirmed: bool = False, reported_by: str | None = None) -> dict:
    """
    Log a troubleshooting incident. root_cause can be recorded unconfirmed
    (a technician's working theory) or confirmed (verified fix). This
    distinction matters: get_previous_incidents() always shows both, and
    the agent must present a confirmed prior cause as 'similar past
    incident, not proof of the current cause' — historical cause is not
    the same as current cause.
    """
    conn = get_conn()
    conn.execute(
        """INSERT INTO incidents (fault_code, symptom, root_cause, confirmed, reported_by)
           VALUES (?,?,?,?,?)""",
        (fault_code, symptom, root_cause, int(confirmed), reported_by),
    )
    conn.commit()
    return {"fault_code": fault_code, "symptom": symptom, "root_cause": root_cause,
            "confirmed": confirmed, "reported_by": reported_by}


def confirm_incident_root_cause(incident_id: int, root_cause: str) -> dict:
    """Promote a prior incident's root cause from unconfirmed to confirmed."""
    conn = get_conn()
    conn.execute(
        "UPDATE incidents SET root_cause = ?, confirmed = 1 WHERE id = ?",
        (root_cause, incident_id),
    )
    conn.commit()
    return {"id": incident_id, "root_cause": root_cause, "confirmed": True}


def get_previous_incidents(fault_code: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, fault_code, occurred_at, symptom, root_cause, confirmed, reported_by
           FROM incidents WHERE fault_code = ? ORDER BY occurred_at DESC""",
        (fault_code,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_live_value(address: str, value: str):
    """Used by tests/scenarios to inject a fault state."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO live_state (address, value) VALUES (?,?) "
        "ON CONFLICT(address) DO UPDATE SET value = excluded.value",
        (address, value),
    )
    conn.commit()
