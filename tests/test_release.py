"""Offline checks for reproducible release packaging and tamper detection."""

import importlib.util
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib
import unittest
import zipfile
from xml.etree import ElementTree


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_release.py"
SPEC = importlib.util.spec_from_file_location("swarm2_build_release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(release)


def public_markdown_files():
    """Return tracked Markdown in a checkout and the public set in an sdist."""
    root = release.PROJECT_ROOT
    repository = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if repository.returncode == 0 and Path(repository.stdout.strip()).resolve() == root:
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.md"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        return [root / path.decode() for path in tracked.split(b"\0") if path]
    return [
        root / "README.md",
        *sorted((root / "docs").glob("*.md")),
        root / release.FIRMWARE_RELATIVE / "README.md",
    ]


def markdown_layout_errors(text):
    """Find explicit hard breaks and prose split across source lines."""
    errors = []
    previous = None
    fence = None
    fence_pattern = re.compile(r"^\s*(`{3,}|~{3,})")
    list_pattern = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
    heading_pattern = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
    reference_pattern = re.compile(r"^\s*\[[^]]+\]:\s*\S")
    thematic_pattern = re.compile(
        r"^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$"
    )

    for number, line in enumerate(text.splitlines(), 1):
        fence_match = fence_pattern.match(line)
        if fence is not None:
            if fence_match and fence_match.group(1).startswith(fence):
                fence = None
            previous = "structure"
            continue
        if fence_match:
            fence = fence_match.group(1)[0]
            previous = "structure"
            continue
        if line.endswith("\\") or re.search(r" {2,}$", line):
            errors.append(f"line {number}: explicit Markdown hard break")
        if not line.strip():
            previous = None
            continue

        quote = re.match(r"^\s*>\s?(.*)$", line)
        if quote:
            content = quote.group(1).strip()
            if not content:
                previous = None
            elif re.fullmatch(r"\[![A-Z]+\]", content):
                previous = "quote-admonition"
            elif previous == "quote-prose":
                errors.append(f"line {number}: manually wrapped blockquote prose")
                previous = "quote-prose"
            else:
                previous = "quote-prose"
            continue

        if list_pattern.match(line):
            previous = "list"
            continue
        if (
            heading_pattern.match(line)
            or thematic_pattern.match(line)
            or reference_pattern.match(line)
            or line.lstrip().startswith("|")
            or line.lstrip().startswith("![")
        ):
            previous = "structure"
            continue
        if previous in {"prose", "list", "quote-prose"}:
            errors.append(f"line {number}: manually wrapped prose")
        previous = "prose"

    if fence is not None:
        errors.append("unclosed fenced code block")
    return errors


def write_firmware_provenance(root):
    root.mkdir(parents=True)
    filename = "command-series-mc7_454-5.4.0.0-0974-v1.7z"
    digest = "a" * 64
    manifest = {
        "schema": "swarm2.vendor-firmware-bundle.v1",
        "scope": {"archive_count": 1, "archive_bytes": 123},
        "releases": [{
            "key": "mouse:5.4.0.0", "filename": filename, "bytes": 123,
            "md5": "b" * 32, "sha256": digest,
            "resolver_url": "https://acpr.prod.turtlebeach.com/example",
            "cdn_url": "https://cdn.turtlebeach.com/example",
        }],
    }
    (root / "README.md").write_text("local backup notes\n", encoding="ascii")
    (root / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="ascii")
    (root / "SHA256SUMS").write_text(f"{digest}  {filename}\n", encoding="ascii")
    (root / "verify.py").write_text("raise SystemExit(0)\n", encoding="ascii")


class ReleaseToolTests(unittest.TestCase):
    def test_project_license_is_gpl3_or_later_everywhere(self):
        project = tomllib.loads((release.PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        metadata = ElementTree.parse(release.PROJECT_ROOT / "packaging/appimage/io.github.dev_zetta.MC7Studio.metainfo.xml")
        self.assertEqual(project["project"]["license"], "GPL-3.0-or-later")
        self.assertEqual(metadata.getroot().findtext("project_license"), "GPL-3.0-or-later")
        self.assertEqual((release.PROJECT_ROOT / "LICENSE").read_bytes(), (release.PROJECT_ROOT / "packaging/appimage/licenses/GPL-3.0.txt").read_bytes())
        self.assertIn("GNU General Public License version 3 or later", (release.PROJECT_ROOT / "README.md").read_text(encoding="utf-8"))

    def test_public_documentation_has_no_manual_line_breaks(self):
        documents = [
            (str(path.relative_to(release.PROJECT_ROOT)), path.read_text(encoding="utf-8"))
            for path in public_markdown_files()
        ]
        documents.append(("generated firmware provenance README", release.PROVENANCE_README))
        for name, content in documents:
            with self.subTest(document=name):
                self.assertEqual([], markdown_layout_errors(content))

    def test_firmware_provenance_is_reproducible_and_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_source = root / "first"
            second_source = root / "second"
            for source in (first_source, second_source):
                write_firmware_provenance(source)
            (first_source / "manifest.json").chmod(0o600)
            (second_source / "manifest.json").chmod(0o755)
            first = root / "first.tar.gz"
            second = root / "second.tar.gz"
            release.create_firmware_provenance_archive(
                first_source, first, "0.1.0", 1_789_516_800)
            release.create_firmware_provenance_archive(
                second_source, second, "0.1.0", 1_789_516_800)
            self.assertEqual(first.read_bytes(), second.read_bytes())

            with tarfile.open(first, "r:gz") as archive:
                members = archive.getmembers()
                files = [member for member in members if member.isfile()]
                self.assertEqual(
                    [member.name.rsplit("/", 1)[-1] for member in files],
                    ["README.md", "manifest.json", "SHA256SUMS", "verify.py"])
                self.assertFalse(any(member.name.endswith(".7z") for member in members))
                self.assertTrue(all(member.mtime == 1_789_516_800 for member in members))
                self.assertTrue(all(member.uid == member.gid == 0 for member in members))
                self.assertTrue(all(member.mode == 0o644 for member in files))

    def test_source_distribution_normalization_removes_build_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outputs = [root / "first.tar.gz", root / "second.tar.gz"]
            for index, output in enumerate(outputs):
                with tarfile.open(output, "w:gz") as archive:
                    directory_info = tarfile.TarInfo("package-0.1.0")
                    directory_info.type = tarfile.DIRTYPE
                    directory_info.mode = 0o700 + index * 0o55
                    directory_info.mtime = 100 + index
                    archive.addfile(directory_info)
                    file_info = tarfile.TarInfo("package-0.1.0/module.py")
                    file_info.size = 5
                    file_info.mode = 0o600 + index * 0o44
                    file_info.mtime = 200 + index
                    archive.addfile(file_info, BytesIO(b"pass\n"))
                release.normalize_source_distribution(output, 1_789_516_800)
            self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
            with tarfile.open(outputs[0], "r:gz") as archive:
                members = archive.getmembers()
                self.assertEqual([member.mode for member in members], [0o755, 0o644])
                self.assertTrue(all(member.mtime == 1_789_516_800 for member in members))

    def test_release_inventory_detects_artifact_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            source = root / "firmware"
            write_firmware_provenance(source)
            artifact = output / "swarm2_mc7-0.1.0-py3-none-any.whl"
            source_distribution = output / "swarm2_mc7-0.1.0.tar.gz"
            provenance = output / "swarm2-mc7-firmware-provenance-0.1.0.tar.gz"
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr("swarm2/__init__.py", "")
            with tarfile.open(source_distribution, "w:gz") as archive:
                info = tarfile.TarInfo("swarm2_mc7-0.1.0/README.md")
                info.size = 7
                archive.addfile(info, BytesIO(b"source\n"))
            release.create_firmware_provenance_archive(
                source, provenance, "0.1.0", 1_789_516_800)
            release.write_release_metadata(
                output, project="swarm2-mc7", version="0.1.0",
                epoch=1_789_516_800, firmware_manifest=source / "manifest.json",
                artifacts=[artifact, source_distribution, provenance])
            manifest = release.verify_release_output(output)
            self.assertEqual(manifest["version"], "0.1.0")
            artifact.write_bytes(b"changed")
            with self.assertRaisesRegex(release.ReleaseError, "checksum mismatch"):
                release.verify_release_output(output)

    def test_firmware_provenance_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            write_firmware_provenance(source)
            target = root / "outside.py"
            target.write_text("raise SystemExit(0)\n", encoding="ascii")
            (source / "verify.py").unlink()
            try:
                (source / "verify.py").symlink_to(target)
            except OSError as error:
                self.skipTest(f"symlinks unavailable: {error}")
            with self.assertRaisesRegex(release.ReleaseError, "missing verify.py"):
                release.create_firmware_provenance_archive(
                    source, root / "output.tar.gz", "0.1.0", 1_789_516_800)

    def test_public_containers_reject_local_only_material_and_vendor_binaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_wheel = root / "private.whl"
            with zipfile.ZipFile(private_wheel, "w") as archive:
                archive.writestr("package/docs-local/evidence.json", "{}")
            with self.assertRaisesRegex(release.ReleaseError, "local-only"):
                release._reject_private_release_material([private_wheel])

            vendor_sdist = root / "vendor.tar.gz"
            with tarfile.open(vendor_sdist, "w:gz") as archive:
                info = tarfile.TarInfo("package/vendor-installer.exe")
                info.size = 1
                archive.addfile(info, BytesIO(b"x"))
            with self.assertRaisesRegex(release.ReleaseError, "vendor binary"):
                release._reject_private_release_material([vendor_sdist])


if __name__ == "__main__":
    unittest.main()
