"use client";

import Link from "next/link";
import React, { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { parseReasoning, renderInlineLinks, shortLabel } from "../../lib/reasoning";
import { VizRenderer } from "../../components/viz";
import type { GapVisual, TimelineVisual, TimelineEvent, PrState } from "../../components/viz/types";

interface GapUpdate {
  date?: string;
  source?: string;
  type?: string;
  url?: string;
  summary?: string;
  reviewed?: boolean;
}

interface SourceObj {
  url?: string;
  date?: string;
  label?: string;
  state?: PrState;
}

interface GapData {
  id?: string;
  title?: string;
  gap?: string;
  severity?: string;
  trajectory?: string;
  technical_detail?: string;
  notes?: string;
  bottom_line?: string | null; // NEW — expert-authored "where we are today"
  project_id?: string;
  project_name?: string;
  layer?: string;
  gap_type?: "feature" | "bug";
  sources?: (string | SourceObj)[] | null;
  tracking_issue?: string | null;
  tracking_issues?: string[] | null;
  feature?: string | null;
  model?: string | null;
  feature_detail?: {
    expert_notes?: string;
    evidence?: { url?: string; type?: string; summary?: string }[];
  } | null;
  project_summary?: string | null;
  discovered_at?: string | null;
  last_verified?: string | null;
  status?: string | null;
  updates?: GapUpdate[] | null;
  visual?: GapVisual | null;
  error?: string;
}

const LAYER_LABELS: Record<string, string> = {
  layer1_model: "L1 · Model Layer",
  layer2_serving: "L2 · Serving Architecture",
};

function buildLinkCtx(data: GapData) {
  return {
    sources: data.sources,
    trackingIssue: data.tracking_issue ?? null,
    trackingIssues: data.tracking_issues ?? null,
  };
}

function sevIndicator(s?: string): string {
  if (s === "critical") return "severity-critical";
  if (s === "high") return "severity-high";
  return "severity-medium";
}

function sevBadge(s?: string): string {
  if (s === "critical") return "badge-critical";
  if (s === "high") return "badge-high";
  return "badge-medium";
}

function trajBadge(t?: string): { text: string; cls: string } | null {
  if (!t) return null;
  const l = t.toLowerCase();
  if (l.includes("closing") || l.includes("improving"))
    return { text: "Closing ↑", cls: "traj-closing-badge" };
  if (l.includes("widening"))
    return { text: "Widening ↓", cls: "traj-widening-badge" };
  if (l.includes("recurring"))
    return { text: "Recurring ↻", cls: "traj-widening-badge" };
  return { text: "Stable", cls: "traj-stable-badge" };
}

function stripPrefix(text: string, re: RegExp): string {
  return text.replace(re, "").trimStart();
}

/**
 * Build a Timeline visual from `sources[].date` + `updates[].date`.
 * Source-level `state` (merged/open/closed/draft) propagates to the event's
 * `prState` so the timeline shows merge status inline.
 */
function timelineFromMeta(data: GapData): TimelineVisual | null {
  const events: TimelineEvent[] = [];

  if (data.discovered_at) {
    events.push({
      date: data.discovered_at,
      label: "Discovered",
      status: "past",
    });
  }

  (data.sources || []).forEach(s => {
    if (typeof s === "object" && s && s.date) {
      const url = s.url;
      events.push({
        date: s.date,
        label: s.label || (url ? shortLabel(url) : "Source"),
        status: "past",
        url: url,
        prState: s.state,
      });
    }
  });

  (data.updates || []).forEach(u => {
    if (!u.date) return;
    events.push({
      date: u.date,
      label: u.summary || "Agent-discovered update",
      note: u.type,
      status: u.reviewed ? "past" : "pending_review",
      url: u.url,
    });
  });

  if (data.last_verified) {
    events.push({
      date: data.last_verified,
      label: "Last verified",
      status: "current",
    });
  }

  if (events.length === 0) return null;

  const key = (e: TimelineEvent) => `${e.date}|${e.label}`;
  const seen = new Set<string>();
  const deduped = events.filter(e => {
    const k = key(e);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  deduped.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));

  return {
    type: "timeline",
    events: deduped,
  };
}

function GapDetailInner() {
  const sp = useSearchParams();
  const project = sp.get("project") ?? "";
  const layer = sp.get("layer") ?? "";
  const type = sp.get("type") ?? "";
  const index = sp.get("index") ?? "";

  const [data, setData] = useState<GapData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!project || !layer || index === "") {
      setLoading(false);
      return;
    }
    setLoading(true);
    const url = type
      ? `/api/kb/gaps/${encodeURIComponent(project)}/${encodeURIComponent(layer)}/${encodeURIComponent(type)}/${encodeURIComponent(index)}`
      : `/api/kb/gaps-legacy/${encodeURIComponent(project)}/${encodeURIComponent(layer)}/${encodeURIComponent(index)}`;
    fetch(url)
      .then(r => r.json())
      .then((d: GapData) => {
        setData(d);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [project, layer, type, index]);

  if (!project || !layer || index === "") {
    return <div style={{ padding: 80, color: "var(--muted)", textAlign: "center" }}>Select a gap from the Dashboard.</div>;
  }
  if (loading) {
    return <div style={{ padding: 80, color: "var(--muted)", textAlign: "center" }}>Loading gap details…</div>;
  }
  if (!data || data.error) {
    return <div style={{ padding: 80, color: "var(--red)", textAlign: "center" }}>Error: {data?.error ?? "Failed to load gap"}</div>;
  }

  const gapKind = data.gap_type ?? (type === "bug" ? "bug" : "feature");
  const titleText = data.title || data.gap || data.feature || data.model || data.id || "Untitled";
  const oneliner = (data.gap || "").trim();
  const chain = parseReasoning(data.technical_detail);
  const tb = trajBadge(data.trajectory);
  const linkCtx = buildLinkCtx(data);

  // Impact pull-quote (first sentence) goes at the top of the card; the rest
  // of the impact paragraph lives in Step 3.
  const impactFull = chain.impact
    ? stripPrefix(chain.impact, /^\s*why\s+amd\s+should\s+care[^:]*:\s*/i)
    : null;
  const impactPull = impactFull
    ? (impactFull.split(/(?<=[.!?])\s+/)[0] || impactFull)
    : null;
  const impactRest = impactFull && impactPull
    ? impactFull.slice(impactPull.length).trim()
    : null;

  const classShort = chain.classification
    ? stripPrefix(chain.classification, /^\s*why\s+it\s+is\s+(a|categorized\s+as\s+a|classified\s+as\s+a)\s+(bug|feature)[^:.]*[:.]\s*/i)
        .split(/(?<=[.!?])\s+/)[0]
    : null;

  const hasDeclaredVisual = !!data.visual;
  const timelineViz = timelineFromMeta(data);

  return (
    <div style={{ maxWidth: 1000, margin: "0 auto", padding: "28px 32px" }}>
      <Link
        href="/"
        style={{
          display: "inline-flex", alignItems: "center", gap: 4,
          fontSize: 13, color: "var(--muted)", marginBottom: 20, textDecoration: "none",
        }}
      >
        ← Back to Dashboard
      </Link>

      <div className="gap-card" style={{ marginBottom: 18 }}>
        <div className="gap-header">
          <div className={`severity-indicator ${sevIndicator(data.severity)}`} />
          <div className="gap-title-area">
            <div className="gap-layer-label" style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span>{LAYER_LABELS[layer] ?? layer}</span>
              <span className={`detail-type-pill ${gapKind}`}>
                <span className="dot" />
                {gapKind === "feature" ? "Feature Gap" : "Bug / Stability"}
              </span>
              {data.project_name && (
                <span style={{ color: "var(--secondary)" }}>· {data.project_name}</span>
              )}
            </div>
            <div className="gap-title">{titleText}</div>
            {oneliner && oneliner !== titleText && <div className="gap-oneliner">{oneliner}</div>}
          </div>
          <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 6, flexShrink: 0 }}>
            <div style={{ display: "flex", gap: 8 }}>
              <span className={`severity-badge ${sevBadge(data.severity)}`}>{data.severity ?? "Medium"}</span>
              {tb && <span className={`trajectory-badge ${tb.cls}`}>{tb.text}</span>}
            </div>
            {data.last_verified && (
              <span style={{ fontSize: 10, color: "var(--dim)" }}>
                Last verified: {data.last_verified}
              </span>
            )}
          </div>
        </div>

        {/* Impact pull-quote — the single lead takeaway for the card */}
        {impactPull && (
          <div style={{ padding: "16px 24px 0" }}>
            <div className="impact-callout">
              <div className="ic-text">
                <div
                  style={{
                    fontSize: 10, textTransform: "uppercase",
                    letterSpacing: "0.9px", color: "var(--orange)",
                    fontWeight: 700, marginBottom: 6,
                  }}
                >
                  Why AMD should care
                </div>
                <div className="step-pullquote" style={{ borderLeft: "none", padding: 0 }}>
                  {renderInlineLinks(impactPull, linkCtx)}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* 4-step reasoning chain (Step 1 is the header card itself — no duplicate breadcrumb) */}
        <div style={{ padding: "18px 24px 22px" }}>

          {/* Step 1 — What we observe */}
          <div className="reasoning-step-block rstep-observation">
            <div className="reasoning-connector">
              <div className="reasoning-icon">1</div>
              <div className="reasoning-line" />
            </div>
            <div className="reasoning-content">
              <div className="reasoning-step-head">
                <span className="reasoning-step-num">Step 1</span>
                <span className="reasoning-step-title">What we observe</span>
                <span className="reasoning-step-kicker">
                  {hasDeclaredVisual ? "structure extracted from the evidence" : "key facts from the issue tracker"}
                </span>
              </div>
              {hasDeclaredVisual && <VizRenderer visual={data.visual!} />}
              {chain.observation.length > 0 && (
                <div className="step-tight-body" style={{ marginTop: hasDeclaredVisual ? 10 : 0 }}>
                  {renderInlineLinks(chain.observation[0], linkCtx)}
                </div>
              )}
            </div>
          </div>

          {/* Step 2 — Feature vs Bug classification */}
          <div className="reasoning-step-block rstep-classification">
            <div className="reasoning-connector">
              <div className="reasoning-icon">2</div>
              <div className="reasoning-line" />
            </div>
            <div className="reasoning-content">
              <div className="reasoning-step-head">
                <span className="reasoning-step-num">Step 2</span>
                <span className="reasoning-step-title">
                  {gapKind === "bug" ? "Bug, not feature gap" : "Feature gap, not bug"}
                </span>
              </div>
              <div style={{ display: "flex", alignItems: "flex-start", gap: 14, flexWrap: "wrap" }}>
                <div className={`classification-badge ${gapKind}`}>
                  <span className="cb-glyph">{gapKind === "bug" ? "!" : "+"}</span>
                  {gapKind === "bug" ? "Broken capability" : "Missing capability"}
                </div>
                {classShort && (
                  <div className="step-tight-body" style={{ flex: 1, minWidth: 240 }}>
                    {renderInlineLinks(classShort, linkCtx)}
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Step 3 — Strategic impact (body only; pull-quote is at the top) */}
          {impactRest && (
            <div className="reasoning-step-block rstep-impact">
              <div className="reasoning-connector">
                <div className="reasoning-icon">3</div>
                <div className="reasoning-line" />
              </div>
              <div className="reasoning-content">
                <div className="reasoning-step-head">
                  <span className="reasoning-step-num">Step 3</span>
                  <span className="reasoning-step-title">Strategic impact</span>
                  <span className="reasoning-step-kicker">roadmap lens</span>
                </div>
                <div className="step-tight-body">
                  {renderInlineLinks(impactRest, linkCtx)}
                </div>
              </div>
            </div>
          )}

          {/* Step 4 — Where we are now */}
          <div className="reasoning-step-block rstep-trajectory">
            <div className="reasoning-connector">
              <div className="reasoning-icon">{impactRest ? "4" : "3"}</div>
            </div>
            <div className="reasoning-content">
              <div className="reasoning-step-head">
                <span className="reasoning-step-num">Step {impactRest ? 4 : 3}</span>
                <span className="reasoning-step-title">Where we are now</span>
                {tb && (
                  <span className={`trajectory-badge ${tb.cls}`} style={{ marginLeft: "auto" }}>
                    {tb.text}
                  </span>
                )}
              </div>

              {/* Bottom-line: expert-authored one-liner on today's state */}
              {data.bottom_line && (
                <div className="bottom-line-box">
                  <div className="bottom-line-label">Latest bottom line</div>
                  <div className="bottom-line-text">
                    {renderInlineLinks(data.bottom_line, linkCtx)}
                  </div>
                </div>
              )}

              {data.trajectory && (
                <div className="step-tight-body" style={{ marginTop: data.bottom_line ? 12 : 0, marginBottom: 10 }}>
                  <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.6px", color: "var(--muted)", fontWeight: 600, marginRight: 8 }}>
                    Trajectory
                  </span>
                  {renderInlineLinks(data.trajectory, linkCtx)}
                </div>
              )}

              {timelineViz && <VizRenderer visual={timelineViz} />}
            </div>
          </div>

        </div>
      </div>

      {data.feature_detail?.expert_notes && (
        <div
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            borderRadius: 10,
            padding: "16px 20px",
            marginTop: 16,
          }}
        >
          <div
            style={{
              fontSize: 11, textTransform: "uppercase",
              letterSpacing: "0.8px", color: "var(--muted)",
              fontWeight: 600, marginBottom: 8,
            }}
          >
            Expert Notes
          </div>
          <div style={{ fontSize: 13, lineHeight: 1.6, color: "var(--secondary)", whiteSpace: "pre-wrap" }}>
            {data.feature_detail.expert_notes}
          </div>
        </div>
      )}
    </div>
  );
}

export default function GapPage() {
  return (
    <Suspense fallback={<div style={{ padding: 80, color: "var(--muted)" }}>Loading…</div>}>
      <GapDetailInner />
    </Suspense>
  );
}
