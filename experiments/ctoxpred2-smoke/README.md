# CToxPred2 RF-SSL smoke

This runtime reproduces the upstream RF-SSL path without the GUI or DNN/Torch
dependencies. It verifies the four reviewed artifacts before loading any
pickle/joblib file, accepts one JSON request per line, and returns one JSON
response per line.

The model archives and extracted weights are intentionally not committed. Build
or deployment automation must obtain upstream commit
`2a31aa119e27b6b69a5588d18a01f2a27fef4524`, extract the reviewed files, and
verify the hashes embedded in `runtime.py`.

The smoke environment uses Python 3.9 with NumPy 1.23.5, pandas 2.0.3, SciPy
1.11.4, scikit-learn 1.3.1, joblib 1.5.1, Mordred 1.2.0, RDKit 2025.3.5 and
PyBioMed commit `45440d8a70b2aa2818762ceadb499dd3a1df90bc`. Changing any runtime package is
a new tool build and requires a new version/fingerprint plus a repeat smoke.

Example administrator-controlled command:

```sh
python runtime.py --artifact-root /opt/ctoxpred2/artifacts
```

Do not pass artifact paths, commands, URLs, or model choices from an Agent.

## Verified smoke

On 2026-09-25, the runtime completed two requests in one process on macOS 14.0
arm64 in 3.38 seconds total. A separate 10-iteration tamoxifen benchmark measured
0.55 seconds for the first inference and a 0.165-second warm median. Process peak
RSS was 486 MiB after loading all models, 511 MiB after the first inference and
519 MiB after ten inferences. The exact inputs, outputs, stage measurements and
warnings are recorded in `report.json`. The same runtime was also called
successfully through the backend `CtoxPred2Provider` and typed adapter.

RSS uses Python `resource.getrusage(RUSAGE_SELF).ru_maxrss`, normalized from
macOS bytes. It is a process-lifetime high-water mark. Repeat the measurement on
the Linux deployment worker and under the intended concurrency limit.
