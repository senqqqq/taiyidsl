"""Register an existing native class once; never import or execute it here.

This uses the offline GreenMOT TTL fixture. For the reviewed actual GreenMOT
source use GreenMOTAdapter(greenmot_source=...) instead, retaining its strict
preparation-tail adapter. These declarations do not retrieve camera images.
"""

from pathlib import Path

from taiyidsl import NativeKernelPackage, ms, ns

package = NativeKernelPackage.from_package(
    root=Path(__file__).resolve().parents[1] / "greenmot_native",
    entry="greenmot_fixture:GreenMOTFixture",
    resources=("ttl_556_3DMOT_Sh", "ttl_Camera_Trig"),
    initialize_method="initialize_native",
    finalize_method="finalize_native",  # this legacy hook runs AFTER EACH SHOT
)
_prepare = package.kernel("prepare_MOT", timing="prefix")
_finish = package.kernel(
    "finish_preparation",
    timing="timed",
    duration=15 * ms + 16 * ns,
    end_boundary="Fixture TTL falling edge; no analog preparation model",
)
readout = package.kernel("execute_readout", timing="suffix", duration=100.48 * ms)


def prepare_MOT():
    """Expose variable preparation plus its fixed final boundary as one call."""
    _prepare()
    _finish()
