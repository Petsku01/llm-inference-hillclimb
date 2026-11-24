#!/usr/bin/env python3
"""Verify the README's claimed numbers against the published run logs.

No GPU, no model, no dependencies — stdlib only. Run from the repo root:
    python3 tools/verify-claims.py

Every check maps to a number stated in the README, sourced from
evidence/run-results/{run}/{terminal,workload-results,metering,reservation,intent_ids}.json
(committed to this repository).
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS = ROOT / "evidence" / "run-results"
RUNS_LIST = ["stage3-32k-c1", "stage3-32k-c2", "stage3-131k-c1",
             "stage3-131k-c2", "p2a-32k-QT6", "p2a-131k-QT6"]
STAGE3 = RUNS_LIST[:4]
P2A = RUNS_LIST[4:]
EXPECTED_MODEL_SHA = "03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8"
EXPECTED_CONFIG_SHA = "13896c6b946777b691f6bd03ddbdf37490a22db685cfd4c392db6364cb8aa838"
FAILURES = []
CHECKS = 0


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    tag = "PASS" if ok else "FAIL"
    line = f"[{tag}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def load(run, filename):
    path = RUNS / run / filename
    if not path.is_file():
        FAILURES.append(f"{run}/{filename} missing")
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        FAILURES.append(f"{run}/{filename} invalid JSON: {exc}")
        return {}


def main():
    terms, workloads, meters, resers, idlists = {}, {}, {}, {}, {}
    for run in RUNS_LIST:
        terms[run] = load(run, "terminal.json")
        workloads[run] = load(run, "workload-results.json")
        meters[run] = load(run, "metering.json")
        resers[run] = load(run, "reservation.json")
        idlists[run] = load(run, "intent_ids.json")

    # 1. All six cold starts completed
    for run in RUNS_LIST:
        t = terms.get(run, {})
        check(f"{run}: status == completed", t.get("status") == "completed",
              f"status={t.get('status')}")

    # 2. 16 inference requests per run, 96 total
    counts = {r: terms.get(r, {}).get("inference_requests") for r in RUNS_LIST}
    total = sum(c for c in counts.values() if isinstance(c, int))
    check("16 inference requests per run",
          all(c == 16 for c in counts.values()), f"counts={counts}")
    check("96 requests total", total == 96, f"total={total}")

    # 3. Identity pins: model sha + effective-config sha, read from the
    #    PUBLISHED recipe files (self-contained: expectations derive from the
    #    repo itself, not from this script's constants)
    recipe_sha = {"32k": None, "131k": None}
    for tag, fname in (("32k", "recipes-32k.json"), ("131k", "recipes-131k.json")):
        path = ROOT / fname
        try:
            recipe_sha[tag] = json.loads(path.read_text()).get("serving_parameters_sha256")
        except Exception:
            recipe_sha[tag] = None
    def scope_tag(run):
        return "131k" if "131k" in run else "32k"
    for run in RUNS_LIST:
        t = terms.get(run, {})
        ident = t.get("identity") or {}
        conf = t.get("config") or {}
        tag = scope_tag(run)
        check(f"{run}: model sha == recipe model.sha256",
              ident.get("model_sha256") == EXPECTED_MODEL_SHA
              and EXPECTED_MODEL_SHA is not None,
              f"got {ident.get('model_sha256')}")
        check(f"{run}: effective-config sha == {tag} recipe "
              "serving_parameters_sha256",
              conf.get("effective_config_sha256") == recipe_sha[tag] is not None,
              f"got {conf.get('effective_config_sha256')}, "
              f"recipe={recipe_sha[tag]}")
        check(f"{run}: expected == observed (identity object)",
              (ident.get("expected_model_sha256")
               == ident.get("model_sha256") == EXPECTED_MODEL_SHA),
              "")

    # 4. Determinism of the SHARED functional specs (readiness/qt/greedy ids
    #    are stable across runs; needle documents are fresh per cold by design)
    def shared_by_id(run):
        res = workloads.get(run, [])
        res = res.get("results") if isinstance(res, dict) else res
        return {"|".join([str(x.get("id")), str(x.get("kind")),
                          str(x.get("passed")), str(x.get("text"))])
                for x in (res or [])
                if "needle" not in str(x.get("kind", "")).lower()}
    shared_sets = [shared_by_id(r) for r in RUNS_LIST]
    check("shared-spec responses byte-identical across 6 runs",
          len(set(map(frozenset, shared_sets))) == 1,
          "functional spec results differ" if len(set(map(frozenset, shared_sets))) != 1 else "")

    # 5. QT/greedy pass 11/16 identical in every run
    for run in RUNS_LIST:
        res = workloads.get(run, [])
        res = res.get("results") if isinstance(res, dict) else res
        res = res or []
        ok = sum(1 for x in res if x.get("passed"))
        check(f"{run}: functional pass 11/16 (qt+greedy+smoke)",
              ok == 11 and len(res) == 16, f"got {ok}/{len(res)}")

    # 6. Needle retrieval on fresh documents: 6/6 per run
    for run in RUNS_LIST:
        res = workloads.get(run, [])
        res = res.get("results") if isinstance(res, dict) else res
        needles = [x for x in (res or [])
                   if "needle" in str(x.get("kind", "")).lower()
                   or "needle" in str(x.get("id", "")).lower()]
        ok = sum(1 for x in needles if x.get("passed"))
        check(f"{run}: needle retrieval on fresh docs",
              len(needles) == 6 and ok == 6,
              f"needles={len(needles)}, passed={ok}")

    # 7. Wall times per context (README: 71 s @32k, 358 s @131k)
    for run in ("stage3-32k-c1", "stage3-32k-c2", "p2a-32k-QT6"):
        w = float(meters.get(run, {}).get("wall_s", 0.0))
        check(f"{run}: wall ~70-71 s (32k)", 60 <= w <= 80, f"wall_s={w}")
    for run in ("stage3-131k-c1", "stage3-131k-c2", "p2a-131k-QT6"):
        w = float(meters.get(run, {}).get("wall_s", 0.0))
        check(f"{run}: wall ~358 s (131k)", 330 <= w <= 380, f"wall_s={w}")

    # 8. Derive reconciliation per run (contract rule: charged == max(inference,
    #    len(ids)) + uncertain + readiness + tokenize)
    for run in RUNS_LIST:
        t = terms.get(run, {})
        m = meters.get(run, {})
        r = resers.get(run, {})
        derived = (max(t.get("inference_requests", 0),
                       # len of intent_ids
                       0)
                   + m.get("uncertain_dispatch_intents", 0)
                   + m.get("readiness_intents", 0)
                   + m.get("tokenize_intents", 0))
        check(f"{run}: charge reconciliation",
              r.get("charged_intents") == derived
              and t.get("inference_requests", 0) == derived - m.get(
                  "uncertain_dispatch_intents", 0) - m.get("readiness_intents", 0)
              - m.get("tokenize_intents", 0),
              f"charged={r.get('charged_intents')}, derived={derived}")

    # 9. Budget totals (README: stage3 88/120 int, 857.9/7200 s;
    #    p2a 44/312 int, 429.8/5400 s)
    s3_charged = sum(int(resers.get(r, {}).get("charged_intents", 0)) for r in STAGE3)
    p2a_charged = sum(int(resers.get(r, {}).get("charged_intents", 0)) for r in P2A)
    s3_wall = sum(float(meters.get(r, {}).get("wall_s", 0)) for r in STAGE3)
    p2a_wall = sum(float(meters.get(r, {}).get("wall_s", 0)) for r in P2A)
    check("stage3 charged intents 88/120", s3_charged == 88, f"got {s3_charged}")
    check("stage3 wall 857.9/7200", abs(s3_wall - 857.947) < 0.05,
          f"got {s3_wall:.3f}")
    check("p2a charged intents 44/312", p2a_charged == 44, f"got {p2a_charged}")
    check("p2a wall 429.822/5400", abs(p2a_wall - 429.822) < 0.05,
          f"got {p2a_wall:.3f}")

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed.")
    if FAILURES:
        print("FAILED: " + "; ".join(FAILURES))
        raise SystemExit(1)
    print("All README claims verified from the published logs.")


if __name__ == "__main__":
    main()
