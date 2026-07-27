# Third-Party Notices

This project's own code is MIT licensed (see [`LICENSE`](LICENSE)). It pins
two runtime dependencies, both confirmed MIT via `importlib.metadata` at the
versions pinned in `requirements.txt`/`pyproject.toml`:

| Package | Version | License | Project |
|---|---|---|---|
| PyYAML | 6.0.1 | MIT | <https://pyyaml.org/> |
| jsonschema | 4.10.3 | MIT | <https://github.com/python-jsonschema/jsonschema> |

This table is a spot-check performed for this project's own use, not a
substitute for a full license-review pass (including transitive
dependencies) before relying on it for compliance purposes.

## Worker images

The optional `media-worker` (FFmpeg) and `ts-worker` (TSDuck) Docker images
are built from separate recipes under `docker/`, are never bundled into the
published simulator image, and carry their own license notices and
provenance documentation. See [`LICENSING.md`](LICENSING.md) and
[`WORKER_PROVENANCE.md`](WORKER_PROVENANCE.md) for the full detail on FFmpeg
(GPL, locally built only) and TSDuck (BSD-2-Clause, distributed separately).
