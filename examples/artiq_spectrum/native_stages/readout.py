"""Independent camera-trigger fixture with explicit terminal-frame context."""

from artiq.experiment import HasEnvironment, delay, kernel


class Readout(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("native_camera")
        self.last_frame = 0.0

    @kernel
    def initialize(self):
        self.native_camera.off()

    @kernel
    def execute_readout(self, duration):
        self.last_frame = self.native_terminal_f_q
        self.native_camera.on()
        delay(duration)
        self.native_camera.off()
