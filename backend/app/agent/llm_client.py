import json
import logging
import os
import re
from typing import Any

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self) -> None:
        load_dotenv()
        self.api_key = os.getenv("OPENAI_API_KEY", "not-needed")
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o")

    async def call(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key != "not-needed":
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return (data["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            logger.exception("LLM API call failed")
            raise

    def extract_json(self, text: str) -> dict[str, Any]:
        decoded = self._decode_json(text)
        if isinstance(decoded, dict):
            return decoded
        raise ValueError("Expected a JSON object at the top level")

    def _decode_json(self, text: str) -> Any:
        block = re.search(
            r"```(?:json)?\s*([\s\S]*?)```",
            text,
            re.IGNORECASE,
        )
        if block:
            candidate = block.group(1).strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        start = text.find("{")
        if start != -1:
            chunk = text[start:]
            try:
                obj, _ = json.JSONDecoder().raw_decode(chunk)
                return obj
            except json.JSONDecodeError:
                pass
        raise ValueError("Could not parse JSON from model output")

    def _decode_json_array(self, text: str) -> list[Any]:
        block = re.search(
            r"```(?:json)?\s*([\s\S]*?)```",
            text,
            re.IGNORECASE,
        )
        if block:
            candidate = block.group(1).strip()
            try:
                val = json.loads(candidate)
                if isinstance(val, list):
                    return val
            except json.JSONDecodeError:
                pass
        start = text.find("[")
        if start != -1:
            chunk = text[start:]
            try:
                obj, _ = json.JSONDecoder().raw_decode(chunk)
                if isinstance(obj, list):
                    return obj
            except json.JSONDecodeError:
                pass
        decoded = self._decode_json(text)
        if isinstance(decoded, dict) and "projects" in decoded:
            inner = decoded["projects"]
            if isinstance(inner, list):
                return inner
        raise ValueError("Could not parse JSON array from model output")

    def _compress_crawl(self, crawl_data: dict[str, Any]) -> str:
        """Reduce crawl data to essential signals that fit in ~6k tokens."""
        lines: list[str] = []
        ov = crawl_data.get("overview", {})
        info = ov.get("info", {})
        lines.append(f"Stars: {info.get('stargazers_count', 'N/A')}, Forks: {info.get('forks_count', 'N/A')}")
        readme = (ov.get("readme") or "")[:2000]
        lines.append(f"README (first 2000 chars):\n{readme}\n")

        stats = crawl_data.get("stats", {})
        lines.append(f"CUDA files: {stats.get('cuda_file_count', 0)}, ROCm/HIP files: {stats.get('rocm_file_count', 0)}")
        lines.append(f"ROCm issues open: {stats.get('rocm_issues_open', 0)}, PRs merged: {stats.get('rocm_prs_merged', 0)}")

        issues = crawl_data.get("issues", {})
        for label, key in [("ROCm Issues", "rocm_related"), ("Model Gap Issues", "model_gaps"), ("Perf Issues", "performance")]:
            items = issues.get(key, [])[:8]
            if items:
                lines.append(f"\n{label} (top {len(items)}):")
                for iss in items:
                    url = iss.get("html_url", "")
                    title = iss.get("title", "")[:120]
                    state = iss.get("state", "")
                    lines.append(f"  - [{state}] {title} ({url})")

        prs = crawl_data.get("pull_requests", {}).get("rocm_merged", [])[:10]
        if prs:
            lines.append(f"\nMerged ROCm PRs (top {len(prs)}):")
            for pr in prs:
                url = pr.get("html_url", "")
                title = pr.get("title", "")[:120]
                lines.append(f"  - {title} ({url})")

        ci = crawl_data.get("ci", {})
        wfs = ci.get("workflows", [])
        rocm_ci = ci.get("rocm_ci_files", {})
        lines.append(f"\nCI Workflows: {len(wfs)} total, {len(rocm_ci)} ROCm-related")
        for name in list(rocm_ci.keys())[:3]:
            lines.append(f"  - {name}")

        releases = crawl_data.get("releases", [])[:3]
        if releases:
            lines.append(f"\nRecent Releases:")
            for r in releases:
                body = (r.get("body") or "")[:300]
                lines.append(f"  - {r.get('tag_name', '')} ({r.get('published_at', '')}): {body}")

        code = crawl_data.get("code", {})
        rocm_files = code.get("rocm_files", [])[:15]
        if rocm_files:
            lines.append(f"\nROCm-related files: {', '.join(rocm_files[:15])}")

        return "\n".join(lines)

    async def analyze_project(
        self,
        project_id: str,
        project_name: str,
        crawl_data: dict[str, Any],
        category: str = "inference",
    ) -> dict[str, Any]:
        """Knowledge-driven analysis: use expert YAML if available, else SKILL pipeline."""
        from app.agent.knowledge import load_project_knowledge, add_agent_finding
        from app.agent.classifier import classify_evidence, _prepare_evidence_items
        from app.agent.assembler import format_matrix_for_summary

        kb = load_project_knowledge(project_id)

        if kb and kb.get("features"):
            logger.info("Knowledge-driven analysis for %s (%d expert features)",
                        project_id, len(kb["features"]))
            return await self._knowledge_driven_analysis(
                project_id, project_name, category, kb, crawl_data
            )
        else:
            logger.info("No knowledge base for %s, using SKILL pipeline", project_id)
            return await self._skill_pipeline_analysis(
                project_id, project_name, category, crawl_data
            )

    async def _knowledge_driven_analysis(
        self,
        project_id: str,
        project_name: str,
        category: str,
        kb: dict[str, Any],
        crawl_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Expert knowledge base exists — Agent collects evidence to verify/update."""
        from app.agent.knowledge import add_agent_finding, get_search_hints
        from app.agent.classifier import classify_evidence, _prepare_evidence_items

        features = kb.get("features", {})
        taxonomy_for_classify = {}
        for fid, spec in features.items():
            taxonomy_for_classify[fid] = {
                "dimension": spec.get("dimension", "feature_availability"),
                "description": spec.get("name", fid),
                "check_keywords": spec.get("agent_search_hints", []),
            }

        tagged = await classify_evidence(
            self.call, project_id, category, taxonomy_for_classify, crawl_data
        )
        logger.info("Classified %d evidence items against expert knowledge", len(tagged))

        for item in tagged:
            if item.get("impact") == "none" or not item.get("features"):
                continue
            for fid in item["features"]:
                if fid in features:
                    add_agent_finding(project_id, fid, {
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "type": item.get("evidence_type", ""),
                        "impact": item.get("impact", ""),
                        "amd_effect": item.get("amd_effect", ""),
                        "hardware": item.get("hardware", "unknown"),
                        "confidence": item.get("confidence", "low"),
                        "date": item.get("date", ""),
                    })

        return self._kb_to_report(kb, tagged, crawl_data)

    def _kb_to_report(
        self,
        kb: dict[str, Any],
        tagged: list[dict[str, Any]],
        crawl_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert expert knowledge base + agent findings to the standard report format."""
        features = kb.get("features", {})
        known_gaps = kb.get("known_gaps", [])

        STATUS_SCORE = {
            "confirmed_available": 95, "confirmed_parity": 95,
            "confirmed_partial": 55, "pending_verification": 40,
            "confirmed_unavailable": 5, "unknown": 30,
        }

        dim_scores: dict[str, list[float]] = {}
        for fid, spec in features.items():
            dim = spec.get("dimension", "feature_availability")
            amd = spec.get("amd_status", "unknown")
            score = STATUS_SCORE.get(amd, 30)
            dim_scores.setdefault(dim, []).append(score)

        dimensions = {}
        for dim, scores in dim_scores.items():
            dimensions[dim] = {
                "score": round(sum(scores) / len(scores), 1) if scores else None,
                "feature_count": len(scores),
            }

        all_dim_scores = [d["score"] for d in dimensions.values() if d.get("score") is not None]
        overall = round(sum(all_dim_scores) / len(all_dim_scores), 1) if all_dim_scores else 50.0

        gaps_out = []
        for gap in known_gaps:
            fid = gap.get("feature", "")
            feat = features.get(fid, {})
            gaps_out.append({
                "type": self._gap_type_from_dim(feat.get("dimension", "feature_availability")),
                "name": feat.get("name", gap.get("description", fid)),
                "severity": gap.get("severity", "medium"),
                "nvidia_status": feat.get("nvidia_status", "available"),
                "amd_status": feat.get("amd_status", "unknown"),
                "nvidia_hw": "H100",
                "amd_hw": gap.get("hardware_context", "MI300X").split("vs")[-1].strip() if "vs" in gap.get("hardware_context", "") else "MI300X",
                "hw_generation_match": True,
                "blocker": gap.get("blocker", ""),
                "evidence_url": gap.get("tracking_issue", ""),
                "evidence_type": "expert",
                "confidence": "high",
            })

        for fid, spec in features.items():
            amd = spec.get("amd_status", "unknown")
            if amd in ("confirmed_available", "confirmed_parity"):
                continue
            if any(g.get("name") == spec.get("name") for g in gaps_out):
                continue

            agent_findings = spec.get("agent_findings", [])
            best_url = ""
            best_type = ""
            for f in agent_findings:
                if f.get("url"):
                    best_url = f["url"]
                    best_type = f.get("type", "")
                    break

            severity = "low"
            if amd in ("confirmed_unavailable",):
                severity = "high"
            elif amd in ("pending_verification", "unknown"):
                severity = "medium"
            elif amd in ("confirmed_partial",):
                severity = "medium"

            gaps_out.append({
                "type": self._gap_type_from_dim(spec.get("dimension", "feature_availability")),
                "name": spec.get("name", fid),
                "severity": severity,
                "nvidia_status": spec.get("nvidia_status", "available"),
                "amd_status": amd,
                "nvidia_hw": "H100",
                "amd_hw": "MI300X",
                "hw_generation_match": True,
                "blocker": (spec.get("expert_notes") or "")[:150],
                "evidence_url": best_url,
                "evidence_type": best_type or "expert",
                "confidence": spec.get("confidence", "medium"),
            })

        summary = kb.get("project", {}).get("expert_summary", "")

        return {
            "overall_score": overall,
            "model_support": dimensions.get("model_support", {}),
            "feature_availability": dimensions.get("feature_availability", {}),
            "performance_parity": dimensions.get("performance_parity", {}),
            "kernel_backend": dimensions.get("kernel_backend", {}),
            "engineering_maturity": dimensions.get("engineering_maturity", {}),
            "gaps": gaps_out,
            "summary": summary.strip(),
        }

    @staticmethod
    def _gap_type_from_dim(dimension: str) -> str:
        return {
            "model_support": "model",
            "feature_availability": "feature",
            "performance_parity": "performance",
            "kernel_backend": "kernel",
            "engineering_maturity": "feature",
        }.get(dimension, "feature")

    async def _skill_pipeline_analysis(
        self,
        project_id: str,
        project_name: str,
        category: str,
        crawl_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Fallback: SKILL pipeline for projects without expert knowledge base."""
        from app.agent.taxonomy import get_taxonomy_for_project, merge_dynamic_features
        from app.agent.classifier import classify_evidence
        from app.agent.assembler import assemble_matrix, format_matrix_for_summary

        base_taxonomy = get_taxonomy_for_project(category)
        dynamic_features = await self._discover_features(
            project_id, category, crawl_data, base_taxonomy
        )
        taxonomy = merge_dynamic_features(base_taxonomy, dynamic_features)
        logger.info("SKILL taxonomy: %d features", len(taxonomy))

        tagged = await classify_evidence(
            self.call, project_id, category, taxonomy, crawl_data
        )

        assembled = assemble_matrix(taxonomy, tagged, crawl_data.get("stats"))
        summary = await self._generate_summary(project_id, project_name, assembled)
        return self._assembled_to_report(assembled, summary)

    async def _discover_features(
        self,
        project_id: str,
        category: str,
        crawl_data: dict[str, Any],
        base_taxonomy: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        readme = (crawl_data.get("overview", {}).get("readme") or "")[:3000]
        if not readme:
            return []

        existing = ", ".join(base_taxonomy.keys())
        prompt = (
            f"Project: {project_id} (category: {category})\n\n"
            f"README excerpt:\n{readme}\n\n"
            f"Existing feature taxonomy IDs: {existing}\n\n"
            "List any GPU-related features of this project that are NOT already "
            "covered by the existing taxonomy. Focus on features relevant to "
            "AMD vs NVIDIA parity.\n\n"
            "Return a JSON array of objects, each with:\n"
            '- "id": snake_case identifier\n'
            '- "dimension": one of model_support|feature_availability|performance_parity|kernel_backend|engineering_maturity\n'
            '- "description": brief description\n'
            '- "check_keywords": list of search keywords\n\n'
            "If no additional features needed, return an empty array [].\n"
            "Return ONLY the JSON array, no markdown fences."
        )
        try:
            raw = await self.call(
                "You extract GPU feature taxonomies from project READMEs. Return JSON only.",
                prompt,
                max_tokens=1500,
            )
            import re as _re
            raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
            parsed = json.loads(raw if raw.startswith("[") else raw[raw.find("["):raw.rfind("]")+1])
            if isinstance(parsed, list):
                return [f for f in parsed if isinstance(f, dict) and f.get("id")]
        except Exception:
            logger.warning("Feature discovery failed for %s, using base taxonomy only", project_id)
        return []

    async def _generate_summary(
        self,
        project_id: str,
        project_name: str,
        assembled: dict[str, Any],
    ) -> str:
        from app.agent.assembler import format_matrix_for_summary
        matrix_text = format_matrix_for_summary(assembled)

        prompt = (
            f"Project: {project_name} ({project_id})\n\n"
            f"Analysis matrix:\n{matrix_text}\n\n"
            "Write a 2-3 sentence summary of AMD support status and key gaps. "
            "Be specific — mention concrete features, model names, and evidence. "
            "Do not invent facts not in the matrix above."
        )
        try:
            raw = await self.call(
                "You summarize AMD vs NVIDIA gap analysis. Be concise and factual.",
                prompt,
                max_tokens=500,
            )
            import re as _re
            raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
            return raw
        except Exception:
            logger.warning("Summary generation failed for %s", project_id)
            return f"Analysis complete for {project_name}. Overall score: {assembled['overall_score']}/100."

    def _assembled_to_report(self, assembled: dict[str, Any], summary: str) -> dict[str, Any]:
        dims = assembled.get("dimensions", {})
        gaps_out = []
        for g in assembled.get("gaps", []):
            gaps_out.append({
                "type": g.get("type", "feature"),
                "name": g.get("name", ""),
                "severity": g.get("severity", "medium"),
                "nvidia_status": g.get("nvidia_status", "available"),
                "amd_status": g.get("amd_status", "unknown"),
                "nvidia_hw": "H100" if g.get("hardware") in ("MI300X", "all", "unknown") else "B200",
                "amd_hw": g.get("hardware", "MI300X") if g.get("hardware") != "unknown" else "MI300X",
                "hw_generation_match": g.get("hw_generation_match", True),
                "blocker": g.get("blocker", ""),
                "evidence_url": g.get("evidence_url", ""),
                "evidence_type": g.get("evidence_type", ""),
                "confidence": g.get("confidence", "medium"),
            })

        return {
            "overall_score": assembled["overall_score"],
            "model_support": dims.get("model_support", {}),
            "feature_availability": dims.get("feature_availability", {}),
            "performance_parity": dims.get("performance_parity", {}),
            "kernel_backend": dims.get("kernel_backend", {}),
            "engineering_maturity": dims.get("engineering_maturity", {}),
            "gaps": gaps_out,
            "summary": summary,
        }

    async def discover_projects(
        self,
        current_projects: list[str],
        category_hint: str = "",
    ) -> list[dict[str, Any]]:
        existing = json.dumps(current_projects, ensure_ascii=False)
        system_prompt = (
            "You suggest open-source LLM infrastructure projects worth monitoring for AMD/NVIDIA parity. "
            "Return JSON only: a JSON array of objects. No prose outside JSON."
        )
        hint = category_hint.strip() or "any relevant LLM infra (kernels, serving, training stacks, ROCm/CUDA bridges)"
        user_prompt = f"""Already monitored (do not repeat these names/repos): {existing}
Category focus hint: {hint}

Return a JSON array where each element has exactly these string fields:
repo (owner/name), name, category, description, reason (why monitor for gap analysis).

Prefer active, influential projects with public repos. Avoid duplicates with the list above.
"""
        raw = await self.call(system_prompt, user_prompt, temperature=0.35)
        items = self._decode_json_array(raw)
        result: list[dict[str, Any]] = []
        required = {"repo", "name", "category", "description", "reason"}
        for item in items:
            if isinstance(item, dict) and required.issubset(set(item.keys())):
                result.append(
                    {
                        "repo": str(item["repo"]),
                        "name": str(item["name"]),
                        "category": str(item["category"]),
                        "description": str(item["description"]),
                        "reason": str(item["reason"]),
                    }
                )
        if not result:
            raise ValueError("Model did not return any valid project suggestions")
        return result
