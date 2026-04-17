# Expert Knowledge Base

Expert-curated facts about AMD vs NVIDIA support on open-source LLM infrastructure.
The agent uses this as a guide for targeted evidence collection, NOT as open-ended exploration.

## Scope (MVP)

Only two architectural layers are active tracks:

- **L1 Model** — does the model run correctly and serving-viably on AMD?
- **L2 Serving Architecture** — can AMD serve at production scale (disagg, wideEP, parallelism)?

L3 (Kernel / Quantization) and L4 (Engineering / Ecosystem) are intentionally
de-scoped: kernel-level items surface through the AITER library roadmap and
are easier to track upstream; engineering items (CI capacity, docker image
quality) are org-scale problems that do not map to a single GitHub issue.
Context about these layers is preserved in each project YAML's
`background_context` section.

## Within each active layer, tracks are split by type

- **features** — capability is missing or incomplete on AMD (e.g. "DWDP
  parallelism not yet available on ROCm").
- **bugs**     — capability exists but produces wrong output, crashes, or
  regresses (e.g. "FP8 serving is slower than BF16 on MI355X").

Both subsets are first-class citizens in the dashboard — bugs are surfaced
as prominently as features, not buried inside features.

## File structure

- `hardware.yaml` — hardware generation mapping, performance baselines, confirmed macro gaps
- `projects/{project_id}.yaml` — per-project YAML in the structure below
- `operator_coverage.yaml` — AMD vs NVIDIA operator coverage matrix (backend reference)
- `model_precision.yaml` — popular model → production serving precision mapping
- `tracking_config.yaml` — per-project agent search hints and tracked labels
- `analysis_methodology.yaml` — the seven-step deep gap analysis process

## Project YAML structure

```yaml
project:
  id: ...
  name: ...
  repo: owner/repo
  category: inference
  expert_summary: |
    Narrative whole-picture assessment from the AMD roadmap lens.

performance:
  ...  # headline performance comparisons (MI300X/MI355X vs H100/H200/B200)

known_gaps:
  layer1_model:
    features:
      - id: ...
        type: feature            # feature | bug
        title: ...               # concise display title
        gap: ...                 # one-liner gap statement
        severity: critical|high|medium|low
        status: active
        discovered_at: YYYY-MM-DD
        last_verified: YYYY-MM-DD
        technical_detail: |
          Multi-paragraph deep technical analysis with:
          - Why it is a bug vs feature (classification rationale)
          - Why AMD should care (roadmap lens)
          - Evidence links inline
        sources:
          - url: https://github.com/...
            date: YYYY-MM-DD
            label: "Issue #XXX — short description"
        tracking_issue: https://github.com/...
        trajectory: "closing|stable|recurring|widening — with reasoning"
        updates:
          - date: YYYY-MM-DD
            source: agent|expert
            type: new_comment|new_pr|status_change
            url: ...
            summary: ...
            reviewed: true|false
    bugs:
      - ... same shape ...

  layer2_serving:
    features: [...]
    bugs:     [...]

agent_focus:
  priority_searches: [...]   # queries agent uses to look for updates
  watch_for: [...]           # signals that matter
  monitor_benchmarks: [...]  # external dashboards to watch

background_context:
  descoped_l3_kernel: [...]     # L3 context kept but NOT active tracks
  descoped_l4_engineering: [...]
  known_nvidia_exclusive_unreachable: [...]  # gaps that will never close
```

## Quality bar for each active track

Every track must have:

1. A clear bug-vs-feature classification (with a one-line rationale in
   `technical_detail`).
2. A **why AMD should care** paragraph grounded in the AMD roadmap
   (Advancing AI, ROCm 7.x, MI355X/MI400).
3. Sources with dates and labels — prefer GitHub issues/PRs, benchmark
   links, and official blogs.
4. A tracking issue the agent can poll for updates.
5. A trajectory label explaining whether the gap is closing, stable,
   recurring, or widening.

Tracks that do not meet all five bars should go into `background_context`
until they do.

## Adding a new track

1. Identify the AMD roadmap priority it belongs to (MoE inference, long
   context, FP4 composability, hybrid attention, etc.).
2. Decide bug vs feature: does the capability exist on AMD? If yes and
   it's broken → bug; if no → feature.
3. Place it in the correct layer's `features` or `bugs` list.
4. Fill every required field. Don't ship with `TODO` placeholders.
5. Add at least one high-signal `priority_search` for the agent so the
   track can auto-refresh.
