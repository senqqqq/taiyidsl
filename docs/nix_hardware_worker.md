# Linux ARTIQ + Spectrum worker

Use the project's uv Python 3.12 environment for authoring and Jupyter. A
separate Nix Python 3.13 runs the installed TaiyiDSL worker, ARTIQ 9 and the
Spectrum SDK. Only that worker owns the core and cards in a formal session.
This is direct DeviceManager access, not submission to the ARTIQ master. The
lab must arrange exclusive access. Building and diagnosing the environment do
not grant permission to connect, configure clocks or run a shot.

## Build and select the launcher

On x86_64 Linux with Nix flakes enabled, from the checkout:

```sh
python3 scripts/build_nix_worker.py
export TAIYIDSL_ARTIQ_PYTHON="$PWD/local_config/hardware-worker/bin/taiyidsl-artiq-python"
.venv/bin/python -m taiyidsl.runtime.worker_env
```

The build helper stages only the package source, viewer resources and build
metadata. In particular, it does not send ignored data, lab databases, the
working tree's `.git` directory or calibration files to the Nix store. It also
includes new package modules in an uncommitted checkout. In a fully committed
checkout `nix build .#hardware-worker` and `nix develop .#hardware` are the
corresponding flake outputs. No change to the master flake or gateware is
required. The flake pins the lab's ARTIQ 9 input; the two Spectrum wheels use
hashes from `uv.lock`. The native driver remains a host prerequisite.

The first check compares the installed Python source fingerprint and protocol
with the host, checks package locations/versions and locates LLD and LLVM strip.
It does not import the Spectrum SDK, load its native library or open a card.
For ARTIQ-only sessions use `--artiq-only`. To test the native library **without
calling its functions**, run:

```sh
.venv/bin/python -m taiyidsl.runtime.worker_env --load-driver
```

If the Nix process cannot find `libspcm_linux.so`, make a lab-local directory
containing just a symlink to the lab's existing, trusted vendor library, and
set `TAIYIDSL_SPCM_LIB_DIR` to its absolute path. The launcher adds this only
to the worker's search path. **Do not add the system `/usr/lib` or an entire
host Python environment.** Do not set `SPCM_NO_DLL` to pass a check. A successful
load does not establish device permissions, driver/card compatibility or PLL
lock. The real SDK is imported before DeviceManager is constructed on an
explicit hybrid connection; it can log its version at that time.

Launch Jupyter with these variables inherited by the **kernel**, or pass the
launcher through `LabConfig(..., artiq_python=...)` explicitly (this wins over
the environment setting). The old compile-only launcher is not a hybrid worker.
Linux fails with a configuration error if neither option is set; Windows retains
the repository's MSYS2 toolchain fallback. An installed Linux worker never uses
the authoring venv's site-packages. When TaiyiDSL starts the worker it sets
`MPLBACKEND=Agg` only in the child process: the Spectrum SDK imports `pyplot`,
but the worker does not need a notebook display. Leave Jupyter's inline backend
unchanged; no IPython plotting package is required in the worker.

## Execution and evidence

Confirm exclusive access before calling `lab.connect()`. External reference defaults to measured calibration. An AWG group may instead explicitly choose uncorrected setpoints with external reference or internal PLLs. See [configure clocks and calibration](hybrid_session_options.md). The worker sends a software report before
receiving a configuration or touching devices. A mismatched checkout/worker
must be rebuilt, not forced. The report is saved as `worker_environment.json`
alongside `connection.json`, `worker.log` and per-run exports/receipts. Native
and SDK output go to the log, not the JSON protocol. A lost connection does not
justify repeating a shot. Only readbacks, not clock continuity or analog timing, are established by the session.

To test a built Nix worker **without connecting to instruments**, run the suite
below. Its import test inherits Jupyter's inline plotting setting on the host
and imports the real Python Spectrum SDK in the worker, replacing the native
Spectrum library with a double. The session tests replace both instrument
endpoints and use synthetic calibration only for those doubles:

```sh
TAIYIDSL_TEST_NIX_PYTHON="$TAIYIDSL_ARTIQ_PYTHON" \
  .venv/bin/python -m pytest -q tests/test_nix_worker.py --basetemp=.test-tmp-pytest
```

Before switching deployments, retain a named Nix output link to the last working
build and its matching host source/lock. Roll both back together. Reverting
software does not restore a clock lock or undo an emitted shot.
