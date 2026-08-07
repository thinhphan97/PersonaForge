import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

from personaforge_worker.domain.media import NativeAudioChunk, SpeechSegment

logger = logging.getLogger(__name__)


class VieNeuTtsSessionRuntime:
    """Session-owned VieNeu model; generated PCM is streamed and never persisted."""

    def __init__(
        self,
        engine: Any,
        voice: dict[str, Any],
        *,
        frame_samples: int = 960,
        apply_watermark: bool = True,
    ) -> None:
        self._engine = engine
        self._voice = voice
        self._frame_samples = frame_samples
        self._apply_watermark = apply_watermark
        self._sample_rate = int(getattr(engine, "sample_rate", 48_000))
        if self._sample_rate != 48_000:
            raise ValueError(f"VieNeu v3 output sample rate is {self._sample_rate}, expected 48000")
        self._closed = False
        self._model_stream_completed_at: float | None = None

    @classmethod
    async def create(
        cls,
        *,
        speaker_embedding: list[float],
        reference_codes: list[list[int]],
        backend: str,
        model_path: str,
        onnx_precision: str,
        frame_samples: int,
        apply_watermark: bool,
    ) -> "VieNeuTtsSessionRuntime":
        def load() -> tuple[Any, dict[str, Any]]:
            import numpy as np
            from vieneu import Vieneu

            runtime_backend = _normalize_backend(backend)
            device = "cuda" if runtime_backend in {"pytorch", "onnx-gpu"} else "cpu"
            _require_gpu_runtime(runtime_backend)
            kwargs: dict[str, Any] = {
                "mode": "v3turbo",
                "backend": "onnx" if runtime_backend == "onnx-gpu" else runtime_backend,
                "device": device,
            }
            if model_path:
                kwargs["backbone_repo"] = model_path
            if runtime_backend in {"onnx", "onnx-gpu"}:
                kwargs["precision"] = onnx_precision
            with _onnx_cuda_sessions(runtime_backend):
                engine = Vieneu(**kwargs)
            _verify_engine_device(engine, runtime_backend)
            voice = {
                "speaker_emb": np.asarray(speaker_embedding, dtype=np.float32),
                "codes": np.asarray(reference_codes, dtype=np.int64),
            }
            if not callable(getattr(engine, "infer_stream", None)):
                raise RuntimeError("VieNeu v3 runtime does not expose infer_stream")
            return engine, voice

        engine, voice = await asyncio.to_thread(load)
        logger.info(
            "VieNeu session runtime loaded",
            extra={
                "event": "worker.tts.loaded",
                "tts_backend": _normalize_backend(backend),
                "tts_device": (
                    "cuda" if _normalize_backend(backend) in {"pytorch", "onnx-gpu"} else "cpu"
                ),
                "tts_onnx_precision": (
                    onnx_precision if _normalize_backend(backend) in {"onnx", "onnx-gpu"} else None
                ),
            },
        )
        return cls(
            engine,
            voice,
            frame_samples=frame_samples,
            apply_watermark=apply_watermark,
        )

    async def synthesize(self, segment: SpeechSegment) -> AsyncIterator[NativeAudioChunk]:
        if self._closed:
            raise RuntimeError("VieNeu TTS runtime is closed")
        started_at = time.perf_counter()
        first_audio_at: float | None = None
        emitted_frames = 0
        logger.info(
            "VieNeu synthesis started",
            extra={
                "event": "worker.tts.synthesis.started",
                "utterance_id": segment.utterance_id,
                "text_chars": len(segment.text),
                "output_mode": "stream",
            },
        )
        pending = bytearray()
        ready_frame: bytes | None = None
        sequence = 0
        inference_started_at = time.perf_counter()
        model_metrics: dict[str, Any] = {}
        model_chunk_count = 0
        async for waveform in self._stream_model_chunks(segment.text):
            pcm, chunk_metrics = await asyncio.to_thread(
                _float32_to_s16le_with_metrics,
                waveform,
                self._sample_rate,
            )
            if not pcm:
                continue
            model_metrics = _merge_model_metrics(model_metrics, chunk_metrics)
            model_chunk_count += 1
            pending.extend(pcm)
            frame_bytes = self._frame_samples * 2
            while len(pending) >= frame_bytes:
                samples = bytes(pending[:frame_bytes])
                del pending[:frame_bytes]
                if ready_frame is not None:
                    first_audio_at = first_audio_at or time.perf_counter()
                    emitted_frames += 1
                    yield NativeAudioChunk(
                        utterance_id=segment.utterance_id,
                        sequence=sequence,
                        samples=ready_frame,
                        sample_rate=48_000,
                        channels=1,
                    )
                    sequence += 1
                ready_frame = samples
        model_metrics = _finalize_model_metrics(model_metrics, model_chunk_count)
        logger.info(
            "VieNeu model output validated",
            extra={
                "event": "worker.tts.model.output",
                "utterance_id": segment.utterance_id,
                "model_inference_ms": round(
                    (
                        (self._model_stream_completed_at or time.perf_counter())
                        - inference_started_at
                    )
                    * 1_000,
                    2,
                ),
                **model_metrics,
            },
        )
        if not model_metrics.get("model_samples"):
            raise RuntimeError("VieNeu returned empty audio")
        frame_bytes = self._frame_samples * 2
        while len(pending) >= frame_bytes:
            samples = bytes(pending[:frame_bytes])
            del pending[:frame_bytes]
            if ready_frame is not None:
                first_audio_at = first_audio_at or time.perf_counter()
                emitted_frames += 1
                yield NativeAudioChunk(
                    utterance_id=segment.utterance_id,
                    sequence=sequence,
                    samples=ready_frame,
                    sample_rate=48_000,
                    channels=1,
                )
                sequence += 1
            ready_frame = samples
        if pending:
            pending.extend(b"\0" * (self._frame_samples * 2 - len(pending)))
            if ready_frame is not None:
                first_audio_at = first_audio_at or time.perf_counter()
                emitted_frames += 1
                yield NativeAudioChunk(
                    utterance_id=segment.utterance_id,
                    sequence=sequence,
                    samples=ready_frame,
                    sample_rate=48_000,
                    channels=1,
                )
                sequence += 1
            ready_frame = bytes(pending)
        if ready_frame is None:
            raise RuntimeError("VieNeu returned audio without a complete PCM frame")
        first_audio_at = first_audio_at or time.perf_counter()
        emitted_frames += 1
        yield NativeAudioChunk(
            utterance_id=segment.utterance_id,
            sequence=sequence,
            samples=ready_frame,
            sample_rate=48_000,
            channels=1,
            end_of_utterance=segment.is_final,
        )
        logger.info(
            "VieNeu synthesis completed",
            extra={
                "event": "worker.tts.synthesis.completed",
                "utterance_id": segment.utterance_id,
                "audio_frames": emitted_frames,
                "audio_duration_ms": round(
                    emitted_frames * self._frame_samples / 48_000 * 1_000,
                    2,
                ),
                "first_audio_ms": round((first_audio_at - started_at) * 1_000, 2),
                "duration_ms": round((time.perf_counter() - started_at) * 1_000, 2),
                "outcome": "success",
            },
        )

    async def _stream_model_chunks(self, text: str) -> AsyncIterator[Any]:
        """Bridge a thread-affine VieNeu generator into the Child event loop."""

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        stop = threading.Event()
        done = asyncio.Event()
        self._model_stream_completed_at = None

        def notify_done(completed_at: float) -> None:
            self._model_stream_completed_at = completed_at
            queue.put_nowait(("end", None))
            done.set()

        def produce() -> None:
            iterator: Iterator[Any] | None = None
            try:
                iterator = iter(
                    self._engine.infer_stream(
                        text,
                        voice=self._voice,
                        apply_watermark=self._apply_watermark,
                    )
                )
                for chunk in iterator:
                    if stop.is_set():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
            except BaseException as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
            finally:
                try:
                    if iterator is not None:
                        close_iterator = getattr(iterator, "close", None)
                        if callable(close_iterator):
                            close_iterator()
                finally:
                    loop.call_soon_threadsafe(notify_done, time.perf_counter())

        thread = threading.Thread(
            target=produce,
            name="vieneu-infer-stream",
            daemon=True,
        )
        thread.start()
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "chunk":
                    yield payload
                elif kind == "error":
                    raise payload
                else:
                    return
        finally:
            stop.set()
            await done.wait()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        engine = self._engine
        self._engine = None
        self._voice = {}
        close = getattr(engine, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
        logger.info("VieNeu session runtime closed", extra={"event": "worker.tts.closed"})


def _float32_to_s16le(chunk: Any) -> bytes:
    pcm, _metrics = _float32_to_s16le_with_metrics(chunk, 48_000)
    return pcm


def _float32_to_s16le_with_metrics(
    chunk: Any,
    sample_rate: int,
) -> tuple[bytes, dict[str, Any]]:
    import numpy as np

    source = np.asarray(chunk)
    values = np.asarray(source, dtype=np.float32).reshape(-1)
    nonfinite_samples = int(values.size - np.count_nonzero(np.isfinite(values)))
    if nonfinite_samples:
        raise RuntimeError(f"VieNeu returned {nonfinite_samples} non-finite audio samples")
    if not values.size:
        return b"", {
            "model_dtype": str(source.dtype),
            "model_shape": "x".join(str(value) for value in source.shape),
            "model_samples": 0,
            "model_sample_rate": sample_rate,
            "model_audio_duration_ms": 0.0,
            "model_min": 0.0,
            "model_max": 0.0,
            "model_peak": 0.0,
            "model_rms": 0.0,
            "model_dc_offset": 0.0,
            "model_clipping_ratio": 0.0,
            "model_nonfinite_samples": 0,
        }
    absolute = np.abs(values)
    metrics = {
        "model_dtype": str(source.dtype),
        "model_shape": "x".join(str(value) for value in source.shape),
        "model_samples": int(values.size),
        "model_sample_rate": sample_rate,
        "model_audio_duration_ms": round(values.size / sample_rate * 1_000, 2),
        "model_min": round(float(values.min()), 6),
        "model_max": round(float(values.max()), 6),
        "model_peak": round(float(absolute.max()), 6),
        "model_rms": round(float(np.sqrt(np.mean(values.astype(np.float64) ** 2))), 6),
        "model_dc_offset": round(float(np.mean(values, dtype=np.float64)), 6),
        "model_clipping_ratio": round(float(np.mean(absolute >= 0.999)), 8),
        "model_nonfinite_samples": nonfinite_samples,
    }
    pcm = (np.clip(values, -1.0, 1.0) * 32_767).astype("<i2").tobytes()
    return pcm, metrics


def _merge_model_metrics(
    aggregate: dict[str, Any],
    chunk: dict[str, Any],
) -> dict[str, Any]:
    samples = int(chunk["model_samples"])
    if not aggregate:
        return {
            "model_dtype": chunk["model_dtype"],
            "model_samples": samples,
            "model_sample_rate": chunk["model_sample_rate"],
            "model_min": chunk["model_min"],
            "model_max": chunk["model_max"],
            "model_peak": chunk["model_peak"],
            "model_nonfinite_samples": chunk["model_nonfinite_samples"],
            "_sum": float(chunk["model_dc_offset"]) * samples,
            "_sum_squares": float(chunk["model_rms"]) ** 2 * samples,
            "_clipped": float(chunk["model_clipping_ratio"]) * samples,
        }
    aggregate["model_samples"] += samples
    if aggregate["model_dtype"] != chunk["model_dtype"]:
        aggregate["model_dtype"] = "mixed"
    aggregate["model_min"] = min(aggregate["model_min"], chunk["model_min"])
    aggregate["model_max"] = max(aggregate["model_max"], chunk["model_max"])
    aggregate["model_peak"] = max(aggregate["model_peak"], chunk["model_peak"])
    aggregate["model_nonfinite_samples"] += chunk["model_nonfinite_samples"]
    aggregate["_sum"] += float(chunk["model_dc_offset"]) * samples
    aggregate["_sum_squares"] += float(chunk["model_rms"]) ** 2 * samples
    aggregate["_clipped"] += float(chunk["model_clipping_ratio"]) * samples
    return aggregate


def _finalize_model_metrics(
    aggregate: dict[str, Any],
    chunk_count: int,
) -> dict[str, Any]:
    if not aggregate:
        return {
            "model_dtype": "unknown",
            "model_shape": "chunks:0",
            "model_samples": 0,
            "model_sample_rate": 48_000,
            "model_audio_duration_ms": 0.0,
            "model_min": 0.0,
            "model_max": 0.0,
            "model_peak": 0.0,
            "model_rms": 0.0,
            "model_dc_offset": 0.0,
            "model_clipping_ratio": 0.0,
            "model_nonfinite_samples": 0,
        }
    samples = int(aggregate["model_samples"])
    aggregate["model_shape"] = f"chunks:{chunk_count}"
    aggregate["model_audio_duration_ms"] = round(
        samples / int(aggregate["model_sample_rate"]) * 1_000,
        2,
    )
    aggregate["model_rms"] = round(
        (float(aggregate.pop("_sum_squares")) / samples) ** 0.5,
        6,
    )
    aggregate["model_dc_offset"] = round(
        float(aggregate.pop("_sum")) / samples,
        6,
    )
    aggregate["model_clipping_ratio"] = round(
        float(aggregate.pop("_clipped")) / samples,
        8,
    )
    return aggregate


def _normalize_backend(backend: str) -> str:
    value = backend.strip().lower()
    aliases = {"torch": "pytorch", "onnx_gpu": "onnx-gpu", "onnx-cuda": "onnx-gpu"}
    value = aliases.get(value, value)
    if value not in {"pytorch", "onnx-gpu", "onnx"}:
        raise ValueError("TTS_VIENEU_BACKEND must be one of: pytorch, onnx-gpu, onnx")
    return value


def _require_gpu_runtime(backend: str) -> None:
    if backend == "pytorch":
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError(
                "VieNeu PyTorch backend requires CUDA, but torch.cuda.is_available() "
                "is false. Use the GPU Worker image and NVIDIA Container Toolkit."
            )
    elif backend == "onnx-gpu":
        import onnxruntime as ort

        available = ort.get_available_providers()
        if "CUDAExecutionProvider" not in available:
            raise RuntimeError(
                "VieNeu ONNX GPU backend requires CUDAExecutionProvider; available "
                f"providers are {available}. Install onnxruntime-gpu in the Worker image."
            )


@contextmanager
def _onnx_cuda_sessions(backend: str) -> Iterator[None]:
    """Redirect VieNeu v3.2.4's CPU-only ONNX sessions to the assigned GPU.

    VieNeu currently hard-codes CPUExecutionProvider in OnnxV3LiteEngine. Each
    Composite Child is a dedicated process, so this temporary module patch is
    isolated to model construction and cannot affect another session.
    CUDA device 0 is the scheduler-assigned card inside CUDA_VISIBLE_DEVICES.
    """

    if backend != "onnx-gpu":
        yield
        return

    import onnxruntime as ort

    original = ort.InferenceSession

    def cuda_inference_session(*args: Any, **kwargs: Any) -> Any:
        providers = kwargs.get("providers")
        if providers == ["CPUExecutionProvider"]:
            kwargs["providers"] = [
                ("CUDAExecutionProvider", {"device_id": 0}),
                "CPUExecutionProvider",
            ]
        return original(*args, **kwargs)

    ort.InferenceSession = cuda_inference_session
    try:
        yield
    finally:
        ort.InferenceSession = original


def _verify_engine_device(engine: Any, backend: str) -> None:
    actual_backend = getattr(engine, "backend", None)
    if backend == "pytorch":
        if actual_backend != "pytorch":
            raise RuntimeError(f"VieNeu selected backend {actual_backend!r}, expected 'pytorch'")
        device = getattr(getattr(engine, "engine", None), "device", None)
        if getattr(device, "type", str(device)) != "cuda":
            raise RuntimeError(f"VieNeu PyTorch model loaded on {device!s}, expected CUDA")
        return
    if backend != "onnx-gpu":
        return
    if actual_backend != "onnx":
        raise RuntimeError(f"VieNeu selected backend {actual_backend!r}, expected 'onnx'")
    runtime = getattr(engine, "engine", None)
    sessions = [
        getattr(runtime, name, None)
        for name in ("sess_pre", "sess_dec", "sess_ac", "sess_codec_dec", "sess_codec_step")
    ]
    active_sessions = [session for session in sessions if session is not None]
    missing_cuda = [
        session.get_providers()
        for session in active_sessions
        if "CUDAExecutionProvider" not in session.get_providers()
    ]
    if not active_sessions or missing_cuda:
        raise RuntimeError(
            "VieNeu ONNX sessions did not activate CUDAExecutionProvider: "
            f"{missing_cuda or 'no sessions found'}"
        )
