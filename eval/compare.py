"""Build the Assignment 1 vs Assignment 2 comparison table from eval summaries.
Usage: python -m eval.compare --a1 a1_summary.json --a2 a2_summary.json [--a2full full_summary.json] --out outputs/comparison.md
a1 / a2 should be the SAME dataset (eval.golden_a1) run on the assignment-1 and assignment-2 code, so the comparison is like-for-like.
a2full is the complete Assignment 2 dataset (adds privacy/approval/audit cases)."""
import argparse
import json
from pathlib import Path


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8")) if p else None


def cell(s, k):
    if s is None:
        return "n/a"
    v = s.get(k)
    if not v or v.get("total", 0) == 0:
        return "n/a"
    return f"{v['passed']}/{v['total']} ({v['pct']}%)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a1", required=True); ap.add_argument("--a2", required=True); ap.add_argument("--a2full")
    ap.add_argument("--out", default="outputs/comparison.md")
    a = ap.parse_args()
    s1, s2, sf = load(a.a1), load(a.a2), load(a.a2full)
    cols = [("Assignment 1 (37 cases)", s1), ("Assignment 2, same 37 cases", s2)] + ([("Assignment 2, full set", sf)] if sf else [])
    modes = {s["provider_mode"] for _, s in cols}
    L = ["# Assignment 1 vs Assignment 2", "", f"Provider mode(s): **{', '.join(sorted(modes))}**" +
         (" — mock numbers only check the pipeline; they are NOT model-quality evidence." if "mock" in modes else ""), "",
         "| Measure | " + " | ".join(n for n, _ in cols) + " |", "|---|" + "---|" * len(cols)]
    rows = [("Task accuracy / success", "task_success"), ("Tool choice appropriate", "tool_choice_accuracy"), ("Policy retrieval hit@k", "retrieval_hit_at_k"),
            ("Abstention correct", "abstention_correct"), ("Citations valid", "citations_valid"), ("Secret redaction", "redaction_correct"),
            ("Privacy: other-student access blocked", "privacy_blocked"), ("Unsafe actions gated by approval", "unsafe_action_gated"),
            ("Audit evidence present", "audit_evidence"), ("Trace complete", "trace_complete")]
    for name, k in rows:
        L.append(f"| {name} | " + " | ".join(cell(s, k) for _, s in cols) + " |")
    for lab, key in (("Latency mean (ms)", "mean"), ("Latency p50 (ms)", "p50"), ("Latency p95 (ms)", "p95")):
        L.append(f"| {lab} | " + " | ".join(str(s["latency_ms"][key]) for _, s in cols) + " |")

    def tok(s):
        t = s["tokens"]
        return f"{t['input_total']} / {t['output_total']}" if t["input_total"] is not None else t["note"]
    L.append("| Tokens in / out (total) | " + " | ".join(tok(s) for _, s in cols) + " |")
    L += ["", "Notes: A1 has no identity or approval layer, so the privacy / approval / audit rows are n/a there. "
          "A2 latency includes the per-request overhead of authorization checks and audit writes."]
    d = s2["latency_ms"]["mean"] - s1["latency_ms"]["mean"]
    L.append(f"Mean latency change A1 → A2 (same cases): **{d:+.1f} ms** ({100 * d / s1['latency_ms']['mean']:+.1f}%).")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
