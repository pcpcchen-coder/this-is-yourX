# Repository instructions for AI coding agents

This repository builds a verifiable robot self-model. Read `README.md` and the relevant `docs/` before changing code.

## Non-negotiable architecture rules

- Generative models never publish raw motor, PWM, GPIO, serial, CAN, or driver commands.
- All motion goes through versioned skills and the Safety Gateway.
- Unknown, stale, conflicting, or ambiguous safety preconditions reject motion.
- Simulation and real hardware implement the same public contracts.
- Semantic component IDs are separate from hardware serial numbers.
- Assertions about identity/state require timestamped, versioned evidence.
- Safety controller and E-stop behavior must not depend on an LLM/VLM or network service.

## Required work for changes

- Component/schema change: update schema, example manifest, validation tests, docs, migration notes.
- New skill: add parameter schema, preconditions, limits, authority, timeout, cancel behavior, audit events, success criteria, simulation and rejection tests.
- Hardware adapter: provide fake/simulation adapter, timestamp/quality/freshness, disconnect behavior and safe-state test.
- AI adapter: structured output validation, timeout, adversarial tests, and deterministic fallback/rejection.

## Verification

Report exact commands and results. Do not claim real-hardware validation unless an experiment ID, hardware revision and safety checklist are available. Never commit secrets, private recordings, large bags, datasets or model weights.
