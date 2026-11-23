# ¹⁷¹Yb benchmarking and waveform references

The [tune-up workbook](yb171_global_rf_tuneup.ipynb) uses ground-state nuclear-spin encoding, global RF and clock/UV illumination shared within an interaction zone. Each complete gate shelves by clock, applies the UV control and returns by clock. Offline sequences, ideal matrix checks and synthetic analysis each have an explicit role. Physical detection, calibration and gate measurements require laboratory data.

## Relation to published protocols

| Publication | Which parts the workbook uses | Conditions specific to the original experiment |
|---|---|---|
| Muniz et al., *High-Fidelity Universal Gates in the 171Yb Ground-State Nuclear-Spin Qubit*, PRX Quantum 6, 020334 (2025). [Paper and appendices](https://harvest.aps.org/v2/journals/articles/10.1103/PRXQuantum.6.020334/fulltext) | Section IV full ground-state gate, clock/CZ controls and U-GERB. Fig. 3 and Appendix E waveform calibration. | U-GERB uses common Haar-random rotations. Independent single-qubit control also allows full two-qubit Clifford RB. |
| Ma et al., *High-fidelity gates and mid-circuit erasure conversion in an atomic qubit*, Nature 622, 279–284 (2023). [Paper](https://doi.org/10.1038/s41586-023-06438-1) · [Preprint and Methods](https://arxiv.org/pdf/2305.05493) | Global random circuits, Bell measurement, input-dependent leakage and UV phase optimization with multiple levels. | The authors use metastable nuclear-spin encoding and additional detection light for mid-circuit erasure detection. The preprint has an earlier title. Use ground-state-specific readout calibrations here. |
| Finkelstein et al., *Universal quantum operations and ancilla-based read-out for tweezer clocks*, Nature 634, 321–327 (2024). [Paper](https://doi.org/10.1038/s41586-024-08005-8) · [Author manuscript](https://arxiv.org/pdf/2402.16220) | Fig. 2, Methods and Extended Data Fig. 3 on symmetric-subspace benchmarking, Bell populations/parity and leakage correction. | The platform uses Sr optical clock qubits. A fixed single-qubit gate count still allows biases. Calibrate independently when leakage can be classified as bright. |
| Tsai et al., *Benchmarking and Fidelity Response Theory of High-Fidelity Rydberg Entangling Gates*, PRX Quantum 6, 010331 (2025). [Paper and appendices](https://journals.aps.org/prxquantum/pdf/10.1103/PRXQuantum.6.010331) | Fig. 2 on symmetric stabilizer benchmarking (SSB) and common-phase scans, Appendix E on protocol bias and Appendix F on leakage. | SSB probes symmetric inputs. The noise model and leakage correction refer to the authors' Sr experiment. |
| Evered et al., *High-fidelity parallel entangling gates on a neutral-atom quantum computer*, Nature 622, 268–272 (2023). [Paper](https://doi.org/10.1038/s41586-023-06481-y) | Continuous phase modulation, smooth envelopes and randomized sequences with and without echo. | The Rb two-photon intermediate state, light shifts and detunings require different calibrations from single-photon UV control in Yb. |

## Experiments in the workbook

The [generator](../../scripts/build_yb171_tuneup_notebook.py) shows the physical pulse order. The [numerical helpers](../../examples/yb171_tuneup_analysis.py) supply ideal models and mathematical gate words.

| Experiment | Scan or control | Interpretation |
|---|---|---|
| `cz_echo_phase` | Second UV phase and odd amplification M | Tune entangling phase for approximately diagonal gates using 2M complete gates. Echo cancels static local phases. |
| `bell_population`, `bell_parity` | Clock return phase and common RF analysis phase | Measure odd/even population and complex coherence. Include the specified target Bell phase when reporting the prepared state's fidelity. |
| `global_gate_word` + `global_echo_word` | Matched wait, clock round trip and complete CZ, with shared random seeds | Diagnose contributions from RF, shelving and UV using common Clifford half pulses in a GERB-style diagnostic. |
| `global_gate_word` + `ssb_sequences` | CZ count and random word, balanced over 12 initial states | Echo-free SSB uses a fixed RF count. Count initialization and recovery CZs. Check ideal recovery and validate the noise model against measurements. |
| `cz_ssb_phase_scan` | Scan clock return phase using frozen words and fixed depth | Tune the common phase as in Tsai Fig. 2(b). Freeze the choice before testing an independent seed. |
| `cz_input_loss_scan` | Common 00, ++ and 11 input and repeated complete CZ without interposed RF | Compare final loss/residual clock population as in Ma Fig. 4(e). Mid-circuit detection needs additional hardware. |
| `shaped_pair_closure`, `shaped_echo` | Single shaped UV candidate, amplitude, detuning and echo amplification | Screen for population return and entangling phase. A complete CZ assessment needs both and a common-phase check. |
| `shaped_bell_population`, `shaped_bell_parity`, `shaped_ssb_word` | Common compensation, analysis phase, training and held-out words | Compare complete ground-state gate candidates with the same benchmark and retain leakage and loss. |

Muniz U-GERB applies `R_random → U → global Xπ → U` per block with a common Haar draw and computed common recovery. The workbook uses a restricted Clifford set and derives its own recovery. The paper's fitted background and error subtraction depend on its protocol. Purely symmetric data cannot exclude a SWAP error. A full two-qubit Clifford fidelity requires wider coverage.

## Define and calibrate a waveform candidate

Keep the two `Square` segments as the baseline. In `uv_shaped_candidates`, one `FlatTopGaussian` UV pulse varies in plateau duration and optical phase amplitude. `play_shaped_gate` adds the complete clock round trip. UV duration is `flat_duration + 2*cutoff_sigma*sigma`. Include both edges, clock pulses and gaps when comparing complete gate times.

For $u=t/T$ choose $m(u)=16\beta u^2(1-u)^2$, with zero endpoint phase and slope. Supply `PolynomialPhase((0, 0, 16*beta, -32*beta, 16*beta))` in radians after applying the calibrated optical phase scale. Use `phase_modulation_mode="local"`. This family explores smooth phase control. The sinusoidal parameterizations in Muniz and Evered and the multilevel GRAPE solution in Ma are distinct optimizations.

`HalfSinePhase` also has zero endpoint phase but finite endpoint slope. Polynomial coefficients use ascending powers of u. Gaussian `sigma`, `cutoff_sigma` and `sample_interval` are static. Rebuild a pulse to change its edges. See [pulse declarations](../../src/taiyidsl/dsl/pulses.py) and [phase conventions](../spectrum_phase_modes.md).

A linear phase derivative and carrier detuning can parameterize the same frequency shift. Fix one representation. Measure how DAC amplitude maps through the amplifier, AOM and optics to atomic Rabi rate. Inspect final DAC samples, then check the actual optical envelope, phase, bandwidth and edges.

1. Acquire independent, physically prepared single-atom and pair-occupancy samples. Measure return, residual excitation and loss. Editing a software target cannot remove a second illuminated atom.
2. Scan candidate, amplitude and frequency and reject high leakage/loss. Use more than one echo amplification to identify the entangling phase.
3. Use Bell population/parity and fixed-depth SSB to tune common compensation, then freeze all parameters.
4. Validate held-out seeds, multiple depths and nearby amplitude/detuning values. Analyze each pair before pooling.

One engineering score with predeclared weights is

\[
J=w_R(1-P_{\rm return}^{\rm pre})+w_LP_{\rm loss}+w_CP_{\rm clock}
  +w_\phi[1-\cos(\beta_{\rm Bell}-\beta_{\rm target})].
\]

Condition `pre` only on a valid pair at the start. Final losses remain in the denominator. J can count related errors in more than one term. Retain raw statistics. Alternatively enforce bounds on loss/leakage first, then compare return and phase. Without measurements the workbook produces candidates and an acquisition plan.

`shaped_candidate_manifest.json`, `shaped_ssb_train.json` and `shaped_ssb_heldout.json` hold reproducible inputs. Implementing Hessian-direction scans would require a trusted local model and real feedback. The workbook includes neither a Hessian optimizer, GRAPE nor a noise-spectrum fidelity-response solver.

## Measurement requirements

- Keep pair, seed, initial state, gate count, candidate parameters, attempt, site outcomes and frame status together per shot. Distinguish 00/01/10/11, residual clock population, loss and missing frames.
- Report raw return conditioned on an initially valid pair, final logical survival, conditional return and atom survival. Calibrate threshold, repump and push-out for the actual detector. Each published experiment's background, leakage or erasure fractions belong to its own readout.
- For SSB, estimate intervals across independent sequences stratified by initial state. Inspect depth windows, residuals and nonexponential oscillations. Interleave depths and candidates to control drift.
- `cz_input_loss_scan` expresses the input-dependent sequences. Measuring separate leakage states requires laboratory detection.
- Mid-circuit erasure detection, local addressing, full 2Q Clifford RB, ancilla readout and noise spectroscopy require additional resources. [Readout adapters](../readout_adapters.md) · [Native components](../native_components.md)

Record offline construction, ideal algebra, real ARTIQ compilation, optical transfer measurements and atomic benchmarks as separate levels of verification.
