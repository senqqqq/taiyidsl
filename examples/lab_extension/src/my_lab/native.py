"""ARTIQ camera-trigger fixture, not camera transfer or calibrated readout."""

from artiq.experiment import HasEnvironment, delay, kernel


class CameraTrigger(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("camera_trigger")

    @kernel
    def initialize(self):
        self.camera_trigger.off()

    @kernel
    def trigger(self, width):
        self.camera_trigger.on()
        delay(width)
        self.camera_trigger.off()

    @kernel
    def finalize(self):
        self.camera_trigger.off()
