"""
FREYA full-engine smoke test (Path 3 / CI verification).

Drives the real MiroFish engine over HTTP, end-to-end:
  health -> upload document + ontology -> build graph -> create simulation
  -> prepare -> start (1 round) -> poll run status -> generate report.

Each step prints OK/FAIL with detail so CI logs are diagnosable.
Uses the real keys configured in the CI environment (LLM + Zep).
"""

import os
import sys
import time

import requests

BASE = os.environ.get("FREYA_BASE_URL", "http://localhost:5001").rstrip("/")
TIMEOUT = 600  # per-request seconds; some engine steps are slow


def log(step, ok, detail=""):
    mark = "OK" if ok else "FAIL"
    print(f"[{mark}] {step} {detail}", flush=True)
    if not ok:
        sys.exit(1)


def post(path, **kw):
    return requests.post(f"{BASE}{path}", timeout=TIMEOUT, **kw)


def get(path, **kw):
    return requests.get(f"{BASE}{path}", timeout=TIMEOUT, **kw)


def wait_for(step, fn, tries=150, delay=5, log_every=12):
    """Poll fn() -> (done: bool, detail: str) until done or timeout.

    Logs progress every ~log_every polls (~1 min) so a slow-but-working step
    is distinguishable from a stuck one; on timeout, reports the last detail.
    """
    last = ""
    for i in range(tries):
        done, detail = fn()
        last = detail
        if done:
            log(step, True, detail)
            return detail
        if (i + 1) % log_every == 0:
            print(f"      ... {step}: {detail} ({i + 1}/{tries} polls)", flush=True)
        time.sleep(delay)
    log(step, False, f"timed out after {tries * delay}s; last: {last}")
    return None


def main():
    # 0. health
    r = get("/health")
    log("health", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

    # 1. upload document + generate ontology
    seed_text = (
        "Alice opens a small coffee shop called 'Ember & Oak' on a quiet street. "
        "Her first customer is Bob, a local writer. The next day a food blogger "
        "visits and posts a mixed review."
    )
    r = post(
        "/api/graph/ontology/generate",
        files={"files": ("seed.txt", seed_text, "text/plain")},
        data={
            "simulation_requirement": "Simulate how the shop's opening week unfolds.",
            "project_name": "Ember & Oak smoke test",
        },
    )
    ok = r.status_code == 200 and r.json().get("success", True)
    log("ontology/generate", ok, f"{r.status_code} {r.text[:200]}")
    project_id = r.json().get("data", {}).get("project_id") or r.json().get("project_id")
    log("project_id", bool(project_id), str(project_id))

    # 2. build graph (async)
    r = post("/api/graph/build", json={"project_id": project_id, "graph_name": "smoke-graph"})
    ok = r.status_code == 200 and r.json().get("success", True)
    log("graph/build", ok, f"{r.status_code} {r.text[:200]}")
    data = r.json().get("data", {})
    task_id = data.get("task_id") or data.get("graph_build_task_id") or r.json().get("task_id")

    def graph_done():
        rr = get(f"/api/graph/task/{task_id}")
        if rr.status_code != 200:
            return False, rr.text[:200]
        d = rr.json().get("data", {})
        return d.get("status") == "completed", f"status={d.get('status')} progress={d.get('progress')}"
    gd = wait_for("graph/task completed", graph_done, tries=40, delay=5)
    graph_id = None
    if gd and isinstance(gd, dict):
        graph_id = gd.get("result", {}).get("graph_id") or gd.get("graph_id")

    if not graph_id:
        rr = get(f"/api/graph/project/{project_id}")
        if rr.status_code == 200:
            graph_id = rr.json().get("data", {}).get("graph_id")
    log("graph_id", bool(graph_id), str(graph_id))

    # 3. create simulation
    r = post(
        "/api/simulation/create",
        json={"project_id": project_id, "graph_id": graph_id, "enable_twitter": True, "enable_reddit": False},
    )
    ok = r.status_code == 200 and r.json().get("success", True)
    log("simulation/create", ok, f"{r.status_code} {r.text[:200]}")
    simulation_id = r.json().get("data", {}).get("simulation_id") or r.json().get("simulation_id")
    log("simulation_id", bool(simulation_id), str(simulation_id))

    # 4. prepare (async; generates agent profiles via the LLM — can be slow)
    r = post(
        "/api/simulation/prepare",
        json={"simulation_id": simulation_id, "use_llm_for_profiles": True, "parallel_profile_count": 2},
    )
    ok = r.status_code == 200 and r.json().get("success", True)
    log("simulation/prepare", ok, f"{r.status_code} {r.text[:200]}")
    prepare_task_id = (r.json().get("data") or {}).get("task_id")
    log("prepare_task_id", bool(prepare_task_id), str(prepare_task_id))

    def prepared():
        rr = post("/api/simulation/prepare/status", json={"task_id": prepare_task_id, "simulation_id": simulation_id})
        if rr.status_code != 200:
            return False, rr.text[:200]
        d = rr.json().get("data", {})
        done = d.get("status") == "completed" or d.get("already_prepared") or d.get("progress") == 100
        return done, f"status={d.get('status')} progress={d.get('progress')} msg={str(d.get('message'))[:60]}"
    wait_for("simulation prepared", prepared, tries=150, delay=5)

    # 5. start (1 round)
    r = post("/api/simulation/start", json={"simulation_id": simulation_id, "platform": "parallel", "max_rounds": 1})
    ok = r.status_code == 200 and r.json().get("success", True)
    log("simulation/start", ok, f"{r.status_code} {r.text[:200]}")

    def running_or_done():
        rr = get(f"/api/simulation/{simulation_id}/run-status")
        if rr.status_code != 200:
            return False, rr.text[:200]
        d = rr.json().get("data", {})
        rs = d.get("runner_status")
        cur = d.get("current_round") or 0
        total = d.get("total_rounds") or 0
        actions = d.get("total_actions_count") or 0
        # The runner can linger in "running" after its rounds complete; treat
        # "rounds finished AND actions produced" as done so we can generate a report.
        done = rs in ("completed", "idle", "stopped") or (bool(total) and cur >= total and actions > 0)
        return done, f"runner={rs} round={cur}/{total} actions={actions}"
    wait_for("simulation finished", running_or_done, tries=60, delay=5)

    # 6. generate report
    r = post("/api/report/generate", json={"simulation_id": simulation_id})
    ok = r.status_code == 200 and r.json().get("success", True)
    log("report/generate", ok, f"{r.status_code} {r.text[:200]}")
    data = r.json().get("data") or {}
    report_id = data.get("report_id") or r.json().get("report_id")
    report_task_id = data.get("task_id")

    def report_done():
        rr = post("/api/report/generate/status", json={"task_id": report_task_id})
        if rr.status_code != 200:
            return False, rr.text[:200]
        d = rr.json().get("data", {})
        done = d.get("status") == "completed" or d.get("progress") == 100
        return done, f"status={d.get('status')} progress={d.get('progress')}"
    wait_for("report completed", report_done, tries=150, delay=5)

    log("SMOKE COMPLETE", True, f"simulation_id={simulation_id} report_id={report_id}")


if __name__ == "__main__":
    main()
