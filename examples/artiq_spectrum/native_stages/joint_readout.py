"""Z-basis readout illumination fixture, independent of a qubit's final frame.

The generated acquisition TTL triggers the camera first. This separate native
TTL illustrates an illumination/readout stage and cannot establish real atom
state classification. Replace the complete component for laboratory readout.
"""

from artiq.experiment import HasEnvironment, delay, kernel


class JointReadout(HasEnvironment):
    def build(self):
        self.setattr_device("core")
        self.setattr_device("native_readout")

    @kernel
    def initialize(self):
        self.native_readout.off()

    @kernel
    def execute_readout(self, duration):
        self.native_readout.on()
        delay(duration)
        self.native_readout.off()
