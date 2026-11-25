# Permission model — draft

The coding agent acts under the invoking user's operating-system and service permissions. Project instructions and proposed OpenCode permissions are not a substitute for filesystem access control, branch protection, or human review.

| Operation | Draft policy | Enforcement / status |
|---|---|---|
| Inspect project source and tests | Allowed for the assigned task | Review project scope and tool configuration. |
| Modify project files | Within the authorized task and repository rules | Changes are reviewed in a branch and tested; this draft is not proof of enforcement. |
| Read or expose credentials | Do not disclose or include credentials in evidence | Redact transcripts; `.env` is ignored by Git, but ignored files can still be present in a workspace or archive. |
| Network and external model calls | Use only when needed and authorized | Note provider, scope, cost and results without exposing credentials; do not claim live verification without evidence. |
| Push, merge, or change repository settings | Follow explicit team authorization and branch rules | GitHub permissions and branch protection govern actual enforcement; see `docs/code-governance/branch-protection.md` for the API read-back. |
| MCP tools | No MCP integration is claimed by this draft | `docs/code-governance/mcp-tool-manifest.md` records inventory status. |

`.opencode/permissions.jsonc` is a **reviewable manifest**, not a claim that OpenCode loads it automatically. The separate root `opencode.jsonc` is a candidate configuration using the `permission` format accepted by the installed OpenCode 1.18.30. On 2026-11-25, `opencode debug config --pure` returned successfully and its resolved configuration contained the permission object and references to both project instruction files. This confirms configuration resolution only; individual allow/deny/ask actions were not tested. Shell commands retain the invoking user's OS permissions, so file-read rules alone cannot prevent shell access. The team must review the policy and run permission enforcement tests before claiming a hard security boundary.
