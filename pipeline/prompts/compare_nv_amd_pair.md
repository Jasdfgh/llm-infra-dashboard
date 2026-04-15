You are an expert in GPU computing frameworks. Compare two repositories that serve similar purposes — one for NVIDIA and one for AMD — and analyze their feature parity.

## NVIDIA Project: {nv_name}
## Repository: {nv_repo}

### README excerpt:
{nv_readme_excerpt}

### Repository structure:
{nv_repo_structure}

### GitHub metrics:
- Stars: {nv_stars}
- Open issues: {nv_open_issues}

---

## AMD Project: {amd_name}
## Repository: {amd_repo}

### README excerpt:
{amd_readme_excerpt}

### Repository structure:
{amd_repo_structure}

### GitHub metrics:
- Stars: {amd_stars}
- Open issues: {amd_open_issues}

---

## Task

1. Identify the **key features** of the NVIDIA project (5-12 most important features).
2. For each NVIDIA feature, check if the AMD project has an equivalent:
   - If yes, compare implementation maturity
   - If no, mark it as an AMD gap
3. Identify any features the AMD project has that NVIDIA doesn't (if applicable).
4. Assess the overall **feature parity** between the two projects.

Support levels:
- `first-class`: Fully implemented and production-ready
- `experimental`: Implemented but may be incomplete or unstable
- `community`: Community-contributed, not officially maintained
- `none`: Not available

## Output Format

Return a JSON object:
```json
{{
  "project_id": "{project_id}",
  "nv_repo": "{nv_repo}",
  "amd_repo": "{amd_repo}",
  "nv_name": "{nv_name}",
  "amd_name": "{amd_name}",
  "comparison": {{
    "shared_features": [
      {{
        "name": "Feature Name",
        "nvidia_support": "first-class|experimental|community|none",
        "amd_support": "first-class|experimental|community|none",
        "description": "Brief description",
        "blockers": ["Blockers for AMD parity"],
        "priority": 1-5
      }}
    ],
    "nv_only_features": [
      {{
        "name": "Feature Name",
        "description": "What it does",
        "nv_only": true
      }}
    ],
    "amd_only_features": [
      {{
        "name": "Feature Name", 
        "description": "What it does",
        "amd_only": true
      }}
    ]
  }},
  "summary": "2-3 sentence assessment of feature parity and key gaps"
}}
```
