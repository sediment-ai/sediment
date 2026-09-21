# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add measured runtimes with the maintained CycloneDX library and validate it.

Run with ``uv tool run --from cyclonedx-bom==7.3.1 python``. Scanner libraries
remain outside Sediment's production distributions.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from cyclonedx.model import HashAlgorithm, HashType, Property
from cyclonedx.model.bom import Bom
from cyclonedx.model.component import Component, ComponentType
from cyclonedx.output import OutputFormat, SchemaVersion, make_outputter
from cyclonedx.validation.json import JsonStrictValidator
from packageurl import PackageURL

from security_policy import bundled_library_ref


def augment(sbom: Path, observations: Path, native: Path | None = None) -> None:
    data = json.loads(sbom.read_text())
    runtimes = json.loads(observations.read_text())
    if (
        data.get("bomFormat") != "CycloneDX"
        or not isinstance(runtimes, dict)
        or not runtimes
    ):
        raise ValueError("missing CycloneDX or runtime evidence")
    bom = Bom.from_json(data)
    for name, version in sorted(runtimes.items()):
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name) or not re.fullmatch(
            r"\d+(?:\.\d+){1,3}", version
        ):
            raise ValueError("unrecognized runtime observation")
        identity = f"sediment:observed-runtime:{name}:{version}"
        if any(str(component.bom_ref) == identity for component in bom.components):
            continue
        bom.components.add(
            Component(
                name=name,
                version=version,
                type=ComponentType.PLATFORM,
                bom_ref=identity,
                purl=PackageURL(type="generic", name=name, version=version),
                properties=[
                    Property(
                        name="sediment:inventory:origin",
                        value="executed runtime version probe",
                    )
                ],
            )
        )
    libraries = json.loads(native.read_text())["bundled_libraries"] if native else []
    numpy_parents = [
        c
        for c in bom.components
        if c.purl and c.purl.type == "pypi" and c.purl.name == "numpy"
    ]
    if not isinstance(libraries, list) or (numpy_parents and not libraries):
        raise ValueError("missing NumPy native library evidence")
    for library in libraries:
        parent = library["parent"]
        parents = [c for c in numpy_parents if c.version == parent["version"]]
        if (
            parent.get("ecosystem") != "pypi"
            or parent.get("name") != "numpy"
            or len(parents) != 1
        ):
            raise ValueError("measured native library parent is absent or ambiguous")
        parent_component = parents[0]
        if not Path(library["path"]).is_absolute() or not re.fullmatch(
            r"[0-9a-f]{64}", library["sha256"]
        ):
            raise ValueError("invalid native file observation")
        properties = [
            Property(name="sediment:bundled:path", value=library["path"]),
            Property(
                name="sediment:bundled:parent", value=str(parent_component.bom_ref)
            ),
        ]
        if "abi" in library:
            properties.append(
                Property(name="sediment:bundled:abi", value=library["abi"])
            )
        component = Component(
            name=library["name"],
            version=library.get("version"),
            type=ComponentType.LIBRARY,
            bom_ref=bundled_library_ref(library),
            hashes=[HashType(alg=HashAlgorithm.SHA_256, content=library["sha256"])],
            properties=properties,
        )
        bom.components.add(component)
        bom.register_dependency(parent_component, [component])
    output = make_outputter(
        bom, OutputFormat.JSON, SchemaVersion.V1_6
    ).output_as_string(indent=2)
    if JsonStrictValidator(SchemaVersion.V1_6).validate_str(output):
        raise ValueError("CycloneDX schema validation failed")
    sbom.write_text(output + "\n")


if __name__ == "__main__":
    if len(sys.argv) not in {3, 4}:
        raise SystemExit(
            "usage: security_sbom.py SBOM.json RUNTIMES.json [NATIVE.json]"
        )
    augment(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        Path(sys.argv[3]) if len(sys.argv) == 4 else None,
    )
