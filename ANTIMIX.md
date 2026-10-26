# Marker Antimix (base upstream 2.0.0)

This fork starts at upstream commit `947d7688c0739297a7b9eb08b1a463e3a6853981`.
The table changes formerly applied by scripts to `site-packages` are now part
of `marker/processors/table.py` and `marker/processors/table_recon.py`.
The existing PySide6 GUI is included as the `marker_antimix_gui` entry point.

## Reproducible Linux CPU install

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

Known issue from the baseline comparison on
`ccnl_commercio_terziario_conflavoro.pdf`, pages 82–83: `ICT Trainer` and
`Network Specialist` can be merged into one row. The old environment and this
fork produce byte-identical Markdown for this test, so it is a pre-existing
quality defect, not a packaging regression. Do not claim that the table
fidelity goal has been achieved until this and other samples are fixed.
