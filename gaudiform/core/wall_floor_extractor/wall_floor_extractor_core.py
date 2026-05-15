# -*- coding: utf-8 -*-
"""
WallFloorExtractor core logic.

특정 층(floor)에 속한 벽(Wall)과 바닥(Floor) prim을 여러 USD 파일에서 수집하여
하나의 USD 파일로 머지 저장합니다.

동작:
  1. 지정 폴더의 .usd 파일을 순회
  2. kind=group + Category=Levels 로 층 Z 범위 자동 계산
  3. kind=component + Category/Family Name 필터 + bbox Z 범위로 prim 수집
  4. fab_splitter 방식으로 ancestor 포함 머지 저장
"""

from __future__ import annotations

import os
from typing import Callable

from pxr import Kind, Sdf, Usd, UsdGeom


# ── Metadata attribute names ──────────────────────────────────────────────────
ATTR_CATEGORY    = "omni:hoops:metadata:Other:Category"
ATTR_FAMILY_NAME = "omni:hoops:metadata:Other:tn__FamilyName_mA"
ATTR_LEVEL_NAME  = "omni:hoops:metadata:tn__IdentityData_qC:Name"

# ── 기본 필터 기준 ────────────────────────────────────────────────────────────
DEFAULT_CATEGORIES   = {"Curtain Panels", "Walls", "Floors"}
DEFAULT_FAMILY_NAMES = {"System Panel", "Access Floor Panel", "Basic Wall", "Floor"}


def _get_attr(prim: Usd.Prim, attr_name: str):
    attr = prim.GetAttribute(attr_name)
    if attr and attr.HasValue():
        return attr.Get()
    return None


# ── 층 탐색 (kind=group + Category=Levels) ──────────────────────────────────

def find_floor_levels(stage: Usd.Stage) -> dict[str, float]:
    """kind=group + Category=Levels prim world Z 수집 → {name: z}"""
    levels: dict[str, float] = {}
    for prim in stage.TraverseAll():
        if Usd.ModelAPI(prim).GetKind() != "group":
            continue
        if _get_attr(prim, ATTR_CATEGORY) != "Levels":
            continue
        name = _get_attr(prim, ATTR_LEVEL_NAME)
        if name and str(name) not in levels:
            xf  = UsdGeom.Xformable(prim)
            mat = xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            levels[str(name)] = mat.ExtractTranslation()[2]
    return levels


def _calc_z_range(
    levels: dict[str, float],
    target_floor_name: str,
    floor_z_auto: bool,
    floor_z_min: float,
    floor_z_max: float,
    log: Callable,
) -> tuple[float, float] | None:
    if floor_z_auto:
        target_z = levels.get(target_floor_name)
        if target_z is None:
            log(f"[WARN] '{target_floor_name}' 층을 찾을 수 없습니다.")
            return None
        sorted_z = sorted(levels.values())
        idx   = sorted_z.index(target_z)
        z_min = target_z
        z_max = sorted_z[idx + 1] if idx + 1 < len(sorted_z) else target_z + 10.0
        log(f"[AUTO] '{target_floor_name}' Z={target_z:.3f} → range: [{z_min:.3f}, {z_max:.3f}]")
        return z_min, z_max
    else:
        log(f"[CONFIG] Z range: [{floor_z_min:.3f}, {floor_z_max:.3f}]")
        return floor_z_min, floor_z_max


# ── 수집 ─────────────────────────────────────────────────────────────────────

def _is_bbox_in_range(prim: Usd.Prim, bbox_cache: UsdGeom.BBoxCache,
                      z_min: float, z_max: float) -> bool:
    try:
        bbox = bbox_cache.ComputeWorldBound(prim)
        rng  = bbox.ComputeAlignedRange()
        if rng.IsEmpty():
            return False
        return rng.GetMin()[2] <= z_max and rng.GetMax()[2] >= z_min
    except Exception:
        return False


