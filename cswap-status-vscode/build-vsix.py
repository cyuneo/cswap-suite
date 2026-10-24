#!/usr/bin/env python3
"""Pack this folder into cswap-status-<version>.vsix (no Node or vsce needed).

  python3 build-vsix.py              -> ./cswap-status-<version from package.json>.vsix
  python3 build-vsix.py --out DIR    -> DIR/cswap-status-<version>.vsix

The satellite script is not kept in this folder: it is copied from
../cswap-satellite/cswap_satellite.py into the package as
extension/satellite/cswap_satellite.py (where lib/satellite.js looks for it).

Install with: code --install-extension "$PWD/cswap-status-<version>.vsix"
"""

import argparse
import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
SATELLITE = HERE.parent / "cswap-satellite" / "cswap_satellite.py"


def files():
    """(source path, path inside extension/) for everything the extension
    loads at runtime; tests and old packages stay out."""
    out = [(HERE / "package.json", "package.json"), (HERE / "extension.js", "extension.js")]
    out += [(p, f"lib/{p.name}") for p in sorted((HERE / "lib").glob("*.js"))]
    out += [(p, f"media/{p.name}") for p in sorted((HERE / "media").iterdir()) if p.is_file()]
    out.append((SATELLITE, "satellite/cswap_satellite.py"))
    return out


TYPES = {".json": "application/json", ".js": "application/javascript", ".svg": "image/svg+xml",
         ".py": "text/x-python", ".vsixmanifest": "text/xml"}


def manifest(pkg):
    kind = ",".join(pkg.get("extensionKind", []))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{escape(pkg['name'])}" Version="{escape(pkg['version'])}" Publisher="{escape(pkg['publisher'])}"/>
    <DisplayName>{escape(pkg['displayName'])}</DisplayName>
    <Description xml:space="preserve">{escape(pkg['description'])}</Description>
    <Tags></Tags>
    <Categories>{escape(",".join(pkg.get("categories", [])))}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{escape(pkg['engines']['vscode'])}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="{escape(kind)}"/>
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def main():
    ap = argparse.ArgumentParser(description="Build the cswap-status .vsix")
    ap.add_argument("--out", type=Path, default=HERE, help="directory for the .vsix (default: this folder)")
    a = ap.parse_args()
    if not SATELLITE.is_file():
        sys.exit(f"missing {SATELLITE} (the cswap-satellite folder must sit next to this one)")
    pkg = json.loads((HERE / "package.json").read_text("utf-8"))
    a.out.mkdir(parents=True, exist_ok=True)
    out = a.out / f"{pkg['name']}-{pkg['version']}.vsix"
    entries = files()
    exts = sorted({Path(dst).suffix for _, dst in entries} | {".vsixmanifest"})
    octet = "application/octet-stream"
    types = "".join(f'<Default Extension="{e}" ContentType="{TYPES.get(e, octet)}"/>' for e in exts)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="utf-8"?>\n'
                   f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">{types}</Types>')
        z.writestr("extension.vsixmanifest", manifest(pkg))
        for src, dst in entries:
            z.write(src, f"extension/{dst}")
    print(out)


if __name__ == "__main__":
    main()
