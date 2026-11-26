"""Package the same read-only adapter registry and replay used by the web application."""

import json
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app import instrument_drivers, measurement_adapters, measurement_replay


def build_replay_kit(plan):
    out = BytesIO()
    with ZipFile(out, "w", compression=ZIP_DEFLATED) as archive:
        for module in (instrument_drivers, measurement_adapters, measurement_replay):
            archive.write(Path(module.__file__), module.__name__.replace(".", "/") + ".py")
        archive.writestr("app/__init__.py", "")
        archive.writestr("operation-plan.json", json.dumps(plan, indent=2, allow_nan=False))
        archive.writestr(
            "recording-template.json",
            json.dumps(measurement_replay.recording_template(plan), indent=2),
        )
        archive.writestr(
            "requirements.txt", "".join(f"{p}=={version(p)}\n" for p in ("Pillow", "pydantic"))
        )
        archive.writestr(
            "README.txt",
            "Use Python 3.12 with the packages in requirements.txt.\n"
            "Fill a copy of recording-template.json with the recorded inputs.\n"
            "Run in this folder.\n"
            "python -m app.measurement_replay operation-plan.json recording.json\n"
            "Optional shell redirection saves the returned JSON.\n"
            "The replay reads supplied files and never opens a physical device.\n",
        )
    return out.getvalue()
