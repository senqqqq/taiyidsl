"""OFFLINE NATIVE KERNEL FIXTURE: not a physical GreenMOT implementation.

This exercises a real @kernel call graph with TTL timing and a variable-slack
prefix. It does not model atom loading, Zotino fields, DRG ramps or cooling.
The actual-source mode uses greenmot_stages.py and the external GreenMOT file.
"""

from artiq.experiment import EnvExperiment, delay, delay_mu, kernel, ms, now_mu, us


class GreenMOTFixture(EnvExperiment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("ttl_556_3DMOT_Sh")
        self.setattr_device("ttl_Camera_Trig")

    @kernel
    def initialize_native(self):
        self.core.reset()
        self.ttl_556_3DMOT_Sh.off()
        self.ttl_Camera_Trig.on()
        delay(10 * us)

    @kernel
    def prepare_MOT(self):
        self.core.break_realtime()
        self.ttl_556_3DMOT_Sh.on()
        delay(1 * ms)

    @kernel
    def finish_preparation(self):
        # A real emitted final TTL edge marks the fixture's preparation end.
        # No Zotino/atom dynamics are simulated by this fixture.
        delay(15 * ms)
        delay_mu(16)
        self.ttl_556_3DMOT_Sh.off()

    @kernel
    def execute_readout(self):
        # The same camera time intervals as imaging_399_hor, without probe light.
        self.ttl_Camera_Trig.off()
        delay(160 * us)
        self.ttl_Camera_Trig.on()
        delay(50 * ms)
        self.ttl_Camera_Trig.off()
        delay(160 * us)
        self.ttl_Camera_Trig.on()
        delay(50 * ms)
        self.ttl_Camera_Trig.off()
        delay(160 * us)
        self.ttl_Camera_Trig.on()

    @kernel
    def finalize_native(self):
        self.ttl_556_3DMOT_Sh.off()
        self.core.wait_until_mu(now_mu())
