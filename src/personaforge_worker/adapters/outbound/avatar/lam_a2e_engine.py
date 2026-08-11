"""Isolated bridge for the pinned LAM-Audio2Expression implementation.

The upstream repository is a script-oriented project (its imports assume the repository root is
on ``sys.path`` and its streaming API is synchronous).  This module is the only place where those
assumptions are adapted to PersonaForge's async, per-child runtime contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import math
import sys
import tarfile
import types
from pathlib import Path
from typing import Any

import numpy as np

from personaforge_worker.domain.avatar import AudioWindow, ExpressionFrame

logger = logging.getLogger(__name__)


def _find_weight(snapshot: Path) -> Path:
    candidates = sorted(snapshot.rglob("LAM_audio2exp_streaming.tar"))
    if not candidates:
        candidates = sorted(snapshot.rglob("*.tar"))
    if not candidates:
        raise FileNotFoundError(f"LAM-A2E streaming checkpoint not found under {snapshot}")
    return candidates[0]


def _extract_weight(weight: Path) -> Path:
    target = weight.parent / (weight.stem + "_extracted")
    marker = target / ".complete"
    if marker.exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    if not tarfile.is_tarfile(weight):
        # Some mirrors expose the torch checkpoint directly while retaining the .tar suffix.
        return weight.parent
    # The artifacts are downloaded from a pinned revision; still reject path traversal in case a
    # mirror is accidentally configured.
    with tarfile.open(weight, "r:gz") as archive:
        for member in archive.getmembers():
            destination = (target / member.name).resolve()
            if target.resolve() not in destination.parents and destination != target.resolve():
                raise ValueError(f"Unsafe path in LAM-A2E archive: {member.name}")
        archive.extractall(target)
    marker.touch()
    return target


@contextlib.contextmanager
def _upstream_import_path(source_dir: Path):
    source = str(source_dir.resolve())
    sys.path.insert(0, source)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(source)


def _load_sync(checkpoint: Path, settings: Any) -> LamA2EEngine:
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - exercised in worker image
        raise RuntimeError(
            "LAM-A2E requires torch and transformers in the GPU worker image"
        ) from exc

    source_dir = Path(getattr(settings, "source_dir", "/app/third_party/LAM_Audio2Expression"))
    if not source_dir.joinpath("models").is_dir():
        raise RuntimeError(f"LAM-A2E source tree is missing: {source_dir}")
    outer_weight = _find_weight(checkpoint)
    extracted = _extract_weight(outer_weight)
    weight_path = next(
        (p for p in sorted(extracted.rglob("*.tar")) if p.resolve() != outer_weight.resolve()),
        None,
    )
    if weight_path is None:
        # Some tarballs contain the torch checkpoint without another archive.
        weight_path = next(iter(sorted(extracted.rglob("*.pth"))), None)
    if weight_path is None and extracted == outer_weight.parent:
        weight_path = outer_weight
    if weight_path is None:
        raise FileNotFoundError(f"No torch checkpoint found after extracting {checkpoint}")

    with _upstream_import_path(source_dir):
        # LAM-A2E was released against Transformers 4.x and imports ModelOutput from the
        # deprecated ``transformers.file_utils`` module. VieNeu currently uses a newer
        # Transformers release, so provide the small compatibility alias before importing the
        # upstream model package.
        try:
            importlib.import_module("transformers.file_utils")
        except ModuleNotFoundError:
            from transformers.utils import ModelOutput

            compatibility = types.ModuleType("transformers.file_utils")
            compatibility.ModelOutput = ModelOutput
            sys.modules["transformers.file_utils"] = compatibility
        # Importing models registers DefaultEstimator and Audio2Expression using the upstream
        # registry.  Keep these imports local so CPU/recorded workers never import torch.
        importlib.import_module("models")
        default_estimator = importlib.import_module("models.default").DefaultEstimator
        network = importlib.import_module("models.network")
        _ = network  # ensure the Audio2Expression class is registered

        config_path = source_dir / "configs" / "wav2vec2_config.json"
        if not config_path.exists():
            raise FileNotFoundError(f"LAM-A2E wav2vec config not found: {config_path}")
        # The upstream model downloads the Wav2Vec weights from HF when this identifier is used.
        backbone = dict(
            type="Audio2Expression",
            pretrained_encoder_type="wav2vec",
            pretrained_encoder_path="facebook/wav2vec2-base-960h",
            wav2vec2_config_path=str(config_path),
            num_identity_classes=12,
            identity_feat_dim=64,
            hidden_dim=512,
            expression_dim=52,
            norm_type="ln",
            use_transformer=False,
            num_attention_heads=8,
            num_transformer_layers=6,
        )
        model = default_estimator(backbone=backbone, criteria=[])
        # LAM-A2E's custom Wav2Vec2 forward enables ``output_attentions``. Newer
        # Transformers defaults to SDPA, which rejects that flag; eager attention
        # is required for compatibility with the pinned upstream implementation.
        encoder_config = model.backbone.audio_encoder.config
        with contextlib.suppress(Exception):
            encoder_config._attn_implementation = "eager"
        with contextlib.suppress(Exception):
            encoder_config.attn_implementation = "eager"
        raw = torch.load(weight_path, map_location="cpu", weights_only=False)
        state = raw.get("state_dict", raw) if isinstance(raw, dict) else raw
        normalized = {}
        for key, value in state.items():        
            normalized[key[7:] if key.startswith("module.") else key] = value
        model.load_state_dict(normalized, strict=True)
        device_name = getattr(settings, "device", "cuda")
        device = torch.device(device_name if torch.cuda.is_available() else "cpu")
        model.to(device).eval()
        # Resolve inference-only dependencies once while the upstream source
        # path is active. `_infer` is called for every audio batch; importing
        # here avoids repeated module lookups without importing LAM/Torch in
        # recorded or CPU-only workers.
        import librosa
        import torch.nn.functional as functional
        from models.utils import (
            apply_frame_blending,
            apply_random_eye_blinks_context,
            apply_savitzky_golay_smoothing,
            smooth_mouth_movements,
            symmetrize_blendshapes,
        )

        inference_dependencies = {
            "librosa": librosa,
            "functional": functional,
            "apply_frame_blending": apply_frame_blending,
            "apply_random_eye_blinks_context": apply_random_eye_blinks_context,
            "apply_savitzky_golay_smoothing": apply_savitzky_golay_smoothing,
            "smooth_mouth_movements": smooth_mouth_movements,
            "symmetrize_blendshapes": symmetrize_blendshapes,
        }
    logger.info(
        "LAM-A2E engine loaded",
        extra={"event": "worker.avatar.lam_a2e.loaded", "device": str(device)},
    )
    return LamA2EEngine(model, torch, device, settings, inference_dependencies)


async def load_engine(checkpoint: Path, settings: Any) -> LamA2EEngine:
    return await asyncio.to_thread(_load_sync, checkpoint, settings)


class LamA2EEngine:
    def __init__(
        self,
        model: Any,
        torch: Any,
        device: Any,
        settings: Any,
        inference_dependencies: dict[str, Any],
    ) -> None:
        self.model, self.torch, self.device = model, torch, device
        self.settings = settings
        self._inference_dependencies = inference_dependencies
        # The released LAM-A2E weights are trained at 30 FPS. Keep the value
        # configurable so runtimes with a compatible checkpoint can publish at
        # another cadence without changing the audio clock.
        # The released checkpoint is a 30 FPS model. Publish cadence is handled
        # later by the speech pipeline using PTS-based temporal downsampling.
        self.motion_fps = max(1, int(getattr(settings, "model_fps", 30)))
        self.inference_chunk_ms = max(
            20, int(getattr(settings, "inference_chunk_ms", 100))
        )
        self._pending_audio = bytearray()
        self._pending_start_sample: int | None = None
        self._pending_sample_rate = 48_000
        self._pending_session_id = ""
        self._pending_utterance_id = ""
        self.context: dict[str, Any] = {
            "is_initial_input": True,
            "previous_audio": None,
            "previous_expression": None,
            "previous_volume": None,
        }

    async def warmup(self) -> None:
        silence = np.zeros(16_000, dtype=np.float32)
        await asyncio.to_thread(self._infer, silence, 16_000)
        self.context = {
            "is_initial_input": True,
            "previous_audio": None,
            "previous_expression": None,
            "previous_volume": None,
        }

    def _infer(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        librosa = self._inference_dependencies["librosa"]
        functional = self._inference_dependencies["functional"]
        apply_frame_blending = self._inference_dependencies["apply_frame_blending"]
        apply_random_eye_blinks_context = self._inference_dependencies[
            "apply_random_eye_blinks_context"
        ]
        apply_savitzky_golay_smoothing = self._inference_dependencies[
            "apply_savitzky_golay_smoothing"
        ]
        smooth_mouth_movements = self._inference_dependencies["smooth_mouth_movements"]
        symmetrize_blendshapes = self._inference_dependencies["symmetrize_blendshapes"]

        # Keep this calculation aligned with engines.infer.InferBase
        # infer_streaming_audio. The model receives a 64-frame rolling window
        # (about 2.13 s at 30 FPS), then only the newly arrived frames are
        # emitted. There is intentionally no future-frame lookahead.
        max_frame_length = 64
        frame_length = math.ceil(len(audio) / sample_rate * self.motion_fps)
        hop_length = max(1, int(sample_rate / self.motion_fps))
        volume = librosa.feature.rms(
            y=audio,
            frame_length=min(hop_length, max(1, len(audio))),
            hop_length=hop_length,
        )[0]
        if volume.shape[0] > frame_length:
            volume = volume[:frame_length]

        if sample_rate != 16_000:
            audio = librosa.resample(
                audio.astype(np.float32),
                orig_sr=sample_rate,
                target_sr=16_000,
            )
        else:
            audio = audio.astype(np.float32, copy=False)

        start_frame = int(max_frame_length - len(audio) / 16_000 * self.motion_fps)
        target_samples = 16_000 * max_frame_length // self.motion_fps
        previous = self.context.get("previous_audio")
        prefix_length = max(0, target_samples - len(audio))
        prefix = (
            np.zeros(prefix_length, dtype=np.float32)
            if self.context.get("is_initial_input") or previous is None
            else previous[-prefix_length:]
        )
        model_audio = np.concatenate([prefix, audio])
        with self.torch.no_grad():
            identity = functional.one_hot(
                self.torch.tensor(getattr(self.settings, "identity_index", 0), device=self.device),
                12,
            ).float()[None, ...]
            input_audio = self.torch.from_numpy(model_audio).to(self.device)[None, ...]
            output = self.model({"id_idx": identity, "input_audio_array": input_audio})["pred_exp"]
        values = output.squeeze(0).detach().float().cpu().numpy()
        values = values[max(0, start_frame) :, :]
        previous_expression = self.context.get("previous_expression")
        previous_volume = self.context.get("previous_volume")
        processed_frames = len(previous_expression) if previous_expression is not None else 0
        expression_params = (
            np.concatenate([previous_expression, values], axis=0)
            if previous_expression is not None
            else values
        )
        audio_volume = (
            np.concatenate([previous_volume, volume])
            if previous_volume is not None
            else volume
        )
        # Keep the exact upstream post-processing order and only emit the new
        # portion after the previous expression context has been protected.
        expression_params = smooth_mouth_movements(
            expression_params,
            processed_frames,
            audio_volume,
        )
        expression_params = apply_frame_blending(expression_params, processed_frames)
        expression_params, _ = apply_savitzky_golay_smoothing(
            expression_params,
            window_length=5,
        )
        expression_params = symmetrize_blendshapes(expression_params)
        expression_params = apply_random_eye_blinks_context(
            expression_params,
            processed_frames=processed_frames,
        )
        values = expression_params[processed_frames:]
        self.context["previous_audio"] = model_audio
        self.context["is_initial_input"] = False
        self.context["previous_expression"] = np.concatenate(
            [previous_expression, values] if previous_expression is not None else [values],
            axis=0,
        )[-max_frame_length:]
        self.context["previous_volume"] = np.concatenate(
            [previous_volume, volume] if previous_volume is not None else [volume],
        )[-max_frame_length:]
        return values

    async def _process_batch(self, audio: AudioWindow):
        array = np.frombuffer(audio.samples, dtype=np.int16).astype(np.float32) / 32768.0
        values = await asyncio.to_thread(self._infer, array, audio.sample_rate)
        duration = max(1, round(audio.sample_rate / self.motion_fps))
        for index, row in enumerate(values):
            yield ExpressionFrame(
                index,
                audio.start_sample + index * duration,
                duration,
                "arkit52-v1",
                tuple(row.tolist()),
            )

    async def process(self, audio: AudioWindow):
        if self._pending_utterance_id and self._pending_utterance_id != audio.utterance_id:
            async for frame in self.flush(self._pending_utterance_id):
                yield frame
        if self._pending_start_sample is None:
            self._pending_start_sample = audio.start_sample
            self._pending_sample_rate = audio.sample_rate
            self._pending_session_id = audio.session_id
            self._pending_utterance_id = audio.utterance_id
        self._pending_audio.extend(audio.samples)
        batch_bytes = max(
            1,
            round(
                audio.sample_rate
                * self.inference_chunk_ms
                / 1_000
            )
            * 2,
        )
        if len(self._pending_audio) < batch_bytes:
            return
        batch = AudioWindow(
            session_id=self._pending_session_id,
            utterance_id=self._pending_utterance_id,
            start_sample=self._pending_start_sample,
            samples=bytes(self._pending_audio),
            sample_rate=self._pending_sample_rate,
        )
        self._pending_audio.clear()
        self._pending_start_sample = None
        async for frame in self._process_batch(batch):
            yield frame

    async def flush(self, utterance_id: str):
        if self._pending_utterance_id != utterance_id or not self._pending_audio:
            return
        pending = AudioWindow(
            session_id=self._pending_session_id,
            utterance_id=utterance_id,
            start_sample=self._pending_start_sample or 0,
            samples=bytes(self._pending_audio),
            sample_rate=self._pending_sample_rate,
        )
        self._pending_audio.clear()
        self._pending_start_sample = None
        async for frame in self._process_batch(pending):
            yield frame

    async def close(self) -> None:
        self.model = None
        if self.torch.cuda.is_available():
            self.torch.cuda.empty_cache()
