# SPDX-License-Identifier: AGPL-3.0-or-later
"""Include the MIT pi runtime in wheels and source distributions."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class PiExtensionHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # Editable installs resolve shims/pi directly from the checkout.
        if version == "editable":
            return
        root = Path(self.root)
        bundled = root / "sediment_cli" / "_pi"
        # A source distribution already contains the runtime as package data.
        if bundled.is_dir():
            return
        source = root.parent / "shims" / "pi"
        for name in ("index.ts", "lib", "package.json", "LICENSE", "README.md"):
            build_data["force_include"][str(source / name)] = f"sediment_cli/_pi/{name}"
