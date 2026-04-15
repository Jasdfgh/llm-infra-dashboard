You are an expert in GPU computing and LLM infrastructure. Compare the **NVIDIA vs AMD (ROCm) support** within a single project that targets both platforms.

## Project: {project_name}
## Repository: {repo}

### README excerpt:
{readme_excerpt}

### Repository structure:
{repo_structure}

### ROCm-related GitHub activity:
- ROCm mentions in README: {rocm_mentions}
- ROCm-related open issues: {rocm_issues_open}
- ROCm-related merged PRs: {rocm_prs_merged}
- Has ROCm CI: {has_rocm_ci}
- Has ROCm Dockerfile: {has_rocm_dockerfile}

### Key source files related to GPU backends:
{backend_files}

---

## Task

For this project that supports both NVIDIA (CUDA) and AMD (ROCm/HIP):

1. Identify the **key features** (5-12 most important ones for LLM infrastructure).
2. For each feature, honestly assess the maturity level on **NVIDIA** vs **AMD**:

Support levels:
- `first-class`: Fully supported, tested, and documented
- `experimental`: Works but may have issues, limited testing  
- `community`: Community-contributed support, not officially maintained
- `none`: Not supported on AMD

3. For any feature where AMD support lags behind NVIDIA, identify **specific technical blockers**.
4. Be honest and precise — don't overstate AMD support.

## Output Format

Return a JSON object:
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
      "description": "What this feature does and why it matters",
      "blockers": ["Specific blockers preventing AMD parity"],
      "priority": 1-5
    }}
  ],
  "key_blockers": [
    {{
      "description": "Blocker description",
      "impact": "high|medium|low",
      "workaround": "Known workaround if any"
    }}
  ],
  "summary": "2-3 sentence honest assessment of AMD vs NVIDIA gap"
}}
```
