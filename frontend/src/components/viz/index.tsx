"use client";

import React from "react";
import type { GapVisual } from "./types";
import { VizMatrix } from "./VizMatrix";
import { VizCompare } from "./VizCompare";
import { VizStack } from "./VizStack";
import { VizTimeline } from "./VizTimeline";

export function VizRenderer({ visual }: { visual: GapVisual | null | undefined }) {
  if (!visual) return null;
  switch (visual.type) {
    case "matrix":
      return <VizMatrix visual={visual} />;
    case "compare":
      return <VizCompare visual={visual} />;
    case "stack":
      return <VizStack visual={visual} />;
    case "timeline":
      return <VizTimeline visual={visual} />;
    default:
      return null;
  }
}

export type { GapVisual } from "./types";
export { VizMatrix, VizCompare, VizStack, VizTimeline };
