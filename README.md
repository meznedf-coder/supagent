# supagent 0.4.3: binaries

Built from tag `v0.4.3`. The code is on `main`; this branch only holds the files to install.
Each version has its own branch `binaries-<version>`; the branches of older versions are kept.

| file | what |
|---|---|
| `supagent-0.4.3/supagent-0.4.3.zip` | the wheel, the guide (HTML + PDF), `INSTALL.txt`, an example catalog, checksums |
| `supagent-0.4.3/supagent-0.4.3-pydantic-wheelhouse.zip` | only for Superset 6.0 offline (pydantic; Superset 6.1 already has it) |
| `supagent-0.4.3/SHA256SUMS` | checksums |

Download: open the file on GitHub, then **Download raw file**; check it with `sha256sum -c SHA256SUMS`.
Install: unzip `supagent-0.4.3.zip` and follow `INSTALL.txt` (pip only, one line in `superset_config.py`).
