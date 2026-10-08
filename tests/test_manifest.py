"""Packaging guards: requirements must stay installable on Home Assistant."""

import json
import re
import tomllib
from importlib import resources
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).parent.parent
INTEGRATION = ROOT / "custom_components" / "inpost_paczkomaty"
MANIFEST = json.loads((INTEGRATION / "manifest.json").read_text())
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())

# PyPI distribution name -> module the integration is expected to import
IMPORT_NAMES = {"dacite": "dacite"}


def _ha_constraints() -> dict[str, Requirement]:
    """Return the pins Home Assistant applies when installing requirements."""
    text = (
        resources.files("homeassistant").joinpath("package_constraints.txt").read_text()
    )
    constraints = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        constraints[canonicalize_name(requirement.name)] = requirement
    return constraints


def test_manifest_requirements_match_pyproject():
    """manifest.json is synced from pyproject by CI; they must not drift."""
    assert MANIFEST["requirements"] == PYPROJECT["project"]["dependencies"]


def test_versions_match():
    """The integration and package versions are released together."""
    assert MANIFEST["version"] == PYPROJECT["project"]["version"]


def test_requirements_do_not_conflict_with_home_assistant():
    """Every requirement must be satisfiable under Home Assistant's pins.

    Home Assistant installs integration requirements together with its own
    package_constraints.txt; two different exact pins of one package make the
    integration impossible to load (python-slugify==8.0.4 vs ==9.1.2).
    """
    constraints = _ha_constraints()
    assert "python-slugify" in constraints  # sanity: the file was parsed

    for line in MANIFEST["requirements"]:
        requirement = Requirement(line)
        constraint = constraints.get(canonicalize_name(requirement.name))
        if constraint is None:
            continue
        pinned = [
            spec.version for spec in requirement.specifier if spec.operator == "=="
        ]
        assert pinned, f"{line}: pin an exact version"
        assert constraint.specifier.contains(pinned[0]), (
            f"{line} conflicts with Home Assistant constraint {constraint}"
        )
        core_pins = [s.version for s in constraint.specifier if s.operator == "=="]
        assert not core_pins, (
            f"{line}: Home Assistant already pins {constraint}; "
            "do not list it as an integration requirement"
        )


def test_every_requirement_is_imported():
    """Unused requirements are a liability: they only add ways to conflict."""
    source = "\n".join(path.read_text() for path in INTEGRATION.glob("*.py"))
    for line in MANIFEST["requirements"]:
        name = canonicalize_name(Requirement(line).name)
        module = IMPORT_NAMES.get(name, name.replace("-", "_"))
        assert re.search(
            rf"^\s*(from|import)\s+{re.escape(module)}\b", source, re.MULTILINE
        ), f"{line} is listed as a requirement but never imported"


def test_manifest_points_to_this_repository():
    """Documentation and issue links must lead to the maintained fork."""
    assert "shockwave9315/Inpost-Paczkomaty" in MANIFEST["documentation"]
    assert "shockwave9315/Inpost-Paczkomaty" in MANIFEST["issue_tracker"]
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert "zip_release" not in hacs  # HACS installs straight from the repository


def test_supported_versions_are_declared_consistently():
    """One minimum Home Assistant, on the Python it runs on - no older targets.

    HACS (hacs.json), the test environment (pyproject.toml, .python-version)
    and the linter must agree, otherwise CI validates something users of the
    declared minimum never run.
    """
    hacs = json.loads((ROOT / "hacs.json").read_text())
    dev_requirements = [
        Requirement(line) for line in PYPROJECT["dependency-groups"]["dev"]
    ]
    (home_assistant,) = [r for r in dev_requirements if r.name == "homeassistant"]
    assert str(home_assistant.specifier) == f">={hacs['homeassistant']}"

    python = (ROOT / ".python-version").read_text().strip()
    assert PYPROJECT["project"]["requires-python"].startswith(f">={python}")
    assert PYPROJECT["tool"]["ruff"]["target-version"] == f"py{python.replace('.', '')}"


def test_release_configuration_continues_the_existing_version_line():
    """The release workflow must produce the next 0.x version, not 1.0.0.

    .releaserc.json cannot carry comments, so the reasons live here:

    * The fork's releases are tagged without a "v" prefix (0.4.3).
      semantic-release looks for "v${version}" by default, finds no previous
      release and starts over at 1.0.0.
    * While the integration is at 0.x, a breaking change raises the minor
      version. By default a "BREAKING CHANGE" footer - there is one in the
      0.5.0 history - forces a major release.
    """
    release = json.loads((ROOT / ".releaserc.json").read_text())
    assert release["tagFormat"] == "${version}"
    assert "shockwave9315/Inpost-Paczkomaty" in release["repositoryUrl"]

    analyzer = release["plugins"][0]
    assert analyzer[0] == "@semantic-release/commit-analyzer"
    assert {"breaking": True, "release": "minor"} in analyzer[1]["releaseRules"]
    assert MANIFEST["version"].startswith("0.")  # revisit the rule at 1.0.0


def test_translations_cover_strings():
    """Every string key exists in each translation file."""

    def keys(node, prefix=""):
        if isinstance(node, dict):
            for key, value in node.items():
                yield from keys(value, f"{prefix}{key}.")
        else:
            yield prefix

    strings = set(keys(json.loads((INTEGRATION / "strings.json").read_text())))
    for language in ("en", "pl"):
        translated = set(
            keys(
                json.loads(
                    (INTEGRATION / "translations" / f"{language}.json").read_text()
                )
            )
        )
        assert translated == strings, language
