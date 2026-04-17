/**
 * Type-discriminated union of gap-visual payloads.
 * A gap YAML may attach ONE optional `visual:` block; the frontend dispatches
 * to the corresponding component.
 */

export type CellStatus = "ok" | "broken" | "partial" | "missing" | "info";

export interface MatrixCell {
  row: number;
  col: number;
  status: CellStatus;
  value?: string;
  note?: string;
}

export interface MatrixVisual {
  type: "matrix";
  title?: string;
  rows: { label: string; sublabel?: string }[];
  columns: { label: string; sublabel?: string; highlight?: boolean }[];
  cells: MatrixCell[];
  footnote?: string;
}

export interface CompareColumn {
  title: string;
  subtitle?: string;
  tone?: "nvidia" | "amd" | "neutral";
  rows: { label: string; value?: string; emphasis?: "strong" | "normal" | "muted" }[];
}

export interface CompareVisual {
  type: "compare";
  title?: string;
  left: CompareColumn;
  right: CompareColumn;
  verdict?: string;
}

export interface StackItem {
  label: string;
  status: CellStatus;
  note?: string;
  url?: string;
  highlighted?: boolean;
}

export interface StackGroup {
  name: string;
  items: StackItem[];
}

export interface StackVisual {
  type: "stack";
  title?: string;
  groups: StackGroup[];
  caption?: string;
}

export type PrState = "merged" | "open" | "closed" | "draft" | "referenced";

export interface TimelineEvent {
  date: string; // ISO or freeform
  label: string;
  note?: string;
  status: "past" | "current" | "future" | "pending_review";
  url?: string;
  prState?: PrState; // rendered as a small pill when present
}

export interface TimelineVisual {
  type: "timeline";
  title?: string;
  events: TimelineEvent[];
  caption?: string;
}

export type GapVisual =
  | MatrixVisual
  | CompareVisual
  | StackVisual
  | TimelineVisual;
