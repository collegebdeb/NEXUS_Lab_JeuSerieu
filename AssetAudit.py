# -*- coding: utf-8 -*-
"""
UE5 - Export CSV audit for large 3D assets + textures under /Game

Columns:
- name
- path
- class
- category
- disk_size_mb
- resolution
- used_in_scene
- git_author_name
- git_author_email
- git_commit_date
- git_commit_hash

Rules:
- Only assets under /Game
- Only textures + common 3D assets
- Skip assets <= 10 MB on disk
- Scene usage is marked X if the asset is reachable from a map/world
  through Asset Registry dependencies
"""

import csv
import os
import subprocess
import traceback
from collections import deque
from datetime import datetime

import unreal


ROOT_PACKAGE_PATH = "/Game"
OUTPUT_SUBDIR = os.path.join("Saved", "AssetAudit")
MIN_SIZE_MB = 10.0
MIN_SIZE_BYTES = int(MIN_SIZE_MB * 1024 * 1024)

INCLUDED_CLASS_NAMES = {
    # Textures
    "Texture",
    "Texture2D",
    "TextureCube",
    "TextureRenderTarget2D",
    "VolumeTexture",

    # 3D / art assets
    "StaticMesh",
    "SkeletalMesh",
    "Skeleton",
    "PhysicsAsset",
    "AnimSequence",
    "AnimMontage",
    "BlendSpace",
    "BlendSpace1D",
    "AimOffsetBlendSpace",
    "AimOffsetBlendSpace1D",
    "PoseAsset",
    "AnimationBlueprint",
    "Material",
    "MaterialInstanceConstant",
    "MaterialFunction",
    "GeometryCollection",
}

PACKAGE_SIDE_EXTENSIONS = [
    ".uasset",
    ".uexp",
    ".ubulk",
    ".uptnl",
    ".m.ubulk",
]


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

def log(msg):
    unreal.log("[AssetGitAudit] {}".format(msg))


def warn(msg):
    unreal.log_warning("[AssetGitAudit] {}".format(msg))


# -----------------------------------------------------------------------------
# Paths / helpers
# -----------------------------------------------------------------------------

def get_project_root():
    return os.path.normpath(unreal.Paths.project_dir())


def get_content_dir():
    return os.path.normpath(unreal.Paths.project_content_dir())


def ensure_output_dir(project_root):
    out_dir = os.path.join(project_root, OUTPUT_SUBDIR)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def safe_str(value):
    try:
        return str(value)
    except Exception:
        try:
            return repr(value)
        except Exception:
            return ""


def get_asset_class_name(asset_data):
    try:
        klass = unreal.AssetRegistryHelpers.find_asset_native_class(asset_data)
        if klass:
            return klass.get_name()
    except Exception:
        pass

    try:
        acp = asset_data.asset_class_path
        if hasattr(acp, "asset_name"):
            return safe_str(acp.asset_name)
        return safe_str(acp)
    except Exception:
        pass

    try:
        return safe_str(asset_data.asset_class)
    except Exception:
        pass

    return "Unknown"


def categorize_asset(class_name):
    if "Texture" in class_name:
        return "texture"
    return "3d_asset"


def asset_matches_filter(class_name):
    if class_name in INCLUDED_CLASS_NAMES:
        return True
    if "Texture" in class_name:
        return True
    return False


def package_name_to_base_fs_path(package_name, content_dir):
    package_name = safe_str(package_name)
    if not package_name.startswith("/Game"):
        return None

    rel = package_name[len("/Game"):].lstrip("/")
    if not rel:
        return None

    return os.path.join(content_dir, *rel.split("/"))


def compute_disk_size_bytes(package_name, content_dir):
    base = package_name_to_base_fs_path(package_name, content_dir)
    if not base:
        return 0

    total = 0
    for ext in PACKAGE_SIDE_EXTENSIONS:
        fp = base + ext
        if os.path.isfile(fp):
            total += os.path.getsize(fp)
    return total


