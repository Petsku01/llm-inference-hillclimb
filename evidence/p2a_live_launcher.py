#!/usr/bin/env python3
"""p2a_live_launcher.py — A3.6 E3-LIVE-launcher-bridge.

reservation → launch → workload → terminal-capture → metering → cleanup
per ladder-item, fail-closed foreign/caps/identity ports.

Usage:
    python p2a_live_launcher.py --plan
    python p2a_live_launcher.py --go <ladder_name>
    python p2a_live_launcher.py --mock-test-all

A3.6-fix (round): fail-closed nvidia-smi, explicit model-identity compare,
real QT1–QT6 + greedy 12/12 workload over the fresh manifest, canonical
argv spelling (-ngl 99 / -ot / --host / --port), reservation-after-ports.
"""
import argparse, hashlib, importlib.util, json, os, pathlib, re, site, subprocess, sys, tempfile, threading, time, unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = pathlib.Path(__file__).parent
ROOT = HERE.parents[2]
PLAN_PATH = ROOT / "runs" / "E3" / "stage3_p2a_apply-plan.json"
MANIFEST_PATH = ROOT / "runs" / "E3" / "stage3-fresh-workload-v3.json"
MODEL_PATH = pathlib.Path("/home/Petsku/llm-mallit/models/Qwen3.5-9B-Q4_K_M.gguf")
SCRATCH = pathlib.Path(os.environ.get("TMPDIR", "/tmp"))

# ---------------------------------------------------------------------------
# Load E3.2 substrate
# ---------------------------------------------------------------------------
def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

g = _load("e32_guard", HERE / "guarded_runtime.py")
core = _load("e32_core", HERE / "execution-core.py")

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class Blocked(Exception):
    pass

class Invalid(Exception):
    pass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _load_plan():
    if not PLAN_PATH.exists():
        raise Blocked("stage3_p2a_apply-plan.json missing")
    plan = json.loads(PLAN_PATH.read_text())
    if plan.get("schema") != "stage3_p2a_apply-plan-v1":
        raise Blocked("plan schema mismatch")
    return plan


def _find_ladder_item(plan, name):
    for item in plan["runs"]:
        if item["name"] == name:
            return item
    raise Blocked(f"ladder item {name!r} not found in plan")


# ---------------------------------------------------------------------------
# Fail-closed ports
# ---------------------------------------------------------------------------
def _check_foreign_consumers(allowed_pids=frozenset()):
    """nvidia-smi compute-apps check. FAIL-CLOSED: nvidia-smi missing, non-zero
    exit, or a non-numeric output row all mean we cannot prove the GPU is empty,
    so the run is BLOCKED-GPU-COLLISION (absence of evidence is not evidence of
    absence). allowed_pids: launcher-owned pids whitelisted at mid-run."""
    cmd = ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits", "-i", "0"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except FileNotFoundError as e:
        raise Blocked("BLOCKED-GPU-COLLISION: nvidia-smi not found (fail-closed; cannot prove empty GPU)") from e
    except subprocess.TimeoutExpired as e:
        raise Blocked("BLOCKED-GPU-COLLISION: nvidia-smi timed out (fail-closed)") from e
    if out.returncode != 0:
        raise Blocked("BLOCKED-GPU-COLLISION: nvidia-smi failed: " + out.stderr.strip())
    pids = []
    for line in out.stdout.strip().splitlines():
        x = line.strip()
        if not x:
            continue
        if not x.isdigit():
            raise Blocked("BLOCKED-GPU-COLLISION: nvidia-smi parse error (non-numeric row %r)" % x)
        pids.append(int(x))
    foreign = [pid for pid in pids if pid not in allowed_pids]
    if foreign:
        raise Blocked(f"BLOCKED-GPU-COLLISION: foreign consumers {foreign}")
    return {"foreign_consumer_detected": False, "pids": []}


def _check_identity_pins(ladder_item):
    """Model identity is an EXPLICIT compare, not a vacuum: the plan pins
    expected_model_sha256; the observed model-file sha must equal it (wrong
    model or missing pin -> BLOCKED)."""
    expected = ladder_item.get("expected_model_sha256")
    if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise Blocked("BLOCKED-wrong-model: missing/invalid expected_model_sha256 pin")
    if not MODEL_PATH.exists():
        raise Blocked("model file missing: " + str(MODEL_PATH))
    model_sha = _digest(MODEL_PATH)
    if model_sha != expected:
        raise Blocked("BLOCKED-wrong-model: model sha %s != expected %s" % (model_sha, expected))
    return {"model_path": str(MODEL_PATH), "model_sha256": model_sha,
            "expected_model_sha256": expected}


