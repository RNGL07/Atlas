"""
Flow-of-operation consistency checking.

When someone uploads a line layout/diagram and states the flow of
operation (e.g. "ST10 -> ST20 -> ST30 -> ST40"), this compares that CLAIM
against what the deterministic relationship data actually shows:

1. Is there a known data-write relationship from a station's address(es)
   into the next station's address(es)? (evidence of a real handshake)
2. Do both neighboring stations have a communication-status bit at all?
   (evidence the system even models a link between them)

This never asserts the layout is "correct" — it reports what's backed by
data (SOURCE_DERIVED relationships) versus what's asserted only by the
layout document itself (unconfirmed), so a human can reconcile them. This
mirrors the evidence-level discipline from the ATLAS spec: a diagram saying
two stations talk to each other is not proof they do.
"""
from atlas.db import get_conn


def _station_addresses(station_code: str) -> set[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT address FROM addresses WHERE station_code = ?", (station_code,)
    ).fetchall()
    return {r["address"] for r in rows}


def _has_comm_bit(station_code: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM addresses WHERE station_code = ? AND description LIKE '%Communication OK%' LIMIT 1",
        (station_code,),
    ).fetchone()
    return row is not None


def _writes_between(from_station: str, to_station: str) -> list[dict]:
    from_addrs = _station_addresses(from_station)
    to_addrs = _station_addresses(to_station)
    if not from_addrs or not to_addrs:
        return []
    conn = get_conn()
    from_ph = ",".join("?" for _ in from_addrs)
    to_ph = ",".join("?" for _ in to_addrs)
    rows = conn.execute(
        f"""SELECT from_address, to_address, rel_type, description FROM relationships
            WHERE from_address IN ({from_ph}) AND to_address IN ({to_ph})""",
        (*from_addrs, *to_addrs),
    ).fetchall()
    return [dict(r) for r in rows]


def check_flow_consistency(station_sequence: list[str]) -> dict:
    """
    station_sequence: ordered list like ['ST10','ST20','ST30','ST40']
    Returns per-hop evidence: whether a data relationship and communication
    bits back up the claimed flow, or whether it's unconfirmed by the
    knowledge graph.
    """
    results = []
    for a, b in zip(station_sequence, station_sequence[1:]):
        writes = _writes_between(a, b)
        comm_a, comm_b = _has_comm_bit(a), _has_comm_bit(b)
        results.append({
            "from": a,
            "to": b,
            "data_relationship_found": bool(writes),
            "relationship_evidence": writes,
            "from_has_communication_bit": comm_a,
            "to_has_communication_bit": comm_b,
            "status": "CONFIRMED" if writes else "UNCONFIRMED_BY_LOGIC",
        })
    confirmed = sum(1 for r in results if r["status"] == "CONFIRMED")
    return {
        "sequence": station_sequence,
        "hops": results,
        "summary": f"{confirmed}/{len(results)} hops confirmed by known PLC-logic relationships",
    }


def parse_flow_string(flow: str) -> list[str]:
    """'ST10 -> ST20 -> ST30' or 'ST10,ST20,ST30' -> ['ST10','ST20','ST30']"""
    import re
    parts = re.split(r"->|,|>", flow)
    return [p.strip().upper() for p in parts if p.strip()]