def bytes_to_mb(size_bytes):
    return round(float(size_bytes) / (1024.0 * 1024.0), 3)


# -----------------------------------------------------------------------------
# Texture resolution
# -----------------------------------------------------------------------------

def try_call(obj, fn_name):
    try:
        fn = getattr(obj, fn_name, None)
        if fn:
            return fn()
    except Exception:
        pass
    return None


def get_texture_resolution(asset_obj):
    """
    Prefer source/imported size over runtime/display size.
    This avoids false tiny values like 32x32 in many cases.
    """
    if not asset_obj:
        return ""

    # 1) Source object size when available
    try:
        source = asset_obj.get_editor_property("source")
        if source:
            sx = try_call(source, "get_size_x")
            sy = try_call(source, "get_size_y")
            if sx and sy:
                return "{}x{}".format(sx, sy)
    except Exception:
        pass

    # 2) Imported/source size properties if exposed
    for wx, hy in [
        ("source_size_x", "source_size_y"),
        ("imported_size_x", "imported_size_y"),
    ]:
        try:
            w = asset_obj.get_editor_property(wx)
            h = asset_obj.get_editor_property(hy)
            if w and h:
                return "{}x{}".format(w, h)
        except Exception:
            pass

    # 3) Editor callable texture size
    try:
        w = asset_obj.blueprint_get_size_x()
        h = asset_obj.blueprint_get_size_y()
        if w and h and (w > 32 or h > 32):
            return "{}x{}".format(w, h)
    except Exception:
        pass

    # 4) Last chance fallback
    for wx, hy in [
        ("size_x", "size_y"),
    ]:
        try:
            w = asset_obj.get_editor_property(wx)
            h = asset_obj.get_editor_property(hy)
            if w and h:
                return "{}x{}".format(w, h)
        except Exception:
            pass

    return ""


# -----------------------------------------------------------------------------
# Git without popup windows on Windows
# -----------------------------------------------------------------------------

def get_subprocess_kwargs():
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "check": False,
    }

    if os.name == "nt":
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW
        kwargs["creationflags"] = creationflags

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = startupinfo

    return kwargs


def run_git(project_root, args):
    cmd = ["git", "-C", project_root] + args
    try:
        result = subprocess.run(cmd, **get_subprocess_kwargs())
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception as ex:
        warn("Git command failed: {} | {}".format(" ".join(cmd), ex))
        return ""


def is_git_repo(project_root):
    out = run_git(project_root, ["rev-parse", "--show-toplevel"])
    return bool(out)


def get_git_added_info_for_file(project_root, rel_path):
    fmt = "%H%x1f%an%x1f%ae%x1f%aI"
    out = run_git(project_root, [
        "log",
        "--diff-filter=A",
        "--follow",
        "--format={}".format(fmt),
        "-n", "1",
        "--",
        rel_path
    ])

    if not out:
        return {
            "git_author_name": "",
            "git_author_email": "",
            "git_commit_date": "",
            "git_commit_hash": "",
        }

    parts = out.split("\x1f")
    if len(parts) != 4:
        return {
            "git_author_name": "",
            "git_author_email": "",
            "git_commit_date": "",
            "git_commit_hash": "",
        }

    return {
        "git_commit_hash": parts[0],
        "git_author_name": parts[1],
        "git_author_email": parts[2],
        "git_commit_date": parts[3],
    }


def get_git_metadata_for_package(project_root, content_dir, package_name):
    base = package_name_to_base_fs_path(package_name, content_dir)
    if not base:
        return {
            "git_author_name": "",
            "git_author_email": "",
            "git_commit_date": "",
            "git_commit_hash": "",
        }

    candidates = []
    for ext in PACKAGE_SIDE_EXTENSIONS:
        abs_path = base + ext
        if os.path.isfile(abs_path):
            rel_path = os.path.relpath(abs_path, project_root).replace("\\", "/")
            candidates.append(rel_path)

    candidates.sort(key=lambda p: (0 if p.endswith(".uasset") else 1, p))

    for rel_path in candidates:
        info = get_git_added_info_for_file(project_root, rel_path)
        if info["git_commit_hash"]:
            return info

    return {
        "git_author_name": "",
        "git_author_email": "",
        "git_commit_date": "",
        "git_commit_hash": "",
    }


