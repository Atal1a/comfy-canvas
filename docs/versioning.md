# Versioning policy

Releases use `MAJOR.MINOR.PATCH` tags prefixed with `v`.

- Patch: compatible bug, documentation, test, or security fixes that do not change workflow/model requirements.
- Minor: new compatible features, workflow revisions, dependency updates, or documented configuration migrations.
- Major: stable-interface breaking changes after `1.0.0`.

The first version is `1.0.0`. Keep `package.json`, the root package entry in `package-lock.json`, and `mobile_server.__version__` in sync. The API metadata uses the Python version constant. A version entry describes prepared source; publication is recorded by the corresponding Git tag and GitHub Release.

Every release records the ComfyUI commit, custom-node lock, model lock revision, supported operating systems, tested GPU/VRAM, migration notes, and known inference limitations. Git tags and release artifacts must never contain model weights or runtime data.
