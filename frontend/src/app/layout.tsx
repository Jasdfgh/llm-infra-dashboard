"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { DM_Sans, JetBrains_Mono } from "next/font/google";
import "./globals.css";

const dmSans = DM_Sans({ subsets: ["latin"], variable: "--font-dm-sans", display: "swap" });
const jbMono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-jetbrains-mono", display: "swap" });

interface SbData {
  total_gaps: number;
  project_count: number;
  pending_updates: number;
  feature_count: number;
  bug_count: number;
  projects: { id: string; name: string; gap_count: number }[];
  layers: Record<string, { features: number; bugs: number }>;
}

const ACTIVE_LAYERS = [
  { key: "layer1_model", label: "L1 · Models", dot: "#3b82f6" },
  { key: "layer2_serving", label: "L2 · Serving Arch", dot: "#f97316" },
];

function SidebarContent({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "/";
  const sp = useSearchParams();
  const [sb, setSb] = useState<SbData | null>(null);

  useEffect(() => {
    Promise.all([
      fetch("/api/kb/summary").then(r => r.json()),
      fetch("/api/kb/gaps").then(r => r.json()),
      fetch("/api/kb/projects").then(r => r.json()),
    ])
      .then(([sum, gaps, projs]) => {
        const layers: Record<string, { features: number; bugs: number }> = {};
        for (const k of ["layer1_model", "layer2_serving"]) {
          const node = gaps?.[k] ?? { features: [], bugs: [] };
          layers[k] = {
            features: (node.features ?? []).length,
            bugs: (node.bugs ?? []).length,
          };
        }
        setSb({
          total_gaps: sum.total_gaps ?? 0,
          project_count: sum.project_count ?? 0,
          pending_updates: sum.pending_updates ?? 0,
          feature_count: sum.feature_count ?? 0,
          bug_count: sum.bug_count ?? 0,
          projects: (projs ?? []).map((p: Record<string, unknown>) => ({
            id: String(p.id ?? ""),
            name: String(p.name ?? ""),
            gap_count: Number(p.gap_count ?? 0),
          })),
          layers,
        });
      })
      .catch(() => {});
  }, []);

  const curProject = sp.get("project") ?? "";
  const curLayer = sp.get("layer") ?? "";
  const curType = sp.get("type") ?? "";
  const isHome = pathname === "/" && !curProject && !curLayer && !curType;
  function navCls(active: boolean) {
    return "sidebar-nav-item" + (active ? " active" : "");
  }

  return (
    <div style={{ display: "flex", minHeight: "100vh" }}>
      <nav
        className="sidebar"
        style={{
          width: 240,
          background: "var(--surface)",
          borderRight: "1px solid var(--border)",
          position: "fixed",
          height: "100vh",
          display: "flex",
          flexDirection: "column",
          zIndex: 10,
        }}
      >
        {/* Logo */}
        <div
          className="logo"
          style={{ padding: "20px 20px 16px", borderBottom: "1px solid var(--border)" }}
        >
          <h1 style={{ fontSize: 14, fontWeight: 700, letterSpacing: "-0.3px", lineHeight: 1.4 }}>
            LLM Infra
            <br />
            Gap Dashboard
          </h1>
          <p style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>
            AMD vs NVIDIA · Roadmap-driven
          </p>
        </div>

        {/* Nav */}
        <div className="nav" style={{ flex: 1, padding: 12, overflowY: "auto" }}>
          {/* Overview */}
          <div className="nav-section" style={{ marginBottom: 16 }}>
            <div
              style={{
                fontSize: 10,
                textTransform: "uppercase",
                letterSpacing: "1.2px",
                color: "var(--muted)",
                padding: "0 8px 6px",
                fontWeight: 600,
              }}
            >
              Overview
            </div>
            <Link href="/" className={navCls(isHome)}>
              <span className="nav-dot" style={{ background: isHome ? "var(--accent)" : "var(--muted)" }} />
              All Tracks
              {sb && <span className="nav-count">{sb.total_gaps}</span>}
            </Link>
            <Link
              href="/?type=feature"
              className={navCls(curType === "feature")}
            >
              <span className="nav-dot" style={{ background: "var(--accent)" }} />
              Feature Gaps
              {sb && <span className="nav-count">{sb.feature_count}</span>}
            </Link>
            <Link href="/?type=bug" className={navCls(curType === "bug")}>
              <span className="nav-dot" style={{ background: "var(--red)" }} />
              Bugs & Stability
              {sb && <span className="nav-count">{sb.bug_count}</span>}
            </Link>
          </div>

          {/* By Project */}
          <div className="nav-section" style={{ marginBottom: 16 }}>
            <div
              style={{
                fontSize: 10,
                textTransform: "uppercase",
                letterSpacing: "1.2px",
                color: "var(--muted)",
                padding: "0 8px 6px",
                fontWeight: 600,
              }}
            >
              By Project
            </div>
            {sb?.projects.map(p => (
              <Link
                key={p.id}
                href={`/?project=${p.id}`}
                className={navCls(curProject === p.id)}
              >
                <span className="nav-dot" style={{ background: "var(--orange)" }} />
                {p.name}
                {p.gap_count > 0 && <span className="nav-count">{p.gap_count}</span>}
              </Link>
            ))}
          </div>

          {/* By Layer (L1 / L2 only) */}
          <div className="nav-section" style={{ marginBottom: 16 }}>
            <div
              style={{
                fontSize: 10,
                textTransform: "uppercase",
                letterSpacing: "1.2px",
                color: "var(--muted)",
                padding: "0 8px 6px",
                fontWeight: 600,
              }}
            >
              By Layer
            </div>
            {ACTIVE_LAYERS.map(l => {
              const layerInfo = sb?.layers[l.key];
              const total = layerInfo ? layerInfo.features + layerInfo.bugs : 0;
              return (
                <div key={l.key}>
                  <Link
                    href={`/?layer=${l.key}`}
                    className={navCls(curLayer === l.key && !curType)}
                  >
                    <span className="nav-dot" style={{ background: l.dot }} />
                    {l.label}
                    {total > 0 && <span className="nav-count">{total}</span>}
                  </Link>
                  {layerInfo && total > 0 && (
                    <div style={{ paddingLeft: 22, fontSize: 11 }}>
                      <Link
                        href={`/?layer=${l.key}&type=feature`}
                        className={navCls(curLayer === l.key && curType === "feature")}
                        style={{ padding: "4px 10px", fontSize: 12 }}
                      >
                        <span className="nav-dot" style={{ background: "var(--accent)" }} />
                        Features
                        <span className="nav-count">{layerInfo.features}</span>
                      </Link>
                      <Link
                        href={`/?layer=${l.key}&type=bug`}
                        className={navCls(curLayer === l.key && curType === "bug")}
                        style={{ padding: "4px 10px", fontSize: 12 }}
                      >
                        <span className="nav-dot" style={{ background: "var(--red)" }} />
                        Bugs
                        <span className="nav-count">{layerInfo.bugs}</span>
                      </Link>
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* Resources */}
          <div className="nav-section" style={{ marginBottom: 16 }}>
            <div
              style={{
                fontSize: 10,
                textTransform: "uppercase",
                letterSpacing: "1.2px",
                color: "var(--muted)",
                padding: "0 8px 6px",
                fontWeight: 600,
              }}
            >
              Resources
            </div>
            <a
              href="https://inferencex.semianalysis.com/"
              target="_blank"
              rel="noopener noreferrer"
              className="sidebar-nav-item"
            >
              <span className="nav-dot" style={{ background: "var(--muted)" }} />
              InferenceX Data
            </a>
            <a
              href="https://rocm.docs.amd.com/"
              target="_blank"
              rel="noopener noreferrer"
              className="sidebar-nav-item"
            >
              <span className="nav-dot" style={{ background: "var(--muted)" }} />
              ROCm Docs
            </a>
          </div>
        </div>

        {/* Status */}
        <div
          className="sidebar-status"
          style={{ padding: "12px 16px", borderTop: "1px solid var(--border)", marginTop: "auto" }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontSize: 11,
              color: "var(--muted)",
              marginBottom: 4,
            }}
          >
            <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--green)" }} />
            Knowledge base: {sb?.project_count ?? "—"} projects
          </div>
          <div style={{ fontSize: 11, color: "var(--secondary)" }}>Last updated: Apr 16, 2026</div>
          <div style={{ fontSize: 11, color: "var(--dim)" }}>
            {sb?.total_gaps ?? "—"} tracks ·{" "}
            {sb?.pending_updates ? `${sb.pending_updates} pending updates` : "up to date"}
          </div>
        </div>
      </nav>

      <main style={{ marginLeft: 240, flex: 1, minHeight: "100vh" }}>{children}</main>
    </div>
  );
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${dmSans.variable} ${jbMono.variable}`}>
      <body className={dmSans.className} style={{ background: "var(--bg)", color: "var(--text)" }}>
        <Suspense
          fallback={<div style={{ marginLeft: 240, padding: 32, color: "var(--muted)" }}>Loading...</div>}
        >
          <SidebarContent>{children}</SidebarContent>
        </Suspense>
      </body>
    </html>
  );
}
