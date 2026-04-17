"use client";

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { parseReasoning, impactOneLiner, observationOneLiner } from "../lib/reasoning";

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

interface GapUpdate { date?: string; summary?: string; reviewed?: boolean; }

interface GapItem {
  id?: string;
  title?: string;
  gap?: string;
  model?: string;
  feature?: string;
  severity?: string;
  technical_detail?: string;
  notes?: string;
  trajectory?: string;
  project_id?: string;
  project_name?: string;
  sources?: unknown[];
  tracking_issue?: string;
  updates?: GapUpdate[];
  discovered_at?: string;
  last_verified?: string;
  status?: string;
  gap_type?: "feature" | "bug";
  index_in_type?: number;
}

interface GapLayers {
  layer1_model: { features: GapItem[]; bugs: GapItem[] };
  layer2_serving: { features: GapItem[]; bugs: GapItem[] };
}
type LK = keyof GapLayers;

const LAYERS: {
  key: LK;
  num: string;
  title: string;
  subtitle: string;
  intro: string;
  intro_lead: string;
  cls: string;
}[] = [
  {
    key: "layer1_model",
    num: "L1",
    title: "Model Layer",
    subtitle: "Is the model runnable, correct, and serving-viable on AMD?",
    intro_lead: "The model question.",
    intro:
      " Tracks here ask whether AMD users can take a frontier model (Gemma 4, GPT-OSS, Qwen3.5, GLM-5, MiniMax) and get the same answer at the same quality as NVIDIA users. Features = capability not yet on AMD (hybrid-attention disagg, NVFP4 checkpoints, multimodal AITER path). Bugs = capability is there but produces wrong output or crashes (FP8 < BF16 on MI355X, MI300X decode HSA exceptions, AITER MLA sparse regression).",
    cls: "layer-1",
  },
  {
    key: "layer2_serving",
    num: "L2",
    title: "Serving Architecture Layer",
    subtitle: "Can AMD serve at production scale — disagg, wide EP, parallelism strategies?",
    intro_lead: "The scale question.",
    intro:
      " Tracks here ask whether, once the model runs, AMD can serve it under real cluster conditions. Features = orchestration or parallelism primitives missing on AMD (vLLM router × MoRI, cross-node wide EP, DWDP, chunked-prefill perf). Bugs = stack combinations that break in production (FP4+disagg+wideEP composability, Thor-2 NIC decode hang, throughput collapse on wrong image).",
    cls: "layer-2",
  },
];

function sevClass(s?: string): string {
  if (!s) return "sev-medium";
  const l = s.toLowerCase();
  if (l === "critical") return "sev-critical";
  if (l === "high") return "sev-high";
  if (l === "medium") return "sev-medium";
  return "sev-low";
}

function sevLabelClass(s?: string): string {
  if (!s) return "gap-sev-label medium";
  return `gap-sev-label ${s.toLowerCase()}`;
}

function trajDisplay(t?: string): { text: string; cls: string } {
  if (!t) return { text: "— stable", cls: "traj-flat" };
  const l = t.toLowerCase();
  if (l.includes("closing") || l.includes("improving") || l.includes("planned"))
    return { text: "↑ closing", cls: "traj-up" };
  if (l.includes("widening") || l.includes("worsening"))
    return { text: "↓ widening", cls: "traj-down" };
  if (l.includes("recurring"))
    return { text: "↻ recurring", cls: "traj-down" };
  return { text: "— stable", cls: "traj-flat" };
}

function gapTitleText(g: GapItem): string {
  return g.title || g.gap || g.feature || g.model || g.id || "Untitled gap";
}

/**
 * Two-line preview for the dashboard row:
 *  - line 1: the gap's one-line problem statement (`gap` field)
 *  - line 2: why AMD cares (lifted from technical_detail via parseReasoning)
 */
function gapPreview(g: GapItem): { problem: string; cares: string | null } {
  const chain = parseReasoning(g.technical_detail);
  const problem = g.gap || observationOneLiner(chain) || "";
  const cares = impactOneLiner(chain);
  return { problem, cares };
}

