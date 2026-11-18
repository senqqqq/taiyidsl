"""Offline host lifecycle and journal demo; no generated ARTIQ kernel is run.

SpectrumRunner validates real exported segment recipes against mock adapters.
The kernel factory records only lifecycle/dispatch calls. This is explicitly
weaker than interpretation or real compilation of the generated ARTIQ source.
"""

import json
from types import SimpleNamespace

from taiyidsl import RunStore, StreamingRunner
from taiyidsl.runtime.spectrum.awg import SpectrumRunner


class OfflineCard:
    is_hardware = False

    def __init__(self, name, contract, calls):
        self.name, self.contract, self.calls = name, contract, calls
        self.clock_session_id = f"offline-{name}"
        self.armed = False

    def probe(self):
        return {
            **self.contract,
            "firmware": "offline-fixture",
            "sequence_available": True,
            "max_segments": 65536,
            "max_steps": 4096,
            "clock_mode": "external_reference",
        }

    def configure(self, contract):
        self.calls.append((self.name, "configure"))

    def upload(self, program, loader):
        self.calls.append((self.name, "upload"))
        for segment in program["segments"]:
            loader(program, segment["id"])

    def select_entry(self, step):
        self.calls.append((self.name, "select_entry", step))

    def arm(self):
        self.armed = True

    def ready(self):
        return self.armed

    def wait_complete(self, timeout_s):
        self.armed = False

    def stop(self):
        self.armed = False


def run_offline_journal(artifact, directory, observation_factory):
    """Commit artificial observations for each global point/shot and test resume.

    observation_factory(coordinates, shot) must return a JSON dictionary. Only
    compact coordinates are retained across chunks. Already committed runs are
    skipped by the ordinary RunStore recovery policy, never replayed implicitly.
    """
    artifact.verify()
    contracts, coordinates, calls = {}, {}, []
    for chunk in artifact.manifest["chunks"]:
        manifest = json.loads(
            artifact.check_file(chunk["directory"] + "/manifest.json").read_text(
                encoding="utf-8"
            )
        )
        for point in manifest["points"]:
            global_point = chunk["points"][point["index"]]
            coordinates[global_point] = point["coordinates"]
            for group in point["groups"]:
                for name, card in group["cards"].items():
                    contracts.setdefault(name, card["contract"])
    cards = {
        name: OfflineCard(name, contract, calls) for name, contract in contracts.items()
    }

    def factory(path, context):
        if path.name == "initialize.py":
            return SimpleNamespace(
                initialize_devices=lambda: calls.append(("control", "initialize"))
            )
        context = context or SimpleNamespace(dispatches=0)

        def execute(point):
            context.dispatches += 1
            calls.append(("mock_dispatch", point))

        return SimpleNamespace(
            native_context=context,
            _validate_target=lambda: None,
            _execute_point=execute,
            initialize_native_once=lambda: calls.append(("native", "initialize")),
            finalize_native_session=lambda: calls.append(("native", "finalize")),
        )

    class Detector:
        def after_shot(self, point, shot):
            return observation_factory(coordinates[point], shot)

    journal = RunStore(directory)
    try:
        runner = StreamingRunner(
            SpectrumRunner(cards, allow_unverified=True), kernel_factory=factory
        )
        runner.run(artifact, result_adapter=Detector(), run_store=journal)
        first_dispatches = sum(call[0] == "mock_dispatch" for call in calls)
        runner.resume(artifact, result_adapter=Detector(), run_store=journal)
        replayed = sum(call[0] == "mock_dispatch" for call in calls) - first_dispatches
        summary = {
            "mode": "offline_host_mock_with_synthetic_observations",
            "artifact_sha256": artifact.sha256,
            "run_id": journal.run_id,
            "generated_kernels_executed": False,
            "hardware_executed": False,
            "new_mock_dispatches": first_dispatches,
            "dispatches_on_completed_resume": replayed,
            "data_commits": sum(
                e["stage"] == "data_committed" for e in journal.events()
            ),
            "card_uploads": sum(len(c) > 1 and c[1] == "upload" for c in calls),
        }
        (journal.directory / "offline_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        return summary
    finally:
        journal.close()
