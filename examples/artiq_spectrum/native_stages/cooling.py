"""A separate fixed-duration cooling boundary fixture, not atom cooling."""

from artiq.experiment import HasEnvironment, delay, kernel


class Cooling(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("native_prepare")

    @kernel
    def GM_cooling(self, duration):
        self.native_prepare.on()
        delay(duration)
        self.native_prepare.off()