function gapRow(g: GapItem, layerKey: LK) {
  const t = trajDisplay(g.trajectory);
  const pendingUpdates = (g.updates ?? []).filter(u => !u.reviewed).length;
  const type = g.gap_type ?? "feature";
  const typeIdx = typeof g.index_in_type === "number" ? g.index_in_type : 0;
  const href =
    `/gap?project=${encodeURIComponent(g.project_id ?? "")}` +
    `&layer=${encodeURIComponent(layerKey)}` +
    `&type=${encodeURIComponent(type)}` +
    `&index=${encodeURIComponent(typeIdx)}`;
  const { problem, cares } = gapPreview(g);

  return (
    <Link
      key={`${g.project_id}-${layerKey}-${type}-${typeIdx}`}
      href={href}
      className="gap-row"
    >
      <div className={`gap-severity ${sevClass(g.severity)}`} />
      <div className="gap-info">
        <div className="gap-name">
          {gapTitleText(g)}
          {pendingUpdates > 0 && (
            <span
              style={{
                display: "inline-block",
                width: 7,
                height: 7,
                borderRadius: "50%",
                background: "var(--accent)",
                marginLeft: 8,
                verticalAlign: "middle",
              }}
              title={`${pendingUpdates} pending update(s)`}
            />
          )}
        </div>
        <div className="gap-desc">{problem}</div>
        {cares && (
          <div className="gap-amd-cares">
            <strong>Why AMD cares:</strong>
            {cares}
          </div>
        )}
      </div>
      <div className="gap-meta">
        <span className="gap-project">{g.project_name}</span>
        <span className={`gap-type-badge ${type}`}>{type}</span>
        <span className={sevLabelClass(g.severity)}>{g.severity}</span>
        <span className={t.cls}>{t.text}</span>
        <span className="gap-arrow">›</span>
      </div>
    </Link>
  );
}

function TypeSection({
  kind,
  items,
  layerKey,
  projectTab,
}: {
  kind: "features" | "bugs";
  items: GapItem[];
  layerKey: LK;
  projectTab: string;
}) {
  const isFeat = kind === "features";
  const headerLabel = isFeat ? "Feature gaps" : "Bugs & stability";
  const hint = isFeat
    ? "capability missing or incomplete on AMD"
    : "capability exists but runs incorrectly / unstably";

  return (
    <div>
      <div className={`gap-type-header gap-type-${kind}`}>
        <span className="dot" />
        <span>{headerLabel}</span>
        <span className="count">{items.length}</span>
        <span className="hint">{hint}</span>
      </div>
      {items.length === 0 ? (
        <div className="gap-type-empty">
          No {isFeat ? "feature gaps" : "bugs"} in this layer
          {projectTab !== "all" ? ` for ${projectTab}` : ""} — reporting clean.
        </div>
      ) : (
        items.map(g => gapRow(g, layerKey))
      )}
    </div>
  );
}

