"""Native bridge to the user's external lib._GreenMOTbase package.

The generated ARTIQ program imports this pinned module. It is not imported by
the offline DSL builder. Device initialization and kernels execute only when a
configured ARTIQ experiment is deliberately run.
"""

from artiq.experiment import delay, delay_mu, kernel, ms, us
from lib._GreenMOTbase import _GreenMOTbase


class GreenMOTStages(_GreenMOTbase):
    """Keep existing MOT/imaging/cleanup bodies; adapt only their boundaries."""

    def build(self):
        _GreenMOTbase.build(self)
        if not self.enable_imaging_399_hor or self.enable_imaging_556_ver:
            raise ValueError("This adapter declares fixed 399-horizontal imaging only")
        if self.core.ref_period != 1e-9 or self.zotino0.bus.ref_period_mu != 8:
            raise ValueError(
                "GreenMOT strict tail requires 1 ns mu and 8 mu Zotino SPI"
            )

    @kernel
    def initialize_native(self):
        # The original reset and device initializations happen once, before shots.
        self.init_hardware()
        # imaging_399_hor begins with a falling edge; make the initial level explicit.
        self.ttl_Camera_Trig.on()
        delay(10 * us)

    @kernel
    def prepare_MOT(self):
        # Original run's lead; then original prepare_GreenMOT up to its 15 ms hold.
        # The offline builder proves prefix + original tail reproduces that body.
        self.core.break_realtime()
        delay(5 * ms)

        self.start_lasers()
        self.start_B_field()

        self.AOM_399_Push.sw.off()
        self.ttl_399_Push_Sh.off()
        self.AOM_399_2DMOT.sw.off()
        self.AOM_399_zeeman.sw.off()

        self.core.break_realtime()
        delay(10 * ms)
        self.start_556_fixed_sweep()
        delay(5 * ms)
        self.ttl_399_3DMOT_Sh.off()
        self.AOM_399_3DMOT.sw.off()

        for i in range(100):
            self.zotino0.set_dac([3.5 + (0.15 - 3.5) / 100 * i], [3])
            delay((30 / 100) * ms)
        delay(5 * ms)

        self.core.break_realtime()
        self.compress_green_MOT_DDS()
        self.ttl_399_Probe_Sh.on()

    @kernel
    def finish_preparation(self):
        # Original final three statements: hold, light off, magnetic field update.
        delay(15 * ms)
        self.AOM_556_3DMOT.sw.off()
        self.zotino0.set_dac([-0.95, 0.0], [2, 3])
        # set_dac restores its input cursor despite queuing a finite LDAC pulse.
        # Advance to the final digital edge; analog settling remains unverified.
        delay_mu(2 * self.zotino0.bus.ref_period_mu)

    @kernel
    def execute_readout(self):
        # Three camera triggers; 3 * (150 + 10) us + 2 * 50 ms = 100.48 ms.
        self.imaging_399_hor()

    @kernel
    def finalize_native(self):
        # Despite its historical name, this is the original end-of-shot cleanup.
        # Its wait_until_mu belongs after readout, outside the strict gate window.
        self.initialization()
