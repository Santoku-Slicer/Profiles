#!/usr/bin/env python3
"""Build root-level profile artifacts from the repo's split profile sources.

Input layout:
    <repo>/profile_sources/
      <repo-id>/
        <vendor-name>/
          metadata.json
          vendor.idx
          vendor/
          printer_model/
          printer/
          print/
          filament/
          physical_printer/
          presets/
          obsolete_presets/

Output layout:
    <repo>/
      manifest.json
      repos/
        <repo-id>/
          vendor_indices.zip
          <vendor-name>/
            <version>.ini
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urljoin


REPO_URLS = (
    "https://preset-repo-api.prusa3d.com/v1/repos",
    "https://raw.githubusercontent.com/Dark98/SliceBeam/refs/heads/master/.profiledumpsrepo/manifest.json",
)

USER_AGENT = "SliceBeamProfileDump/1.0"
INVALID_FILE_CHARS = '<>:"/\\|?*'
ASSET_KEYS = {"thumbnail", "bed_model", "bed_texture"}
REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_SOURCES_DIR = REPO_ROOT / "profile_sources"
BACKEND_STATIC_DIR = REPO_ROOT


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as response:
        return response.read()


def fetch_json(url: str):
    return json.loads(fetch_bytes(url).decode("utf-8"))


def parse_vendor_version(index_bytes: bytes) -> str:
    for raw_line in index_bytes.decode("utf-8").splitlines():
        line = raw_line.strip()
        if not line or " = " in line:
            continue
        space_index = line.find(" ")
        if space_index == -1:
            raise ValueError(f"Malformed vendor index line: {line!r}")
        return line[:space_index]
    raise ValueError("Could not determine vendor version from index file")


def sanitize_file_name(name: str) -> str:
    sanitized = "".join("_" if ch in INVALID_FILE_CHARS else ch for ch in name).strip()
    sanitized = sanitized.rstrip(". ")
    return sanitized or "unnamed"


def safe_asset_relative_path(path_str: str) -> Path:
    normalized = path_str.replace("\\", "/").strip().lstrip("/")
    if not normalized:
        raise ValueError("Empty asset path")
    candidate = Path(*[part for part in normalized.split("/") if part not in ("", ".")])
    if any(part == ".." for part in candidate.parts):
        raise ValueError(f"Unsafe asset path: {path_str}")
    return candidate


def parse_sections(ini_text: str) -> dict[str, list[tuple[str, list[str]]]]:
    grouped: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    for section_type, section_name, lines in parse_sections_ordered(ini_text):
        grouped[section_type].append((section_name, lines))
    return dict(grouped)


def parse_sections_ordered(ini_text: str) -> list[tuple[str, str, list[str]]]:
    sections: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    current_type: str | None = None
    current_name: str | None = None
    current_lines: list[str] = []
    ordered: list[tuple[str, str, list[str]]] = []

    def flush() -> None:
        nonlocal current_type, current_name, current_lines
        if current_type is None:
            return
        name = current_name or current_type
        sections[current_type].append((name, current_lines.copy()))
        ordered.append((current_type, name, current_lines.copy()))
        current_type = None
        current_name = None
        current_lines = []

    for line in ini_text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            flush()
            section = line[1:-1]
            if ":" in section:
                section_type, section_name = section.split(":", 1)
            else:
                section_type, section_name = section, section
            current_type = section_type
            current_name = section_name
            current_lines = [line]
        elif current_type is not None:
            current_lines.append(line)

    flush()
    return ordered


def iter_referenced_assets(ordered_sections: list[tuple[str, str, list[str]]]) -> list[str]:
    referenced: list[str] = []
    seen: set[str] = set()
    for section_type, _, lines in ordered_sections:
        if section_type != "printer_model":
            continue
        for line in lines[1:]:
            stripped = line.strip()
            if not stripped or stripped.startswith(";") or stripped.startswith("#") or " = " not in stripped:
                continue
            key, value = stripped.split(" = ", 1)
            if key not in ASSET_KEYS:
                continue
            value = value.strip()
            if not value or value in seen:
                continue
            safe_asset_relative_path(value)
            seen.add(value)
            referenced.append(value)
    return referenced


def fetch_optional_asset(base_url: str, asset_path: str) -> bytes | None:
    safe_rel_path = safe_asset_relative_path(asset_path).as_posix()
    encoded_path = "/".join(quote(part) for part in safe_rel_path.split("/"))
    asset_url = urljoin(base_url.rstrip("/") + "/", encoded_path)
    try:
        return fetch_bytes(asset_url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"    [warn] missing asset {asset_path} at {asset_url}")
            return None
        raise


def write_assets(assets_root: Path, assets: dict[str, bytes]) -> list[str]:
    written: list[str] = []
    for rel_path_str, data in sorted(assets.items()):
        rel_path = safe_asset_relative_path(rel_path_str)
        target = assets_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(rel_path.as_posix())
    return written


def collect_split_source_assets(vendor_dir: Path) -> dict[str, bytes]:
    assets_dir = vendor_dir / "assets"
    if not assets_dir.exists():
        return {}

    assets: dict[str, bytes] = {}
    for path in sorted(p for p in assets_dir.rglob("*") if p.is_file()):
        rel_path = path.relative_to(assets_dir).as_posix()
        safe_asset_relative_path(rel_path)
        assets[rel_path] = path.read_bytes()
    return assets


def write_sections(split_dir: Path, ini_text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for section_type, _, _ in parse_sections_ordered(ini_text):
        counts[section_type] = counts.get(section_type, 0) + 1
    _write_split_sections(split_dir, parse_sections_ordered(ini_text))
    return counts


def _write_split_sections(split_dir: Path, ordered_sections: list[tuple[str, str, list[str]]]) -> list[dict[str, str]]:
    file_usage: dict[tuple[str, str], int] = defaultdict(int)
    section_order: list[dict[str, str]] = []
    for section_type, section_name, lines in ordered_sections:
        target_dir = split_dir / sanitize_file_name(section_type)
        target_dir.mkdir(parents=True, exist_ok=True)

        file_usage[(section_type, section_name)] += 1
        suffix = file_usage[(section_type, section_name)]
        file_name = sanitize_file_name(section_name)
        if not file_name.lower().endswith(".ini"):
            file_name = f"{file_name}.ini"
        if suffix > 1:
            stem = Path(file_name).stem
            file_name = f"{stem}__{suffix}{Path(file_name).suffix}"

        relative_path = Path(sanitize_file_name(section_type)) / file_name
        (split_dir / relative_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        section_order.append(
            {
                "type": section_type,
                "name": section_name,
                "path": relative_path.as_posix(),
            }
        )
    return section_order


def iter_filtered_repos(repo_filter: set[str] | None) -> list[dict]:
    all_repos: list[dict] = []
    for manifest_url in REPO_URLS:
        entries = fetch_json(manifest_url)
        for entry in entries:
            if not entry["id"].endswith("-fff"):
                continue
            if repo_filter and entry["id"] not in repo_filter:
                continue
            all_repos.append(entry)
    return all_repos


def iter_vendor_entries(index_zip_bytes: bytes) -> Iterable[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(index_zip_bytes)) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith("/"):
                continue
            with zf.open(name) as fp:
                yield name, fp.read()


def dump_vendor(repo: dict, vendor_index_name: str, vendor_index_bytes: bytes, output_root: Path) -> tuple[dict, dict[str, bytes]]:
    vendor_name = Path(vendor_index_name).stem
    version = parse_vendor_version(vendor_index_bytes)
    ini_url = repo["url"].rstrip("/") + f"/{vendor_name}/{version}.ini"
    ini_bytes = fetch_bytes(ini_url)
    ini_text = ini_bytes.decode("utf-8")
    base_url = repo["url"].rstrip("/") + f"/{vendor_name}"
    ordered_sections = parse_sections_ordered(ini_text)
    referenced_assets = iter_referenced_assets(ordered_sections)
    assets: dict[str, bytes] = {}
    for asset_path in referenced_assets:
        asset_bytes = fetch_optional_asset(base_url, asset_path)
        if asset_bytes is not None:
            assets[asset_path] = asset_bytes

    vendor_dir = output_root / repo["id"] / sanitize_file_name(vendor_name)
    raw_dir = vendor_dir / "raw"
    split_dir = vendor_dir / "split"
    assets_dir = vendor_dir / "assets"
    raw_dir.mkdir(parents=True, exist_ok=True)
    split_dir.mkdir(parents=True, exist_ok=True)
    if assets:
        assets_dir.mkdir(parents=True, exist_ok=True)

    raw_path = raw_dir / f"{sanitize_file_name(version)}.ini"
    raw_path.write_bytes(ini_bytes)
    section_counts = write_sections(split_dir, ini_text)
    asset_paths = write_assets(assets_dir, assets) if assets else []

    metadata = {
        "repo_id": repo["id"],
        "repo_name": repo.get("name"),
        "vendor": vendor_name,
        "version": version,
        "ini_url": ini_url,
        "index_url": repo["index_url"],
        "section_counts": section_counts,
        "asset_paths": asset_paths,
    }
    (vendor_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata, assets


def write_split_source_vendor(
    split_root: Path,
    repo: dict,
    vendor_index_name: str,
    vendor_index_bytes: bytes,
    version: str,
    ini_text: str,
    assets: dict[str, bytes] | None = None,
) -> None:
    vendor_name = Path(vendor_index_name).stem
    vendor_dir = split_root / sanitize_file_name(repo["id"]) / sanitize_file_name(vendor_name)
    vendor_dir.mkdir(parents=True, exist_ok=True)

    ordered_sections = parse_sections_ordered(ini_text)
    section_order = _write_split_sections(vendor_dir, ordered_sections)
    asset_paths = write_assets(vendor_dir / "assets", assets or {}) if assets else []
    (vendor_dir / "vendor.idx").write_bytes(vendor_index_bytes)
    metadata = {
        "repo": {
            "id": repo["id"],
            "name": repo.get("name", repo["id"]),
            "description": repo.get("description", ""),
            "visibility": repo.get("visibility", ""),
        },
        "vendor": vendor_name,
        "version": version,
        "index_name": vendor_index_name,
        "section_order": section_order,
        "asset_paths": asset_paths,
    }
    (vendor_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def write_backend_static_repo(static_root: Path, repos: list[dict], vendor_artifacts: dict[str, list[dict]]) -> None:
    static_root.mkdir(parents=True, exist_ok=True)
    repos_root = static_root / "repos"
    repos_root.mkdir(parents=True, exist_ok=True)

    manifest_entries = []
    for repo in repos:
        repo_id = repo["id"]
        repo_root = repos_root / sanitize_file_name(repo_id)
        repo_root.mkdir(parents=True, exist_ok=True)

        artifacts = vendor_artifacts.get(repo_id, [])
        for artifact in artifacts:
            vendor_root = repo_root / sanitize_file_name(artifact["vendor_name"])
            vendor_root.mkdir(parents=True, exist_ok=True)
            (vendor_root / f"{sanitize_file_name(artifact['version'])}.ini").write_bytes(artifact["ini_bytes"])
            for asset_rel_path, asset_bytes in sorted(artifact.get("assets", {}).items()):
                asset_target = vendor_root / safe_asset_relative_path(asset_rel_path)
                asset_target.parent.mkdir(parents=True, exist_ok=True)
                asset_target.write_bytes(asset_bytes)

        with zipfile.ZipFile(repo_root / "vendor_indices.zip", "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for artifact in artifacts:
                zf.writestr(artifact["index_name"], artifact["index_bytes"])

        manifest_entries.append(
            {
                "name": repo.get("name", repo_id),
                "description": repo.get("description", ""),
                "visibility": repo.get("visibility", ""),
                "id": repo_id,
                "url": f"./repos/{repo_id}",
                "index_url": f"./repos/{repo_id}/vendor_indices.zip",
            }
        )

    (static_root / "manifest.json").write_text(json.dumps(manifest_entries, indent=2), encoding="utf-8")


def build_backend_static_from_split_source(split_root: Path, static_root: Path) -> None:
    repos_map: dict[str, dict] = {}
    vendor_artifacts: dict[str, list[dict]] = defaultdict(list)

    for repo_dir in sorted(path for path in split_root.iterdir() if path.is_dir()):
        for vendor_dir in sorted(path for path in repo_dir.iterdir() if path.is_dir()):
            metadata_path = vendor_dir / "metadata.json"
            if not metadata_path.exists():
                raise FileNotFoundError(f"Missing metadata.json in {vendor_dir}")

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            repo = metadata["repo"]
            repo_id = repo["id"]
            repos_map[repo_id] = {
                "id": repo_id,
                "name": repo.get("name", repo_id),
                "description": repo.get("description", ""),
                "visibility": repo.get("visibility", ""),
            }

            parts: list[str] = []
            for section in metadata["section_order"]:
                section_path = vendor_dir / Path(section["path"])
                content = section_path.read_text(encoding="utf-8").rstrip()
                parts.append(content)
            ini_text = "\n\n".join(parts) + "\n"

            vendor_artifacts[repo_id].append(
                {
                    "vendor_name": metadata["vendor"],
                    "index_name": metadata["index_name"],
                    "index_bytes": (vendor_dir / "vendor.idx").read_bytes(),
                    "version": metadata["version"],
                    "ini_bytes": ini_text.encode("utf-8"),
                    "assets": collect_split_source_assets(vendor_dir),
                }
            )

    repos = sorted(repos_map.values(), key=lambda item: item["id"])
    write_backend_static_repo(static_root, repos, vendor_artifacts)


def build_backend_static_repo_in_repo() -> None:
    build_backend_static_from_split_source(PROFILE_SOURCES_DIR, BACKEND_STATIC_DIR)


def split_backend_static_repo(static_root: Path, split_root: Path) -> None:
    manifest_path = static_root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing manifest.json in {static_root}")

    repos = json.loads(manifest_path.read_text(encoding="utf-8"))
    for repo in repos:
        repo_id = repo["id"]
        repo_dir = static_root / "repos" / sanitize_file_name(repo_id)
        index_zip_path = repo_dir / "vendor_indices.zip"
        if not index_zip_path.exists():
            raise FileNotFoundError(f"Missing vendor_indices.zip for repo {repo_id}")

        with zipfile.ZipFile(index_zip_path) as zf:
            for index_name in sorted(zf.namelist()):
                if index_name.endswith("/"):
                    continue
                vendor_name = Path(index_name).stem
                index_bytes = zf.read(index_name)
                version = parse_vendor_version(index_bytes)
                ini_path = repo_dir / sanitize_file_name(vendor_name) / f"{sanitize_file_name(version)}.ini"
                if not ini_path.exists():
                    raise FileNotFoundError(f"Missing INI for {repo_id}/{vendor_name}/{version}")
                ini_text = ini_path.read_text(encoding="utf-8")
                ordered_sections = parse_sections_ordered(ini_text)
                assets: dict[str, bytes] = {}
                for asset_rel_path in iter_referenced_assets(ordered_sections):
                    asset_path = repo_dir / sanitize_file_name(vendor_name) / safe_asset_relative_path(asset_rel_path)
                    if asset_path.exists():
                        assets[asset_rel_path] = asset_path.read_bytes()
                write_split_source_vendor(split_root, repo, index_name, index_bytes, version, ini_text, assets)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args(argv)

    try:
        build_backend_static_repo_in_repo()
        print(f"Backend static repo written to {BACKEND_STATIC_DIR}")
        return 0
    except urllib.error.URLError as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
