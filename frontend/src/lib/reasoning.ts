/**
 * Parser that lifts a `technical_detail` prose block into a structured
 * reasoning chain, and helpers for inline evidence-link rendering.
 *
 * Dashboard row, gap detail page, and the priority-gap visuals all consume
 * these helpers.
 */

import React from "react";

export interface ReasoningChain {
  observation: string[];
  classification: string | null;
  impact: string | null;
  residual: string[];
}

const CLASSIFICATION_RE = /^\s*why\s+it\s+is\s+(a|categorized|(?:a\s+)?classified)\b/i;
const IMPACT_RE = /^\s*why\s+amd\s+should\s+care/i;

function splitParagraphs(text: string): string[] {
  return text
    .split(/\n{2,}/)
    .map(p => p.trim())
    .filter(Boolean);
}

export function parseReasoning(technicalDetail: string | null | undefined): ReasoningChain {
  const result: ReasoningChain = {
    observation: [],
    classification: null,
    impact: null,
    residual: [],
  };
  if (!technicalDetail) return result;

  const paragraphs = splitParagraphs(technicalDetail);

  for (const p of paragraphs) {
    if (IMPACT_RE.test(p)) {
      result.impact = p;
      continue;
    }
    if (CLASSIFICATION_RE.test(p)) {
      result.classification = p;
      continue;
    }
    result.observation.push(p);
  }

  return result;
}

export function impactOneLiner(chain: ReasoningChain, max = 180): string | null {
  if (!chain.impact) return null;
  let t = chain.impact
    .replace(/^\s*why\s+amd\s+should\s+care[^:]*:\s*/i, "")
    .replace(/\n+/g, " ")
    .trim();
  const firstSentence = t.split(/(?<=[.!?])\s+/)[0];
  if (firstSentence && firstSentence.length >= 40 && firstSentence.length <= max) {
    t = firstSentence;
  } else if (t.length > max) {
    t = t.slice(0, max - 1).replace(/\s+\S*$/, "") + "…";
  }
  return t;
}

export function observationOneLiner(chain: ReasoningChain, max = 160): string | null {
  const first = chain.observation[0];
  if (!first) return null;
  const s = first.split(/(?<=[.!?])\s+/)[0] || first;
  if (s.length > max) return s.slice(0, max - 1).replace(/\s+\S*$/, "") + "…";
  return s;
}

/* ────────────────────────────────────────────────────────────
   Inline evidence link rendering
   ──────────────────────────────────────────────────────────── */

export type SourceItem = string | { url?: string; date?: string; label?: string };

export interface InlineLinkContext {
  sources?: SourceItem[] | null;
  trackingIssue?: string | null;
  trackingIssues?: string[] | null;
}

interface RefIndex {
  // "#38692" → full URL
  byHash: Map<string, string>;
  // "vllm-project/vllm#38692" → full URL
  byRepoHash: Map<string, string>;
}

function buildIndex(ctx: InlineLinkContext): RefIndex {
  const byHash = new Map<string, string>();
  const byRepoHash = new Map<string, string>();

  const addUrl = (u?: string) => {
    if (!u || !u.startsWith("http")) return;
    const m = u.match(/github\.com\/([^/]+\/[^/]+)\/(issues|pull)\/(\d+)/);
    if (!m) return;
    const repo = m[1];
    const num = m[3];
    // First one wins on hash collision
    if (!byHash.has(`#${num}`)) byHash.set(`#${num}`, u);
    byRepoHash.set(`${repo}#${num}`, u);
  };

  if (ctx.sources) {
    for (const s of ctx.sources) {
      if (typeof s === "string") {
        const m = s.match(/^(https?:\/\/\S+)/);
        if (m) addUrl(m[1]);
      } else if (s && typeof s === "object") {
        addUrl(s.url);
      }
    }
  }
  if (ctx.trackingIssue) addUrl(ctx.trackingIssue);
  if (ctx.trackingIssues) ctx.trackingIssues.forEach(addUrl);

  return { byHash, byRepoHash };
}

export function shortLabel(url: string): string {
  const m = url.match(/github\.com\/([^/]+\/[^/]+)\/(issues|pull)\/(\d+)/);
  if (m) return `${m[1]}#${m[3]}`;
  try {
    const u = new URL(url);
    const path = u.pathname.length > 40 ? u.pathname.slice(0, 40) + "…" : u.pathname;
    return u.hostname.replace(/^www\./, "") + path;
  } catch {
    return url.slice(0, 50);
  }
}

/**
 * Render prose text with two kinds of inline links:
 *   1. Explicit URLs in the text become clickable
 *   2. GitHub-style #NNNN references become clickable if we can match them
 *      against `ctx` (sources + tracking_issue)
 */
export function renderInlineLinks(
  text: string,
  ctx: InlineLinkContext,
): React.ReactNode {
  const idx = buildIndex(ctx);
  // Match either a URL, or a #NNNN / org/repo#NNNN GitHub ref token
  const combinedRe = /(https?:\/\/\S+)|((?:[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)?#\d+)/g;
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  let keyCounter = 0;

  while ((m = combinedRe.exec(text)) !== null) {
    if (m.index > lastIndex) parts.push(text.slice(lastIndex, m.index));

    const urlMatch = m[1];
    const refMatch = m[2];

    if (urlMatch) {
      const clean = urlMatch.replace(/[).,;]+$/, "");
      parts.push(
        React.createElement(
          "a",
          {
            key: `l${keyCounter++}`,
            href: clean,
            target: "_blank",
            rel: "noopener noreferrer",
          },
          shortLabel(clean),
        ),
      );
      lastIndex = m.index + clean.length;
    } else if (refMatch) {
      // Try the full "repo#NNN" key first, then fall back to "#NNN"
      const url =
        idx.byRepoHash.get(refMatch) ||
        (refMatch.includes("/") ? null : idx.byHash.get(refMatch)) ||
        (refMatch.match(/#\d+$/) ? idx.byHash.get(refMatch.match(/#\d+$/)![0]) : null);

      if (url) {
        parts.push(
          React.createElement(
            "a",
            {
              key: `l${keyCounter++}`,
              href: url,
              target: "_blank",
              rel: "noopener noreferrer",
            },
            refMatch,
          ),
        );
      } else {
        parts.push(refMatch);
      }
      lastIndex = m.index + refMatch.length;
    }
  }

  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return parts.length > 0 ? React.createElement(React.Fragment, null, ...parts) : text;
}
