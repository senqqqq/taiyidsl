"""Offline TTL fixture for a variable-duration preparation prefix."""

from artiq.experiment import HasEnvironment, delay, kernel, us


class MOT(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("native_prepare")
        self.initializations = 0
        self.completed_shots = 0
        self.finalizations = 0

    @kernel
    def initialize(self):
        self.initializations += 1
        self.native_prepare.off()

    @kernel
    def prepare_MOT(self):
        self.core.break_realtime()
        self.native_prepare.on()
        delay(100 * us)

    @kernel
    def after_shot(self):
        self.completed_shots += 1
        self.native_prepare.off()

    @kernel
    def finalize(self):
        self.finalizations += 1
        self.native_prepare.off()
