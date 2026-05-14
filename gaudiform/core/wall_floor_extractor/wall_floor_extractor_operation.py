# -*- coding: utf-8 -*-
"""WallFloorExtractor post-processing operation.

phase = "post_all" — 건설 USD 폴더에서 특정 층의 벽/바닥 prim을 수집해 머지 저장.

params:
    construction_usd_dir  (str)   — 건설 USD 파일들이 있는 폴더 경로
    output_path           (str)   — 머지 결과 USD 저장 경로
    target_floor_name     (str,   default "9th FL")
    floor_z_auto          (bool,  default true)
    floor_z_min           (float, default 0.0)  — floor_z_auto=false일 때 사용
    floor_z_max           (float, default 0.0)  — floor_z_auto=false일 때 사용
    wall_types            (list,  default ["IFCWALL","IFCWALLSTANDARDCASE","IFCCURTAINWALL"])
    floor_types           (list,  default ["IFCSLAB","IFCPLATE"])
    recursive             (bool,  default true)
"""

from __future__ import annotations

from gaudiform.core.post_processing import PostProcessOperation, PostProcessContext
from gaudiform.core.wall_floor_extractor.wall_floor_extractor_core import process_folder

_TAG = "WallFloorExtractorOperation"


class WallFloorExtractorOperation(PostProcessOperation):
    """건설 USD 폴더에서 특정 층 벽/바닥 추출 및 머지 오퍼레이션."""

    phase = "post_all"

    def execute(self, context: PostProcessContext) -> None:
        p = context.params

        construction_usd_dir = p.get("construction_usd_dir", "")
        output_path          = p.get("output_path", "")

        if not construction_usd_dir:
            context.on_warn(_TAG, "construction_usd_dir 파라미터가 없습니다.")
            return
        if not output_path:
            context.on_warn(_TAG, "output_path 파라미터가 없습니다.")
            return

        target_floor_name = p.get("target_floor_name", "9th FL")
        floor_z_auto      = bool(p.get("floor_z_auto", True))
        floor_z_min       = float(p.get("floor_z_min", 0.0))
        floor_z_max       = float(p.get("floor_z_max", 0.0))
        wall_types        = p.get("wall_types", None)
        floor_types       = p.get("floor_types", None)
        recursive         = bool(p.get("recursive", True))

        context.on_info(_TAG, f"벽/바닥 추출 시작 — 대상 층: {target_floor_name}")
        context.on_info(_TAG, f"입력 폴더: {construction_usd_dir}")
        context.on_info(_TAG, f"출력 파일: {output_path}")

        def _log(msg: str) -> None:
            if "[WARN]" in msg:
                context.on_warn(_TAG, msg.strip())
            else:
                context.on_info(_TAG, msg.strip())

        total = process_folder(
            input_dir=construction_usd_dir,
            output_path=output_path,
            target_floor_name=target_floor_name,
            floor_z_auto=floor_z_auto,
            floor_z_min=floor_z_min,
            floor_z_max=floor_z_max,
            wall_types=wall_types,
            floor_types=floor_types,
            recursive=recursive,
            log=_log,
        )

        context.on_info(_TAG, f"완료: {total}개 prim 머지 → {output_path}")
