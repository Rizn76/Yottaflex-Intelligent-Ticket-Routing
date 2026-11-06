# Challenge submission checklist

- [x] Source modules and tests reviewed. Run a final secrets/privacy review before publication.
- [x] README setup, generation, validation, analytics, routing, SLA, lifecycle, and known-limitations sections match the implementation.
- [x] Data dictionary and ER diagram reviewed against `sql/schema.sql`.
- [x] Full-scale manifest, validation report, and analytics summary are present for seed `20261022` and required scale.
- [x] Ground truth is separate from operational source features; generated exports are excluded from Git by `.gitignore`.
- [x] Prompt history records AI assistance and actual verification without credentials or sensitive data.
- [x] Full test suite, compilation, dependency checks, database integrity, SQL import, and full-scale reproducibility checks pass.
- [x] Dashboard starts; all pages render and database-changing actions require explicit button presses.
- [x] Required dataset files and exact local generation/validation instructions are included.
- [x] Project status and ignore rule reviewed; no files are staged.
- [ ] If public GitHub submission is desired, create the repository and publish only after an explicit user decision; no repository creation or push is part of this checklist.
