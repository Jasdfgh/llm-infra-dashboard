from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "not-needed")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")


def call_llm(system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
    from openai import OpenAI

    api_key = OPENAI_API_KEY if OPENAI_API_KEY else "not-needed"
    client = OpenAI(api_key=api_key, base_url=OPENAI_BASE_URL)

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=4096,
    )
    return response.choices[0].message.content


def extract_json_from_response(text: str) -> dict:
    json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if json_match:
        text = json_match.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Failed to parse JSON from LLM response: {text[:200]}")


PROMPTS_DIR = Path(__file__).parent.parent / "pipeline" / "prompts"


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


def analyze_dual_platform(
    project_id: str,
    project_name: str,
    repo: str,
    metrics: dict,
) -> dict:
    prompt_template = load_prompt("extract_features")
    prompt = prompt_template.format(
        project_id=project_id,
        project_name=project_name,
        repo=repo,
        readme_excerpt=metrics.get("readme_excerpt", "N/A")[:3000],
        repo_structure="\n".join(metrics.get("repo_structure", [])[:100]),
        rocm_mentions=", ".join(metrics.get("rocm_keywords_in_readme", [])),
        stars=metrics.get("stars", 0),
        open_issues=metrics.get("open_issues", 0),
        rocm_issues_open=metrics.get("rocm_issues_open", 0),
        rocm_prs_merged=metrics.get("rocm_prs_merged", 0),
        has_rocm_ci=metrics.get("has_rocm_ci", False),
        has_rocm_dockerfile=metrics.get("has_rocm_dockerfile", False),
    )

    system_prompt = "You are a GPU computing infrastructure analyst. Return ONLY valid JSON."
    response = call_llm(system_prompt, prompt)
    result = extract_json_from_response(response)
    result["project_id"] = project_id
    result["repo"] = repo
    result["analyzed_at"] = metrics.get("fetched_at", "")
    return result


def analyze_nv_amd_pair(
    project_id: str,
    nv_name: str,
    nv_repo: str,
    nv_metrics: dict,
    amd_name: str,
    amd_repo: str,
    amd_metrics: dict,
) -> dict:
    prompt_template = load_prompt("compare_nv_amd_pair")
    prompt = prompt_template.format(
        project_id=project_id,
        nv_name=nv_name,
        nv_repo=nv_repo,
        nv_readme_excerpt=nv_metrics.get("readme_excerpt", "N/A")[:2500],
        nv_repo_structure="\n".join(nv_metrics.get("repo_structure", [])[:80]),
        nv_stars=nv_metrics.get("stars", 0),
        nv_open_issues=nv_metrics.get("open_issues", 0),
        amd_name=amd_name,
        amd_repo=amd_repo,
        amd_readme_excerpt=amd_metrics.get("readme_excerpt", "N/A")[:2500],
        amd_repo_structure="\n".join(amd_metrics.get("repo_structure", [])[:80]),
        amd_stars=amd_metrics.get("stars", 0),
        amd_open_issues=amd_metrics.get("open_issues", 0),
    )

    system_prompt = "You are a GPU computing infrastructure analyst. Return ONLY valid JSON."
    response = call_llm(system_prompt, prompt)
    result = extract_json_from_response(response)
    result["project_id"] = project_id
    result["nv_repo"] = nv_repo
    result["amd_repo"] = amd_repo
    result["nv_name"] = nv_name
    result["amd_name"] = amd_name
    result["analyzed_at"] = nv_metrics.get("fetched_at", "")
    return result


def discover_amd_counterpart(
    project_id: str,
    project_name: str,
    repo: str,
    description: str,
    metrics: dict,
    known_mappings: str,
) -> dict:
    prompt_template = load_prompt("discover_counterpart")
    prompt = prompt_template.format(
        project_id=project_id,
        project_name=project_name,
        repo=repo,
        description=description,
        readme_excerpt=metrics.get("readme_excerpt", "N/A")[:2000],
        known_mappings=known_mappings,
    )

    system_prompt = "You are a GPU computing infrastructure analyst. Return ONLY valid JSON."
    response = call_llm(system_prompt, prompt)
    return extract_json_from_response(response)
