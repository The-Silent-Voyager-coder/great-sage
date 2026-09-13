"""Built-in default configuration.

Values mirror config/sage.example.yaml from Phase 0.
The Windows C:\\GREATSAGE root is a documented default (docs/CONFIGURATION.md),
never a hard-coded path in code.
"""

from __future__ import annotations

DEFAULTS: dict[str, object] = {
    "core": {
        "name": "Great Sage",
        "data_dir": "C:/GREATSAGE/data",
        "cache_dir": "C:/GREATSAGE/cache",
        "logs_dir": "C:/GREATSAGE/logs",
        "runtime_dir": "C:/GREATSAGE/runtime",
        "workspaces_dir": "C:/GREATSAGE/workspaces",
        "models_dir": "C:/GREATSAGE/models",
        "backups_dir": "C:/GREATSAGE/backups",
        "timezone": "Asia/Kolkata",
    },
    "logging": {
        "level": "INFO",
        "format": "json",
        "retention_days": 30,
    },
    "events": {
        "queue_maxsize": 1000,
        "worker_count": 4,
    },
    "ai": {
        "default_provider": "local",
        "providers": {
            "local": {
                "type": "local",
                "enabled": True,
                "base_url": "http://127.0.0.1:11434",
                "model": "",
                "timeout_seconds": 120.0,
            },
            "opencode": {
                "type": "opencode",
                "enabled": False,
                "base_url": "http://127.0.0.1:4096",
                "api_key_env": "",
                "model": "",
                "timeout_seconds": 300.0,
            },
        },
    },
    "memory": {
        "enabled": True,
        "database_path": "C:/GREATSAGE/data/sage-memory.db",
        "auto_save_conversations": False,
        "default_confidence": 0.8,
        "retention_days": 365,
        "embeddings_enabled": False,
        "embedding_model": "nomic-embed-text",
        "embedding_base_url": "http://127.0.0.1:11434",
    },
    "tasks": {
        "max_iterations": 10,
        "default_timeout_minutes": 30,
        "persist_interval_seconds": 5,
    },
    "tools": {
        "working_directory": "C:/GREATSAGE/workspaces",
        "execution_timeout_seconds": 30.0,
        "max_output_bytes": 65536,
        "allowed_roots": ["C:/GREATSAGE/workspaces"],
        "denied_roots": [],
        "terminal": {"default_risk": "LOW_WRITE"},
        "browser": {"default_risk": "READ"},
    },
    "agent": {
        "enabled": True,
        "max_steps": 12,
        "max_tool_calls": 8,
        "max_wall_time_seconds": 300.0,
        "max_single_tool_calls": 3,
        "max_total_tool_output_bytes": 2097152,
        "loop_detection_threshold": 3,
    },
    "delegation": {
        "enabled": False,
        "default_provider": "opencode",
        "max_wall_time_seconds": 1800.0,
        "max_output_bytes": 4194304,
        "max_permission_requests": 50,
        "max_session_count": 3,
        "max_delegation_depth": 1,
    },
    "workspace": {
        "enabled": True,
        "max_scan_depth": 3,
        "max_entries": 500,
        "scan_timeout_seconds": 10.0,
        "database_path": "C:/GREATSAGE/data/sage-workspace.db",
    },
    "planning": {
        "enabled": True,
        "max_plan_steps": 25,
        "database_path": "C:/GREATSAGE/data/sage-plans.db",
    },
    "task": {
        "enabled": True,
        "max_steps": 25,
        "per_step_timeout_seconds": 30.0,
        "total_timeout_seconds": 600.0,
        "database_path": "C:/GREATSAGE/data/sage-tasks.db",
    },
    "scheduler": {
        "enabled": True,
        "max_schedules": 50,
        "database_path": "C:/GREATSAGE/data/sage-scheduler.db",
    },
    "telegram": {
        "enabled": False,
        "token_env": "TELEGRAM_BOT_TOKEN",
        "allowed_chat_ids": [],
        "poll_timeout_seconds": 20.0,
        "max_listen_seconds": 600.0,
    },
    "security": {
        "mode": "normal",
        "default_mode": "ask",
        "allow_auto_approve_read": True,
        "destructive_confirm": True,
        "audit_log": "C:/GREATSAGE/data/sage-audit.log",
    },
    "voice": {
        # Deterministic mocks by default (offline, no downloads). Real local
        # engines are opt-in: stt vosk | faster-whisper, tts piper,
        # wake_word fuzzy. Missing packages/models report unavailable.
        "wake_word": {"enabled": False, "model": "keyword"},
        "stt": {"engine": "mock", "model": "small", "language": "en"},
        "tts": {"engine": "mock", "voice": "en_GB-alan-medium"},
    },
}
