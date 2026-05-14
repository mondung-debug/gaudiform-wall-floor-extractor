# -*- coding: utf-8 -*-
"""
WallFloorExtractor core logic.

특정 층(floor)에 속한 벽(Wall)과 바닥(Slab) prim을 여러 USD 파일에서 수집하여
하나의 USD 파일로 머지 저장합니다.

동작:
  1. 지정 폴더의 .usd 파일을 순회
  2. IFCBUILDINGSTOREY prim으로 대상 층 Z 범위 자동 계산 (fab_splitter와 동일)
  3. 해당 Z 범위에 걸치는 벽/바닥 prim 수집
  4. 수집된 모든 prim을 단일 USD로 머지 저장

벽/바닥 판별 기준: TODO — 내일 확인 후 구현
  현재 플레이스홀더: ATTR_TYPE 기준 IFC 타입 목록으로 필터링
"""

from __future__ import annotations

import os
from typing import Callable

from pxr import Sdf, Usd, UsdGeom, Gf


# ── Metadata attribute names (Hoops Connector 규칙) ───────────────────────────
ATTR_TYPE       = "omni:hoops:metadata:TYPE"
ATTR_LEVEL_NAME = "omni:hoops:metadata:tn__IdentityData_qC:Name"

# ── 기본 벽/바닥 IFC 타입 (TODO: 실제 데이터 확인 후 수정) ────────────────────
DEFAULT_WALL_TYPES  = ["IFCWALL", "IFCWALLSTANDARDCASE", "IFCCURTAINWALL"]
DEFAULT_FLOOR_TYPES = ["IFCSLAB", "IFCPLATE"]


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
    """층 Z 범위 계산. (z_min, z_max) 반환, 실패 시 None."""
    if floor_z_auto:
        target_z = levels.get(target_floor_name)
        if target_z is None:
            log(f"[WARN] '{target_floor_name}' 층을 찾을 수 없습니다.")
            return None
        sorted_z = sorted(levels.values())
        idx  = sorted_z.index(target_z)
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


def _is_wall_or_floor(prim: Usd.Prim, wall_types: set[str], floor_types: set[str]) -> bool:
    """
    TODO: 실제 데이터 확인 후 판별 로직 확정.
    현재: ATTR_TYPE이 wall_types 또는 floor_types에 속하면 True.
    """
    prim_type = _get_attr(prim, ATTR_TYPE)
    if prim_type is None:
        return False
    return prim_type in wall_types or prim_type in floor_types


def _copy_stage_metadata(src_layer: Sdf.Layer, dst_layer: Sdf.Layer) -> None:
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


def _ensure_ancestors(src_stage: Usd.Stage, dst_layer: Sdf.Layer,
                      prim_path: Sdf.Path) -> None:
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


def collect_from_usd(
    usd_path: str,
    z_min: float,
    z_max: float,
    wall_types: set[str],
    floor_types: set[str],
    dst_layer: Sdf.Layer,
    log: Callable,
) -> int:
    """
    단일 USD 파일에서 대상 층 벽/바닥 prim을 수집해 dst_layer에 복사.
    Returns: 복사된 prim 수
    """
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
        if not _is_wall_or_floor(prim, wall_types, floor_types):
            continue
        if not _is_bbox_in_range(prim, bbox_cache, z_min, z_max):
            continue
        _ensure_ancestors(stage, dst_layer, prim.GetPath())
        try:
            Sdf.CopySpec(src_layer, prim.GetPath(), dst_layer, prim.GetPath())
            count += 1
        except Exception as e:
            log(f"[WARN] CopySpec 실패 {prim.GetPath()}: {e}")

    return count


def process_folder(
    input_dir: str,
    output_path: str,
    target_floor_name: str = "9th FL",
    floor_z_auto: bool = True,
    floor_z_min: float = 0.0,
    floor_z_max: float = 0.0,
    wall_types: list[str] | None = None,
    floor_types: list[str] | None = None,
    recursive: bool = True,
    log: Callable[[str], None] | None = None,
) -> int:
    """
    input_dir의 USD 파일들에서 대상 층 벽/바닥을 수집해 output_path에 머지 저장.

    Returns: 총 수집된 prim 수
    """
    _log = log or print
    wall_set  = set(wall_types  or DEFAULT_WALL_TYPES)
    floor_set = set(floor_types or DEFAULT_FLOOR_TYPES)

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

    # Z 범위 결정: 첫 번째 USD에서 층 정보 탐색
    resolved_z_min, resolved_z_max = floor_z_min, floor_z_max
    if floor_z_auto and usd_files:
        for usd_path in usd_files:
            try:
                stage  = Usd.Stage.Open(usd_path)
                levels = find_floor_levels(stage)
                if levels:
                    for name, z in sorted(levels.items(), key=lambda x: x[1]):
                        marker = " ← TARGET" if name == target_floor_name else ""
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
            _log(f"[WARN] 층 정보를 찾을 수 없습니다. floor_z_min/max 기본값 사용.")
    else:
        _log(f"[CONFIG] Z range: [{resolved_z_min:.3f}, {resolved_z_max:.3f}]")

    # 머지 레이어 생성
    dst_layer = Sdf.Layer.CreateAnonymous()
    total = 0

    for usd_path in usd_files:
        filename = os.path.basename(usd_path)
        _log(f"  처리 중: {filename}")
        count = collect_from_usd(
            usd_path, resolved_z_min, resolved_z_max,
            wall_set, floor_set, dst_layer, _log)
        _log(f"    → {count}개 수집")
        total += count

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    dst_layer.Export(output_path)
    _log(f"[WallFloorExtractor] 완료: 총 {total}개 prim → {output_path}")
    return total
