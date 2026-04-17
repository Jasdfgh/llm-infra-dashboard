"use client";

import React from "react";
import type { TimelineVisual, PrState } from "./types";

const PR_STATE_LABEL: Record<PrState, string> = {
  merged: "merged",
  open: "open",
  closed: "closed",
  draft: "draft",
  referenced: "ref",
};

function PrStatePill({ state }: { state: PrState }) {
  return (
    <span className={`viz-tev-prstate viz-prstate-${state}`}>
      {PR_STATE_LABEL[state]}
    </span>
  );
}

export function VizTimeline({ visual }: { visual: TimelineVisual }) {
  const { title, events, caption } = visual;
  return (
    <div className="viz-root viz-timeline">
      {title && <div className="viz-title">{title}</div>}
      <div className="viz-timeline-track">
        {events.map((ev, i) => {
          const last = i === events.length - 1;
          return (
            <div key={i} className={`viz-timeline-event viz-tev-${ev.status}`}>
              <div className="viz-tev-connector">
                <div className="viz-tev-dot" />
                {!last && <div className="viz-tev-line" />}
              </div>
              <div className="viz-tev-body">
                <div className="viz-tev-date">{ev.date}</div>
                <div className="viz-tev-label">
                  {ev.url ? (
                    <a href={ev.url} target="_blank" rel="noopener noreferrer">
                      {ev.label}
                    </a>
                  ) : (
                    ev.label
                  )}
                  {ev.prState && <PrStatePill state={ev.prState} />}
                </div>
                {ev.note && <div className="viz-tev-note">{ev.note}</div>}
              </div>
            </div>
          );
        })}
      </div>
      {caption && <div className="viz-footnote">{caption}</div>}
    </div>
  );
}