# -----------------------------------------------------------------------------
# Scene usage detection - map/world dependency graph
# -----------------------------------------------------------------------------

def get_dependency_options():
    """
    More permissive options, closer to what Reference Viewer can expose.
    Tries newer UE versions first, then older signatures.
    """
    constructors = [
        {
            "include_soft_package_references": True,
            "include_hard_package_references": True,
            "include_searchable_names": False,
            "include_soft_management_references": True,
            "include_hard_management_references": True,
            "include_game_package_references": True,
            "include_editor_only_references": True,
        },
        {
            "include_soft_package_references": True,
            "include_hard_package_references": True,
            "include_searchable_names": False,
            "include_soft_management_references": True,
            "include_hard_management_references": True,
        },
    ]

    for kwargs in constructors:
        try:
            return unreal.AssetRegistryDependencyOptions(**kwargs)
        except Exception:
            pass

    return None


def get_all_assets_under_game():
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    assets = registry.get_assets_by_path(ROOT_PACKAGE_PATH, recursive=True)
    return assets or []


def is_map_or_world_asset(asset_data):
    class_name = get_asset_class_name(asset_data)
    package_name = safe_str(asset_data.package_name)

    if class_name in ("World", "Level"):
        return True

    if package_name.endswith(".umap"):
        return True

    return False


def build_asset_data_map(all_assets):
    out = {}
    for ad in all_assets:
        out[safe_str(ad.package_name)] = ad
    return out


def build_scene_dependency_set(registry, all_assets):
    """
    Builds a set of all package names reachable from all maps/worlds
    through Asset Registry dependencies.

    This is generally more reliable than walking upward through referencers.
    """
    dep_opts = get_dependency_options()

    world_packages = []
    for ad in all_assets:
        try:
            if is_map_or_world_asset(ad):
                world_packages.append(safe_str(ad.package_name))
        except Exception:
            pass

    log("Found {} world/map packages".format(len(world_packages)))

    visited = set()
    queue = deque(world_packages)

    with unreal.ScopedSlowTask(max(len(world_packages), 1), "Building scene dependency graph...") as task:
        task.make_dialog(True)

        processed = 0
        while queue:
            current = queue.popleft()

            if current in visited:
                continue
            visited.add(current)

            processed += 1
            task.enter_progress_frame(1)

            if task.should_cancel():
                break

            try:
                if dep_opts is not None:
                    deps = registry.get_dependencies(current, dep_opts)
                else:
                    deps = registry.get_dependencies(current)

                deps = [safe_str(d) for d in deps if d]

                for dep in deps:
                    if dep not in visited:
                        queue.append(dep)

            except Exception as ex:
                warn("Dependency traversal failed for {} | {}".format(current, ex))

    log("Scene dependency set contains {} packages".format(len(visited)))
    return visited


# -----------------------------------------------------------------------------
# Asset gathering
# -----------------------------------------------------------------------------

def gather_assets():
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    assets = registry.get_assets_by_path(ROOT_PACKAGE_PATH, recursive=True)

    if not assets:
        return []

    filtered = []
    for asset_data in assets:
        try:
            class_name = get_asset_class_name(asset_data)
            if asset_matches_filter(class_name):
                filtered.append(asset_data)
        except Exception as ex:
            warn("Filter failed for asset_data {} | {}".format(safe_str(asset_data), ex))

    return filtered


# -----------------------------------------------------------------------------
# Main build
# -----------------------------------------------------------------------------

