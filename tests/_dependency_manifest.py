"""The manifest reader the dependency guards share (#242, #294).

``tests/test_dependency_extras.py`` checks each declared requirement's extras;
``tests/test_dependency_imports.py`` checks that nothing it must not declare is
declared anywhere. Both read every table through this one function, so the
tables they consult cannot drift apart.

Production code never imports from this module.
"""

from packaging.requirements import Requirement


def declared_requirements(manifest: dict) -> list[Requirement]:
    """Every requirement *manifest* declares: dependencies, each optional extra, each group.

    ``dependency-groups`` entries may be ``{include-group = "..."}`` tables
    (PEP 735) rather than requirement strings; those are skipped — the included
    group is itself a key in the same table, so nothing is missed.
    """
    project = manifest["project"]
    tables = [
        project.get("dependencies", []),
        *project.get("optional-dependencies", {}).values(),
        *manifest.get("dependency-groups", {}).values(),
    ]
    return [Requirement(spec) for table in tables for spec in table if isinstance(spec, str)]
