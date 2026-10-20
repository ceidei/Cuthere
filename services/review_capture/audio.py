"""Microphone capture with a durable raw stream and a 48 kHz mono review WAV."""

from __future__ import annotations

import math
import os
import queue
import shutil
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Any

from .storage import atomic_write_json, sha256_file, utc_now

TARGET_RATE = 48_000
CHANNELS = 1
SAMPLE_WIDTH = 2
_STOP = object()


def _audioop() -> Any:
    """Load the PCM resampler lazily.

    ``audioop`` was removed from the standard library in Python 3.13; the
    ``audioop-lts`` backport provides the same ``ratecv`` there. Importing it at
    module level made every import of the capture package fail on 3.13, so the
    import happens only when a non-48 kHz device actually needs resampling.
    """
    try:
        import audioop  # type: ignore[import-not-found]

        return audioop
    except ImportError:
        try:
            import audioop_lts as audioop  # type: ignore[import-not-found]

            return audioop
        except ImportError as exc:
            raise RuntimeError(
                "This Python has no 'audioop' module (removed in 3.13); install 'audioop-lts' "
                "or record with a 48 kHz device"
            ) from exc


def enumerate_input_devices(sd_module: Any | None = None) -> list[dict[str, Any]]:
    if sd_module is None:
        try:
            import sounddevice as sd_module  # type: ignore[no-redef]
        except ImportError as exc:
            raise RuntimeError("录音组件 sounddevice 尚未安装") from exc
    devices = sd_module.query_devices()
    hostapis = sd_module.query_hostapis()
    default_input = sd_module.default.device[0]
    result: list[dict[str, Any]] = []
    for index, device in enumerate(devices):
        inputs = int(device.get("max_input_channels", 0))
        if inputs <= 0:
            continue
        hostapi_index = int(device.get("hostapi", -1))
        hostapi = hostapis[hostapi_index].get("name", "unknown") if 0 <= hostapi_index < len(hostapis) else "unknown"
        stable_key = f"{hostapi}|{device.get('name', '')}|{inputs}"
        result.append({
            "index": index,
            "key": stable_key,
            "name": str(device.get("name", "")),
            "hostapi": str(hostapi),
            "max_input_channels": inputs,
            "default": index == default_input,
            "default_samplerate": int(round(float(device.get("default_samplerate", TARGET_RATE)))),
        })
    return result


