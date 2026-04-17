"use client";

import React from "react";
import type { StackVisual, CellStatus } from "./types";

const STATUS_GLYPH: Record<CellStatus, string> = {
  ok: "✓",
  broken: "✗",
  partial: "⟳",
  missing: "✗",
  info: "·",
};

const STATUS_WORD: Record<CellStatus, string> = {
  ok: "present",
  broken: "broken",
  partial: "in progress",
  missing: "missing",
  info: "—",
};

export function VizStack({ visual }: { visual: StackVisual }) {
  const { title, groups, caption } = visual;
  return (
    <div className="viz-root viz-stack">
      {title && <div className="viz-title">{title}</div>}
      <div className="viz-stack-groups">
        {groups.map((g, gi) => (
          <div key={gi} className="viz-stack-group">
            <div className="viz-stack-groupname">{g.name}</div>
            <div className="viz-stack-items">
              {g.items.map((it, ii) => (
                <div
                  key={ii}
                  className={`viz-stack-item viz-stack-status-${it.status}${it.highlighted ? " viz-stack-item-hl" : ""}`}
                >
                  <span className="viz-stack-glyph" aria-hidden>
                    {STATUS_GLYPH[it.status]}
                  </span>
                  <span className="viz-stack-label">
                    {it.url ? (
                      <a href={it.url} target="_blank" rel="noopener noreferrer">
                        {it.label}
                      </a>
                    ) : (
                      it.label
                    )}
                  </span>
                  {it.note && <span className="viz-stack-note">{it.note}</span>}
                  <span className={`viz-stack-pill viz-pill-${it.status}`}>
                    {STATUS_WORD[it.status]}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      {caption && <div className="viz-footnote">{caption}</div>}
    </div>
  );
}