def collect_prims(
    stage: Usd.Stage,
    bbox_cache: UsdGeom.BBoxCache,
    z_min: float,
    z_max: float,
    categories: set[str],
    family_names: set[str],
    kind_filter: str,
    log: Callable,
) -> list[Sdf.Path]:
    """조건에 맞는 prim path 목록 반환."""
    result: list[Sdf.Path] = []
    for prim in stage.TraverseAll():
        if not prim.IsActive():
            continue
        if kind_filter and Usd.ModelAPI(prim).GetKind() != kind_filter:
            continue
        cat = _get_attr(prim, ATTR_CATEGORY)
        fam = _get_attr(prim, ATTR_FAMILY_NAME)
        if not (cat and str(cat) in categories) and \
           not (fam and str(fam) in family_names):
            continue
        if not _is_bbox_in_range(prim, bbox_cache, z_min, z_max):
            continue
        result.append(prim.GetPath())
    return result


# ── 내보내기 (fab_splitter 방식) ─────────────────────────────────────────────

def _copy_stage_metadata(src_layer: Sdf.Layer, dst_layer: Sdf.Layer,
                          up_axis: str = "Z") -> None:
    dst_layer.defaultPrim   = src_layer.defaultPrim
    dst_layer.documentation = src_layer.documentation
    if src_layer.customLayerData:
        dst_layer.customLayerData = dict(src_layer.customLayerData)
    src_pr = src_layer.pseudoRoot
    dst_pr = dst_layer.pseudoRoot
    for key in ["upAxis", "metersPerUnit", "kilogramsPerUnit",
                "framesPerSecond", "timeCodesPerSecond"]:
        if key in src_pr.ListInfoKeys():
            try:
                dst_pr.SetInfo(key, src_pr.GetInfo(key))
            except Exception:
                pass
    # upAxis 강제 설정
    if up_axis:
        try:
            dst_pr.SetInfo("upAxis", up_axis)
        except Exception:
            pass


def _ensure_ancestors(src_stage: Usd.Stage, dst_layer: Sdf.Layer,
                      prim_path: Sdf.Path) -> None:
    """fab_splitter 방식: ancestor prim을 속성/메타 포함해서 생성."""
    src_layer   = src_stage.GetRootLayer()
    parent_path = prim_path.GetParentPath()
    if parent_path in (Sdf.Path.absoluteRootPath, Sdf.Path.emptyPath):
        return
    _ensure_ancestors(src_stage, dst_layer, parent_path)
    if dst_layer.GetPrimAtPath(parent_path):
        return
    src_spec = src_layer.GetPrimAtPath(parent_path)
    if not src_spec:
        return
    par_parent = parent_path.GetParentPath()
    if par_parent == Sdf.Path.absoluteRootPath:
        dst_spec = Sdf.PrimSpec(dst_layer, parent_path.name, src_spec.specifier)
    else:
        par_spec = dst_layer.GetPrimAtPath(par_parent)
        if not par_spec:
            return
        dst_spec = Sdf.PrimSpec(par_spec, parent_path.name, src_spec.specifier)
    dst_spec.typeName = src_spec.typeName
    for key in src_spec.ListInfoKeys():
        if key in ("specifier", "typeName"):
            continue
        try:
            dst_spec.SetInfo(key, src_spec.GetInfo(key))
        except Exception:
            pass
    for prop_spec in src_spec.properties.values():
        try:
            Sdf.CopySpec(src_layer, prop_spec.path, dst_layer, prop_spec.path)
        except Exception:
            pass


def export_merged(
    src_stage: Usd.Stage,
    prim_paths: list[Sdf.Path],
    output_path: str,
    up_axis: str = "Z",
    log: Callable = print,
) -> None:
    src_layer = src_stage.GetRootLayer()
    dst_layer = Sdf.Layer.CreateAnonymous()
    _copy_stage_metadata(src_layer, dst_layer, up_axis)

    for path in prim_paths:
        _ensure_ancestors(src_stage, dst_layer, path)
        try:
            Sdf.CopySpec(src_layer, path, dst_layer, path)
        except Exception as e:
            log(f"[WARN] CopySpec 실패 {path}: {e}")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    dst_layer.Export(output_path)


# ── 메인 ─────────────────────────────────────────────────────────────────────

