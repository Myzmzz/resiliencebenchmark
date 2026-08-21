# Private Evaluator Bundle Policy

This public file documents the boundary only. It is not a storage location for formal evaluation answers.

The public repository defines `schemas/ground-truth.schema.json` so the Evaluator input contract remains reviewable. Concrete hidden instances must be loaded at run time from a private bundle or evaluation service mounted outside every Agent-visible workspace.

Publicly allowed material:

- schema references and version rules;
- redacted, unlocked examples that do not reveal an implanted component or patch;
- documentation describing how the Evaluator receives private data.

Forbidden material:

- hidden answers for locked Episodes;
- exact defect patches, healthy diffs, root-cause filenames, or private issue links;
- Oracle credentials and environment access credentials;
- private cluster identities or private source paths.

The runtime must fail closed when the private bundle path overlaps the Agent workspace, source snapshot, Harness transcript directory, or MCP filesystem roots.
