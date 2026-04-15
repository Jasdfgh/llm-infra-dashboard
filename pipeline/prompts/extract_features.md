You are an expert in GPU computing and LLM infrastructure. Analyze the following project information and extract its key features relevant to NVIDIA and AMD GPU support.

## Project: {project_name}
## Repository: {repo}

### README excerpt:
{readme_excerpt}

### Repository structure:
{repo_structure}

### ROCm/HIP mentions in README:
{rocm_mentions}

### GitHub metrics:
- Stars: {stars}
- Open issues: {open_issues}
- ROCm-related open issues: {rocm_issues_open}
- ROCm-related merged PRs: {rocm_prs_merged}
- Has ROCm CI: {has_rocm_ci}
- Has ROCm Dockerfile: {has_rocm_dockerfile}

---

## Task

1. Extract the **most important features** of this project (5-12 features that are critical for LLM infrastructure).
2. For each feature, assess the support level on both NVIDIA and AMD:

Support levels:
- `first-class`: Fully supported, tested, and documented
- `experimental`: Works but may have issues, limited testing
- `community`: Community-contributed support, not officially maintained
- `none`: Not supported or no known path to support

3. For features where AMD support is less than NVIDIA, identify specific **blockers** (e.g., "depends on cuBLAS", "needs ROCm kernel port", "NCCL-specific code").

4. Provide an overall `rocm_support_level` assessment for the project.

## Output Format

Return a JSON object with this exact structure:
```json
{{
  "project_id": "{project_id}",
  "repo": "{repo}",
  "rocm_support_level": "first-class|experimental|community|none",
  "features": [
    {{
      "name": "Feature Name",
      "nvidia_support": "first-class|experimental|community|none",
      "amd_support": "first-class|experimental|community|none",
      "description": "Brief description of the feature and its significance",
      "blockers": ["List of specific blockers for AMD support"],
      "priority": 1-5
    }}
  ],
  "key_blockers": [
    {{
      "description": "Description of the blocker",
      "impact": "high|medium|low",
      "workaround": "Any known workaround"
    }}
  ],
  "summary": "2-3 sentence summary of AMD support status and key gaps"
}}
```

Priority scale: 1=critical (fundamental for LLM infra), 5=nice-to-have.
