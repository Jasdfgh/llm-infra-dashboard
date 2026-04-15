You are an expert in GPU computing libraries. Given an NVIDIA GPU library/project, identify if there is a corresponding AMD ROCm project that serves the same purpose.

## Project: {project_name}
## Repository: {repo}

### Description: {description}

### README excerpt:
{readme_excerpt}

---

## Known NVIDIA-to-AMD Mappings
{known_mappings}

## Task

1. Based on this project's purpose, determine if there is a well-known AMD ROCm equivalent.
2. Search your knowledge for repositories under the `ROCm/` organization or other AMD-affiliated orgs.
3. Consider alternative projects maintained by the community that serve the same GPU computing purpose.

Return a JSON object:
```json
{{
  "project_id": "{project_id}",
  "nv_repo": "{repo}",
  "has_amd_counterpart": true|false,
  "amd_counterpart": {{
    "repo": "org/repo-name",
    "name": "Project Name",
    "confidence": "high|medium|low",
    "reasoning": "Why you believe this is the AMD counterpart"
  }},
  "alternative_candidates": [
    {{
      "repo": "org/repo-name", 
      "name": "Project Name",
      "relevance": "high|medium|low",
      "reasoning": "Why this might be relevant"
    }}
  ]
}}
```

If no clear AMD counterpart exists, set `has_amd_counterpart` to false and explain in reasoning what capabilities AMD is missing.
