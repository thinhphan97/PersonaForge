import asyncio
import threading

import pytest

from personaforge_worker.adapters.outbound.vieneu_tts import (
    VieNeuTtsSessionRuntime,
    _normalize_backend,
)
from personaforge_worker.domain.media import SpeechSegment


class FakeVieNeuEngine:
    def infer_stream(self, text: str, **_kwargs: object):
        assert text == "Xin chào."
        yield [0.5] * 1_200
        yield [-0.5] * 1_200


@pytest.mark.asyncio
async def test_vieneu_pcm_is_streamed_in_20ms_frames_without_extra_silence() -> None:
    pytest.importorskip("numpy")
    runtime = VieNeuTtsSessionRuntime(
        FakeVieNeuEngine(),
        {"speaker_emb": (), "codes": ()},
        frame_samples=960,
        apply_watermark=False,
    )
    segment = SpeechSegment(
        session_id="session-1",
        turn_id="turn-1",
        utterance_id="utterance-1",
        sequence=0,
        text="Xin chào.",
        is_final=True,
    )

    chunks = [chunk async for chunk in runtime.synthesize(segment)]
    await runtime.close()

    assert [chunk.sequence for chunk in chunks] == [0, 1, 2]
    assert all(len(chunk.samples) == 960 * 2 for chunk in chunks)
    assert [chunk.end_of_utterance for chunk in chunks] == [False, False, True]
    assert chunks[-1].samples[960:] == b"\0" * 960


class ThreadAffineVieNeuEngine:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.invocation_thread_ids: list[set[int]] = []

    def infer_stream(self, _text: str, **_kwargs: object):
        thread_ids: set[int] = set()
        self.invocation_thread_ids.append(thread_ids)
        with self.lock:
            for _ in range(2):
                thread_ids.add(threading.get_ident())
                yield [0.1] * 960


@pytest.mark.asyncio
async def test_model_generator_operations_use_one_thread_per_utterance() -> None:
    engine = ThreadAffineVieNeuEngine()
    runtime = VieNeuTtsSessionRuntime(
        engine,
        {"speaker_emb": (), "codes": ()},
        apply_watermark=False,
    )

    for _ in range(2):
        chunks = await asyncio.wait_for(
            _collect(runtime._stream_model_chunks("Xin chào.")), timeout=1
        )
        assert len(chunks) == 2
    await runtime.close()

    assert len(engine.invocation_thread_ids) == 2
    assert all(len(thread_ids) == 1 for thread_ids in engine.invocation_thread_ids)


@pytest.mark.asyncio
async def test_vieneu_stream_mode_completes_consecutive_sentences() -> None:
    pytest.importorskip("numpy")
    engine = ThreadAffineVieNeuEngine()
    runtime = VieNeuTtsSessionRuntime(
        engine,
        {"speaker_emb": (), "codes": ()},
        frame_samples=960,
        apply_watermark=False,
    )

    async def synthesize(utterance_id: str) -> None:
        segment = SpeechSegment(
            session_id="session-1",
            turn_id="turn-1",
            utterance_id=utterance_id,
            sequence=0,
            text="Xin chào.",
            is_final=True,
        )
        chunks = [chunk async for chunk in runtime.synthesize(segment)]
        assert chunks[-1].end_of_utterance is True

    await asyncio.wait_for(synthesize("utterance-1"), timeout=2)
    await asyncio.wait_for(synthesize("utterance-2"), timeout=2)
    await runtime.close()

    assert len(engine.invocation_thread_ids) == 2
    assert all(len(thread_ids) == 1 for thread_ids in engine.invocation_thread_ids)


async def _collect(iterator):
    return [item async for item in iterator]


@pytest.mark.parametrize(
    ("configured", "normalized"),
    [("torch", "pytorch"), ("onnx_gpu", "onnx-gpu"), ("onnx", "onnx")],
)
def test_backend_aliases_are_explicit(configured: str, normalized: str) -> None:
    assert _normalize_backend(configured) == normalized


def test_auto_backend_is_rejected_to_prevent_silent_cpu_fallback() -> None:
    with pytest.raises(ValueError, match="TTS_VIENEU_BACKEND"):
        _normalize_backend("auto")
