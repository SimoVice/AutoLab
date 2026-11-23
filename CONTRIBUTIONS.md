# Engineering Contributions and Attribution

AutoLab separates application engineering, open-hardware CAD integration, supplier/pricing checks, and external AI model inference so every part of a project has clear provenance.

## Original AutoLab Application Engineering

- **Backend and Domain Compiler (`app/`)**: FastAPI workspace API, SQLite and portable JSON persistence, workflow/engineering design state machines, liquid-handling arithmetic, geometric clearance and envelope validation, source-grounding verification, parametric rack/holder CAD compilation (`build123d` / OpenCASCADE), BOM and pricing aggregation, and revision-bound instruction/program exports.
- **Interactive 3D Frontend (`web/`)**: React + Three.js workspace (`App.tsx`, `Scene.tsx`, `OperatingPreview.tsx`), responsive layout, local browser state recovery, expanded 3D inspection modes, hosted authentication flows, and voice/image workflow integration.
- **Provider Integration (`app/model_client.py`)**: Strict OpenAI-compatible client boundary isolating model prompts, structured JSON validation, usage telemetry, transcription, and optional project illustration from deterministic domain checks.

## Open-Source & Third-Party Foundations

Pinned third-party assets, software dependencies, and upstream licensing terms remain separate from original AutoLab code and are documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [docs/software-acknowledgements.md](docs/software-acknowledgements.md). Key external foundations include:

- **Jubilee V2.2.2** (Joshua Vasquez / Machine Agency, University of Washington, **CC BY 4.0**): pinned upstream STEP foundation (`app/assets/`).
- **Opentrons OT-2 & Labware Data** (**Apache-2.0**): pinned software deck coordinates and labware reference schemas (`app/data/ot2/`, `app/data/labware/`).
- **Science Jubilee** (**MIT**): referenced upstream integration patterns (`docs/source-grounding.md`).

All hardware modifications, custom mounts, actuation, electrical integration, and calibration remain unverified engineering work owned by the operator.
