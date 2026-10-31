"""Local, durable per-run tracing (works with no external service). Payloads are already redacted by callers."""
import json
import time
import uuid

from .db import tx
from .timeutil import now_iso


class Trace:
    def __init__(self, student_id: str, conversation_id: str, question: str):
        self.run_id = "run-" + uuid.uuid4().hex[:10]
        self.student_id, self.conversation_id, self.question = student_id, conversation_id, question
        self.started_at = now_iso()
        self.t0 = time.perf_counter()
        self.events: list[dict] = []

    def event(self, etype: str, payload: dict | None = None, duration_ms: float | None = None):
        self.events.append({"seq": len(self.events) + 1, "event_type": etype, "payload": payload or {},
                            "duration_ms": None if duration_ms is None else round(duration_ms, 1), "created_at": now_iso()})

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.t0) * 1000

    def save(self, summary: dict, db_path: str | None = None):
        with tx(db_path) as c:
            c.execute("INSERT INTO agent_runs (run_id,conversation_id,student_id,provider_mode,model,question,answer,status,"
                      "stop_reason,started_at,latency_ms,input_tokens,output_tokens,token_note,tools_used,policy_chunks,"
                      "memory_ids,redactions,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (self.run_id, self.conversation_id, self.student_id, summary["provider_mode"], summary["model"],
                       self.question, summary["answer"], summary["status"], summary["stop_reason"], self.started_at,
                       round(summary["latency_ms"], 1), summary["input_tokens"], summary["output_tokens"],
                       summary["token_note"], json.dumps(summary["tools_used"]), json.dumps(summary["policy_chunks"]),
                       json.dumps(summary["memory_ids"]), json.dumps(summary["redactions"]), summary.get("error")))
            c.executemany("INSERT INTO agent_events (run_id,seq,event_type,payload,duration_ms,created_at) VALUES (?,?,?,?,?,?)",
                          [(self.run_id, e["seq"], e["event_type"], json.dumps(e["payload"], default=str),
                            e["duration_ms"], e["created_at"]) for e in self.events])
