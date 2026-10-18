"""Pin an existing YuiToIssho release in the N.E.K.O plugin (no download/build)."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--license", required=True, type=Path)
    args = parser.parse_args()
    archive = args.archive.resolve(strict=True)
    license_path = args.license.resolve(strict=True)
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if any(
            "config.json" in name.casefold() or ".." in name.split("/")
            for name in names
        ):
            raise ValueError("Release must not contain runtime config or unsafe paths")
        manifest = json.loads(package.read("YuiToIssho/manifest.json"))
        version = manifest["Version"]
        if (
            manifest["UniqueID"] != "Himifox.YuiToIssho"
            or re.fullmatch(r"\d+\.\d+\.\d+", version) is None
        ):
            raise ValueError("Invalid mod identity or version")
    destination = (
        Path(__file__).resolve().parents[1] / "plugin/plugins/yui_stardew/bundled"
    )
    destination.mkdir(exist_ok=True)
    name = f"YuiToIssho-{version}.zip"
    shutil.copyfile(archive, destination / name)
    shutil.copyfile(license_path, destination / "LICENSE.txt")
    (destination / "manifest.json").write_text(
        json.dumps(
            {
                "version": version,
                "protocol_version": 2,
                "archive": name,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "source": "https://github.com/Himifox/Yui-for-stawvalley",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Pinned YuiToIssho {version} with SHA-256; no runtime config included.")


if __name__ == "__main__":
    main()
