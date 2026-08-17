"""
ATLAS agent orchestrator.

The orchestrator loop is decoupled from the LLM backend via a simple
protocol: `decide_next_step(question, history)` -> either a tool call or a
final answer. Today `MockLLM` implements that protocol with keyword rules
(free, deterministic, good for testing the plumbing). Swap in `ClaudeLLM`
or `GPTLLM` later — same orchestrator code, zero rewrite, real tokens spent
only when you choose to.
"""
from dataclasses import dataclass, field
import re
from atlas import tools


TOOL_REGISTRY = {
    "get_address": tools.get_address,
    "find_writers": tools.find_writers,
    "find_readers": tools.find_readers,
    "cross_reference": tools.cross_reference,
    "trace_address": tools.trace_address,
    "trace_internal_bit": tools.trace_internal_bit,
    "get_priority_state": tools.get_priority_state,
    "get_fault": tools.get_fault,
    "trace_fault": tools.trace_fault,
    "get_previous_incidents": tools.get_previous_incidents,
}


@dataclass
class Step:
    tool: str
    args: dict
    result: object = None


@dataclass
class AgentResult:
    question: str
    steps: list = field(default_factory=list)
    answer: str = ""
    needs_clarification: bool = False


STATION_TOKEN_RE = re.compile(r"\b(ST\d{2}|PIS-1|VISION-1)\b", re.I)


class MockLLM:
    """
    Deterministic stand-in for a real LLM's tool-calling decisions.
    Rule-based: enough to prove the orchestrator loop, tool chaining, and
    answer assembly work correctly — NOT a substitute for real reasoning
    over ambiguous phrasing. See README for what this can/can't prove.

    Enforces the same discipline as the original TRACE spec: a fault/alarm
    question with no station given gets a clarifying question, never a
    guessed trace. This is what keeps multi-station data from blurring
    together in an answer.
    """

    def decide(self, question: str, history: list[Step]) -> tuple[str, dict] | None:
        q = question.lower()

        fault_match = re.search(r"\b(?:f|fault)\s*(\d{3})\b", q) or re.search(r"\b(\d{3})\b", q)
        is_fault_question = bool(fault_match) or "alarm" in q or "fault" in q

        if is_fault_question and not STATION_TOKEN_RE.search(question) and not history:
            return "CLARIFY_STATION", {}

        if fault_match and not any(s.tool == "trace_fault" for s in history):
            code = int(fault_match.group(1))
            station_in_q = STATION_TOKEN_RE.search(question)
            fault = tools.get_fault(code)
            if fault and station_in_q and fault.get("station_code") and \
                    fault["station_code"].upper() != station_in_q.group(0).upper():
                return "MISMATCH_STATION", {
                    "asked": station_in_q.group(0).upper(),
                    "actual": fault["station_code"],
                    "code": code,
                }
            return "trace_fault", {"code": code}

        if ("happened before" in q or "previous incident" in q or "past incident" in q) \
                and not any(s.tool == "get_previous_incidents" for s in history):
            code_match = re.search(r"\b(\d{3})\b", q)
            if code_match:
                return "get_previous_incidents", {"fault_code": int(code_match.group(1))}

        addr_match = re.search(r"\b([DMXYR]\d{3,4}[A-Z]?)\b", question.upper())
        if addr_match and not any(s.tool == "trace_address" for s in history):
            return "trace_address", {"address": addr_match.group(1)}

        if "priority" in q or "queue" in q:
            if not any(s.tool == "get_priority_state" for s in history):
                return "get_priority_state", {}

        return None  # nothing more to do -> finalize

    def summarize(self, question: str, history: list[Step]) -> str:
        if not history:
            return "I don't have enough information in the dataset to answer that."
        lines = [f"Question: {question}", "", "Evidence gathered:"]
        for step in history:
            lines.append(f"- {step.tool}({step.args}) -> {_short(step.result)}")

        incidents = self._collect_incidents(history)
        if incidents:
            lines.append("")
            lines.append("Prior incidents found (historical cause is NOT the same as current cause "
                          "— verify before assuming it repeats):")
            for inc in incidents:
                tag = "CONFIRMED root cause" if inc.get("confirmed") else "UNCONFIRMED (reported, not verified)"
                lines.append(f"  - [{tag}] {inc.get('symptom')} -> {inc.get('root_cause') or 'not recorded'}")

        lines.append("")
        lines.append("(Mock summary — a real LLM would turn this evidence into prose "
                      "with proper source citations. This proves the tool-calling chain works.)")
        return "\n".join(lines)

    @staticmethod
    def _collect_incidents(history: list[Step]) -> list[dict]:
        found = []
        for step in history:
            if step.tool == "get_previous_incidents" and isinstance(step.result, list):
                found.extend(step.result)
            if step.tool == "trace_fault" and isinstance(step.result, dict):
                found.extend(step.result.get("previous_incidents") or [])
        return found


def _short(result, limit=300):
    s = str(result)
    return s[:limit] + ("..." if len(s) > limit else "")


def run_agent(question: str, llm=None, max_steps: int = 6) -> AgentResult:
    llm = llm or MockLLM()
    history: list[Step] = []

    for _ in range(max_steps):
        decision = llm.decide(question, history)
        if decision is None:
            break
        tool_name, args = decision
        if tool_name == "CLARIFY_STATION":
            return AgentResult(
                question=question,
                steps=history,
                answer="Which station is this at? (e.g. ST10, ST20, ST30, ST40, ST45) "
                       "I don't trace a fault without knowing the station — the same "
                       "fault code can mean different things at different stations, "
                       "and guessing risks mixing up unrelated data.",
                needs_clarification=True,
            )
        if tool_name == "MISMATCH_STATION":
            return AgentResult(
                question=question,
                steps=history,
                answer=f"Fault {args['code']} in this dataset belongs to {args['actual']}, "
                       f"not {args['asked']}. Did you mean {args['actual']}, or is this a "
                       f"different fault code at {args['asked']}? I won't guess — "
                       f"tell me which one you meant.",
                needs_clarification=True,
            )
        fn = TOOL_REGISTRY[tool_name]
        result = fn(**args)
        history.append(Step(tool=tool_name, args=args, result=result))

    answer = llm.summarize(question, history)
    return AgentResult(question=question, steps=history, answer=answer)
