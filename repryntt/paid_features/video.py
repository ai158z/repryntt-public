"""
repryntt.paid_features.video — Video Production (Pro tree: local impl).

Thin re-exports over the full local ``repryntt.tools.video_production``
pipeline. The OSS distribution ships a divergent HTTPS-only variant of
this file; both expose the same public surface. See MIRROR_EXCLUDE.md
for why this file is not mirrored.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict

from repryntt.tools import video_production as _vp

logger = logging.getLogger("repryntt.paid_features.video")


def create_video_project(*args: Any, **kw: Any) -> Any:    return _vp.create_video_project(*args, **kw)
def write_screenplay(*args: Any, **kw: Any) -> Any:        return _vp.write_screenplay(*args, **kw)
def create_shot_list(*args: Any, **kw: Any) -> Any:        return _vp.create_shot_list(*args, **kw)
def generate_video_clip(*args: Any, **kw: Any) -> Any:     return _vp.generate_video_clip(*args, **kw)
def generate_all_clips(*args: Any, **kw: Any) -> Any:      return _vp.generate_all_clips(*args, **kw)
def generate_narration(*args: Any, **kw: Any) -> Any:      return _vp.generate_narration(*args, **kw)
def generate_music(*args: Any, **kw: Any) -> Any:          return _vp.generate_music(*args, **kw)
def assemble_edit(*args: Any, **kw: Any) -> Any:           return _vp.assemble_edit(*args, **kw)
def qa_review_video(*args: Any, **kw: Any) -> Any:         return _vp.qa_review_video(*args, **kw)
def render_final(*args: Any, **kw: Any) -> Any:            return _vp.render_final(*args, **kw)
def video_project_status(*args: Any, **kw: Any) -> Any:    return _vp.video_project_status(*args, **kw)
def generate_thumbnail(*args: Any, **kw: Any) -> Any:      return _vp.generate_thumbnail(*args, **kw)
def auto_produce_video(*args: Any, **kw: Any) -> Any:      return _vp.auto_produce_video(*args, **kw)


ALL_VIDEO_TOOLS: Dict[str, Callable] = {
    "create_video_project": create_video_project,
    "write_screenplay":     write_screenplay,
    "create_shot_list":     create_shot_list,
    "generate_video_clip":  generate_video_clip,
    "generate_all_clips":   generate_all_clips,
    "generate_narration":   generate_narration,
    "generate_music":       generate_music,
    "assemble_edit":        assemble_edit,
    "qa_review_video":      qa_review_video,
    "render_final":         render_final,
    "video_project_status": video_project_status,
    "generate_thumbnail":   generate_thumbnail,
    "auto_produce_video":   auto_produce_video,
}
