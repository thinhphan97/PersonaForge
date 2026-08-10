# PersonaForge third-party model dependencies

This directory is the boundary for upstream avatar/model dependencies. Upstream repositories are
not imported by the application directly. They are fetched at the exact commit declared in
`manifests/*.json`, then wrapped by PersonaForge adapters.

The repositories and model checkpoints are intentionally not committed into the main source tree.
This keeps the application repository small and avoids silently changing model code. Use the
manifest and the Phase 0 tooling to fetch and verify them into a local cache/volume.

Required properties for a reproducible checkout:

- source repository commit SHA is pinned;
- model repository revision is pinned;
- expected artifact size and SHA-256 are recorded;
- renderer package version and integrity are pinned;
- model files are stored outside the container image and mounted read-only at runtime.

Do not update an upstream commit or checkpoint without updating its manifest, license review and
compatibility test results.
