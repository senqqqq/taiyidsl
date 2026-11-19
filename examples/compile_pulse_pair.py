"""Print the generated program and the digital events observed offline."""

from examples.pulse_pair import pulse_pair
from examples.schedule_pulse_pair import example_wiring
from taiyidsl import compile_experiment
from taiyidsl.backends.artiq.codegen import generate_source
from taiyidsl.backends.artiq.verification import verify_export

if __name__ == "__main__":
    compiled = compile_experiment(pulse_pair(), *example_wiring())
    source = generate_source(compiled, mode="unrolled")
    print(source)
    print(verify_export(compiled, source=source))
