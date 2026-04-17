"""
Evidence Classifier — batch-classifies crawled GitHub evidence items
(issues, PRs, code signals) against the project's feature taxonomy.
Each item gets tagged with: affected features, dimension, AMD impact,
hardware context, and confidence.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agent.taxonomy import taxonomy_to_prompt_text

logger = logging.getLogger(__name__)

BATCH_SIZE = 12

SYSTEM_PROMPT = (
    "You classify GitHub evidence items (issues, pull requests, CI signals) "
    "into a project's feature taxonomy for AMD vs NVIDIA GPU parity analysis. "
    "Return ONLY a valid JSON array — one object per input item, in order. "
    "No markdown fences, no prose outside the array."
)

HW_MAPPING_NOTE = (
    "Hardware generation mapping:\n"
    "- MI250X <-> A100\n"
    "- MI300X <-> H100/H200\n"
    "- MI325X <-> H200\n"
    "- MI355X <-> B200\n"
    "Use 'all' when hardware is not specified or not relevant."
)


def _build_batch_prompt(
    project_id: str,
    category: str,
    taxonomy_text: str,
    items: list[dict[str, Any]],
) -> str:
    item_lines: list[str] = []
    for idx, it in enumerate(items):
        etype = it.get("evidence_type", "unknown")
        title = it.get("title", "")[:150]
        body = it.get("body", "")[:250]
        url = it.get("url", "")
        state = it.get("state", "")
        date = it.get("date", "")
        item_lines.append(
            f"[{idx}] type={etype} state={state} date={date}\n"
            f"    title: {title}\n"
            f"    body: {body}\n"
            f"    url: {url}"
        )

    items_block = "\n\n".join(item_lines)

    return f"""Project: {project_id} (category: {category})

Feature taxonomy:
{taxonomy_text}

{HW_MAPPING_NOTE}

Evidence items to classify:
{items_block}

For EACH item, return a JSON object with these fields:
- "index": the item index [0..{len(items)-1}]
- "features": list of feature IDs from the taxonomy that this item affects (can be empty if irrelevant)
- "dimension": primary dimension affected ("model_support"|"feature_availability"|"performance_parity"|"kernel_backend"|"engineering_maturity"|"none")
- "impact": AMD impact type ("new_support"|"improvement"|"regression"|"bug_report"|"bug_fix"|"request"|"info"|"none")
- "amd_effect": one-sentence description of the effect on AMD support (empty if none)
- "hardware": hardware context ("MI300X"|"MI355X"|"MI250X"|"MI325X"|"all"|"unknown")
- "confidence": "high" if merged PR or CI evidence, "medium" if open issue with details, "low" if vague/indirect
- "severity": gap severity if this is a gap ("critical"|"high"|"medium"|"low"|"none")

Return a JSON array of {len(items)} objects. Items unrelated to AMD/NVIDIA GPU parity should have features=[], impact="none"."""


def _prepare_evidence_items(crawl_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten crawled issues, PRs, CI signals, and code signals into
    a uniform list of evidence items for classification."""
    items: list[dict[str, Any]] = []

    for iss in crawl_data.get("issues", {}).get("rocm_related", []):
        items.append({
            "evidence_type": "issue",
            "title": iss.get("title", ""),
            "body": (iss.get("body") or "")[:500],
            "url": iss.get("html_url", ""),
            "state": iss.get("state", ""),
            "date": iss.get("created_at", ""),
            "number": iss.get("number"),
        })

    for iss in crawl_data.get("issues", {}).get("model_gaps", []):
        if not any(e["url"] == iss.get("html_url") for e in items):
            items.append({
                "evidence_type": "issue",
                "title": iss.get("title", ""),
                "body": (iss.get("body") or "")[:500],
                "url": iss.get("html_url", ""),
                "state": iss.get("state", ""),
                "date": iss.get("created_at", ""),
                "number": iss.get("number"),
            })

    for iss in crawl_data.get("issues", {}).get("performance", []):
        if not any(e["url"] == iss.get("html_url") for e in items):
            items.append({
                "evidence_type": "issue_performance",
                "title": iss.get("title", ""),
                "body": (iss.get("body") or "")[:500],
                "url": iss.get("html_url", ""),
                "state": iss.get("state", ""),
                "date": iss.get("created_at", ""),
                "number": iss.get("number"),
            })

    for pr in crawl_data.get("pull_requests", {}).get("rocm_merged", []):
        if not any(e["url"] == pr.get("html_url") for e in items):
            items.append({
                "evidence_type": "pull_request_merged",
                "title": pr.get("title", ""),
                "body": (pr.get("body") or "")[:500],
                "url": pr.get("html_url", ""),
                "state": "merged",
                "date": pr.get("created_at", ""),
                "number": pr.get("number"),
            })

    ci = crawl_data.get("ci", {})
    for wf_name in ci.get("rocm_ci_files", {}):
        items.append({
            "evidence_type": "ci_config",
            "title": f"ROCm CI workflow: {wf_name}",
            "body": f"CI workflow file {wf_name} contains ROCm/HIP/AMD references",
            "url": "",
            "state": "active",
            "date": "",
        })

    for rel in crawl_data.get("releases", []):
        body = rel.get("body") or ""
        body_lower = body.lower()
        if any(kw in body_lower for kw in ("rocm", "amd", "hip", "mi300", "mi250", "rccl")):
            items.append({
                "evidence_type": "release",
                "title": f"Release {rel.get('tag_name', '')}: {rel.get('name', '')}",
                "body": body[:600],
                "url": rel.get("html_url", ""),
                "state": "released",
                "date": rel.get("published_at", ""),
            })

    stats = crawl_data.get("stats", {})
    cuda_count = stats.get("cuda_file_count", 0)
    rocm_count = stats.get("rocm_file_count", 0)
    if cuda_count > 0 or rocm_count > 0:
        items.append({
            "evidence_type": "code_stats",
            "title": f"Code file counts: {cuda_count} CUDA files, {rocm_count} ROCm/HIP files",
            "body": f"CUDA files: {cuda_count}, ROCm/HIP files: {rocm_count}. "
                    f"Ratio: {rocm_count}/{cuda_count} = {rocm_count/max(cuda_count,1):.1%}",
            "url": "",
            "state": "current",
            "date": crawl_data.get("crawled_at", ""),
        })

    return items