function DashboardInner() {
  const sp = useSearchParams();
  const filterProject = sp.get("project") ?? "";
  const filterLayer = sp.get("layer") ?? "";
  const filterType = sp.get("type") ?? "";

  const [summary, setSummary] = useState<Summary | null>(null);
  const [layers, setLayers] = useState<GapLayers | null>(null);
  const [loading, setLoading] = useState(true);
  const [projectTab, setProjectTab] = useState("all");

  useEffect(() => {
    if (filterProject) setProjectTab(filterProject);
    else setProjectTab("all");
  }, [filterProject]);

  useEffect(() => {
    setLoading(true);
    const qs = filterProject ? `?project=${encodeURIComponent(filterProject)}` : "";
    Promise.all([
      fetch("/api/kb/summary").then(r => r.json()),
      fetch(`/api/kb/gaps${qs}`).then(r => r.json()),
    ])
      .then(([s, g]) => {
        setSummary(s);
        setLayers(g);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [filterProject]);

  const allProjectSet: Record<string, boolean> = {};
  if (layers) {
    (Object.keys(layers) as LK[]).forEach(k => {
      [...(layers[k]?.features ?? []), ...(layers[k]?.bugs ?? [])].forEach(g => {
        if (g.project_id) allProjectSet[g.project_id] = true;
      });
    });
  }
  const allProjects = Object.keys(allProjectSet);

  const filteredItems = (key: LK): { features: GapItem[]; bugs: GapItem[] } => {
    if (!layers) return { features: [], bugs: [] };
    const bucket = layers[key] ?? { features: [], bugs: [] };
    const doFilter = (arr: GapItem[]) =>
      projectTab === "all" ? arr : arr.filter(g => g.project_id === projectTab);
    return { features: doFilter(bucket.features), bugs: doFilter(bucket.bugs) };
  };

  if (loading) {
    return (
      <div style={{ padding: "80px 32px", color: "var(--muted)", textAlign: "center" }}>
        Loading dashboard…
      </div>
    );
  }

  const activeFilterLabel = (() => {
    if (filterType === "feature") return "Feature gaps";
    if (filterType === "bug") return "Bugs & stability";
    if (filterLayer === "layer1_model") return "L1 · Model layer";
    if (filterLayer === "layer2_serving") return "L2 · Serving architecture";
    if (filterProject) return filterProject === "vllm" ? "vLLM" : filterProject === "sglang" ? "SGLang" : filterProject;
    return "All tracks";
  })();

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "28px 32px" }}>
      {/* Slim page header — title + inline stats, no banner */}
      <div className="page-header fade-in">
        <div className="page-header-title-row">
          <h2 className="page-header-title">{activeFilterLabel}</h2>
          {summary && (
            <div className="page-header-stats">
              <span className="stat-chip">
                <strong>{summary.total_gaps}</strong> tracks
              </span>
              <span className="stat-chip chip-feature">
                <strong>{summary.feature_count}</strong> features
              </span>
              <span className="stat-chip chip-bug">
                <strong>{summary.bug_count}</strong> bugs
              </span>
              {summary.critical > 0 && (
                <span className="stat-chip chip-critical">
                  <strong>{summary.critical}</strong> critical
                </span>
              )}
              {summary.closing > 0 && (
                <span className="stat-chip chip-closing">
                  <strong>{summary.closing}</strong> closing ↑
                </span>
              )}
              {summary.pending_updates > 0 && (
                <span className="stat-chip chip-pending">
                  <strong>{summary.pending_updates}</strong> pending review
                </span>
              )}
            </div>
          )}
        </div>
        <div className="page-header-sub">
          Roadmap-driven AMD vs NVIDIA tracking · L1 Model · L2 Serving · split by feature gaps and bugs
        </div>
      </div>

      {/* Project tabs */}
      {!filterLayer && (
        <div className="project-tabs fade-in">
          <div
            className={`project-tab ${projectTab === "all" ? "active" : ""}`}
            onClick={() => setProjectTab("all")}
          >
            All Projects
          </div>
          {allProjects.map(pid => (
            <div
              key={pid}
              className={`project-tab ${projectTab === pid ? "active" : ""}`}
              onClick={() => setProjectTab(pid)}
            >
              {pid === "vllm" ? "vLLM" : pid === "sglang" ? "SGLang" : pid}
            </div>
          ))}
        </div>
      )}

      {/* Layer sections */}
      {LAYERS.map(layer => {
        if (filterLayer && filterLayer !== layer.key) return null;
        const { features, bugs } = filteredItems(layer.key);
        const total = features.length + bugs.length;

        const showFeatures = !filterType || filterType === "feature";
        const showBugs = !filterType || filterType === "bug";

        return (
          <div key={layer.key} style={{ marginBottom: 32 }} className="fade-in">
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                marginBottom: 6,
                paddingBottom: 10,
                borderBottom: "1px solid var(--border)",
              }}
            >
              <div className={`layer-number ${layer.cls}`}>{layer.num}</div>
              <div>
                <div style={{ fontSize: 15, fontWeight: 600 }}>{layer.title}</div>
                <div style={{ fontSize: 12, color: "var(--muted)" }}>{layer.subtitle}</div>
              </div>
              <div
                style={{
                  marginLeft: "auto",
                  fontSize: 12,
                  color: "var(--muted)",
                  fontVariantNumeric: "tabular-nums",
                }}
              >
                {total} track{total !== 1 ? "s" : ""}
                {" · "}
                <span style={{ color: "var(--accent)" }}>{features.length} features</span>
                {" · "}
                <span style={{ color: "var(--red)" }}>{bugs.length} bugs</span>
              </div>
            </div>

            <div className={`layer-intro ${layer.cls}`}>
              <span className="intro-lead">{layer.intro_lead}</span>
              {layer.intro}
            </div>

            {showFeatures && (
              <TypeSection
                kind="features"
                items={features}
                layerKey={layer.key}
                projectTab={projectTab}
              />
            )}
            {showBugs && (
              <TypeSection
                kind="bugs"
                items={bugs}
                layerKey={layer.key}
                projectTab={projectTab}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function DashboardPage() {
  return (
    <Suspense
      fallback={
        <div style={{ padding: "80px 32px", color: "var(--muted)" }}>Loading…</div>
      }
    >
      <DashboardInner />
    </Suspense>
  );
}
