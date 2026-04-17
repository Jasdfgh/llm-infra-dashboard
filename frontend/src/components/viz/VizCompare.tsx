"use client";

import React from "react";
import type { CompareVisual, CompareColumn } from "./types";

function Column({ col }: { col: CompareColumn }) {
  const tone = col.tone ?? "neutral";
  return (
    <div className={`viz-compare-col viz-tone-${tone}`}>
      <div className="viz-compare-head">
        <div className="viz-compare-title">{col.title}</div>
        {col.subtitle && <div className="viz-compare-sub">{col.subtitle}</div>}
      </div>
      <div className="viz-compare-body">
        {col.rows.map((r, i) => (
          <div key={i} className={`viz-compare-row viz-emph-${r.emphasis ?? "normal"}`}>
            <div className="viz-compare-label">{r.label}</div>
            {r.value && <div className="viz-compare-value">{r.value}</div>}
          </div>
        ))}
      </div>
    </div>
  );
}

export function VizCompare({ visual }: { visual: CompareVisual }) {
  return (
    <div className="viz-root viz-compare">
      {visual.title && <div className="viz-title">{visual.title}</div>}
      <div className="viz-compare-grid">
        <Column col={visual.left} />
        <div className="viz-compare-divider" aria-hidden>
          <div className="viz-compare-vs">vs</div>
        </div>
        <Column col={visual.right} />
      </div>
      {visual.verdict && <div className="viz-compare-verdict">{visual.verdict}</div>}
    </div>
  );
}
