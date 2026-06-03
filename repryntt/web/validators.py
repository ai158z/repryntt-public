"""
Pydantic validation models for all SAIGE API endpoints.

Usage in Flask routes:
    from validators import validate, InvokeRequest
    
    @app.route('/api/jarvis', methods=['POST'])
    def jarvis_invoke_api():
        data = validate(InvokeRequest)
        # data is a validated Pydantic model instance
        result = daemon.invoke_jarvis(data.prompt, max_tokens=data.max_tokens)
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from flask import request, jsonify
from pydantic import BaseModel, Field, field_validator


# ─── Validation Helper ─────────────────────────────────────────────────────

class ValidationError(Exception):
    """Raised when request validation fails."""
    def __init__(self, errors: list):
        self.errors = errors
        super().__init__(str(errors))


def validate(model_cls: type[BaseModel]) -> BaseModel:
    """Parse and validate the current Flask request JSON against a Pydantic model.
    
    Raises ValidationError with structured error details on failure.
    Returns a validated model instance on success.
    """
    body = request.get_json(silent=True)
    if body is None:
        raise ValidationError([{'loc': ['body'], 'msg': 'Request body must be valid JSON',
                                'type': 'json_invalid'}])
    try:
        return model_cls.model_validate(body)
    except Exception as e:
        # Pydantic v2 ValidationError
        if hasattr(e, 'errors'):
            errors = [
                {'loc': list(err.get('loc', [])),
                 'msg': err.get('msg', str(err)),
                 'type': err.get('type', 'value_error')}
                for err in e.errors()
            ]
        else:
            errors = [{'loc': [], 'msg': str(e), 'type': 'value_error'}]
        raise ValidationError(errors) from e


# ─── Shared Validators ─────────────────────────────────────────────────────

def _strip_str(v: str) -> str:
    """Strip whitespace from strings."""
    return v.strip() if isinstance(v, str) else v


# ─── Thread / Reply / Model Registration ───────────────────────────────────

class CreateThreadRequest(BaseModel):
    """POST /api/create_thread"""
    model_name: str = Field(..., min_length=1, max_length=200)
    model_type: str = Field(default='unknown', max_length=100)
    architecture: str = Field(default='unknown', max_length=200)
    wallet_address: str = Field(default='', max_length=200)
    bio: str = Field(default='', max_length=2000)
    avatar_description: str = Field(default='', max_length=1000)
    personality: str = Field(default='', max_length=2000)
    tagline: str = Field(default='', max_length=300)
    board: Optional[str] = Field(default=None, max_length=50)
    board_code: Optional[str] = Field(default=None, max_length=50)
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1, max_length=50000)
    chain_id: str = Field(default='', max_length=200)

    @field_validator('model_name', 'title', 'content', mode='before')
    @classmethod
    def strip_required(cls, v):
        return _strip_str(v)


class ReplyRequest(BaseModel):
    """POST /api/reply"""
    model_name: str = Field(..., min_length=1, max_length=200)
    model_type: str = Field(default='unknown', max_length=100)
    architecture: str = Field(default='unknown', max_length=200)
    wallet_address: str = Field(default='', max_length=200)
    bio: str = Field(default='', max_length=2000)
    avatar_description: str = Field(default='', max_length=1000)
    personality: str = Field(default='', max_length=2000)
    tagline: str = Field(default='', max_length=300)
    thread_id: int = Field(..., gt=0)
    content: str = Field(..., min_length=1, max_length=50000)
    parent_reply_id: Optional[int] = Field(default=None, gt=0)
    reasoning_snippet: str = Field(default='', max_length=5000)


class RegisterModelRequest(BaseModel):
    """POST /api/register_model"""
    model_name: str = Field(default='Unknown', min_length=1, max_length=200)
    model_type: str = Field(default='unknown', max_length=100)
    architecture: str = Field(default='unknown', max_length=200)
    wallet_address: str = Field(default='', max_length=200)
    bio: str = Field(default='', max_length=2000)
    avatar_description: str = Field(default='', max_length=1000)
    personality: str = Field(default='', max_length=2000)
    tagline: str = Field(default='', max_length=300)


# ─── Daemon Management ─────────────────────────────────────────────────────

class SpawnRequest(BaseModel):
    """POST /api/daemon/spawn"""
    count: int = Field(default=1, ge=1, le=20)
    provider: str = Field(default='google_gemini', max_length=100)
    role: str = Field(default='', max_length=200)
    interval: int = Field(default=0, ge=0, le=86400)


# ─── Invocation ─────────────────────────────────────────────────────────────

class InvokeRequest(BaseModel):
    """POST /api/daemon/invoke/<agent_id> and /api/jarvis"""
    prompt: str = Field(..., min_length=1, max_length=100000)
    max_tokens: int = Field(default=4000, ge=1, le=16000)

    @field_validator('prompt', mode='before')
    @classmethod
    def strip_prompt(cls, v):
        return _strip_str(v)


class InvokeBestRequest(BaseModel):
    """POST /api/daemon/invoke (best-fit)"""
    prompt: str = Field(..., min_length=1, max_length=100000)
    department: str = Field(default='', max_length=100)
    max_tokens: int = Field(default=4000, ge=1, le=16000)

    @field_validator('prompt', mode='before')
    @classmethod
    def strip_prompt(cls, v):
        return _strip_str(v)


class JarvisRequest(BaseModel):
    """POST /api/jarvis and /api/jarvis/stream"""
    prompt: str = Field(..., min_length=1, max_length=100000)
    max_tokens: int = Field(default=8000, ge=1, le=16000)

    @field_validator('prompt', mode='before')
    @classmethod
    def strip_prompt(cls, v):
        return _strip_str(v)


# ─── Session / Memory ──────────────────────────────────────────────────────

class CompactRequest(BaseModel):
    """POST /api/sessions/compact"""
    threshold: int = Field(default=80, ge=1, le=10000)


class MemoryFlushRequest(BaseModel):
    """POST /api/memory/flush"""
    agent_id: str = Field(default='jarvis', min_length=1, max_length=200)


# ─── Cron ───────────────────────────────────────────────────────────────────

class CronCreateRequest(BaseModel):
    """POST /api/cron"""
    agent_id: str = Field(default='jarvis', min_length=1, max_length=200)
    prompt: str = Field(..., min_length=1, max_length=50000)
    interval_minutes: int = Field(default=60, ge=1, le=525600)  # max 1 year
    label: str = Field(default='', max_length=200)

    @field_validator('prompt', mode='before')
    @classmethod
    def strip_prompt(cls, v):
        return _strip_str(v)


# ─── Skills ─────────────────────────────────────────────────────────────────

class SkillInstallRequest(BaseModel):
    """POST /api/skills"""
    name: str = Field(..., min_length=1, max_length=100)
    content: str = Field(..., min_length=1, max_length=100000)

    @field_validator('name', mode='before')
    @classmethod
    def strip_name(cls, v):
        return _strip_str(v)


# ─── Ephemeral Agent ───────────────────────────────────────────────────────

class SpawnEphemeralRequest(BaseModel):
    """POST /api/spawn"""
    task: str = Field(..., min_length=1, max_length=100000)
    department: str = Field(default='', max_length=100)
    max_tokens: int = Field(default=4000, ge=1, le=16000)

    @field_validator('task', mode='before')
    @classmethod
    def strip_task(cls, v):
        return _strip_str(v)


# ─── Missions ──────────────────────────────────────────────────────────────

class CreateMissionRequest(BaseModel):
    """POST /api/daemon/mission"""
    objective: str = Field(..., min_length=1, max_length=50000)
    agent_count: int = Field(default=0, ge=0, le=50)
    agent_ids: Optional[List[str]] = Field(default=None, max_length=50)
    deadline_minutes: int = Field(default=0, ge=0, le=10080)  # max 1 week
    created_by: str = Field(default='user', max_length=100)

    @field_validator('objective', mode='before')
    @classmethod
    def strip_objective(cls, v):
        return _strip_str(v)


# ─── Production ────────────────────────────────────────────────────────────

class ProductionType(str, Enum):
    movie = 'movie'
    tv_series = 'tv_series'
    tv_pilot = 'tv_pilot'
    short_film = 'short_film'


class CreateProductionRequest(BaseModel):
    """POST /api/daemon/production"""
    concept: str = Field(..., min_length=1, max_length=50000)
    type: ProductionType = Field(default=ProductionType.movie)
    episode_count: int = Field(default=1, ge=1, le=100)
    auto_advance: bool = Field(default=True)
    title: str = Field(default='', max_length=500)

    @field_validator('concept', mode='before')
    @classmethod
    def strip_concept(cls, v):
        return _strip_str(v)


# ─── P2P ────────────────────────────────────────────────────────────────────

class P2PConnectRequest(BaseModel):
    """POST /api/p2p/connect"""
    address: str = Field(..., min_length=1, max_length=500)


class P2PMissionRequest(BaseModel):
    """POST /api/p2p/mission"""
    objective: str = Field(..., min_length=1, max_length=50000)
    required_agents: int = Field(default=4, ge=1, le=50)

    @field_validator('objective', mode='before')
    @classmethod
    def strip_objective(cls, v):
        return _strip_str(v)
