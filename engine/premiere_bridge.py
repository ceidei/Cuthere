"""
Premiere CEP Bridge — Python-side communication layer.

Phase 3b: File-based communication between Cuthere (WSL/Linux)
and Adobe Premiere Pro (Windows) via a shared filesystem bridge directory.

Protocol:
  - WSL writes command files to Windows Temp directory
  - CEP Panel polls and reads command files
  - CEP Panel writes response files
  - WSL polls and reads response files

Directory structure:
  {AVE_BRIDGE_DIR}/
    commands/
      {uuid}.json     # Command from AutoVE to Premiere
    responses/
      {uuid}.json     # Response from Premiere to AutoVE
    locks/
      {uuid}.lock     # Atomicity guard

Environment variables:
  AVE_BRIDGE_DIR: Path to bridge directory (must be Windows-accessible)
"""

from __future__ import annotations

import json
import os
import time
import uuid
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

import sys

# Auto-detect default bridge directory by platform
if sys.platform == "win32":
    DEFAULT_BRIDGE_DIR = os.environ.get("AVE_BRIDGE_DIR", r"C:\Users\Public\Documents\ave-bridge")
elif sys.platform == "darwin":
    DEFAULT_BRIDGE_DIR = os.environ.get(
        "AVE_BRIDGE_DIR",
        os.path.expanduser("~/ave-bridge"),
    )
else:
    DEFAULT_BRIDGE_DIR = os.environ.get(
        "AVE_BRIDGE_DIR",
        "/mnt/c/Users/Public/Documents/ave-bridge",
    )

COMMAND_TIMEOUT_SECONDS = 30  # Max wait for a response
RESPONSE_POLL_INTERVAL = 0.1  # Poll every 100ms


# ═══════════════════════════════════════════════════════════════════════════
# Data types
# ═══════════════════════════════════════════════════════════════════════════


class PremiereCommand(str, Enum):
    """Commands sent from AutoVE to Premiere."""

    GET_PROJECT_INFO = "get_project_info"
    GET_SEQUENCE = "get_sequence"
    ADD_CLIP = "add_clip"
    REMOVE_CLIP = "remove_clip"
    TRIM_CLIP = "trim_clip"
    REPLACE_CLIP = "replace_clip"
    ADD_MARKER = "add_marker"
    EXPORT_XML = "export_xml"
    PING = "ping"  # Health check
    IMPORT_TIMELINE = "import_timeline"
    ACTIVATE_SEQUENCE = "activate_sequence"
    SAVE_PROJECT = "save_project"
    CREATE_CHECKPOINT = "create_checkpoint"
    ROLLBACK_PROJECT = "rollback_project"


@dataclass
class BridgeCommand:
    """A command to execute in Premiere."""

    command: PremiereCommand
    params: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: float = field(default_factory=time.time)


@dataclass
class BridgeResponse:
    """A response from Premiere."""

    request_id: str = ""
    success: bool = False
    data: Any = None
    error: str = ""
    timestamp: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════
# PremiereBridgeClient
# ═══════════════════════════════════════════════════════════════════════════