def build_rows():
    project_root = get_project_root()
    content_dir = get_content_dir()
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    git_ok = is_git_repo(project_root)

    if not git_ok:
        warn("No Git repository detected at project root: {}".format(project_root))

    all_assets_under_game = get_all_assets_under_game()
    scene_dependency_set = build_scene_dependency_set(registry, all_assets_under_game)

    all_candidate_assets = []
    for asset_data in all_assets_under_game:
        try:
            class_name = get_asset_class_name(asset_data)
            if asset_matches_filter(class_name):
                all_candidate_assets.append(asset_data)
        except Exception as ex:
            warn("Filter failed for asset_data {} | {}".format(safe_str(asset_data), ex))

    log("Found {} matching candidate assets under {}".format(len(all_candidate_assets), ROOT_PACKAGE_PATH))

    # Pass 1: cheap disk-size filter first
    large_assets = []
    with unreal.ScopedSlowTask(max(len(all_candidate_assets), 1), "Filtering assets by disk size...") as task:
        task.make_dialog(True)
        for asset_data in all_candidate_assets:
            if task.should_cancel():
                break
            task.enter_progress_frame(1)

            try:
                package_name = safe_str(asset_data.package_name)
                size_bytes = compute_disk_size_bytes(package_name, content_dir)
                if size_bytes > MIN_SIZE_BYTES:
                    large_assets.append((asset_data, size_bytes))
            except Exception as ex:
                warn("Size filter failed for {} | {}".format(safe_str(asset_data), ex))

    log("Kept {} assets > {} MB".format(len(large_assets), MIN_SIZE_MB))

    rows = []

    with unreal.ScopedSlowTask(max(len(large_assets), 1), "Auditing large assets...") as task:
        task.make_dialog(True)

        for asset_data, size_bytes in large_assets:
            if task.should_cancel():
                break
            task.enter_progress_frame(1)

            row = {
                "name": "",
                "path": "",
                "class": "",
                "category": "",
                "disk_size_mb": "",
                "resolution": "",
                "used_in_scene": "",
                "git_author_name": "",
                "git_author_email": "",
                "git_commit_date": "",
                "git_commit_hash": "",
            }

            try:
                package_name = safe_str(asset_data.package_name)
                asset_name = safe_str(asset_data.asset_name)
                class_name = get_asset_class_name(asset_data)
                category = categorize_asset(class_name)

                row["name"] = asset_name
                row["path"] = package_name
                row["class"] = class_name
                row["category"] = category
                row["disk_size_mb"] = bytes_to_mb(size_bytes)

                if package_name in scene_dependency_set:
                    row["used_in_scene"] = "X"

                if category == "texture":
                    asset_obj = None
                    try:
                        asset_obj = unreal.AssetRegistryHelpers.get_asset(asset_data)
                    except Exception:
                        try:
                            asset_obj = asset_data.get_asset()
                        except Exception:
                            asset_obj = None

                    if asset_obj:
                        row["resolution"] = get_texture_resolution(asset_obj)

                if git_ok:
                    row.update(get_git_metadata_for_package(project_root, content_dir, package_name))

            except Exception as ex:
                warn("Failed to process asset: {}".format(safe_str(asset_data)))
                warn("Exception: {}".format(ex))
                warn(traceback.format_exc())

            rows.append(row)

    return rows


def write_csv(rows):
    project_root = get_project_root()
    output_dir = ensure_output_dir(project_root)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(output_dir, "asset_audit_{}.csv".format(timestamp))

    fieldnames = [
        "name",
        "path",
        "class",
        "category",
        "disk_size_mb",
        "resolution",
        "used_in_scene",
        "git_author_name",
        "git_author_email",
        "git_commit_date",
        "git_commit_hash",
    ]

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return output_path


def main():
    rows = build_rows()
    output_path = write_csv(rows)

    log("Wrote {} rows to {}".format(len(rows), output_path))
    unreal.EditorDialog.show_message(
        "Asset Git Audit",
        "CSV generated:\n{}\n\nRows: {}".format(output_path, len(rows)),
        unreal.AppMsgType.OK
    )


if __name__ == "__main__":
    main()