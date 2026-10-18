"""AC-AGT-35 (UI4-09): scripted-latency run of the round-4 standard conversation.

The LLM is scripted (the same tool calls and answers for every code version), but each call sleeps for a delay derived
from the request size and the answer length, calibrated on the deployed T4 (Gemma 4 12B QAT): the first step read as
"Reading the question" for 5-8 s, and Q1 took about 23 s. Times are simulated seconds (wall time / SCALE).

Simulated time = model time (scripted) + 0.1 s per inventory-api request + the real time of everything else (the
harness itself, unscaled).

"First meaningful text" is the first ``facts`` status event (AC-AGT-35) or, when there is none, the end of the turn
(the reply is only streamed after the grounding check). This module only uses the harness API that existed before
round 4, so the same scenario measures the old code too (run it with the old package first on PYTHONPATH)."""
from __future__ import annotations

import asyncio
import json
import time

from nasa_agent.context import estimate_request_tokens, estimate_tokens
from nasa_agent.harness import AgentHarness
from tests.fakes import FakeInventoryBlock, FakeLLM, assistant_text, assistant_tools, tool_call
from tests.live_round4 import load_round4

#: wall seconds per simulated model second (only makes the test fast)
SCALE = 0.002
#: simulated latency of one inventory-api request
INVENTORY_S = 0.1
PREFILL_TPS = 800.0
DECODE_TPS = 18.0
OVERHEAD_S = 0.3
#: a realistic inventory block (the deployed renderer fills its 600-token budget)
BLOCK = "INVENTORY WORKING SET:\n" + "\n".join(
    f"- laptop [{i:08d}] provisional at USOS, last seen 2026-10-18T03:{i:02d}:00Z" for i in range(40))


class TimedLLM(FakeLLM):
    """FakeLLM whose calls take PREFILL + DECODE time (scaled); records simulated per-call cost and prompt size."""

    def __init__(self, script):
        super().__init__(script)
        self.sim_costs: list[float] = []
        self.prompt_sizes: list[int] = []
        self.slept = 0.0  # wall seconds spent sleeping

    async def complete(self, messages, tools, tool_choice="auto"):
        prompt = estimate_request_tokens(messages, tools)
        reply = self.script[0] if self.script else {}
        out = estimate_tokens(reply.get("content") or json.dumps(reply.get("tool_calls") or []))
        cost = prompt / PREFILL_TPS + out / DECODE_TPS + OVERHEAD_S
        self.sim_costs.append(cost)
        self.prompt_sizes.append(prompt)
        t0 = time.perf_counter()
        await asyncio.sleep(cost * SCALE)
        self.slept += time.perf_counter() - t0
        return await super().complete(messages, tools, tool_choice)


LAPTOP_LIST = ("I have found multiple laptops in the following locations:\n"
               "1. laptop: US Orbital Segment (USOS), 03:30 UTC\n2. laptop: Nauka MLM (NAUKA), 03:28 UTC\n"
               "3. laptop: Zvezda Service Module (ZVEZDA), 03:26 UTC\n4. laptop: Cupola (CUP1), 03:21 UTC\n"
               "5. laptop: US Orbital Segment (USOS), 03:15 UTC")

#: (question, the model's scripted messages): the tool calls the deployed model made in round 4
STANDARD = [
    ("Where is the laptop right now?",
     [assistant_tools(tool_call("where_is", {"name": "laptop"})), assistant_text(LAPTOP_LIST)]),
    ("Where is the multimeter?",
     [assistant_tools(tool_call("where_is", {"name": "multimeter"})),
      assistant_text("The Fluke multimeter was last seen in Destiny (US Lab) (LAB1) at 03:05 UTC.")]),
    ("Who used the drill?",
     [assistant_tools(tool_call("who_moved", {"name": "drill"})),
      assistant_text("The pistol-grip drill was used by a person in a blue shirt, dark shorts at 03:10 UTC.")]),
    ("What changed in the last hour?",
     [assistant_tools(tool_call("recent_changes", {"offset": 1}))]),
    ("show more changes", []),
    ("Are there any unknown objects in Node 1?",
     [assistant_tools(tool_call("list_unknowns", {"location": "NOD1"})),
      assistant_text("There are 2 unknown objects in Node 1 Unity (NOD1): unknown grey strap and unknown white bag, "
                     "both last seen at 03:00 UTC.")]),
    ("Who was in the Cupola?", [assistant_tools(tool_call("people_seen", {}))]),
    ("How many people have been seen today?", [assistant_tools(tool_call("people_seen", {}))]),
]


async def run_standard(toolbox, fake_inventory, fake_memory, settings) -> list[dict]:
    load_round4(fake_inventory, toolbox)
    history: list[dict] = []
    rows = []
    for question, script in STANDARD:
        llm = TimedLLM(script)
        harness = AgentHarness(llm, toolbox, settings=settings, memory=fake_memory,
                               inventory_block=FakeInventoryBlock(BLOCK))
        first_facts: list[float] = []
        started = time.perf_counter()
        requests_before = len(fake_inventory.requests)

        def sim_now(llm=llm, started=started, requests_before=requests_before) -> float:
            wall = time.perf_counter() - started - llm.slept
            return wall + sum(llm.sim_costs) + INVENTORY_S * (len(fake_inventory.requests) - requests_before)

        def on_event(ev, sim_now=sim_now, first_facts=first_facts):
            if ev.get("phase") == "facts" and not first_facts:
                first_facts.append(sim_now())

        history.append({"role": "user", "content": question})
        res = await harness.run(list(history), on_event=on_event)
        done = sim_now()
        history.append({"role": "assistant", "content": res["reply"]})
        rows.append({"q": question, "llm_calls": llm.calls, "prompt_tokens": llm.prompt_sizes,
                     "first_text_s": round(first_facts[0] if first_facts else done, 1),
                     "done_s": round(done, 1), "reply": res["reply"], "tool_calls": res["tool_calls"]})
    return rows


async def test_ac_agt_35_standard_conversation_latency(toolbox, fake_inventory, fake_memory, plain_settings):
    rows = await run_standard(toolbox, fake_inventory, fake_memory, plain_settings)
    print("\nquestion | LLM calls | prompt tokens/step | first text s | done s")
    for r in rows:
        print(f"{r['q']} | {r['llm_calls']} | {r['prompt_tokens']} | {r['first_text_s']} | {r['done_s']}")
    print(f"TOTAL LLM calls {sum(r['llm_calls'] for r in rows)}; worst first text "
          f"{max(r['first_text_s'] for r in rows)} s; sum done {round(sum(r['done_s'] for r in rows), 1)} s")
    by_q = {r["q"]: r for r in rows}
    # fewer LLM steps: a where-list, people and changes answers need at most one model call
    assert by_q["Where is the laptop right now?"]["llm_calls"] == 1
    for q in ("What changed in the last hour?", "Who was in the Cupola?", "How many people have been seen today?"):
        assert by_q[q]["llm_calls"] == 0, q
    # first meaningful text within one model step (about 5-6 simulated seconds) for every question
    assert all(r["first_text_s"] <= 7.0 for r in rows), [(r["q"], r["first_text_s"]) for r in rows]
    # and the answers are the right ones
    assert "(showing 1-10 of" in by_q["What changed in the last hour?"]["reply"]
    assert "(showing 11-20 of" in by_q["show more changes"]["reply"]
    cup = by_q["Who was in the Cupola?"]["reply"]
    assert cup.startswith("2 person tracks were seen in Cupola (CUP1)"), cup
    assert "9" in by_q["Where is the laptop right now?"]["reply"].split("\n")[0]
