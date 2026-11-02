from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


# Segmentation visualization configuration
class FieldColorConfig(BaseModel):
    """Color configuration for a specific annotation field"""

    colorByAttribute: str = "label"  # "label" for polygons, "id" for masks
    valueColors: Optional[Dict[str, str]] = None  # value/id -> hex color
    opacity: float = 0.5


class SegmentationConfig(BaseModel):
    """Dataset-level segmentation visualization settings"""

    default_mask_targets: Optional[Dict[str, str]] = None  # id -> class name (default for all fields)
    mask_targets: Optional[Dict[str, Dict[str, str]]] = None  # field -> {id -> class name}
    color_scheme: Optional[Dict[str, Any]] = None  # color_pool, opacity, fields config


class Dataset(BaseModel):
    id: int
    deleting: bool = False
    deletion_job_id: Optional[UUID] = None
    name: str
    description: Optional[str] = None
    media_fields: List[str] = ["filepath"]
    grid_media_field: str = "filepath"
    modal_media_field: str = "filepath"
    segmentation_config: Optional[Dict[str, Any]] = None
    storage_config_id: Optional[int] = None  # NULL → fall back to default storage config / env vars
    owner_id: Optional[int] = None  # User who created the dataset (always has access)
    is_global: bool = False  # If True, every signed-in user can access (no per-user grants needed)
    created_at: datetime
    # GET /datasets/{id} only (None elsewhere): the filter sidebar opens with every field expanded and
    # computed, because the dataset's rows are few enough to filter every field at once.
    sidebar_expand_all: Optional[bool] = None


class Sample(BaseModel):
    id: int
    filepath: str
    media_type: str
    tags: List[str] = []
    metadata: Dict[str, Any]
    location: Optional[Dict[str, Any]] = None
    embedding: Optional[List[float]] = None
    created_at: Optional[datetime] = None
    last_modified_at: Optional[datetime] = None
    distance: Optional[float] = None


class FieldDefinition(BaseModel):
    name: str
    type: str  # bool, int, float, string, date, datetime, list, dict
    min: Optional[float] = None
    max: Optional[float] = None
    values: Optional[List[str]] = None


class SearchRequest(BaseModel):
    dataset_id: int
    vector: Optional[List[float]] = None
    sample_id: Optional[int] = None  # Single sample: use its embedding
    sample_ids: Optional[List[int]] = None  # Multiple samples: average their embeddings
    text: Optional[str] = None  # Natural-language prompt, CLIP-text-encoded server-side
    field_name: str = "clip"
    limit: int = Field(50, ge=1)  # a negative LIMIT reached Postgres and 500'd
    filters: Optional[Dict[str, Any]] = None


class ImageSearchRequest(BaseModel):
    dataset_id: int
    # Exactly one image source: a base64 upload (optionally a data: URL) or a remote URL.
    image_base64: Optional[str] = None
    image_url: Optional[str] = None
    field_name: str = "clip"
    limit: int = Field(50, ge=1)  # a negative LIMIT reached Postgres and 500'd
    filters: Optional[Dict[str, Any]] = None


class PatchSearchRequest(BaseModel):
    dataset_id: int
    sample_id: int
    field_name: str = "clip"
    patch_scale: str  # e.g. "2x2", "4x4"
    patch_row: int = 0
    patch_col: int = 0
    limit: int = Field(50, ge=1)  # a negative LIMIT reached Postgres and 500'd
    filters: Optional[Dict[str, Any]] = None

    @model_validator(mode='after')
    def validate_patch_params(self):
        import re

        if not re.match(r'^\d+x\d+$', self.patch_scale):
            raise ValueError(f"patch_scale must be NxN format, got: {self.patch_scale}")
        rows, cols = (int(x) for x in self.patch_scale.split('x'))
        if self.patch_row < 0 or self.patch_row >= rows:
            raise ValueError(f"patch_row must be 0..{rows - 1} for scale {self.patch_scale}")
        if self.patch_col < 0 or self.patch_col >= cols:
            raise ValueError(f"patch_col must be 0..{cols - 1} for scale {self.patch_scale}")
        self.limit = min(self.limit, 1000)
        return self


class ObjectToPatchSearchRequest(BaseModel):
    """Cross-grain search: from a labeled object crop, find similar fixed-grid PATCH tiles
    that are NOT already covered by a labeled box — i.e. surface unlabeled look-alikes.

    Object crops (clip_obj) and patch tiles (clip_patch_NxN_*) are the same CLIP-512 space,
    so the object's vector probes the patch tiles directly. Region-level exclusion drops any
    candidate tile that overlaps an existing object box on the SAME parent image (IoU >
    iou_threshold), keeping only the uncovered (unlabeled) regions.
    """

    dataset_id: int
    object_sample_id: int  # the seed: a media_type='object' row
    base_field: str = "clip"  # searches '{base_field}_patch_%' across all computed scales
    patch_scale: Optional[str] = None  # e.g. "4x4" to restrict to one scale; None = all scales
    limit: int = Field(50, ge=1)  # a negative LIMIT reached Postgres and 500'd
    iou_threshold: float = 0.3
    filters: Optional[Dict[str, Any]] = None


class User(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    created_at: datetime


class UserCreate(BaseModel):
    username: str
    email: Optional[str] = None


class View(BaseModel):
    id: int
    dataset_id: int
    user_id: Optional[int] = None
    name: str
    description: Optional[str] = None
    slug: str
    content: Dict[str, Any]
    is_public: bool = False
    shared_with_user_ids: List[int] = []
    created_at: datetime


class ViewCreate(BaseModel):
    name: str
    description: Optional[str] = None
    content: Dict[str, Any]
    is_public: bool = False
    shared_with_user_ids: List[int] = []
    username: Optional[str] = None  # For now, pass username to link to user


# Recursive Filter Model


class Filter(BaseModel):
    # Leaf node
    field: Optional[str] = None
    operator: Optional[str] = None  # =, !=, >, <, >=, <=, in, not in, contains
    value: Optional[Any] = None

    # Branch node
    logic: Optional[str] = None  # AND, OR, NOT
    filters: Optional[List['Filter']] = None


Filter.update_forward_refs()