async def classify_evidence(
    llm_call,
    project_id: str,
    category: str,
    taxonomy: dict[str, dict[str, Any]],
    crawl_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Classify all crawled evidence against the taxonomy using batched LLM calls.

    Args:
        llm_call: async callable(system_prompt, user_prompt, **kw) -> str
        project_id: e.g. "vllm"
        category: e.g. "inference"
        taxonomy: merged taxonomy dict
        crawl_data: raw crawl output from CrawlAgent

    Returns:
        List of tagged evidence items, each with original data + classification fields.
    """
    items = _prepare_evidence_items(crawl_data)
    if not items:
        logger.warning("No evidence items to classify for %s", project_id)
        return []

    taxonomy_text = taxonomy_to_prompt_text(taxonomy)
    tagged: list[dict[str, Any]] = []

    for batch_start in range(0, len(items), BATCH_SIZE):
        batch = items[batch_start : batch_start + BATCH_SIZE]
        prompt = _build_batch_prompt(project_id, category, taxonomy_text, batch)

        try:
            raw = await llm_call(SYSTEM_PROMPT, prompt, max_tokens=3000)
            classifications = _parse_classifications(raw, len(batch))
        except Exception:
            logger.exception(
                "Classification batch failed for %s (items %d-%d)",
                project_id, batch_start, batch_start + len(batch),
            )
            classifications = [_empty_classification(i) for i in range(len(batch))]

        for item, cls in zip(batch, classifications):
            merged = {**item, **cls}
            tagged.append(merged)

    logger.info(
        "Classified %d evidence items for %s (%d with features)",
        len(tagged), project_id,
        sum(1 for t in tagged if t.get("features")),
    )
    return tagged


def _parse_classifications(raw: str, expected: int) -> list[dict[str, Any]]:
    text = raw.strip()
    # Strip thinking tags if model wraps in <think>...</think>
    import re
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        arr = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            try:
                arr = json.loads(text[start:end])
            except json.JSONDecodeError:
                logger.warning("Failed to parse classification JSON")
                return [_empty_classification(i) for i in range(expected)]
        else:
            logger.warning("No JSON array found in classification response")
            return [_empty_classification(i) for i in range(expected)]

    if not isinstance(arr, list):
        return [_empty_classification(i) for i in range(expected)]

    while len(arr) < expected:
        arr.append(_empty_classification(len(arr)))

    result = []
    for item in arr[:expected]:
        if not isinstance(item, dict):
            item = {}
        result.append({
            "features": item.get("features") or [],
            "dimension": item.get("dimension", "none"),
            "impact": item.get("impact", "none"),
            "amd_effect": item.get("amd_effect", ""),
            "hardware": item.get("hardware", "unknown"),
            "confidence": item.get("confidence", "low"),
            "severity": item.get("severity", "none"),
        })
    return result


def _empty_classification(index: int) -> dict[str, Any]:
    return {
        "index": index,
        "features": [],
        "dimension": "none",
        "impact": "none",
        "amd_effect": "",
        "hardware": "unknown",
        "confidence": "low",
        "severity": "none",
    }
