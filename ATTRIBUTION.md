# Attribution & provenance

## Methodology source
- [`inference-optimisation-skill`](https://github.com/sybil-solutions/inference-optimisation-skill)
  by [Sybil Solutions](https://github.com/sybil-solutions) (MIT License).
  Used as the methodology source (ceilings-first, one-change-per-experiment, interleaved
  controls, median-of-N, stability gates, publish-with-evidence).
- Their [`local-ai-registry`](https://github.com/sybil-solutions/local-ai-registry) recipe concept
  (one validated recipe per machine, evidence attached) motivates this artifact's format.

## What is original in this repository
- All measurements (ledger.jsonl) -- taken on the hardware named in README.md, 2026-11-17.
- The two measurement findings: (1) wrapper tax — decode +15.1 % of baseline / +13.1 % of direct,
  prefill +11.9 % of baseline (ollama vs llama.cpp direct with the identical GGUF) — an
  uncontrolled n=4 comparison, not a controlled win; (2)
  `cudaMemcpy D2D != SM-read bandwidth` on this GPU (CE 78 GB/s one-way vs ~282 GB/s effective
  SM-read during decode) — stored summary; unit/normalization UNCONFIRMED (raw run log absent);
  282 is model-derived via the 10.28 GB/token premise, not a direct SM traffic measurement.
- Contribution status: neither additional finding has been submitted upstream (state
  `prepared`, not submitted/merged). The `registry-sub-arete` bundle is ACCEPTANCE FAIL
  (its `evidence/REPORT.txt:9` records the refusal), so it is not an official-ready recipe.
- The visibility/audit layer: PLAN.md gate log, THEORY.md derivations, the interactive
  blind-spot results card.

## What is NOT here
No numbers imported from the skill's README; its claims were read, not replicated. This project
does not modify or redistribute the skill; the skill's own repository remains the source.

## Model & engine provenance
- Weights: `bartowski/NousResearch_Arete-27B-GGUF` IQ2_M, HF commit `9762c850`,
  file sha256 `73d7c1c97f6bebc6...`
- Engine: llama.cpp build `b12359` (v1.0.0-dev, CUDA 13.5 CI tarball),
  `llama-server` binary sha256 `2085180fafb6...`
- Baseline engine for comparison: ollama 0.40.15 (system service)
