# Claims → evidence matrix (D0.2 correction, 2026-11-23)

Every public claim in the 5070 study (`README.md`, results cards) is mapped to a raw source
and an evidence class. Classes: **MEASURED** (direct ledger/config reading), **DERIVED**
(computed from the same measured number, not an independent observation), **DIAGNOSTIC**
(uncontrolled or identity-unverified comparison), **UNCONFIRMED** (assumption/estimate with an
unverified premise), **SPEC/UNMEASURED** (spec-sheet term with no local measurement). Source
paths are relative to the repo root `inference-optimisation-5070-laptop/`; `../` paths point to
sibling repos under `llm-mallit/`. The ledger and `evidence/` raw files are immutable; this
matrix is a correction addendum, not an overwrite.

| # | Public claim (README/card) | Raw source (path:line) | Class | Corrected wording |
|---|---|---|---|---|
| 1 | c1 decode "n=4" / "median-of-6" (README:29; cards) | `evidence/ledger.jsonl:1-2` — `c1_all` has 4 samples (baseline 4, direct 4); `ledger:4` `c1_all` has 6 samples | **MEASURED (n=4 stored sample count); comparison DIAGNOSTIC** | "n=4" (protocol wanted ≥6; `evidence/PLAN.md:75` records reps=4). The final `ledger:4` run is **n=6**, not n=4. |
| 2 | "prefill 4096" (README:30; cards) | `evidence/ledger.jsonl:1-4` — `prefill_4096_tokens=2839` | **MEASURED (2839)** | "2839 real tokenizer tokens (nominal 4096)" — now shown on both cards. |
| 3 | "+15.1% / +11.9% wrapper win" (README:29-30, finding 1) | `evidence/ledger.jsonl:1-2` (raw 23.82/27.41, 623.7/698.1); `../inference-opt-9b/PLAN.md:24` — UNCONTROLLED N=4 | **DIAGNOSTIC** (uncontrolled comparison); percent differences **DERIVED** | "uncontrolled n=4 comparison, not a causal wrapper win". Denominators named: decode +15.0714% of baseline (23.82→27.41) = +13.0974% of direct; prefill +11.9288% of baseline (623.7→698.1). |
| 4 | "FA rejected as a lever (measured equality)" (README:57-61) | `evidence/ledger.jsonl:2-3` — FA-off vs FA-on; FA-on `startup_s=0.0` | **DIAGNOSTIC** | "candidate identity unverified (startup_s=0.0); FA comparison diagnostic; 0.26% DERIVED = abs(27.34 - 27.41)/27.41 = 0.2554% (DIAGNOSTIC-limited comparison)" |
| 5 | "~282 GB/s SM-read ceiling" (README:27-29, 38-45 finding 1; cards) | `evidence/THEORY.md:5-6` — 282 = 27.41 × 10.28 GB (same decode number) | **DERIVED** | "~282 GB/s derived from the same measured decode rate, not an independent ceiling" |
| 6 | "128K dense 27B ~18.8 GB, physically impossible" (README:63-69, finding 4; cards) | `evidence/THEORY.md:17-19` — KV layout UNCONFIRMED, ~17-34 GB KV + 10.28 GB weights | **UNCONFIRMED** | "dense-KV-layout estimate UNCONFIRMED (~17-34 GB KV); exact 18.8 GB figure not established" |
| 7 | "two new measurement findings contributed back" (README:6) | `../registry-sub-arete/evidence/REPORT.txt:9` — ACCEPTANCE FAIL; no remote/PR | **UNCONFIRMED → prepared** | "two findings prepared for contribution (bundle registry-sub-arete, not yet submitted)". "prepared" is a documented workflow claim, not a measurement class; contribution status is UNCONFIRMED (no verified submission receipt). |
| 8 | `cudaMemcpy` D2D 156.3 / 78.2 GB/s (README:49-55, finding 2) | `evidence/THEORY.md:7` + `evidence/PLAN.md:23-24` (stored summary) | **MEASURED (stored summary) at most; unit/normalization UNCONFIRMED** | `tools/bandwidth_probe.py:56-58` computes `moved = n_iters*N` (copied bytes, not read+write) and prints "GB/s gross (read+write)" after `/2**30` (GiB, not decimal GB); `:63` halves it to "one-way". This numeric naming bug is documented here, not fixed in code; no new numbers. |
| 9 | needle 3/3 exact @5102 tok, TTFT 7.3 s (README:32) | `evidence/ledger.jsonl:4` — `retrieval: "3/3"`, `long.ttft_s: 7.3` | **MEASURED (stored summary)** | (unchanged; no independent re-scoring performed). Needle TTFT 7.3 s is distinct from short-prompt TTFT (row 10). |
| 10 | short-prompt TTFT per run (README:31; cards) | `evidence/ledger.jsonl:1-4` — `ttft_short_ms` 260.1 / 239.4 / 208.7 / 227.9 | **MEASURED (stored summary)** | Four distinct short-prompt TTFT measurements across runs; 239.4 ms is `ledger:2` (fa-off), NOT the needle's TTFT. Needle TTFT 7.3 s is `ledger:4` `.long` — a different measurement class and run. |
| 11 | needle context fraction "62% / 66%" (README:32; cards) | `evidence/ledger.jsonl:4` (`long.prompt_tokens` 5102) ÷ `evidence/PLAN.md:4` (working context 8192) = **62.2803%** | **DERIVED** | "5102 of 8192 = 62% ctx". Historical `evidence/PLAN.md:78` still says 66% — kept as an immutable historical line, corrected here to 62%. |
| 12 | prefill practical ceiling 1265 tok/s; achieved-throughput share 49-55% (README:30; cards) | `evidence/THEORY.md:14-16` — 1265 = spec peak-TFLOPS × 0.5 MFU; peak-TFLOPS UNMEASURED | **SPEC/UNMEASURED** | "1265 tok/s spec-derived practical ceiling (peak-TFLOPS UNMEASURED)". Achieved-throughput share of the SPEC estimate, NOT remaining headroom: 623.7/1265 = 49.3% (ollama), 698.1/1265 = 55.2% (llama.cpp). |
| 13 | KV memory: ~131 KiB/tok, ~1.05 GB @8192, ~34 GB @262K (results-card.html:73; cards) | `evidence/THEORY.md:17-19` — KV layout UNCONFIRMED, ~17-34 GB KV | **UNCONFIRMED** | "KV-per-token and pool estimates are UNCONFIRMED KV-layout assumptions (~17-34 GB KV); not an established physical bound". |
| 14 | lab-gate diagnostic run: context 6384 tok / needle 58213 / 11.5 s; speed 27.5 tok/s / 825 tok / 30 s | `../registry-sub-arete/evidence/REPORT.txt:5-6` + `evidence/PLAN.md:55-60` (E8) | **MEASURED (stored summary)** | Distinct from the ledger 5102-tok needle test (row 9). All six diagnostic gates PASS; official acceptance FAIL (row 7). |

Notes:
- `PLAN.md:75` in row 1 means `evidence/PLAN.md:75` (the repo-root `PLAN.md` does not exist).
- Historical README line anchors moved in D0.2: the 128K/18.8 GB claim now sits at
  README:63-69 (claim 63-65, caveat 66-69), and FA at README:57-61 (the full finding 3 section).
- The context-fraction figure is DERIVED arithmetic (5102/8192 = 62.2803%), not a new
  measurement.
