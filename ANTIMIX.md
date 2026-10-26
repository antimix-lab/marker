# Marker Antimix (base upstream 2.0.0)

This fork starts at upstream commit `947d7688c0739297a7b9eb08b1a463e3a6853981`.
The table changes formerly applied by scripts to `site-packages` are now part
of `marker/processors/table.py` and `marker/processors/table_recon.py`.
The existing PySide6 GUI is included as the `marker_antimix_gui` entry point.

## Reproducible Linux CPU install

Clone the dedicated release branch, not the fork's `master` branch:

```bash
git clone --branch antimix-2.0.0 --single-branch https://github.com/antimix-lab/marker.git
cd marker
```

From this checkout, run:

```bash
bash antimix_gui/install-linux-cpu.sh /home/roberto/.venv/marker-antimix-2.0.0
```

The target directory must not exist. The script builds a local wheel and
installs it into a new environment with the versions in
`antimix_gui/requirements-linux-cpu.lock`. It neither runs `pip --upgrade`
nor downloads Marker from PyPI or upstream GitHub. It does not install APT
packages, touch the old environment, or replace desktop launchers.

Launch the parallel installation with:

```bash
/home/roberto/.venv/marker-antimix-2.0.0/bin/marker_antimix_gui
```

This recipe reproduces the known-working **CPU** dependency set on Python
3.13. GPU support for the RTX 5060 is a separate, unverified task; do not
assume this installation uses CUDA.

## Cutover policy

Before changing the desktop launcher, compare representative scanned and
digital PDFs, especially tables spanning pages and images. Keep the old
environment and cached models for rollback. Do not run the old
`marker-pdf-gui/install_marker.sh` against this fork: it installs the PyPI
package and reapplies text patches to `site-packages`.

The inherited `uv.lock` is upstream's lock for version 2.0.0, not this fork's
CPU installation recipe. Do not use `uv sync` for this installation; use the
script and its separate pinned requirements file above.

The baseline comparison on `ccnl_commercio_terziario_conflavoro.pdf`, pages
82–83, found an existing bug: `ICT Trainer` and `Network Specialist` merged
into one row. The second fork revision separates them while preserving the
five following records. Run `tests/processors/test_antimix_tables.py` with
`MARKER_CCNL_PDF` pointing to an OCRmyPDF-processed copy to check these
regressions. More scanned PDFs, tables and images still need validation before
claiming general fidelity.
