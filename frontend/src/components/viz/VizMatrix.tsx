"use client";

import React from "react";
import type { MatrixVisual, MatrixCell, CellStatus } from "./types";

const GLYPH: Record<CellStatus, string> = {
  ok: "✓",
  broken: "✗",
  partial: "~",
  missing: "—",
  info: "·",
};

function cellClass(status: CellStatus): string {
  return `viz-cell viz-cell-${status}`;
}

function findCell(cells: MatrixCell[], r: number, c: number): MatrixCell | undefined {
  return cells.find(x => x.row === r && x.col === c);
}

export function VizMatrix({ visual }: { visual: MatrixVisual }) {
  const { rows, columns, cells, title, footnote } = visual;
  return (
    <div className="viz-root viz-matrix">
      {title && <div className="viz-title">{title}</div>}
      <div className="viz-matrix-wrap">
        <table className="viz-matrix-table">
          <thead>
            <tr>
              <th className="viz-corner" />
              {columns.map((c, i) => (
                <th key={i} className={`viz-colhead${c.highlight ? " viz-colhead-hl" : ""}`}>
                  <div className="viz-colhead-label">{c.label}</div>
                  {c.sublabel && <div className="viz-colhead-sub">{c.sublabel}</div>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri}>
                <th className="viz-rowhead">
                  <div className="viz-rowhead-label">{r.label}</div>
                  {r.sublabel && <div className="viz-rowhead-sub">{r.sublabel}</div>}
                </th>
                {columns.map((_, ci) => {
                  const cell = findCell(cells, ri, ci);
                  if (!cell) {
                    return (
                      <td key={ci} className="viz-cell viz-cell-missing">
                        <span className="viz-cell-glyph">—</span>
                      </td>
                    );
                  }
                  return (
                    <td
                      key={ci}
                      className={cellClass(cell.status)}
                      title={cell.note}
                    >
                      <span className="viz-cell-glyph">{GLYPH[cell.status]}</span>
                      {cell.value && <span className="viz-cell-value">{cell.value}</span>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {footnote && <div className="viz-footnote">{footnote}</div>}
    </div>
  );
}
