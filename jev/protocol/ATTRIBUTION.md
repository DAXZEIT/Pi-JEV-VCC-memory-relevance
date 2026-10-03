# Attribution — vendored simple-jev protocol code

This directory vendors code from [featherless-ai/simple-jev](https://github.com/featherless-ai/simple-jev),
distributed under the **Apache License 2.0** (full text in `LICENSE` alongside this file).

- **Upstream pin:** commit `9c11582` (2026-10-03, "Add native CLEF and
  CLEF-Flash decision-head backend")
- **Status: UNMODIFIED.** Vendored byte-identical; verified against upstream
  HEAD on 2026-10-03 (`cmp` on every file below). First vendoring
  2026-09-30 with sha256 verification (see `../NOTES.md`, section
  "Fidélité du protocole").
- No per-file copyright notices exist upstream; the repo-level `LICENSE`
  (copied here) is the attribution notice, per Apache 2.0 §4.

## File mapping (upstream path → here)

| upstream (`simple-jev` @ 9c11582)        | here                     | sha256 |
|---|---|---|
| `common/__init__.py`          | `common/__init__.py`          | `670f21965e6799ef3b0b28e6f6c9d36d2c067c713f39f5c5886e9bd5a5266896` |
| `common/prompt_builder.py`    | `common/prompt_builder.py`    | `888de1e34ab88a510963d8277095e65f1cf16c8dc4c5b29a6c68e11150ed8375` |
| `common/request_schema.py`    | `common/request_schema.py`    | `4350ba05a3ff54f4d6983da38cf7dd8c6ed804481a86b4d032ce401df684f18d` |
| `common/response_scoring.py`  | `common/response_scoring.py`  | `b1b1303ace5218fb40904e7aaf5a1ad01aa3d072c30678d45e921bb0e8613dfa` |
| `hf-server/hf_prompt_policies.py` | `hf_prompt_policies.py` | `5c376103cb879b5e25554942c9ad8203ef2706147571affe3c1f60a357b7f007` |

Re-verify at any time with:

```bash
sha256sum common/__init__.py common/prompt_builder.py common/request_schema.py \
          common/response_scoring.py hf_prompt_policies.py
```

The surrounding skill code (`../jev`, `../canaris/`, `../presets/`,
`../template_qwen38.j2`, `../SKILL.md`, `../NOTES.md`) is **not** part of
simple-jev and is licensed under the repo's MIT license.
