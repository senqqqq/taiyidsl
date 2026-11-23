# Host sweep chunks and resident Spectrum banks

A chunk determines which points share a card upload and when the host executes their shots. Each shot retains its original coherent gate sequence, times, phase, acquisition coordinates and preparation/readout.

```python
length = SweepParameter("length", [1, 2, 3, 4], "dimensionless")
with exp.average(100, mode="cyclic", scope="global"):
    with exp.sweep(length, chunks=2):
        with exp.realtime():
            # Preparation, triggered sequence and readout
            ...
```

`chunks=2` divides the marked axis into two groups. The first groups receive the extra value when division is uneven. A positive chunk count cannot exceed axis length. Alternatively `chunk_size=2` sets a maximum of two values per group, with a possibly shorter last group. Select one of these options. With no chunk option, points remain individual upload units. `chunks="auto"` chooses contiguous groups that fit each active card.

## Nested axes and execution order

```python
with exp.sweep(detuning):
    with exp.sweep(length, chunks=2):
        with exp.sweep(seed):
            ...
```

One host sweep axis can be chunked. Hold outer coordinates fixed, take a contiguous interval of indices along the marked axis, and include every inner point for each index. Equal axis values at different indices stay distinct. Chunk boundaries and uploads occur outside realtime control.

For cyclic averages, global scope (default) runs each shot across the entire scan. Chunk scope finishes the shots of a chunk before loading the next one. A sequential average completes a point's shots in succession. Chunk scope requires a chunked axis. For two shots and two chunks of two points each, cyclic order is:

```text
global: p0/s0 p1/s0 p2/s0 p3/s0 p0/s1 p1/s1 p2/s1 p3/s1
chunk:  p0/s0 p1/s0 p0/s1 p1/s1 p2/s0 p3/s0 p2/s1 p3/s1
```

Changing scope can change sensitivity to experimental drift. The compact `host_execution_plan` holds chunk membership and point order. `compiled.execution_order()`, generated host code, pulse sheets and results read this plan. Storage uses point/shot/qubit indices. Wall-clock gaps require separate evidence.

## Bank layout and failure behavior

Each chunk, active group and card has a resident bank. Segments with equal length and final quantized SHA-256 can share a slot. Each point has its own step subgraph, entry and END. The runner preflights every card before configuring or uploading. Each shot selects the entry, arms all cards, verifies readiness, executes the ARTIQ kernel and checks completion. A bank change can require DMA. Reusing a resident bank still requires normal per-shot device, clock and calibration checks. Invalidate the runner's residency assumptions after external card reconfiguration.

An idle card in an active group executes its zero program. Skip a group with no work at that point. If all groups are idle the ARTIQ plan still executes. An error during upload, entry selection, arming, readiness, execution or completion stops affected cards in reverse order and invalidates residency.

Validate memory per card using enabled channels, equal slot allocation, longest segment, segment count and step capacity. Check declared limits during compilation and actual hardware limits at deployment. An oversize manual chunk reports the chunk, card and limit. A shot that cannot fit individually needs a different validated representation.

## Artifact formats

Hybrid manifest schema 2 contains `points`, `host_execution_plan` and `banks`. Each bank records `index`, global `points`, `groups` and `sha256`. Each card records `segments`, `steps`, `memory` and entries with `point/entry_step/step_count/segment_ids`. Preserve per-entry recipe provenance even when the final bytes share a slot. Independently compare final replay with the original point program.

Schema 1 remains usable through per-shot upload. In schema 2, `run_point()` dispatches to bank load and resident execution. `SpcmAdapter.select_entry()` configures a stopped card's finite program entry; the trigger starts playback. Successful receipts identify point, shot, execution index, chunk, bank hash, upload status and entries. See [streamed execution](streamed_execution.md) for disk compilation and recovery.