def process_folder(
    input_dir: str,
    output_path: str,
    target_floor_name: str = "9th FL",
    floor_z_auto: bool = True,
    floor_z_min: float = 0.0,
    floor_z_max: float = 0.0,
    categories: list[str] | None = None,
    family_names: list[str] | None = None,
    kind_filter: str = "component",
    up_axis: str = "Z",
    recursive: bool = True,
    log: Callable[[str], None] | None = None,
) -> int:
    """
    input_dir의 USD 파일들에서 대상 층 벽/바닥을 수집해 output_path에 머지 저장.
    Returns: 총 수집된 prim 수
    """
    _log    = log or print
    cat_set = set(categories   or DEFAULT_CATEGORIES)
    fam_set = set(family_names or DEFAULT_FAMILY_NAMES)

    # USD 파일 목록 수집
    usd_files: list[str] = []
    if recursive:
        for root, _, files in os.walk(input_dir):
            for f in files:
                if f.lower().endswith((".usd", ".usda", ".usdc", ".usdz")):
                    usd_files.append(os.path.join(root, f))
    else:
        usd_files = [
            os.path.join(input_dir, f) for f in os.listdir(input_dir)
            if f.lower().endswith((".usd", ".usda", ".usdc", ".usdz"))
        ]

    _log(f"[WallFloorExtractor] USD 파일 {len(usd_files)}개 발견")

    # Z 범위 결정 (첫 번째 유효 USD에서 층 정보 탐색)
    resolved_z_min, resolved_z_max = floor_z_min, floor_z_max
    if floor_z_auto and usd_files:
        for usd_path in usd_files:
            try:
                stage  = Usd.Stage.Open(usd_path)
                levels = find_floor_levels(stage)
                if levels:
                    for name, z in sorted(levels.items(), key=lambda x: x[1]):
                        marker = " <- TARGET" if name == target_floor_name else ""
                        _log(f"  [FLOOR] {name}: Z={z:.3f}m{marker}")
                    result = _calc_z_range(
                        levels, target_floor_name, floor_z_auto,
                        floor_z_min, floor_z_max, _log)
                    if result:
                        resolved_z_min, resolved_z_max = result
                        break
            except Exception:
                continue
        else:
            _log("[WARN] 층 정보를 찾을 수 없습니다. floor_z_min/max 기본값 사용.")
    else:
        _log(f"[CONFIG] Z range: [{resolved_z_min:.3f}, {resolved_z_max:.3f}]")

    # 각 USD에서 prim 수집 후 머지
    all_paths_per_stage: list[tuple[Usd.Stage, list[Sdf.Path]]] = []
    total = 0

    for usd_path in usd_files:
        filename = os.path.basename(usd_path)
        try:
            stage = Usd.Stage.Open(usd_path)
        except Exception as e:
            _log(f"  [WARN] 열기 실패 {filename}: {e}")
            continue

        bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
        paths = collect_prims(stage, bbox_cache, resolved_z_min, resolved_z_max,
                               cat_set, fam_set, kind_filter, _log)
        _log(f"  {filename}: {len(paths)}개 수집")
        if paths:
            all_paths_per_stage.append((stage, paths))
            total += len(paths)

    if total == 0:
        _log("[WallFloorExtractor] 수집된 prim 없음.")
        return 0

    # 첫 번째 stage 기준으로 머지 레이어 생성 후 나머지 append
    first_stage, first_paths = all_paths_per_stage[0]
    src_layer = first_stage.GetRootLayer()
    dst_layer = Sdf.Layer.CreateAnonymous()
    _copy_stage_metadata(src_layer, dst_layer, up_axis)

    for stage, paths in all_paths_per_stage:
        s_layer = stage.GetRootLayer()
        for path in paths:
            _ensure_ancestors(stage, dst_layer, path)
            try:
                Sdf.CopySpec(s_layer, path, dst_layer, path)
            except Exception as e:
                _log(f"  [WARN] CopySpec 실패 {path}: {e}")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    dst_layer.Export(output_path)
    _log(f"[WallFloorExtractor] 완료: 총 {total}개 prim -> {output_path}")
    return total
