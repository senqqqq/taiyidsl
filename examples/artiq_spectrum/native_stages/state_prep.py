"""A separate fixed-duration state-preparation boundary fixture."""

from artiq.experiment import HasEnvironment, delay, kernel


class StatePreparation(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("native_prepare")

    @kernel
    def state_prep(self, duration):
        self.native_prepare.on()
        delay(duration)
        self.native_prepare.off()