class PremiereBridgeClient:
    """Client for communicating with Premiere Pro CEP Panel.

    Uses file-based protocol: writes commands as JSON files,
    reads responses from corresponding response files.

    Usage:
        bridge = PremiereBridgeClient()
        bridge.ensure_dirs()

        # Send command
        resp = bridge.send_command(
            PremiereCommand.ADD_CLIP,
            {"clip_path": "E:/videos/clip.mp4", "position": 10.0}
        )

        # Check connection
        if bridge.ping():
            print("Premiere Bridge is online")
    """

    def __init__(self, bridge_dir: Optional[str] = None):
        self.bridge_dir = bridge_dir or DEFAULT_BRIDGE_DIR
        self._commands_dir = os.path.join(self.bridge_dir, "commands")
        self._responses_dir = os.path.join(self.bridge_dir, "responses")
        self._locks_dir = os.path.join(self.bridge_dir, "locks")

    def ensure_dirs(self) -> bool:
        """Create bridge directories if they don't exist."""
        try:
            for d in [self._commands_dir, self._responses_dir, self._locks_dir]:
                os.makedirs(d, exist_ok=True)
            return True
        except OSError as e:
            return False

    @property
    def is_ready(self) -> bool:
        """Check if bridge directories exist and are writable."""
        return all(
            os.path.isdir(d) and os.access(d, os.W_OK)
            for d in [self._commands_dir, self._responses_dir]
        )

    def ping(self) -> bool:
        """Check if Premiere Bridge CEP panel is responding."""
        resp = self.send_command(PremiereCommand.PING, {})
        return resp.success

    def send_command(
        self,
        command: PremiereCommand,
        params: dict[str, Any],
        timeout: float = COMMAND_TIMEOUT_SECONDS,
        *, request_id: str | None = None,
    ) -> BridgeResponse:
        """Send a command to Premiere and wait for response.

        Args:
            command: The command to execute.
            params: Command parameters (dict, will be JSON-serialized).
            timeout: Maximum seconds to wait for response.

        Returns:
            BridgeResponse with success status and data/error.
        """
        request = BridgeCommand(command=command, params=params)
        if request_id is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id):
                raise ValueError("Invalid request_id")
            request.request_id = request_id
        self.ensure_dirs()

        # Write command file atomically
        cmd_path = os.path.join(self._commands_dir, f"{request.request_id}.json")
        tmp_path = os.path.join(self._commands_dir, f"{request.request_id}.tmp")
        lock_path = os.path.join(self._locks_dir, f"{request.request_id}.lock")

        try:
            # Atomic write: .tmp → rename
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({
                    "request_id": request.request_id,
                    "command": command.value,
                    "params": params,
                    "timestamp": request.timestamp,
                    "expires_at": request.timestamp + timeout,
                }, f, ensure_ascii=False)

            os.replace(tmp_path, cmd_path)

        except OSError as e:
            return BridgeResponse(
                request_id=request.request_id,
                success=False,
                error=f"Failed to write command file: {e}",
            )

        # Wait for response
        resp_path = os.path.join(self._responses_dir, f"{request.request_id}.json")
        start = time.monotonic()

        while time.monotonic() - start < timeout:
            if os.path.exists(resp_path):
                try:
                    with open(resp_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("request_id") != request.request_id:
                        raise ValueError("Response request_id does not match")
                    # Clean up response file
                    try:
                        os.remove(resp_path)
                    except OSError:
                        pass
                    # Clean up command file
                    try:
                        os.remove(cmd_path)
                    except OSError:
                        pass

                    return BridgeResponse(
                        request_id=request.request_id,
                        success=data.get("success", False),
                        data=data.get("data"),
                        error=data.get("error", ""),
                        timestamp=data.get("timestamp", 0),
                    )
                except (json.JSONDecodeError, OSError):
                    # CEP may still be writing the response. Retry until deadline.
                    pass

            time.sleep(RESPONSE_POLL_INTERVAL)

        # Timeout — clean up command file
        try:
            os.remove(cmd_path)
        except OSError:
            pass

        return BridgeResponse(
            request_id=request.request_id,
            success=False,
            error=f"Command timed out after {timeout}s; host outcome unknown, inspect before retrying a mutation",
        )

    def send_command_async(self, command: PremiereCommand, params: dict[str, Any]) -> str:
        """Send a command without waiting for response.

        Returns the request_id for later polling.
        """
        request = BridgeCommand(command=command, params=params)
        cmd_path = os.path.join(self._commands_dir, f"{request.request_id}.json")
        self.ensure_dirs()
        tmp_path = os.path.join(self._commands_dir, f"{request.request_id}.tmp")

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({
                "request_id": request.request_id,
                "command": command.value,
                "params": params,
                "timestamp": request.timestamp,
                "expires_at": request.timestamp + COMMAND_TIMEOUT_SECONDS,
            }, f, ensure_ascii=False)

        os.rename(tmp_path, cmd_path)
        return request.request_id

    def poll_response(self, request_id: str) -> Optional[BridgeResponse]:
        """Check if a response is available for the given request_id."""
        resp_path = os.path.join(self._responses_dir, f"{request_id}.json")
        if not os.path.exists(resp_path):
            return None

        try:
            with open(resp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            try:
                os.remove(resp_path)
            except OSError:
                pass
            return BridgeResponse(
                request_id=request_id,
                success=data.get("success", False),
                data=data.get("data"),
                error=data.get("error", ""),
                timestamp=data.get("timestamp", 0),
            )
        except (json.JSONDecodeError, OSError):
            return None

    # ── Convenience Methods ─────────────────────────────────────────────

    def get_project_info(self) -> BridgeResponse:
        """Get current Premiere project information."""
        return self.send_command(PremiereCommand.GET_PROJECT_INFO, {})

    def get_sequence(self, sequence_name: str = "", *, sequence_id: str = "") -> BridgeResponse:
        """Get Premiere sequence/timeline data."""
        return self.send_command(
            PremiereCommand.GET_SEQUENCE,
            {"sequence_name": sequence_name, "sequence_id": sequence_id},
        )

    def add_clip(
        self,
        clip_path: str,
        position: float,
        track_index: int = 0,
    ) -> BridgeResponse:
        """Add a clip to the Premiere timeline."""
        return self.send_command(
            PremiereCommand.ADD_CLIP,
            {
                "clip_path": clip_path,
                "position": position,
                "track_index": track_index,
            },
        )

    def remove_clip(self, clip_id: str) -> BridgeResponse:
        """Remove a clip from Premiere timeline."""
        return self.send_command(
            PremiereCommand.REMOVE_CLIP,
            {"clip_id": clip_id},
        )

    def trim_clip(
        self,
        clip_id: str,
        start_delta: float = 0.0,
        end_delta: float = 0.0,
    ) -> BridgeResponse:
        """Trim a clip in Premiere (relative or absolute)."""
        return self.send_command(
            PremiereCommand.TRIM_CLIP,
            {
                "clip_id": clip_id,
                "start_delta": start_delta,
                "end_delta": end_delta,
            },
        )

    def replace_clip(
        self,
        old_clip_id: str,
        new_clip_path: str,
        keep_properties: bool = True,
    ) -> BridgeResponse:
        """Replace a clip while preserving properties."""
        return self.send_command(
            PremiereCommand.REPLACE_CLIP,
            {
                "old_clip_id": old_clip_id,
                "new_clip_path": new_clip_path,
                "keep_properties": keep_properties,
            },
        )

    def add_marker(
        self,
        position: float,
        label: str,
        color: str = "blue",
    ) -> BridgeResponse:
        """Add a marker at the given timeline position."""
        return self.send_command(
            PremiereCommand.ADD_MARKER,
            {"position": position, "label": label, "color": color},
        )

    def export_xml(self, output_path: str) -> BridgeResponse:
        """Export current Premiere sequence as FCP7 XML."""
        return self.send_command(
            PremiereCommand.EXPORT_XML,
            {"output_path": output_path},
        )

    def save_project(self, expected_project_path: str, output_path: str = "") -> BridgeResponse:
        return self.send_command(PremiereCommand.SAVE_PROJECT, {
            "expected_project_path": expected_project_path, "output_path": output_path,
        }, timeout=120)

    def create_checkpoint(self, expected_project_path: str, checkpoint_path: str) -> BridgeResponse:
        return self.send_command(PremiereCommand.CREATE_CHECKPOINT, {
            "expected_project_path": expected_project_path, "checkpoint_path": checkpoint_path,
        }, timeout=120)

    def import_timeline(self, expected_project_path: str, xml_path: str, sequence_name: str,
                        operation_dir: str, operation_id: str, xml_sha256: str) -> BridgeResponse:
        return self.send_command(PremiereCommand.IMPORT_TIMELINE, {
            "expected_project_path": expected_project_path, "xml_path": xml_path,
            "sequence_name": sequence_name, "operation_dir": operation_dir,
            "operation_id": operation_id, "xml_sha256": xml_sha256,
        }, timeout=180)

    def activate_sequence(self, expected_project_path: str, sequence_id: str,
                          position_ticks: str | None = None) -> BridgeResponse:
        params = {"expected_project_path": expected_project_path, "sequence_id": sequence_id}
        if position_ticks is not None:
            params["position_ticks"] = position_ticks
        return self.send_command(PremiereCommand.ACTIVATE_SEQUENCE, params)

    def rollback_project(self, expected_project_path: str, checkpoint_path: str,
                         recovery_path: str) -> BridgeResponse:
        return self.send_command(PremiereCommand.ROLLBACK_PROJECT, {
            "expected_project_path": expected_project_path, "checkpoint_path": checkpoint_path,
            "recovery_path": recovery_path,
        }, timeout=180)


# ═══════════════════════════════════════════════════════════════════════════
# WSL path helpers
# ═══════════════════════════════════════════════════════════════════════════


def wsl_to_windows_path(wsl_path: str) -> str:
    """Convert WSL path (/mnt/c/...) to Windows path (C:\\...)."""
    if wsl_path.startswith("/mnt/"):
        parts = wsl_path[5:].split("/", 1)
        drive = parts[0].upper()
        rest = parts[1] if len(parts) > 1 else ""
        rest = rest.replace("/", "\\")
        return f"{drive}:\\{rest}"
    return wsl_path


def windows_to_wsl_path(win_path: str) -> str:
    """Convert Windows path (C:\\...) to WSL path (/mnt/c/...)."""
    if len(win_path) >= 2 and win_path[1] == ":":
        drive = win_path[0].lower()
        rest = win_path[2:].replace("\\", "/")
        return f"/mnt/{drive}{rest}"
    return win_path


def get_bridge_dir() -> str:
    """Get the bridge directory, creating it if needed."""
    bridge_dir = os.environ.get("AVE_BRIDGE_DIR", DEFAULT_BRIDGE_DIR)
    os.makedirs(bridge_dir, exist_ok=True)
    os.makedirs(os.path.join(bridge_dir, "commands"), exist_ok=True)
    os.makedirs(os.path.join(bridge_dir, "responses"), exist_ok=True)
    return bridge_dir
