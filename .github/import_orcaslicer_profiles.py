#!/usr/bin/env python3
"""Import official OrcaSlicer profiles into profile_sources/orcaslicer-fff.

The imported source is kept in Orca's native JSON layout so it can be versioned
alongside the existing INI-based sources without lossy conversion.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Iterable
from pathlib import Path


UPSTREAM_ZIP_URL = "https://codeload.github.com/OrcaSlicer/OrcaSlicer/zip/refs/heads/main"
REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_SOURCES_DIR = REPO_ROOT / "profile_sources"
TARGET_REPO_ID = "orcaslicer-fff"
TARGET_REPO_DIR = PROFILE_SOURCES_DIR / TARGET_REPO_ID
UPSTREAM_REPO_URL = "https://github.com/OrcaSlicer/OrcaSlicer"
UPSTREAM_PROFILE_ROOT = Path("resources") / "profiles"
ROOT_SKIP_NAMES = {"blacklist.json", "check_unused_setting_id.py"}
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".stl", ".bmp", ".gif", ".webp"}
VENDOR_SECTION_PATH = Path("vendor") / "vendor.json"
SOURCE_FORMAT = "orcaslicer-json-split"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="Use an existing OrcaSlicer checkout root instead of downloading the official repository archive.",
    )
    return parser.parse_args(argv)


def download_upstream_archive() -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="orcaslicer-import-"))
    archive_path = temp_dir / "orcaslicer-main.zip"
    print(f"Downloading {UPSTREAM_ZIP_URL}")
    urllib.request.urlretrieve(UPSTREAM_ZIP_URL, archive_path)
    with zipfile.ZipFile(archive_path) as zf:
        zf.extractall(temp_dir)
    extracted_roots = [path for path in temp_dir.iterdir() if path.is_dir() and path.name.startswith("OrcaSlicer-")]
    if len(extracted_roots) != 1:
        raise RuntimeError(f"Expected one extracted OrcaSlicer root, found {len(extracted_roots)}")
    return extracted_roots[0]


def ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_string_values(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_string_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_string_values(item)


def collect_referenced_root_assets(json_objects: Iterable[dict], source_root: Path) -> set[Path]:
    assets: set[Path] = set()
    for obj in json_objects:
        for string_value in iter_string_values(obj):
            candidate = Path(string_value.strip())
            if candidate.suffix.lower() not in ASSET_SUFFIXES:
                continue
            if candidate.is_absolute():
                continue
            if len(candidate.parts) != 1:
                continue
            candidate_path = source_root / candidate.name
            if candidate_path.is_file():
                assets.add(candidate_path)
    return assets


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def normalize_section_path(section_type: str, source_rel_path: str) -> Path:
    section_rel = Path(source_rel_path.replace("\\", "/"))
    if section_rel.parts:
        section_rel = Path(*section_rel.parts[1:])
    return Path(section_type) / section_rel


def build_section_order(vendor_manifest: dict) -> list[dict[str, str]]:
    section_order = [{"type": "vendor", "name": vendor_manifest["name"], "path": VENDOR_SECTION_PATH.as_posix()}]
    for key, section_type in (
        ("machine_model_list", "printer_model"),
        ("machine_list", "printer"),
        ("process_list", "print"),
        ("filament_list", "filament"),
    ):
        for entry in vendor_manifest.get(key, []):
            section_order.append(
                {
                    "type": section_type,
                    "name": entry["name"],
                    "source_path": entry["sub_path"].replace("\\", "/"),
                    "path": normalize_section_path(section_type, entry["sub_path"]).as_posix(),
                }
            )
    return section_order


def import_vendor(source_profiles_dir: Path, vendor_manifest_path: Path, target_repo_dir: Path) -> dict[str, int | str]:
    vendor_manifest = load_json(vendor_manifest_path)
    vendor_name = vendor_manifest["name"]
    source_vendor_dir = source_profiles_dir / vendor_manifest_path.stem
    if not source_vendor_dir.is_dir():
        raise FileNotFoundError(f"Missing vendor directory for {vendor_name}: {source_vendor_dir}")

    target_vendor_dir = target_repo_dir / vendor_name
    target_vendor_dir.mkdir(parents=True, exist_ok=True)

    copy_file(vendor_manifest_path, target_vendor_dir / VENDOR_SECTION_PATH)

    section_order = build_section_order(vendor_manifest)
    json_objects: list[dict] = [vendor_manifest]
    json_file_count = 1
    for section in section_order[1:]:
        section_path = Path(section["path"])
        source_section_path = source_vendor_dir / Path(section["source_path"])
        if not source_section_path.exists():
            raise FileNotFoundError(f"Missing profile file for {vendor_name}: {source_section_path}")
        copy_file(source_section_path, target_vendor_dir / section_path)
        json_objects.append(load_json(source_section_path))
        json_file_count += 1

    copied_asset_paths: set[str] = set()
    for asset_path in sorted(path for path in source_vendor_dir.rglob("*") if path.is_file() and path.suffix.lower() != ".json"):
        rel_path = asset_path.relative_to(source_vendor_dir)
        target_rel_path = Path("assets") / rel_path
        copy_file(asset_path, target_vendor_dir / target_rel_path)
        copied_asset_paths.add(target_rel_path.as_posix())

    for shared_asset in sorted(collect_referenced_root_assets(json_objects, source_profiles_dir)):
        target_rel_path = Path("assets") / shared_asset.name
        copy_file(shared_asset, target_vendor_dir / target_rel_path)
        copied_asset_paths.add(target_rel_path.as_posix())

    idx_path = target_vendor_dir / "vendor.idx"
    idx_path.write_text(f'{vendor_manifest["version"]} Imported from official OrcaSlicer profiles\n', encoding="utf-8")

    metadata_section_order = [
        {"type": section["type"], "name": section["name"], "path": section["path"]}
        for section in section_order
    ]

    metadata = {
        "format": SOURCE_FORMAT,
        "repo": {
            "id": TARGET_REPO_ID,
            "name": "OrcaSlicer FFF",
            "description": "Profiles imported from the official OrcaSlicer repository",
            "visibility": "",
        },
        "source": {
            "upstream": UPSTREAM_REPO_URL,
            "profile_root": UPSTREAM_PROFILE_ROOT.as_posix(),
        },
        "vendor": vendor_name,
        "version": vendor_manifest["version"],
        "index_name": f"{vendor_name}.idx",
        "section_order": metadata_section_order,
        "asset_paths": sorted(copied_asset_paths),
    }
    (target_vendor_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return {
        "vendor": vendor_name,
        "version": vendor_manifest["version"],
        "json_files": json_file_count,
        "assets": len(copied_asset_paths),
    }


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    temp_root: Path | None = None
    try:
        source_root = args.source_dir.resolve() if args.source_dir else download_upstream_archive()
        if not args.source_dir:
            temp_root = source_root.parent

        source_profiles_dir = source_root / UPSTREAM_PROFILE_ROOT
        if not source_profiles_dir.is_dir():
            raise FileNotFoundError(f"Could not find Orca profile root at {source_profiles_dir}")

        ensure_clean_dir(TARGET_REPO_DIR)

        vendor_manifest_paths = sorted(
            path
            for path in source_profiles_dir.glob("*.json")
            if path.name not in ROOT_SKIP_NAMES and (source_profiles_dir / path.stem).is_dir()
        )

        summaries = [import_vendor(source_profiles_dir, path, TARGET_REPO_DIR) for path in vendor_manifest_paths]
        total_json_files = sum(int(item["json_files"]) for item in summaries)
        total_assets = sum(int(item["assets"]) for item in summaries)

        print(
            f"Imported {len(summaries)} OrcaSlicer vendors into {TARGET_REPO_DIR} "
            f"({total_json_files} json files, {total_assets} assets)"
        )
        return 0
    except Exception as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if temp_root and temp_root.exists():
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
