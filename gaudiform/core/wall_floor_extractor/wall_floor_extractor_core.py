# -*- coding: utf-8 -*-
"""
WallFloorExtractor core logic.

특정 층(floor)에 속한 벽(Wall)과 바닥(Floor) prim을 여러 USD 파일에서 수집하여
하나의 USD 파일로 머지 저장합니다.

동작:
  1. 지정 폴더의 .usd 파일을 순회
  2. IFCBUILDINGSTOREY prim으로 대상 층 Z 범위 자동 계산
  3. kind=component + Category/Family Name 필터 통과 prim 수집
  4. 수집된 모든 prim을 단일 USD (defaultPrim 아래)로 머지 저장

벽/바닥 판별 기준:
  - kind == "component"  (kind_filter="" 이면 무시)
  - omni:hoops:metadata:Other:Category 가 CATEGORIES에 속하거나
  - omni:hoops:metadata:Other:tn__FamilyName_mA 가 FAMILY_NAMES에 속하면 대상
"""

from __future__ import annotations

import os
from typing import Callable

from pxr import Kind, Sdf, Usd, UsdGeom, Gf


# ── Metadata attribute names ──────────────────────────────────────────────────
ATTR_TYPE        = "omni:hoops:metadata:TYPE"
ATTR_LEVEL_NAME  = "omni:hoops:metadata:tn__IdentityData_qC:Name"
ATTR_CATEGORY    = "omni:hoops:metadata:Other:Category"
ATTR_FAMILY_NAME = "omni:hoops:metadata:Other:tn__FamilyName_mA"

# ── 기본 필터 기준 ────────────────────────────────────────────────────────────
DEFAULT_CATEGORIES   = {"Curtain Panels", "Walls", "Floors"}
DEFAULT_FAMILY_NAMES = {"System Panel", "Access Floor Panel", "Basic Wall", "Floor"}


def _get_attr(prim: Usd.Prim, attr_name: str):
    attr = prim.GetAttribute(attr_name)
    if attr and attr.HasValue():
        return attr.Get()
    return None


def find_floor_levels(stage: Usd.Stage) -> dict[str, float]:
    """IFCBUILDINGSTOREY prim world Z 수집 → {name: z}"""
    levels: dict[str, float] = {}
    for prim in stage.TraverseAll():
        if _get_attr(prim, ATTR_TYPE) != "IFCBUILDINGSTOREY":
            continue
        name = _get_attr(prim, ATTR_LEVEL_NAME)
        if name and name not in levels:
            xf  = UsdGeom.Xformable(prim)
            mat = xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            levels[name] = mat.ExtractTranslation()[2]
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


def _is_target(prim: Usd.Prim,
               categories: set[str],
               family_names: set[str]) -> bool:
    """Category 또는 Family Name이 필터 목록에 속하면 True."""
    cat = _get_attr(prim, ATTR_CATEGORY)
    if cat and str(cat) in categories:
        return True
    fam = _get_attr(prim, ATTR_FAMILY_NAME)
    if fam and str(fam) in family_names:
        return True
    return False


def _dst_path(src_path: Sdf.Path, prim_root: str) -> Sdf.Path:
    """src_path를 prim_root 아래로 리맵. prim_root가 빈 문자열이면 원본 유지."""
    if not prim_root:
        return src_path
    rel = src_path.MakeRelativePath(Sdf.Path.absoluteRootPath)
    return Sdf.Path(f"/{prim_root}").AppendPath(rel)


def _ensure_ancestors(src_stage: Usd.Stage, dst_layer: Sdf.Layer,
                      src_path: Sdf.Path, prim_root: str) -> None:
    src_layer   = src_stage.GetRootLayer()
    parent_path = src_path.GetParentPath()
    if parent_path in (Sdf.Path.absoluteRootPath, Sdf.Path.emptyPath):
        return
    _ensure_ancestors(src_stage, dst_layer, parent_path, prim_root)

    dst_parent = _dst_path(parent_path, prim_root)
    if dst_layer.GetPrimAtPath(dst_parent):
        return

    src_spec = src_layer.GetPrimAtPath(parent_path)
    spec_type = src_spec.typeName if src_spec else ""
    specifier = src_spec.specifier if src_spec else Sdf.SpecifierDef

    dst_par_parent = dst_parent.GetParentPath()
    if dst_par_parent == Sdf.Path.absoluteRootPath:
        dst_spec = Sdf.PrimSpec(dst_layer, dst_parent.name, specifier)
    else:
        par_spec = dst_layer.GetPrimAtPath(dst_par_parent)
        if not par_spec:
            return
        dst_spec = Sdf.PrimSpec(par_spec, dst_parent.name, specifier)
    dst_spec.typeName = spec_type


def _ensure_root_prim(dst_layer: Sdf.Layer, prim_root: str) -> None:
    """dst_layer에 defaultPrim Xform 생성."""
    if not prim_root:
        return
    root_path = Sdf.Path(f"/{prim_root}")
    if not dst_layer.GetPrimAtPath(root_path):
        Sdf.PrimSpec(dst_layer, prim_root, Sdf.SpecifierDef, "Xform")
    dst_layer.defaultPrim = prim_root


def _copy_stage_metadata(src_layer: Sdf.Layer, dst_layer: Sdf.Layer) -> None:
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


def collect_from_usd(
    usd_path: str,
    z_min: float,
    z_max: float,
    categories: set[str],
    family_names: set[str],
    dst_layer: Sdf.Layer,
    log: Callable,
    kind_filter: str = "component",
    prim_root: str = "World",
) -> int:
    """단일 USD 파일에서 대상 층 벽/바닥 prim을 수집해 dst_layer에 복사."""
    try:
        stage = Usd.Stage.Open(usd_path)
    except Exception as e:
        log(f"[WARN] 열기 실패 {usd_path}: {e}")
        return 0

    src_layer  = stage.GetRootLayer()
    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])

    if not dst_layer.pseudoRoot.ListInfoKeys():
        _copy_stage_metadata(src_layer, dst_layer)

    count = 0
    for prim in stage.TraverseAll():
        if not prim.IsActive():
            continue
        if kind_filter and Usd.ModelAPI(prim).GetKind() != kind_filter:
            continue
        if not _is_target(prim, categories, family_names):
            continue
        if not _is_bbox_in_range(prim, bbox_cache, z_min, z_max):
            continue
        src_path = prim.GetPath()
        dst      = _dst_path(src_path, prim_root)
        _ensure_ancestors(stage, dst_layer, src_path, prim_root)
        try:
            Sdf.CopySpec(src_layer, src_path, dst_layer, dst)
            count += 1
        except Exception as e:
            log(f"[WARN] CopySpec 실패 {src_path}: {e}")

    return count


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
    default_prim: str = "World",
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

    # Z 범위 결정
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

    # 머지 레이어 생성
    dst_layer = Sdf.Layer.CreateAnonymous()
    _ensure_root_prim(dst_layer, default_prim)
    total = 0

    for usd_path in usd_files:
        filename = os.path.basename(usd_path)
        _log(f"  처리 중: {filename}")
        count = collect_from_usd(
            usd_path, resolved_z_min, resolved_z_max,
            cat_set, fam_set, dst_layer, _log, kind_filter, default_prim)
        _log(f"    -> {count}개 수집")
        total += count

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    dst_layer.Export(output_path)
    _log(f"[WallFloorExtractor] 완료: 총 {total}개 prim -> {output_path}")
    return total
