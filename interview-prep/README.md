# Wavebreak interview dossier

Read [wavebreak.md](wavebreak.md) first. It is a source-grounded dossier for SWE and AI-engineer interviews covering the implemented OTA rollout safety system, its limits, and questions to rehearse.

Evidence labels used throughout:

- `[CODE]` verified in implementation or tests.
- `[DOC]` stated in project documentation/status, but not independently established by the implementation cited.
- `[INFERRED]` reasoning from the repository; validate the intent with the author before presenting it as fact.

The project is a prototype/MVP rather than a production-ready multi-tenant control plane. The most important interview themes are evidence-gated actions, fail-closed assessment, A/B OTA installation, human approval, state-machine correctness, and the gap between demo validation and operational readiness.