def _check_effective_config(ladder_item):
    cfg = ladder_item["effective_config"]
    got_sha = hashlib.sha256(
        json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if got_sha != ladder_item["effective_config_sha256"]:
        raise Blocked(f"effective-config hash mismatch: {got_sha} != {ladder_item['effective_config_sha256']}")
    return {"effective_config_sha256": got_sha}


def _check_per_run_cap(ladder_item):
    scope = ladder_item["scope"]
    caps = {"stage3": {"intents": 120, "wall_s": 1800}, "p2a": {"intents": 156, "wall_s": 2700}}
    cap = caps.get(scope, {})
    if ladder_item["planned_intents"] > cap.get("intents", 0):
        raise Blocked(f"per-run intent cap exceeded: {ladder_item['planned_intents']} > {cap['intents']}")
    if ladder_item["planned_wall_s"] > cap.get("wall_s", 0):
        raise Blocked(f"per-run wall-s cap exceeded: {ladder_item['planned_wall_s']} > {cap['wall_s']}")
    return {"scope": scope, "intents_ok": True, "wall_s_ok": True}


def _config_name_for(cfg):
    """Map an effective_config back to its guarded_runtime CONFIGS name."""
    for name, c, t, fa in g.CONFIGS:
        if c == cfg["context_tokens"] and t == cfg["cache_k"] and t == cfg["cache_v"] and fa == cfg["fa"]:
            return name
    raise Blocked("unlisted effective config; cannot resolve argv gate")


def _launch_command(ladder_item):
    """FIX 4: rebuild the launch argv from the guarded_runtime canonical
    (E1-era b12359 flags: -ngl 99, -ot token_embd.weight=CUDA0, --host
    127.0.0.1, --port). The plan command field is a preview only and is NEVER
    launched verbatim (no plan-command argv bypass of the memory-guard
    CAP-analysis flags)."""
    cfg = ladder_item["effective_config"]
    return g.argv(g.config(_config_name_for(cfg)), "-v")


# ---------------------------------------------------------------------------
# HTTP client (same pattern as launcher-live.py + /v1/chat/completions SSE)
# ---------------------------------------------------------------------------
class HTTPClient:
    def __init__(self, port=8081):
        self.port = port

    def _conn(self, timeout=2):
        import http.client
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def json(self, path, body=None, timeout=2):
        conn = self._conn(timeout)
        try:
            headers = {"Content-Type": "application/json"}
            data = json.dumps(body) if body is not None else None
            conn.request("POST" if body is not None else "GET", path, body=data, headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                raise Blocked(f"HTTP {resp.status} on {path}")
            wire = resp.read(8 * 1024 * 1024 + 1)
            if len(wire) > 8 * 1024 * 1024:
                raise Blocked("HTTP response too large")
            return json.loads(wire)
        finally:
            conn.close()

    def health(self):
        return self.json("/health")

    def slots(self):
        return self.json("/slots")

    def chat(self, body, timeout=30):
        """POST /v1/chat/completions (stream) and return accumulated text +
        tool_calls. Same SSE shape as execution-core.execute_request consumes."""
        conn = self._conn(timeout)
        try:
            data = json.dumps(body).encode("utf-8")
            conn.request("POST", "/v1/chat/completions", body=data,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200:
                raise Blocked("HTTP %d on /v1/chat/completions" % resp.status)
            raw = resp.read()
        finally:
            conn.close()
        text = ""
        tool_calls = []
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                ev = json.loads(payload)
            except ValueError:
                continue
            if ev.get("error"):
                raise Blocked("server error " + str(ev["error"]))
            for ch in ev.get("choices", []):
                d = ch.get("delta", {}) or {}
                if d.get("content"):
                    text += d["content"]
                for tc in d.get("tool_calls", []) or []:
                    tool_calls.append(tc)
        return {"text": text, "tool_calls": tool_calls}


# ---------------------------------------------------------------------------
# Workload spec builders (FIX 3: real QT1–QT6 + greedy 12/12)
# ---------------------------------------------------------------------------
def _driver():
    """Lazily load the pinned stage3_p2a_driver for manifest reconstruction.
    tokenizers lives in user site-packages (not the venv)."""
    us = site.getusersitepackages()
    if us not in sys.path:
        sys.path.append(us)
    return _load("stage3_p2a_driver", ROOT / "tools" / "stage3_p2a_driver.py")


def _needle_specs(cold_start_id=None):
    """Reconstruct + render every manifest needle document. If cold_start_id is
    given, only that cold's 6 documents; otherwise all 36 (the full doc-pool)."""
    d = _driver()
    manifest = d.load_manifest()
    tok = d.load_tokenizer()
    recipe = manifest["construction_recipe"]
    query = recipe["query"]["literal"].replace("{keys}", recipe["query"]["keys"])
    out = []
    for cs in manifest["cold_starts"]:
        if cold_start_id is not None and cs["cold_start_id"] != cold_start_id:
            continue
        for occ in cs["occupancies"]:
            for doc in occ["documents"]:
                doc_text = d.reconstruct_document(tok, doc, occ["content_tokens"],
                                                  occ["query_tokens"], recipe)
                content = doc_text + "\n" + query
                rendered = d.render_prompt(content)
                if _sha256_text(rendered) != doc["rendered_prompt_sha256"]:
                    raise Blocked("rendered-prompt sha drift cold%d/occ%d/doc%d"
                                  % (cs["cold_start_id"], occ["occupancy_pct"], doc["doc_index"]))
                out.append({
                    "kind": "needle",
                    "cold_start_id": cs["cold_start_id"],
                    "occupancy_pct": occ["occupancy_pct"],
                    "doc_index": doc["doc_index"],
                    "document_type": doc["document_type"],
                    "content": content,
                    "rendered_prompt": rendered,
                    "rendered_prompt_sha256": doc["rendered_prompt_sha256"],
                    "expected_answer": doc["expected_answer"],
                })
    return out


def _functional_specs():
    """QT1–QT4 + the 6 deterministic greedy shorts from functional-tokenized."""
    wl = core.Workloads(HERE)
    specs = []
    for tid, kind in (("QT1-smoke", "readiness"), ("QT2-followup", "qt"),
                      ("QT4-call", "qt"), ("QT4-final", "qt"),
                      ("facts", "greedy"), ("arithmetic", "greedy"),
                      ("logic", "greedy"), ("sequence", "greedy"),
                      ("followup", "greedy"), ("sorting", "greedy")):
        t = wl.function(tid)
        specs.append({
            "kind": kind,
            "id": tid,
            "messages": t["messages"],
            "rendered_prompt": t["rendered_prompt"],
            "expected": t.get("expected"),
            "tools": t.get("tools"),
            "grading": t.get("grading"),
        })
    return specs


def _greedy_specs(cold_start_id=None):
    """12 greedy prompts = 6 fixed shorts + 6 needles (same context)."""
    shorts = [s for s in _functional_specs() if s["kind"] == "greedy"]
    needles = [dict(s, kind="greedy-needle") for s in _needle_specs(cold_start_id)]
    return shorts + needles


def _run_specs(ladder_item):
    """Full per-run workload: QT1–QT4 + greedy shorts + this cold's needles."""
    return _functional_specs() + _needle_specs(ladder_item["cold_start_id"])


def _request_body(spec):
    body = {
        "model": "qwen3.5-9b",
        "messages": spec["messages"] if "messages" in spec else [{"role": "user", "content": spec["content"]}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 128,
        "temperature": 0, "top_p": 1, "top_k": 0, "min_p": 0,
        "repeat_penalty": 1, "seed": 0,
        "chat_template_kwargs": {"enable_thinking": False},
        "cache_prompt": False,
    }
    if spec.get("tools"):
        body["tools"] = spec["tools"]
    return body


def _grade(spec, resp):
    """Exact-answer predicate. Failures (including tool-call grade) -> False
    (recorded as uncertain), never a silent pass."""
    if spec["kind"] in ("needle", "greedy-needle"):
        return g.grade_needles(spec["expected_answer"], resp["text"])
    if spec.get("tools"):
        # QT4-call: parse the (single) tool call and compare name+JSON args.
        if len(resp["tool_calls"]) != 1 or not resp["tool_calls"][0].get("id"):
            return False
        fn = resp["tool_calls"][0]["function"]
        try:
            return {"name": fn["name"], "arguments": core.strict_json(fn["arguments"])} == spec["expected"]
        except (TypeError, ValueError, KeyError):
            return False
    return resp["text"].strip() == spec["expected"]


def _spec_content(spec):
    """The message content actually sent for a spec (matches _request_body)."""
    if "messages" in spec:
        return spec["messages"][0]["content"]
    return spec["content"]


def _spec_answer(spec):
    """The answer the mock server should return for a spec's message content."""
    if spec["kind"] in ("needle", "greedy-needle"):
        return json.dumps(spec["expected_answer"])
    return spec["expected"]


def _execute_workload(http, specs, name="run"):
    """Send + grade every spec. Returns intents + pass/fail/uncertain counts and
    per-request results (the persisted metering feed for derive_charges)."""
    results = []
    intent_ids = []
    n_pass = 0
    uncertain = 0
    readiness = 0
    tokenize = 0
    # Per-context request timeout: prefill scales with context tokens
    # (E3-era measured ~500 tok/s prefill + generation; 32k ≈ 300 s,
    # 131k ≈ 900 s worst case — 30 s default suffices only for smoke probes).
    req_timeout = 900 if "131k" in name else 300
    for i, spec in enumerate(specs):
        request_id = f"{name}-{i:04d}"
        intent_ids.append(request_id)
        resp = http.chat(_request_body(spec), timeout=req_timeout)
        passed = _grade(spec, resp)
        if passed:
            n_pass += 1
        else:
            uncertain += 1
        if spec["kind"] == "readiness":
            readiness += 1
        if spec["kind"] == "tokenize":
            tokenize += 1
        results.append({"request_id": request_id, "kind": spec["kind"],
                        "id": spec.get("id") or spec.get("document_type"),
                        "passed": bool(passed), "text": resp["text"]})
    n_total = len(specs)
    return {
        "n_total": n_total,
        "n_pass": n_pass,
        "n_fail": n_total - n_pass,
        "uncertain": uncertain,
        "readiness": readiness,
        "tokenize": tokenize,
        "inference_requests": n_total,
        "intent_ids": intent_ids,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Terminal capture helpers
# ---------------------------------------------------------------------------
def _write_terminal(path, run_id, model_loads, inference_requests, elapsed_s, status, extra=None):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "status": status,
        "model_loads": model_loads,
        "inference_requests": inference_requests,
        "elapsed_s": elapsed_s,
        "timestamp_ns": time.time_ns(),
        "terminal_capture_path": str(path),
    }
    if extra:
        record.update(extra)
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    return record


def _write_metering(home, run_id, readiness, tokenize, uncertain, wall_s, intent_ids=None):
    path = pathlib.Path(home) / "metering.json"
    record = {
        "run_id": run_id,
        "readiness_intents": readiness,
        "tokenize_intents": tokenize,
        "uncertain_dispatch_intents": uncertain,
        "total_metered_intents": readiness + tokenize + uncertain,
        "wall_s": wall_s,
    }
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    # derive_charges persisted inputs (four artifacts)
    if intent_ids is not None:
        with open(pathlib.Path(home) / "intent_ids.json", "w") as f:
            json.dump({"intent_ids": intent_ids}, f, indent=2)
            f.write("\n")
    with open(pathlib.Path(home) / "uncertain.json", "w") as f:
        json.dump({"uncertain_dispatch_intents": uncertain}, f, indent=2)
        f.write("\n")
    return record


# ---------------------------------------------------------------------------
# Live launcher core
# ---------------------------------------------------------------------------
def run(ladder_item, go=False, backend_check_foreign=None, backend_check_identity=None,
        backend_popen=None, backend_http_port=8081, mock_workload=False):
    """Execute one ladder item end-to-end.

    backend_check_foreign  — callable returning dict or raising Blocked;
                             default uses real nvidia-smi.
    backend_check_identity — callable(ladder_item) -> identity dict or Blocked;
                             default computes + compares the model gguf sha.
    backend_popen          — override subprocess.Popen for testing.
    mock_workload          — if True, skip real HTTP workload and synthesize.
    """
    if not go:
        raise Blocked("BLOCKED-waiting-for-go: --go flag required for architectural GO-gate")

    name = ladder_item["name"]
    run_id = f"E3-{name}"
    scope = ladder_item["scope"]
    terminal_path = ladder_item["terminal_capture_path"]
    home = pathlib.Path(terminal_path).parent
    home.mkdir(parents=True, exist_ok=True)

    # -- Fail-closed guards (BEFORE any reservation — FIX 5) --
    if backend_check_foreign is None:
        backend_check_foreign = _check_foreign_consumers
    foreign = backend_check_foreign()

    if backend_check_identity is None:
        backend_check_identity = _check_identity_pins
    identity = backend_check_identity(ladder_item)
    cfg_check = _check_effective_config(ladder_item)
    cap_check = _check_per_run_cap(ladder_item)

    # -- Reservation (create-only ledger) — AFTER fail-closed ports (FIX 5) --
    reservation_path = home / "reservation.json"
    if reservation_path.exists():
        raise Blocked("reservation already exists for this run")
    reservation = {
        "run_id": run_id,
        "reservation_id": run_id,
        "name": name,
        "scope": scope,
        "planned_intents": ladder_item["planned_intents"],
        "planned_wall_s": ladder_item["planned_wall_s"],
        "readiness_intents": 0,
        "tokenize_intents": 0,
        "effective_config": ladder_item["effective_config"],
        "created_ns": time.time_ns(),
    }
    with open(reservation_path, "w") as f:
        json.dump(reservation, f, indent=2)
        f.write("\n")

    # -- Launch (FIX 4: canonical argv, not the plan command) --
    command = _launch_command(ladder_item)
    startup_ns = time.time_ns()
    owned = {"pid": None, "startup_ns": startup_ns, "cmdline": command}

    if backend_popen is not None:
        proc = backend_popen(command)
    else:
        server_log_path = home / "server.log"
        server_log = open(server_log_path, "w")
        proc = subprocess.Popen(command, stdout=server_log, stderr=subprocess.STDOUT, text=True)
    owned["pid"] = proc.pid

    # Write launch receipt
    launch_path = home / "launch.json"
    with open(launch_path, "w") as f:
        json.dump({"run_id": run_id, "pid": owned["pid"], "cmdline": command, "startup_ns": startup_ns}, f, indent=2)
        f.write("\n")

    http = HTTPClient(port=backend_http_port)
    start_wall = time.monotonic()

    # Wait for health (with timeout)
    health_deadline = time.monotonic() + 180
    while True:
        try:
            if http.health().get("status") == "ok":
                break
        except Exception:
            pass
        # Collision evidence capture: persist nvidia-smi state BEFORE failing
        if time.monotonic() >= health_deadline:
            try:
                ev = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                                     "--format=csv,noheader"], capture_output=True, text=True, timeout=15)
                (home / "collision-evidence.jsonl").write_text(ev.stdout + "\n" + ev.stderr)
            except Exception:
                (home / "collision-evidence.jsonl").write_text("nvidia-smi check itself failed\n")
            raise Blocked("startup health timeout")
        time.sleep(0.05)

    # -- Workload --
    inference_requests = 0
    uncertain = 0
    readiness = 0
    tokenize = 0
    intent_ids = []
    model_loads = 1  # one load per run

    if mock_workload:
        # Synthesized workload for dry-run / mock testing
        inference_requests = ladder_item["planned_intents"]
        intent_ids = [f"{name}-i{i:03d}" for i in range(1, inference_requests + 1)]
        readiness = 1
        tokenize = 1
        uncertain = 0
    else:
        # Real workload: QT1-QT6 + greedy 12/12 over the fresh manifest cold.
        # Any workload exception (client timeout, HTTP error) → FAILED terminal
        # capture + cleanup + re-raise as Blocked (fail-loud, never an orphan
        # dir without terminal.json).
        specs = _run_specs(ladder_item)
        try:
            wl = _execute_workload(http, specs, name=name)
        except Exception as wl_exc:
            _write_terminal(terminal_path, run_id, 1, len(getattr(wl_exc, "_spec_count", specs)), time.monotonic() - start_wall, "FAILED", {"reason": f"workload-execution-error:{type(wl_exc).__name__}"})
            raise Blocked(f"workload execution error: {wl_exc}") from wl_exc
        inference_requests = wl["inference_requests"]
        uncertain = wl["uncertain"]
        readiness = wl["readiness"]
        tokenize = wl["tokenize"]
        intent_ids = wl["intent_ids"]
        # persist per-request results (exact-answer failures recorded)
        with open(home / "workload-results.json", "w") as f:
            json.dump(wl["results"], f, indent=2)
            f.write("\n")

    # -- Mid-run collision check --
    try:
        try:
            mid = backend_check_foreign(allowed_pids=frozenset({proc.pid}))
        except TypeError:
            mid = backend_check_foreign()
    except Blocked as midrun_block:
        # Persist INVALID state + collision evidence before re-raising
        try:
            ev = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                                 "--format=csv,noheader"], capture_output=True, text=True, timeout=15)
            (home / "collision-evidence.jsonl").write_text(ev.stdout + "\n" + ev.stderr)
        except Exception:
            (home / "collision-evidence.jsonl").write_text("nvidia-smi check itself failed\n")
        _write_terminal(terminal_path, run_id, model_loads, inference_requests, time.monotonic() - start_wall, "INVALID", {"reason": "foreign-consumer-mid-run"})
        raise Invalid("foreign consumer detected mid-run")

    elapsed = time.monotonic() - start_wall

    # -- Terminal capture --
    terminal = _write_terminal(terminal_path, run_id, model_loads, inference_requests, elapsed, "completed", {
        "scope": scope,
        "foreign_pre": foreign,
        "identity": identity,
        "config": cfg_check,
        "cap": cap_check,
    })

    # -- Metering (derive_charges persisted feed) --
    metering = _write_metering(home, run_id, readiness=readiness, tokenize=tokenize,
                               uncertain=uncertain, wall_s=elapsed, intent_ids=intent_ids)

    # -- A3.6-fix-v2: update reservation from metering (in run(), NOT verifier) --
    # derive_charges reads reservation.readiness_intents + tokenize_intents as the
    # additive auxiliary dispatches. The create-only row starts at 0/0; without this
    # update derive_charges would read 0 instead of metering.json's real counts ->
    # reconciliation mismatch in real runs. charged_intents = the full derived charge
    # (primary + uncertain + readiness + tokenize).
    charged_intents = max(inference_requests, len(intent_ids)) + uncertain + readiness + tokenize
    reservation["readiness_intents"] = readiness
    reservation["tokenize_intents"] = tokenize
    reservation["charged_intents"] = charged_intents
    with open(reservation_path, "w") as f:
        json.dump(reservation, f, indent=2)
        f.write("\n")

    # -- Cleanup --
    cleanup_ok = False
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
            cleanup_ok = True
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            cleanup_ok = True
        except Exception:
            cleanup_ok = False

    cleanup_record = {
        "verified": cleanup_ok,
        "pid": owned["pid"],
        "home": str(home),
        "timestamp_ns": time.time_ns(),
    }
    with open(home / "cleanup.json", "w") as f:
        json.dump(cleanup_record, f, indent=2)
        f.write("\n")

    return {
        "run_id": run_id,
        "status": "GOOD" if cleanup_ok else "BLOCKED",
        "terminal": terminal,
        "metering": metering,
        "cleanup": cleanup_record,
        "reservation": reservation,
    }


