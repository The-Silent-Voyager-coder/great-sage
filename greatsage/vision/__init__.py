"""Bounded local-first vision (Phase 8): capture, grounding stubs, service."""

from greatsage.vision.capture import CaptureManager, StubCaptureBackend
from greatsage.vision.file_repository import FileVisionRepository
from greatsage.vision.grounding import GroundingStub
from greatsage.vision.limits import VisionLimits, default_limits, resolve_limits
from greatsage.vision.models import (
    VisionBackend,
    VisionCapture,
    VisionDescription,
    VisionRegion,
)
from greatsage.vision.repository import VisionRepository
from greatsage.vision.service import VisionService
from greatsage.vision.tools import (
    VisionCaptureTool,
    VisionCompareTool,
    VisionDescribeTool,
    register_vision_tools,
)

__all__ = [
    "CaptureManager",
    "FileVisionRepository",
    "GroundingStub",
    "StubCaptureBackend",
    "VisionBackend",
    "VisionCapture",
    "VisionCaptureTool",
    "VisionCompareTool",
    "VisionDescribeTool",
    "VisionDescription",
    "VisionLimits",
    "VisionRegion",
    "VisionRepository",
    "VisionService",
    "default_limits",
    "register_vision_tools",
    "resolve_limits",
]
