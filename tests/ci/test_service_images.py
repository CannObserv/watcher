"""Every container image CI pulls comes from Google's Docker Hub mirror.

GitHub's runners share egress addresses, so Docker Hub's anonymous pull limit
is spent by strangers: on 2026-10-09 the ``integration`` and ``migrations``
jobs failed at *Initialize containers* with ``toomanyrequests`` twice running,
before a line of watcher's code ran. ``mirror.gcr.io`` serves the same official
images with no anonymous limit and no credential, so the fix is the registry
prefix, not a Docker Hub token in two secret stores.

An image named without a registry is a Docker Hub pull, so this fails any
``services:`` or ``container:`` image that is not on the mirror. Pure file reads.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
MIRROR = "mirror.gcr.io/"


def _image(spec: object) -> str | None:
    """The image a ``services.<id>`` or ``container`` value names."""
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        return spec.get("image")
    return None


def _images() -> list[tuple[str, str]]:
    """``(workflow:job[:service], image)`` for every container image a job pulls."""
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        jobs = yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs", {})
        for name, job in jobs.items():
            if (image := _image(job.get("container"))) is not None:
                found.append((f"{path.name}:{name}", image))
            for service, spec in (job.get("services") or {}).items():
                if (image := _image(spec)) is not None:
                    found.append((f"{path.name}:{name}:{service}", image))
    return found


def test_the_sweep_finds_the_database_services() -> None:
    """Guard the guard: ``integration`` and ``migrations`` each run a postgres."""
    sites = {site for site, _ in _images()}
    assert {"ci.yml:integration:postgres", "ci.yml:migrations:postgres"} <= sites


@pytest.mark.parametrize(("site", "image"), _images(), ids=[s for s, _ in _images()])
def test_image_comes_from_the_mirror(site: str, image: str) -> None:
    assert image.startswith(MIRROR), (
        f"{site} pulls {image!r} from Docker Hub, whose anonymous limit GitHub's shared "
        f"runners exhaust. Use {MIRROR}library/<image> for an official image."
    )