class MicrophoneRecorder:
    def __init__(self, output_dir: Path, device: dict[str, Any], sd_module: Any | None = None):
        self.output_dir = output_dir
        self.device = device
        self.sd = sd_module
        self.input_rate = 0
        self.samples = 0
        self.dropped_frames = 0
        self.input_overflows = 0
        self.peak = 0
        self.last_level = 0.0
        self.sum_squares = 0
        self._queue: queue.Queue[bytes | object] = queue.Queue(maxsize=256)
        self._first_chunk = threading.Event()
        self._writer_error: str | None = None
        self._stream: Any | None = None
        self._writer: threading.Thread | None = None
        self._started = False
        self._stopped = False
        self.started_monotonic = 0.0
        self.started_at = ""
        self._final_status = "preparing"
        self.raw_path = output_dir / "audio.capture.pcm"
        self.wav_partial_path = output_dir / "audio.wav.partial"
        self.wav_path = output_dir / "audio.wav"
        self.metadata_path = output_dir / "capture.json"

    def start(self, timeout_seconds: float = 1.5) -> dict[str, Any]:
        if self._started:
            raise RuntimeError("recording already started")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.raw_path.exists() or self.wav_path.exists() or self.wav_partial_path.exists():
            raise FileExistsError("capture destination already exists")
        if shutil.disk_usage(self.output_dir).free < 100 * 1024 * 1024:
            raise OSError("磁盘剩余空间少于 100 MB，无法开始录音")
        if self.sd is None:
            try:
                import sounddevice as sd
            except ImportError as exc:
                raise RuntimeError("录音组件 sounddevice 尚未安装") from exc
            self.sd = sd
        self.input_rate = self._choose_supported_rate()
        self.started_at = utc_now()
        self.started_monotonic = time.monotonic()
        self._writer = threading.Thread(target=self._write_loop, name="review-audio-writer", daemon=True)
        self._writer.start()
        try:
            self._stream = self.sd.RawInputStream(
                device=int(self.device["index"]),
                samplerate=self.input_rate,
                channels=CHANNELS,
                dtype="int16",
                blocksize=max(256, self.input_rate // 10),
                latency="low",
                callback=self._audio_callback,
            )
            self._stream.start()
            if not self._first_chunk.wait(timeout_seconds):
                raise TimeoutError("麦克风没有在 1.5 秒内提供输入数据")
        except Exception:
            try:
                self._close_stream()
            except Exception:
                pass
            if self._writer and self._writer.is_alive():
                try:
                    self._queue.put(_STOP, timeout=1)
                except queue.Full:
                    pass
            if self._writer:
                self._writer.join(timeout=2)
            try:
                self.raw_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._started = True
        self._final_status = "recording"
        atomic_write_json(self.metadata_path, self._metadata("recording"))
        return self.summary()

    def _choose_supported_rate(self) -> int:
        sd = self.sd
        device_index = int(self.device["index"])
        current = enumerate_input_devices(sd)
        match = next((d for d in current if d["index"] == device_index), None)
        if not match or match["key"] != self.device.get("key"):
            raise RuntimeError("所选麦克风列表已变化，请重新选择输入设备")
        for rate in (48_000, 44_100, 32_000):
            try:
                sd.check_input_settings(device=device_index, channels=CHANNELS, dtype="int16", samplerate=rate)
                return rate
            except Exception:
                continue
        raise RuntimeError("麦克风不支持 48 kHz、44.1 kHz 或 32 kHz 单声道输入")

    def _audio_callback(self, indata: Any, frames: int, _time_info: Any, status: Any) -> None:
        if getattr(status, "input_overflow", False):
            self.input_overflows += 1
        try:
            self._queue.put_nowait(bytes(indata))
            self._first_chunk.set()
        except queue.Full:
            self.dropped_frames += int(frames)

    def _write_loop(self) -> None:
        try:
            with self.raw_path.open("xb", buffering=0) as raw:
                last_fsync = time.monotonic()
                while True:
                    block = self._queue.get()
                    if block is _STOP:
                        break
                    assert isinstance(block, bytes)
                    raw.write(block)
                    sample_count = len(block) // SAMPLE_WIDTH
                    self.samples += sample_count
                    if sample_count:
                        values = struct.unpack("<" + "h" * sample_count, block)
                        block_peak = max(abs(value) for value in values)
                        self.peak = max(self.peak, block_peak)
                        self.last_level = block_peak / 32768.0
                        self.sum_squares += sum(value * value for value in values)
                    if self.samples % (self.input_rate * 2) < sample_count:
                        atomic_write_json(self.metadata_path, self._metadata("recording"))
                    if time.monotonic() - last_fsync >= 1.0:
                        os.fsync(raw.fileno())
                        last_fsync = time.monotonic()
                raw.flush()
                os.fsync(raw.fileno())
        except Exception as exc:
            self._writer_error = f"{type(exc).__name__}: {exc}"

    def stop(self, status: str = "saved") -> dict[str, Any]:
        if self._stopped:
            return self.summary()
        self._stopped = True
        try:
            self._close_stream()
        except Exception as exc:
            self._writer_error = self._writer_error or f"stream close: {type(exc).__name__}: {exc}"
        if self._writer and self._writer.is_alive():
            try:
                self._queue.put(_STOP, timeout=2)
            except queue.Full:
                self._writer_error = self._writer_error or "audio queue did not drain"
            self._writer.join(timeout=15)
        if self._writer and self._writer.is_alive():
            status = "interrupted"
            self._writer_error = self._writer_error or "audio writer did not stop within 15 seconds"
        if self._writer_error:
            status = "interrupted"
        if self.raw_path.exists() and self.samples:
            _finalize_raw_pcm(self.raw_path, self.wav_partial_path, self.wav_path, self.input_rate)
        if not self.wav_path.exists() or self.samples <= 0:
            status = "failed"
            raise RuntimeError(self._writer_error or "录音没有产生可用音频")
        # Read back using a separate standard-library decoder before reporting saved.
        with wave.open(str(self.wav_path), "rb") as audio:
            info = {
                "sample_rate": audio.getframerate(),
                "channels": audio.getnchannels(),
                "samples": audio.getnframes(),
                "sample_width": audio.getsampwidth(),
            }
            if info["channels"] != 1 or info["sample_rate"] != TARGET_RATE or info["samples"] <= 0:
                status = "failed"
                raise RuntimeError("标准 WAV 回读校验失败")
        self._final_status = status
        atomic_write_json(self.metadata_path, self._metadata(status))
        return self.summary()

    def _write_normalized_wav(self) -> None:
        _finalize_raw_pcm(self.raw_path, self.wav_partial_path, self.wav_path, self.input_rate)

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            finally:
                try:
                    self._stream.close()
                finally:
                    self._stream = None

    def summary(self) -> dict[str, Any]:
        wav_path = self.wav_path if self.wav_path.exists() else None
        rms = math.sqrt(self.sum_squares / self.samples) if self.samples else 0.0
        result: dict[str, Any] = {
            "sample_rate_input": self.input_rate,
            "sample_rate": TARGET_RATE,
            "channels": CHANNELS,
            "sample_width_bytes": SAMPLE_WIDTH,
            "samples_input": self.samples,
            "duration_seconds": self.samples / self.input_rate if self.input_rate else 0.0,
            "dropped_frames": self.dropped_frames,
            "input_overflows": self.input_overflows,
            "peak": self.peak,
            "rms": round(rms, 3),
            "device": self.device,
            "started_at": self.started_at,
            "stopped_at": utc_now() if self._stopped else None,
            "writer_error": self._writer_error,
            "capture_status": self._final_status,
        }
        if wav_path:
            result["path"] = "audio.wav"
            result["sha256"] = sha256_file(wav_path)
            result["size_bytes"] = wav_path.stat().st_size
            with wave.open(str(wav_path), "rb") as f:
                result["samples"] = f.getnframes()
        if self.raw_path.exists():
            result["raw_path"] = "audio.capture.pcm"
            result["raw_sha256"] = sha256_file(self.raw_path)
            result["raw_size_bytes"] = self.raw_path.stat().st_size
        return result

    def _metadata(self, status: str) -> dict[str, Any]:
        return {"schema_version": "1.0", "status": status, "input_rate": self.input_rate, "channels": CHANNELS, "sample_width_bytes": SAMPLE_WIDTH, "samples": self.samples, "dropped_frames": self.dropped_frames, "input_overflows": self.input_overflows, "updated_at": utc_now()}


def _finalize_raw_pcm(raw_path: Path, partial_path: Path, wav_path: Path, input_rate: int) -> dict[str, Any]:
    """Convert durable signed-16-bit mono PCM into a validated 48 kHz WAV."""
    if input_rate <= 0:
        raise ValueError("capture sample rate is missing")
    byte_count = raw_path.stat().st_size
    if byte_count <= 0 or byte_count % SAMPLE_WIDTH:
        raise ValueError("raw PCM is empty or ends with a partial sample")
    with raw_path.open("rb") as raw, wave.open(str(partial_path), "wb") as output:
        output.setnchannels(CHANNELS)
        output.setsampwidth(SAMPLE_WIDTH)
        output.setframerate(TARGET_RATE)
        state = None
        while True:
            block = raw.read(64 * 1024)
            if not block:
                break
            if input_rate == TARGET_RATE:
                converted = block
            else:
                converted, state = _audioop().ratecv(block, SAMPLE_WIDTH, CHANNELS, input_rate, TARGET_RATE, state)
            output.writeframesraw(converted)
    with partial_path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    with wave.open(str(partial_path), "rb") as check:
        if check.getnchannels() != CHANNELS or check.getframerate() != TARGET_RATE or check.getsampwidth() != SAMPLE_WIDTH or check.getnframes() <= 0:
            raise RuntimeError("normalized WAV header or sample count is invalid")
        normalized_samples = check.getnframes()
    os.replace(partial_path, wav_path)
    return {"samples_input": byte_count // SAMPLE_WIDTH, "samples": normalized_samples, "sample_rate_input": input_rate, "sample_rate": TARGET_RATE, "channels": CHANNELS, "path": "audio.wav", "sha256": sha256_file(wav_path), "size_bytes": wav_path.stat().st_size}


def recover_interrupted_capture(note_dir: Path, input_rate: int) -> dict[str, Any]:
    """Salvage audio.capture.pcm after a worker crash without inventing a time range."""
    raw_path = note_dir / "audio.capture.pcm"
    wav_path = note_dir / "audio.wav"
    partial_path = note_dir / "audio.wav.partial"
    result = _finalize_raw_pcm(raw_path, partial_path, wav_path, input_rate)
    result["raw_path"] = raw_path.name
    result["raw_sha256"] = sha256_file(raw_path)
    result["raw_size_bytes"] = raw_path.stat().st_size
    return result

