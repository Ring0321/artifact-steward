# Changelog

## 2.1.0

- Unified the portable skill and removed Codex-only session parsers and obsolete implementation planning from distribution.
- Added explicit per-file duplicate judgments, declared text-reference checks, stable-keeper requirements and full ZIP-member verification.
- Reject circular keeper dependencies and require an independent protected copy for a cleanup proposal.
- Reject duplicate paths or file objects so conflicting roles cannot produce contradictory deletion advice.
- Protect known application-managed paths in classification and ordinary execution; require an explicit operation in every imported plan.
- Bind version digest and document inspection to an unchanged file identity so a concurrent save cannot inherit prior acceptance.
- Include speaker notes, slide order and hidden slides in presentation differences.
- Record restoration path conflicts per item and continue restoring unrelated files.
- Added portable synthetic regression tests, release-content checks, reproducible skill packaging and CI.
