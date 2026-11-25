# Technical debt register — draft

| ID | Item | Evidence / consequence | Status or next decision |
|---|---|---|---|
| TD-01 | Trace does not expose SDK-internal retries, complete vendor fallbacks, or certain bare-thread calls. | PR #5; traces only capture callback-visible events. | Document limitation; decide whether to add instrumentation. |
| TD-02 | Existing same-ticker/date state logs, report paths, and checkpoint identities are not all isolated per trace. | PR #5 known limitations. | Decide whether per-attempt artifact isolation is in scope. |
| TD-03 | Contract-specific trace matrix retained outdated status language after tests were added. | Original `docs/architecture/trace-matrix.md` at source revision. | Correct status wording in this draft and align the formal matrix after requirement approval. |
| TD-04 | Type-check baseline was not clean. | PR #4 verification document reports 171 findings across 31 files in an earlier integration check, with no added diagnostics by the reported comparison. | Re-run on the selected final revision before claiming current counts. |
| TD-05 | Tool instructions and permissions are not established as enforced by the mere presence of draft files. | `.opencode/permissions.jsonc` is a proposed manifest. | Validate against the installed OpenCode version and actual project configuration. |
| TD-06 | Formal team requirements, individual transcripts, member contributions and course-spec compliance remain incomplete. | This draft's review status. | Team confirmation and detailed Project Requirement Sheet needed. |
