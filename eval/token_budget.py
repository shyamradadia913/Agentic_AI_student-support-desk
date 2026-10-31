"""Estimate model INPUT tokens per request for the golden dataset under different settings (chars/4 estimate, the same proxy that
matched a live trace within ~6%). Mock provider, so it measures what we SEND, not what a live model decides to call.
Usage: python -m eval.token_budget"""
import json
import statistics

from app import config
from eval.golden_a2 import CASES
from eval.run import evaluate_case
from app.llm import MockProvider

est = lambda o: len(o if isinstance(o, str) else json.dumps(o)) // 4


class Measuring(MockProvider):
    log: list = []

    def chat(self, messages, tools):
        Measuring.log.append(est(messages) + est(tools))
        return super().chat(messages, tools)


def run(label, saver, routing):
    config.TOKEN_SAVER, config.TOOL_ROUTING = saver, routing
    per_case, calls, ok = [], [], 0
    for c in CASES:
        Measuring.log = []
        r = evaluate_case(c, Measuring)
        per_case.append(sum(Measuring.log)); calls.append(len(Measuring.log)); ok += r["passed"]
        c.setdefault("_m", {})[label] = (sum(Measuring.log), len(Measuring.log))
    return {"label": label, "total": sum(per_case), "mean_case": round(statistics.mean(per_case)), "calls": sum(calls), "passed": ok, "cases": len(CASES),
            "per_call": round(sum(per_case) / max(1, sum(calls)))}


def main():
    old = (config.TOKEN_SAVER, config.TOOL_ROUTING)
    rows = [run("before (no savers)", False, "off"), run("lean schemas + results", True, "off"), run("+ tool routing (new default)", True, "core")]
    config.TOKEN_SAVER, config.TOOL_ROUTING = old
    base = rows[0]["total"]
    print(f"{'setting':32}{'avg tokens/case':>16}{'avg tokens/call':>17}{'total':>10}{'vs before':>11}{'cases passed':>14}")
    for r in rows:
        print(f"{r['label']:32}{r['mean_case']:>16,}{r['per_call']:>17,}{r['total']:>10,}{100*(r['total']-base)/base:>10.0f}%{r['passed']:>10}/{r['cases']}")
    print("\nSelected flows (estimated input tokens summed over all model calls of the request):")
    for cid, title in [("F01", "fee status (1 tool)"), ("W01", "payment-portal (4 tools)"), ("M01", "memory + fee, 2 sessions"), ("AP01", "course change request")]:
        c = next(c for c in CASES if c["id"] == cid)
        print(f"  {cid:5}{title:28}" + "  ".join(f"{k.split(' (')[0][:14]:>14}: {v[0]:>6,} ({v[1]} calls)" for k, v in c["_m"].items()))


if __name__ == "__main__":
    main()
