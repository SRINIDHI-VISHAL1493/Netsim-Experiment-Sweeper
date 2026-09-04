# AI usage and team checks

This project was produced with Codex (GPT-5) as a development assistant.

AI-assisted work: architecture selection, standard-library Python backend, web UI, XML catalogue and mutation logic, campaign runner, mock test, documentation, and sample artefacts.

Checks performed by the team/author:

- Reviewed the supplied NetSim reference script to retain its command-line contract (`NetSimCore.exe -apppath -iopath -license`) and `NETSIM_AUTO=1` behaviour.
- Inspected generated source for offline operation and confirmed no hard-coded NetSim parameter, metric, or result data is used by the application.
- Ran the included deterministic mock-process test and Python syntax compilation.
- Opened the local application in a browser and captured the three packaged PNG screens.
- Manually reviewed that baseline configurations are copied into per-run folders before XML mutation and that every terminal run status is written to cumulative CSV.

The real NetSim execution path must still be checked against the evaluator's installed NetSim version, license method, and simulation configuration.
