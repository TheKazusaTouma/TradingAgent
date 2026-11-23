# Trace Matrix — Agent I/O Contracts (v1)

Maps every approved requirement to approved tests, and every approved test back to
requirements. Both directions must remain complete; a requirement with no test, or a test
with no requirement, is a governance defect.

Test definitions live in `docs/verification/agent-contract-test-spec.md` v1.0.
Requirements live in `docs/req/requirements.yaml`.

## Requirements -> Tests

| Req | Tests |
|---|---|
| FR-1 | T-2, T-3, T-4 |
| FR-2 | T-5, T-28 |
| FR-3 | T-6, T-7, T-26 |
| FR-4 | T-8, T-9, T-28 |
| FR-5 | T-10, T-11, T-12, T-27 |
| FR-6 | T-13, T-14, T-19 |
| FR-7 | T-3, T-4 |
| FR-8 | T-15, T-16, T-17, T-18 |
| FR-9 | T-15, T-19 |
| FR-10 | T-13, T-29 |
| FR-11 | T-24 |
| FR-12 | T-23 |
| INV-1 | T-15, T-16, T-17 |
| INV-2 | T-15, T-19 |
| INV-3 | T-3, T-4 |
| INV-4 | T-11, T-12, T-27 |
| INV-5 | T-14 |
| INV-6 | T-2, T-3 |
| INV-7 | T-1, T-24, T-26 |
| INV-8 | T-15, T-20, T-30 |
| INV-9 | T-18, T-19, T-30 |
| INV-10 | T-21, T-22 |
| INV-11 | T-24 |
| INV-12 | T-23 |
| NFR-1 | T-3, T-4 |
| NFR-2 | T-10, T-11, T-27 |
| NFR-3 | T-13, T-29 |
| NFR-4 | T-1 |
| NFR-5 | T-18, T-19, T-25 |
| NFR-6 | T-23 |
| NFR-7 | T-21, T-22 |
| NFR-8 | all tests |

## Tests -> Requirements

| Test | Requirements | Category |
|---|---|---|
| T-1 | INV-7, NFR-4 | Structural / regression |
| T-2 | FR-1, INV-6 | Structural |
| T-3 | FR-1, FR-7, INV-3, INV-6, NFR-1 | Structural |
| T-4 | FR-1, FR-7, FR-8, INV-3, NFR-1 | Structural |
| T-5 | FR-2 | Structural |
| T-6 | FR-3 | Structural |
| T-7 | FR-3 | Structural |
| T-8 | FR-4 | Behavioural (fixture) |
| T-9 | FR-4 | Structural |
| T-10 | FR-5, NFR-2 | Behavioural (fixture) |
| T-11 | FR-5, INV-4, NFR-2 | Structural |
| T-12 | FR-5, INV-4 | Structural |
| T-13 | FR-6, FR-10, NFR-3 | Structural |
| T-14 | FR-6, INV-5 | Structural |
| T-15 | FR-8, FR-9, INV-1, INV-2, INV-8 | Behavioural (full graph fixture) |
| T-16 | FR-8, INV-1 | Behavioural (full graph fixture) |
| T-17 | FR-8, INV-1 | Behavioural (full graph fixture) |
| T-18 | FR-8, INV-9, NFR-5 | Behavioural (full graph fixture) |
| T-19 | FR-6, FR-9, INV-2, INV-9, NFR-5 | Behavioural (full graph fixture) |
| T-20 | INV-8 | Behavioural (CLI, monkeypatched) |
| T-21 | INV-10, NFR-7 | Structural (checkpoint) |
| T-22 | INV-10, NFR-7 | Structural (checkpoint) |
| T-23 | FR-12, INV-12, NFR-6 | Structural |
| T-24 | FR-11, INV-7, INV-11 | Structural |
| T-25 | NFR-5 | Structural |
| T-26 | FR-3, INV-7 | Structural |
| T-27 | FR-5, INV-4, NFR-2 | Structural |
| T-28 | FR-2, FR-4 | Structural |
| T-29 | FR-10, NFR-3 | Structural |
| T-30 | INV-8, INV-9 | Behavioural (full graph fixture) |

## Status

All mappings are approved as design. **No test has been implemented and no result exists.**
When implementation begins, each row gains a result column in
`docs/verification/agent-contract-test-spec.md`; this matrix keeps the mapping stable.