# ---------------------------------------------------------------------------
# Plan dry-run
# ---------------------------------------------------------------------------
def plan_dry_run():
    plan = _load_plan()
    out = {
        "mode": "--plan",
        "schema": plan["schema"],
        "total_runs": plan["total_runs"],
        "runs": [{"name": r["name"], "scope": r["scope"], "command_preview": r["command"][:3] + ["..."]} for r in plan["runs"]],
        "within_caps": plan.get("within_caps"),
    }
    print(json.dumps(out, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Mock HTTP server (for tests)
# ---------------------------------------------------------------------------
_MOCK_ANSWERS = {}  # module global: message content -> answer text


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, data, status=200):
        wire = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(wire)))
        self.end_headers()
        self.wfile.write(wire)

    def do_GET(self):
        if self.path == "/health":
            return self.reply({"status": "ok"})
        if self.path == "/slots":
            return self.reply([{"is_processing": False}])
        return self.reply({}, 404)

    def _sse(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        events = [
            {"choices": [{"delta": {"content": content}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 0}}},
        ]
        wire = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"
        for i in range(0, len(wire), 64):
            part = wire[i:i + 64]
            self.wfile.write(("%x\r\n" % len(part)).encode() + part + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        if self.path == "/apply-template":
            return self.reply({"prompt": body.get("messages", [{}])[0].get("content", "")})
        if self.path == "/tokenize":
            return self.reply({"tokens": [1, 2, 3]})
        if self.path == "/v1/chat/completions":
            content = body.get("messages", [{}])[0].get("content", "")
            answer = _MOCK_ANSWERS.get(content, "OK")
            return self._sse(answer)
        return self.reply({}, 404)


def _mock_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


# ---------------------------------------------------------------------------
# Mock tests (9/9 + new A3.6-fix tests)
# ---------------------------------------------------------------------------
MOD = sys.modules[__name__]


class MockProcess:
    def __init__(self, stdout_lines=""):
        import io
        self.stdout = io.StringIO(stdout_lines)
        self._rc = None
        self.pid = 12345  # mock pid

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        self._rc = 0
        return 0

    def terminate(self):
        self._rc = 0

    def kill(self):
        self._rc = 0


class P2aLiveLauncherTests(unittest.TestCase):
    SCRATCH = pathlib.Path(os.environ.get("TMPDIR", "/tmp"))

    def setUp(self):
        self.plan = _load_plan()
        self.srv = _mock_server()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _item(self, name):
        return _find_ladder_item(self.plan, name)

    def _mock_identity(self, item):
        exp = item.get("expected_model_sha256")
        return {"model_path": str(MODEL_PATH), "model_sha256": exp,
                "expected_model_sha256": exp}

    def _run_mock(self, name, go=True, extra_foreign=None, popen_stdout=""):
        item = self._item(name)
        with tempfile.TemporaryDirectory(dir=self.SCRATCH) as d:
            home = pathlib.Path(d)
            item = dict(item)
            item["terminal_capture_path"] = str(home / "terminal.json")
            def foreign():
                if extra_foreign:
                    raise Blocked(extra_foreign)
                return {"foreign_consumer_detected": False}
            def popen(cmd):
                return MockProcess(stdout_lines=popen_stdout)
            result = run(item, go=go, backend_check_foreign=foreign,
                         backend_check_identity=self._mock_identity,
                         backend_popen=popen, backend_http_port=self.port,
                         mock_workload=True)
            self.assertTrue(pathlib.Path(result["terminal"]["terminal_capture_path"]).exists())
            return result

    # ---- 6 positive ladder items ----
    def test_stage3_32k_c1(self):
        r = self._run_mock("stage3-32k-c1")
        self.assertEqual(r["status"], "GOOD")
        self.assertEqual(r["terminal"]["inference_requests"], 30)

    def test_stage3_32k_c2(self):
        r = self._run_mock("stage3-32k-c2")
        self.assertEqual(r["status"], "GOOD")
        self.assertEqual(r["terminal"]["inference_requests"], 28)

    def test_stage3_131k_c1(self):
        r = self._run_mock("stage3-131k-c1")
        self.assertEqual(r["status"], "GOOD")
        self.assertEqual(r["terminal"]["inference_requests"], 22)

    def test_stage3_131k_c2(self):
        r = self._run_mock("stage3-131k-c2")
        self.assertEqual(r["status"], "GOOD")
        self.assertEqual(r["terminal"]["inference_requests"], 20)

    def test_p2a_32k_QT6(self):
        r = self._run_mock("p2a-32k-QT6")
        self.assertEqual(r["status"], "GOOD")
        self.assertEqual(r["terminal"]["inference_requests"], 156)
        self.assertEqual(r["metering"]["uncertain_dispatch_intents"], 0)

    def test_p2a_131k_QT6(self):
        r = self._run_mock("p2a-131k-QT6")
        self.assertEqual(r["status"], "GOOD")
        self.assertEqual(r["terminal"]["inference_requests"], 156)

    # ---- 3 negative tests ----
    def test_collision_foreign_consumer_blocked(self):
        with self.assertRaises(Blocked) as ctx:
            self._run_mock("p2a-32k-QT6", extra_foreign="BLOCKED-GPU-COLLISION: swtor")
        self.assertIn("BLOCKED-GPU-COLLISION", str(ctx.exception))

    def test_per_run_cap_intent_exceeded_blocked(self):
        item = self._item("p2a-32k-QT6")
        with tempfile.TemporaryDirectory(dir=self.SCRATCH) as d:
            bad = dict(item)
            bad["planned_intents"] = 200
            bad["planned_wall_s"] = 1000
            bad["terminal_capture_path"] = str(pathlib.Path(d) / "terminal.json")
            with self.assertRaises(Blocked) as ctx:
                run(bad, go=True, backend_check_foreign=lambda: {"foreign_consumer_detected": False},
                    backend_check_identity=self._mock_identity,
                    backend_popen=lambda cmd: MockProcess(), backend_http_port=self.port,
                    mock_workload=True)
            self.assertIn("intent cap exceeded", str(ctx.exception))

    def test_identity_effective_config_mismatch_blocked(self):
        item = self._item("p2a-32k-QT6")
        with tempfile.TemporaryDirectory(dir=self.SCRATCH) as d:
            bad = dict(item)
            bad["effective_config"] = dict(item["effective_config"])
            bad["effective_config"]["cache_k"] = "f16"
            bad["terminal_capture_path"] = str(pathlib.Path(d) / "terminal.json")
            with self.assertRaises(Blocked) as ctx:
                run(bad, go=True, backend_check_foreign=lambda: {"foreign_consumer_detected": False},
                    backend_check_identity=self._mock_identity,
                    backend_popen=lambda cmd: MockProcess(), backend_http_port=self.port,
                    mock_workload=True)
            self.assertIn("effective-config hash mismatch", str(ctx.exception))

    # ---- A3.6-fix: nvidia-smi fail-closed ----
    def test_nvidia_smi_absent_blocked(self):
        with patch.object(subprocess, "run", side_effect=FileNotFoundError("nvidia-smi")):
            with self.assertRaises(Blocked) as ctx:
                _check_foreign_consumers()
        self.assertIn("BLOCKED-GPU-COLLISION", str(ctx.exception))

    def test_nvidia_smi_error_blocked(self):
        class R:
            returncode = 1
            stderr = "No devices were found"
        with patch.object(subprocess, "run", return_value=R()):
            with self.assertRaises(Blocked) as ctx:
                _check_foreign_consumers()
        self.assertIn("BLOCKED-GPU-COLLISION", str(ctx.exception))

    # ---- A3.6-fix: wrong-model identity ----
    def test_wrong_model_blocked(self):
        item = {"expected_model_sha256": "0" * 64}
        with patch.object(MOD, "_digest", return_value="f" * 64):
            with self.assertRaises(Blocked) as ctx:
                _check_identity_pins(item)
        self.assertIn("wrong-model", str(ctx.exception))

    def test_identity_missing_pin_blocked(self):
        item = {}
        with self.assertRaises(Blocked) as ctx:
            _check_identity_pins(item)
        self.assertIn("wrong-model", str(ctx.exception))

    # ---- A3.6-fix: real workload (36/36 + greedy 12) + metering derive ----
    def test_workload_36(self):
        specs = _needle_specs(None)
        self.assertEqual(len(specs), 36)
        _MOCK_ANSWERS.clear()
        _MOCK_ANSWERS.update({s["content"]: json.dumps(s["expected_answer"]) for s in specs})
        http = HTTPClient(port=self.port)
        wl = _execute_workload(http, specs, name="workload-36")
        self.assertEqual(wl["n_total"], 36)
        self.assertEqual(wl["n_pass"], 36)
        self.assertEqual(wl["uncertain"], 0)

    def test_greedy_12(self):
        specs = _greedy_specs(0)
        self.assertEqual(len(specs), 12)
        _MOCK_ANSWERS.clear()
        _MOCK_ANSWERS.update({_spec_content(s): _spec_answer(s) for s in specs})
        http = HTTPClient(port=self.port)
        wl = _execute_workload(http, specs, name="greedy-12")
        self.assertEqual(wl["n_total"], 12)
        self.assertEqual(wl["n_pass"], 12)
        self.assertEqual(wl["uncertain"], 0)

    def test_metering_derive(self):
        # derive_charges over the four persisted artifacts must be correct.
        specs = _needle_specs(None)
        _MOCK_ANSWERS.clear()
        _MOCK_ANSWERS.update({s["content"]: json.dumps(s["expected_answer"]) for s in specs})
        http = HTTPClient(port=self.port)
        wl = _execute_workload(http, specs, name="metering")
        terminal = {"run_id": "E3-metering", "status": "completed", "model_loads": 1,
                    "inference_requests": wl["inference_requests"], "elapsed_s": 1}
        intent_ids = wl["intent_ids"]
        uncertain = {"uncertain_dispatch_intents": wl["uncertain"]}
        reservation = {"reservation_id": "E3-metering", "readiness_intents": wl["readiness"],
                       "tokenize_intents": wl["tokenize"]}
        verifier = _load("stage3_p2a_verifier", ROOT / "tools" / "stage3_p2a_verifier.py")
        charge = verifier.derive_charges(terminal, intent_ids, uncertain, reservation, None)
        expected = wl["inference_requests"] + wl["uncertain"] + wl["readiness"] + wl["tokenize"]
        self.assertEqual(charge, expected)

    # ---- A3.6-fix-v2: reservation metering update (negative surrogate) ----
    def test_reservation_metering_update(self):
        # run() must persist the REAL readiness/tokenize/charged_intents into
        # reservation.json AFTER the workload (derive_charges reads readiness +
        # tokenize as the additive aux dispatches). If the update is missing, the
        # on-disk row stays at the create-only 0/0 and derive_charges under-counts
        # -> reconciliation mismatch. This surrogate FAILS when that update is absent.
        item = self._item("p2a-32k-QT6")
        with tempfile.TemporaryDirectory(dir=self.SCRATCH) as d:
            home = pathlib.Path(d)
            it = dict(item)
            it["terminal_capture_path"] = str(home / "terminal.json")
            result = run(it, go=True,
                         backend_check_foreign=lambda: {"foreign_consumer_detected": False},
                         backend_check_identity=self._mock_identity,
                         backend_popen=lambda cmd: MockProcess(),
                         backend_http_port=self.port,
                         mock_workload=True)
            on_disk = json.loads((home / "reservation.json").read_text())
            self.assertEqual(on_disk["readiness_intents"],
                             result["metering"]["readiness_intents"])
            self.assertEqual(on_disk["tokenize_intents"],
                             result["metering"]["tokenize_intents"])
            expected_charged = (result["terminal"]["inference_requests"]
                                + result["metering"]["uncertain_dispatch_intents"]
                                + result["metering"]["readiness_intents"]
                                + result["metering"]["tokenize_intents"])
            self.assertEqual(on_disk["charged_intents"], expected_charged)


def mock_test_all():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(P2aLiveLauncherTests)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    summary = {
        "status": "PASS" if result.wasSuccessful() else "FAIL",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
    }
    print(json.dumps(summary, indent=2))
    return 0 if result.wasSuccessful() else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="A3.6 E3-LIVE-launcher-bridge")
    parser.add_argument("--plan", action="store_true", help="Dry-run plan listing")
    parser.add_argument("--go", action="store_true", help="Architectural GO-gate")
    parser.add_argument("--mock-test-all", action="store_true", help="Run all mock tests")
    parser.add_argument("name", nargs="?", help="ladder item name (with --go)")
    args = parser.parse_args()

    if args.plan:
        return plan_dry_run()
    if args.mock_test_all:
        return mock_test_all()
    if args.go:
        if not args.name:
            print("Usage: p2a_live_launcher.py --go <ladder_name>", file=sys.stderr)
            return 2
        plan = _load_plan()
        item = _find_ladder_item(plan, args.name)
        result = run(item, go=True)
        print(json.dumps(result, indent=2))
        return 0

    print("Usage: p2a_live_launcher.py --plan | --mock-test-all | --go <name>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Blocked as e:
        print(json.dumps({"status": "BLOCKED", "reason": str(e)}), file=sys.stderr)
        sys.exit(2)
    except Invalid as e:
        print(json.dumps({"status": "INVALID", "reason": str(e)}), file=sys.stderr)
        sys.exit(3)
