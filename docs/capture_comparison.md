# Compare a capture with final DAC samples

The comparison reads an existing NPZ capture offline and replays the **packed DAC samples of a particular compiled artifact**. A configured output range converts DAC codes to nominal volts. Cable response, reconstruction filters, amplifiers, AOMs and the optical field are outside this model.

```python
from taiyidsl import write_capture_comparison

paths = write_capture_comparison(
    compiled_or_store,
    "capture.npz",
    capture_metadata,
    "output/comparison.html",
    exploratory_alignment=False,
)
```

Supply a one-dimensional `voltage` array in volts. Optional `time_s` must agree with the declared sample rate and starting time. The default limit is 100,000 capture samples with a bounded decode window. Select a shorter interval before comparing longer captures. Optional envelope, `reference_phase_rad` and `phase_uncertainty_deg` inputs support the existing phase-fit diagnostics.

Metadata identifies `artifact_sha256`, global `point`, `shot`, `attempt`, `run_id`, `group`, `card`, `channel`, `sample_rate_hz`, `time_origin_s`, `deskew_ns`, `calibration_revision` and the boolean `synthetic`. Use the compiled integrity hash or `CompilationStore.sha256` and the calibration revision used to compile that group.

The first sample's `time_origin_s` is relative to logical shot T0. A positive deskew is an independently measured probe/cable delay and is subtracted from capture time. Across different sample rates the reference uses zero-order hold on the actual DAC data. The offline HTML embeds Plotly, and the companion JSON records raw residuals, source identity and capture hash.

Set `exploratory_alignment=True` for an additional limited timing search and gain/DC fit. The raw residual remains available and `physical_acceptance` stays `not_evaluated`. The synthetic example adds a deliberate 10 mV offset so the raw discrepancy remains visible even after a close fit.

```powershell
uv run --locked python -m examples.artiq_spectrum.capture_comparison
```
