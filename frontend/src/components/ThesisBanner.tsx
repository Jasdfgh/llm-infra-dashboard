"use client";

import React from "react";

interface Summary {
  total_gaps: number;
  project_count: number;
  critical: number;
  high: number;
  closing: number;
  widening: number;
  feature_count: number;
  bug_count: number;
  pending_updates: number;
}

type Tone = "win" | "in-progress" | "behind" | "wait";

interface ScoreQuadrant {
  head: string;
  tone: Tone;
  glyph: string;
  verdict: string;
  body: React.ReactNode;
}

const SCORECARD: ScoreQuadrant[] = [
  {
    head: "FP8 single-node + FP8 disagg SGLang",
    tone: "win",
    glyph: "✓",
    verdict: "parity",
    body: (
      <>
        MI355X matches / slightly leads B200 on{" "}
        <a
          href="https://inferencex.semianalysis.com/blog/inferencex-v2-nvidia-blackwell-vs-amd-vs-hopper"
          target="_blank"
          rel="noopener noreferrer"
        >
          InferenceX v2
        </a>
        . FP8 single-node and FP8 disagg SGLang are AMD&apos;s confirmed wins for 2026.
      </>
    ),
  },
  {
    head: "FP4 + disagg + wide-EP composability",
    tone: "behind",
    glyph: "⟳",
    verdict: "~6 mo behind",
    body: (
      <>
        Individual pieces work; composed they break. DPA =&nbsp;0% accuracy,
        MTP truncates — AMD&apos;s #1 open technical gap per SemiAnalysis.
      </>
    ),
  },
  {
    head: "Rack-scale interconnect (NVLink-72 class)",
    tone: "wait",
    glyph: "◯",
    verdict: "waiting HW",
    body: (
      <>
        MI455X / Helios (UALoE72) ships H2&nbsp;2026 samples, Q2&nbsp;2027 volume.
        Until then AMD runs cross-node on IB ~50–100 GB/s vs NVLink 900 GB/s.
      </>
    ),
  },
  {
    head: "Day-0 popular-model completeness",
    tone: "in-progress",
    glyph: "⟳",
    verdict: "partial",
    body: (
      <>
        Gemma 4 / GPT-OSS / Qwen3.5 / DeepSeek V3.2 claim day-0 — but only on
        baseline paths. Fork-vs-main deltas and AITER &quot;planned&quot; still open.
      </>
    ),
  },
];

interface RoadmapMarker {
  year: string;
  position: number; // 0-100 along the axis
  amd: string;
  nv: string;
  state: "past" | "today" | "future";
}

const ROADMAP: RoadmapMarker[] = [
  { year: "2024", position: 5, amd: "MI300X", nv: "H100", state: "past" },
  { year: "2025", position: 32, amd: "MI325X", nv: "H200", state: "past" },
  { year: "2026", position: 60, amd: "MI355X", nv: "B200 / B300", state: "today" },
  { year: "2027", position: 92, amd: "MI455X · Helios", nv: "Rubin NVL144", state: "future" },
];

export function ThesisBanner({ summary }: { summary: Summary | null }) {
  return (
    <div className="thesis-banner fade-in">
      <div className="thesis-header">
        <div>
          <div className="thesis-title">
            <span className="tt-amd">AMD</span>
            <span style={{ color: "var(--muted)", fontWeight: 400, margin: "0 6px" }}>
              on open-source LLM serving vs
            </span>
            <span className="tt-nv">NVIDIA</span>
            <span style={{ color: "var(--muted)", fontWeight: 500 }}> · 2026</span>
          </div>
          <div className="thesis-subtitle" style={{ marginTop: 2 }}>
            Four strategic fronts, fifteen tracked issues. Below shows where AMD
            is winning, catching up, waiting on hardware, or executing partially.
          </div>
        </div>
      </div>

      {/* Four-quadrant scorecard */}
      <div className="scorecard-grid">
        {SCORECARD.map(q => (
          <div key={q.head} className={`scorecard sc-tone-${q.tone}`}>
            <div className="scorecard-head">{q.head}</div>
            <div className="scorecard-status">
              <div className="scorecard-glyph">{q.glyph}</div>
              <div className="scorecard-verdict">{q.verdict}</div>
            </div>
            <div className="scorecard-body">{q.body}</div>
          </div>
        ))}
      </div>

      {/* Hardware roadmap strip */}
      <div className="roadmap-strip">
        <div className="roadmap-axis">
          <div className="roadmap-line" />
          {ROADMAP.map(m => (
            <div
              key={m.year}
              className="roadmap-marker"
              style={{ left: `${m.position}%` }}
            >
              <div className="roadmap-marker-year">{m.year}</div>
              <div
                className={`roadmap-marker-dot${m.state === "today" ? " today" : ""}${m.state === "future" ? " future" : ""}`}
              />
              {m.state === "today" && (
                <div className="roadmap-marker-today-label">today</div>
              )}
              <div
                className="roadmap-marker-hw"
                style={{ marginTop: m.state === "today" ? 2 : 6 }}
              >
                <span style={{ color: "var(--amd)" }}>{m.amd}</span>
                <span style={{ color: "var(--muted)", margin: "0 4px" }}>·</span>
                <span style={{ color: "var(--nvidia)" }}>{m.nv}</span>
              </div>
            </div>
          ))}
        </div>

        {summary && (
          <div className="roadmap-counter-legend">
            <span className="stat">
              <strong>{summary.total_gaps}</strong> tracks
            </span>
            <span className="stat">
              <strong style={{ color: "var(--accent)" }}>{summary.feature_count}</strong>{" "}
              feature gaps
            </span>
            <span className="stat">
              <strong style={{ color: "var(--red)" }}>{summary.bug_count}</strong> bugs
            </span>
            <span className="stat">
              <strong style={{ color: "var(--red)" }}>{summary.critical}</strong> critical
            </span>
            <span className="stat">
              <strong style={{ color: "var(--green)" }}>{summary.closing}</strong> closing ↑
            </span>
            {summary.pending_updates > 0 && (
              <span className="stat">
                <strong style={{ color: "var(--accent)" }}>{summary.pending_updates}</strong>{" "}
                pending review
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
