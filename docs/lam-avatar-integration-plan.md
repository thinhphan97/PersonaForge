# Kế hoạch tích hợp LAM Avatar và Custom Character

> Trạng thái: design draft. Việc triển khai được chia thành hai giai đoạn giống chiến lược
> VieNeu-TTS: trước hết chạy end-to-end với system default character, sau đó mới bổ sung custom
> character, build job, phân quyền và quản lý artifact. Recorded-video mode tiếp tục được giữ để
> tương thích ngược.

Upstream/reference chính thức:

- [LAM](https://github.com/aigc3d/LAM): one-shot animatable Gaussian avatar reconstruction và
  character artifact export.
- [LAM_Audio2Expression](https://github.com/aigc3d/LAM_Audio2Expression): sinh ARKit blendshape
  coefficients từ audio.
- [LAM_WebRender](https://github.com/aigc3d/LAM_WebRender): WebGL renderer cho LAM avatar và
  LAM_Audio2Expression output.
- [gaussian-splat-renderer-for-lam](https://www.npmjs.com/package/gaussian-splat-renderer-for-lam):
  package renderer được LAM_WebRender công bố.
- [LAM model artifacts trên Hugging Face](https://huggingface.co/3DAIGC/LAM-20K).
- [LAM-A2E model artifacts trên Hugging Face](https://huggingface.co/3DAIGC/LAM_audio2exp).

Mọi dependency, source revision, model revision và artifact checksum phải được pin trong manifest
của PersonaForge. Không tải nhánh, tag hoặc model revision mới nhất một cách ngầm định khi Child
khởi động.

## 1. Mục tiêu

PersonaForge không xem avatar model chỉ là một video generator. Avatar runtime tạo một
presentation output, có thể là:

- video frame được render ở server và publish bằng LiveKit VideoTrack;
- motion metadata được sinh ở server, publish bằng LiveKit data transport và render ở browser.

Trong integration này, [LAM](https://github.com/aigc3d/LAM) tạo character-specific asset,
[LAM_Audio2Expression](https://github.com/aigc3d/LAM_Audio2Expression) tạo facial motion và
[LAM_WebRender](https://github.com/aigc3d/LAM_WebRender) render asset cùng motion tại browser.

Luồng LAM mục tiêu:

```text
VieNeu TTS
    ↓
Canonical PCM 48 kHz
    ↓
BoundedAudioTimeline
    ├──────────────► LiveKit AudioTrack
    │
    └──► resample 48 kHz → 16 kHz
             ↓
         LAM-A2E
             ↓
         ARKit52
             ↓
       LiveKit data transport
             ↓
      browser ExpressionBuffer
             ↓
        LAM_WebRender
             ▲
             │
         avatar.zip
```

Các invariant hiện tại tiếp tục được giữ:

- Audio là master clock.
- PTS được derive từ audio sample index, không phải packet arrival hoặc inference completion.
- Worker Master không import Torch và không tạo CUDA context.
- Model chỉ được tạo trong Composite Child hoặc model-engine process chuyên biệt.
- Core không vận chuyển PCM, tensor, motion frame hoặc video frame.
- PostgreSQL là durable source of truth; Redis dùng cho command, event, lease và coordination.

## 2. Hai giai đoạn triển khai

### 2.1. Giai đoạn 1: default character và realtime lip-sync

Mục tiêu của giai đoạn 1 là chứng minh toàn bộ realtime path bằng một hoặc một số nhân vật hệ
thống được chuẩn bị sẵn. Chưa hỗ trợ user upload ảnh, build hoặc training nhân vật.

```text
System character có sẵn
    + VieNeu TTS
    + LAM-A2E shared checkpoint
    + LiveKit
    + LAM_WebRender
    = avatar nói theo audio trong browser
```

Giai đoạn này gồm:

1. Generalize `LipSyncRuntime` thành `AvatarRuntime` nhưng giữ recorded adapter hoạt động.
2. Truyền immutable `AvatarRuntimeSpec` từ Core qua Control, Redis, Master tới Child.
3. Trả `PresentationSpec` trong media-access response.
4. Render một `avatar.zip` mặc định bằng LAM_WebRender.
5. Kiểm chứng data transport bằng fake ARKit52 trước khi kết nối model thật.
6. Tích hợp LAM-A2E, stateful resampler và PTS-based `ExpressionBuffer`.
7. Test reconnect, worker restart, multi-viewer, session close và recorded fallback.

Không thuộc giai đoạn 1:

- custom-character upload;
- LAM reconstruction/build worker;
- fine-tuning model theo nhân vật;
- character share/ACL;
- artifact rebuild và version-management UI;
- shared A2E engine hoặc microbatch nhiều session.

### 2.2. Giai đoạn 2: custom character và artifact lifecycle

Đối với LAM, thao tác tạo nhân vật mặc định là reconstruction/build artifact, không phải
fine-tune neural checkpoint theo từng người.

```text
portrait image
    ↓
Avatar Builder Worker
    ↓
preprocess
    ↓
FLAME fitting
    ↓
shared pretrained LAM checkpoint
    ↓
WebRender export
    ↓
versioned avatar.zip
    ↓
Object Storage
```

Giai đoạn 2 bổ sung:

- `SYSTEM` và `CUSTOM` character;
- owner, `PRIVATE`, `PUBLIC` và quyền shared `USE`;
- source portrait và versioned character artifact;
- build queue, lease, retry, cancellation và progress;
- signed asset access;
- checkpoint/model provenance;
- upload/build/management UI.

Để hỗ trợ model khác LAM, build job không được hardcode thành reconstruction:

```python
class CharacterBuildKind(str, Enum):
    RECONSTRUCT = "reconstruct"
    TRAIN = "train"
    IMPORT = "import"
```

Ví dụ:

```text
LAM                 → RECONSTRUCT → avatar.zip
TalkingGaussian     → TRAIN       → checkpoint + runtime artifact
Validated user ZIP  → IMPORT      → avatar.zip
```

## 3. Runtime và presentation contracts

Ba khái niệm phải độc lập:

```python
class AvatarOutputKind(str, Enum):
    VIDEO = "video"
    MOTION = "motion"


class RenderLocation(str, Enum):
    SERVER = "server"
    CLIENT = "client"


class AvatarTransportKind(str, Enum):
    MEDIA_TRACK = "media-track"
    DATA_TRACK = "data-track"
    DATA_PACKET = "data-packet"
```

Runtime output:

```python
AvatarRenderOutput = TimedVideoFrame | TimedMotionFrame
```

Motion frame cần mang timeline và fencing metadata:

```python
@dataclass(frozen=True, slots=True)
class TimedMotionFrame:
    session_id: str
    utterance_id: str
    generation: int
    sequence: int
    pts_sample: int
    duration_samples: int
    sample_rate: int
    schema: str
    values: tuple[float, ...]
    flags: MotionFrameFlags
```

`generation` giúp browser loại packet cũ sau worker restart, reprovision hoặc track republish.
`RESET`, `END_OF_UTTERANCE` và `DISCONTINUITY` là các flag tối thiểu.

Avatar runtime nên stream output thay vì trả một tuple batch:

```python
class AvatarSessionRuntimePort(Protocol):
    @property
    def output_kind(self) -> AvatarOutputKind:
        ...

    def process(
        self,
        request: AvatarRenderRequest,
    ) -> AsyncIterator[AvatarRenderOutput]:
        ...

    def flush(
        self,
        utterance_id: str,
    ) -> AsyncIterator[AvatarRenderOutput]:
        ...

    async def close(self) -> None:
        ...
```

Recorded adapter có thể yield một `TimedVideoFrame`; A2E có thể yield nhiều motion frame ngay
khi frame đầu sẵn sàng.

## 4. State policy: idle, thinking và speaking

### 4.1. Tách conversation state khỏi speech activity

Không nên đồng nhất trạng thái nghiệp vụ với việc có audio đang phát.

```text
conversation_state:
    idle | thinking | speaking | closed

speech_activity:
    silent | prompt | answer
```

Ví dụ, khi hệ thống đang chờ agent:

```text
conversation_state = thinking
speech_activity = prompt
```

Avatar vẫn hiển thị animation/state `thinking`, nhưng miệng chuyển động theo câu TTS thông báo.
Không đổi business state sang `speaking` chỉ vì thinking prompt có giọng nói.

### 4.2. Idle vẫn duy trì audio và lip-sync timeline

Khi `idle`, Child tiếp tục tạo canonical silence:

```text
PCM S16LE
mono
48 kHz
20 ms/chunk
```

Silence đi qua cùng `BoundedAudioTimeline`:

```text
silence
    ├──► LiveKit AudioTrack
    └──► AvatarRuntime/LAM-A2E
              ↓
         neutral ARKit52
```

Mục tiêu:

- audio track không bị unpublish/recreate;
- audio sample clock không dừng;
- motion sequence và presentation generation giữ liên tục;
- avatar giữ neutral face/lip position trong khi base idle animation vẫn chạy;
- chuyển `idle → thinking → speaking` không renegotiate WebRTC.

Trong giai đoạn đầu, silence được đưa qua A2E thật để kiểm chứng cùng một data path. Sau benchmark
có thể dùng `NeutralMotionGenerator` để giảm GPU duty khi idle, nhưng adapter phải tạo output có
cùng schema, PTS và sequence; đây chỉ là optimization, không thay đổi contract.

### 4.3. Thinking với silence

Mặc định, khi agent chưa trả lời, `thinking` cũng tiếp tục phát silence và chạy avatar runtime:

```text
thinking state
    + silence audio
    + neutral facial motion
    + thinking base animation
```

Base animation và facial motion là hai lớp độc lập:

```text
thinking animation.glb
        +
neutral/ARKit52 facial coefficients
        ↓
LAM_WebRender
```

### 4.4. Thinking voice prompt tùy chọn

Nếu agent xử lý lâu, hệ thống có thể phát một câu ngắn như:

```text
"Tôi đang suy nghĩ, bạn vui lòng chờ trong giây lát."
```

Câu này là một `SpeechSegment` hệ thống, dùng cùng voice đã được session chọn:

```text
thinking threshold reached
    ↓
SYSTEM_THINKING_PROMPT SpeechSegment
    ↓
VieNeu TTS
    ↓
AudioTimeline
    ├──► LiveKit audio
    └──► A2E motion
```

Policy đề xuất:

- Prompt bị tắt mặc định hoặc được bật bằng session/persona configuration.
- Chỉ enqueue nếu agent chưa có text/audio output sau `thinking_prompt_delay_ms`.
- Mỗi agent turn chỉ phát tối đa một prompt.
- Nếu agent trả lời trước threshold, hủy prompt chưa bắt đầu.
- Nếu prompt đã bắt đầu, không phát answer chồng lên prompt; answer chờ sentence boundary hoặc
  prompt kết thúc.
- Không lặp prompt vô hạn nếu agent timeout.
- Nội dung hỗ trợ locale và có thể được cấu hình theo persona.
- Prompt failure không làm fail agent turn; pipeline quay về thinking silence.
- Prompt phải có `utterance_id` và `segment_kind=thinking_prompt` riêng để tracing.

Settings gợi ý:

```env
THINKING_VOICE_PROMPT_ENABLED=false
THINKING_VOICE_PROMPT_DELAY_MS=1500
THINKING_VOICE_PROMPT_MAX_PER_TURN=1
THINKING_VOICE_PROMPT_TEXT_VI=Tôi đang suy nghĩ, bạn vui lòng chờ trong giây lát.
```

Sau prompt:

```text
prompt audio completed
    ↓
thinking silence continues
    ↓
agent answer becomes available
    ↓
answer SpeechSegment
    ↓
conversation_state = speaking
```

### 4.5. State transition table

| Conversation state | Audio source | Facial motion | Base animation | Business meaning |
|---|---|---|---|---|
| `idle` | Continuous silence | Neutral from A2E or compatible neutral generator | Idle | Chưa có turn đang xử lý |
| `thinking` | Silence | Neutral | Thinking | Agent đang xử lý |
| `thinking` + prompt | VieNeu TTS prompt | ARKit52 từ prompt audio | Thinking | Thông báo người dùng tiếp tục chờ |
| `speaking` | VieNeu TTS answer | ARKit52 từ answer audio | Responding | Agent đang trả lời |
| `closed` | Track drain rồi stop | Final neutral/reset | Closed overlay | Session không nhận thêm command |

## 5. Audio timeline và A2E windowing

Canonical timeline không đổi khỏi 48 kHz. LAM-A2E adapter tự resample về 16 kHz bằng stateful
streaming resampler.

Không được resample độc lập từng chunk 20 ms vì có thể gây discontinuity và drift. Adapter phải
quản lý:

- rolling context;
- window overlap;
- committed output range;
- frame deduplication;
- initial/final padding;
- exact mapping về sample index 48 kHz;
- flush cuối utterance;
- reset khi generation thay đổi.

`160–200 ms` chỉ là giá trị benchmark ban đầu, không phải domain invariant. Chunk size và context
phải được pin theo model revision sau khi đo first-motion latency, quality và GPU duty.

Thông số upstream hiện công bố có thể đối chiếu tại
[`lam_audio2exp_config_streaming.py`](https://github.com/aigc3d/LAM_Audio2Expression/blob/main/configs/lam_audio2exp_config_streaming.py).
PersonaForge không xem config upstream là domain contract; adapter phải validate sample rate,
expression dimension và motion FPS khi load model.

## 6. Default character specification

Giai đoạn 1 có thể seed một character:

```json
{
  "character_id": "system-lam-default",
  "display_name": "LAM Default",
  "kind": "SYSTEM",
  "status": "READY",
  "runtime": {
    "provider": "lam-a2e",
    "model_id": "lam-a2e",
    "model_revision": "pinned-revision",
    "output_kind": "motion",
    "output_schema": "arkit52-v1",
    "motion_fps": 30
  },
  "presentation": {
    "render_location": "client",
    "renderer": "lam-webgl",
    "transport": "livekit-data"
  },
  "artifact": {
    "uri": "characters/system-lam-default/v1/avatar.zip",
    "version": 1,
    "checksum": "sha256:..."
  }
}
```

Trong development, asset có thể được mount bằng Docker volume. Production dùng immutable object
key và signed URL. Model checkpoint và character artifact là hai resource khác nhau:

| Resource | Scope | Ví dụ |
|---|---|---|
| Shared model checkpoint | Toàn hệ thống/GPU runtime | LAM-20K, LAM-A2E |
| Character source | Một custom character | `portrait.jpg` |
| Character runtime artifact | Một character version | `avatar.zip` |

## 7. Runtime resolution và provisioning

Session chỉ lưu reference nghiệp vụ:

```text
agent_id
voice_id
character_id
```

Khi provision, Core resolve thành immutable snapshots:

```text
VoiceRuntimeSpec
AvatarRuntimeSpec
PresentationSpec
```

`AvatarRuntimeSpec` đi tới Child. `PresentationSpec` đi tới browser. Signed asset URL được cấp sau
authorization và không được lưu trong PostgreSQL session, Redis event hoặc log.

Compatibility được validate trước khi reserve GPU:

```text
runtime output_schema = arkit52-v1
renderer input_schema = arkit52-v1
browser supports WebGL
worker supports lam-a2e revision
LiveKit transport supported
    → provision
```

Mismatch phải trả typed error trước khi khởi tạo Child.

## 8. Frontend architecture

```text
AvatarPresentationController
    ├── ServerVideoPresentation
    └── ClientMotionPresentation
            ├── MotionTransportAdapter
            ├── ExpressionBuffer
            ├── AudioPlaybackClock
            └── LamWebRendererAdapter
```

Renderer không connect LiveKit và không thực hiện authorization. Transport adapter không biết
LAM. `ExpressionBuffer` sample theo `AudioPlaybackClock`, không theo packet arrival time.

`LamWebRendererAdapter` bao bọc package
[`gaussian-splat-renderer-for-lam`](https://www.npmjs.com/package/gaussian-splat-renderer-for-lam)
theo hướng dẫn của [LAM_WebRender](https://github.com/aigc3d/LAM_WebRender). Application code
không gọi trực tiếp package này ngoài adapter, nhờ đó có thể thay renderer mà không sửa LiveKit
transport hoặc presentation controller.

Client phải xử lý:

- packet sequence gap;
- stale generation;
- late frame drop;
- reconnect và reset;
- late join;
- session closed;
- WebGL context loss;
- asset load/checksum failure.

Tài liệu LiveKit dùng để chốt transport và backpressure:

- [LiveKit data tracks](https://docs.livekit.io/transport/data/data-tracks/);
- [LiveKit data packets](https://docs.livekit.io/transport/data/packets/).

Phase feasibility phải kiểm tra API thực tế của LiveKit server, Python SDK và JavaScript SDK đã
được pin trước khi chọn data track hay lossy data packet cho production.

## 9. Custom character data model

Các bảng dự kiến trong giai đoạn 2:

```text
characters
character_shares
character_artifacts
character_build_jobs
character_build_events
model_artifacts
```

Character artifact không chứa neural checkpoint dùng chung. Artifact lưu provenance:

```text
generator_model_id
generator_revision
input_checksum
artifact_checksum
build_job_id
```

Build job cần:

```text
idempotency_key
lease_owner
lease_token
lease_expires_at
attempt
max_attempts
cancel_requested_at
failure_code
failure_detail
```

Update từ builder phải kèm fencing token để job process cũ không ghi đè kết quả sau khi lease đã
được cấp lại.

## 10. Trình tự triển khai

### Phase 0: feasibility

- Pin upstream source, model và renderer revisions.
- Review code/weight/package license.
- Build A2E experimental GPU image.
- Inspect default ZIP manifest.
- Benchmark load, warmup, inference, VRAM và output `[T, 52]`.
- Xác nhận LiveKit data API tương thích với server/Python/JavaScript SDK được pin.

### Phase 1A: avatar domain compatibility

- `AvatarOutputKind`, `RenderLocation`, `AvatarTransportKind`.
- `TimedMotionFrame`, `AvatarRenderRequest`.
- `AvatarSessionRuntimePort` và output sink.
- Recorded runtime compatibility adapter.
- Generalize completion từ video-specific sang avatar-output completion.

### Phase 1B: runtime and presentation resolution

- Truyền `AvatarRuntimeSpec` Core → Control → Redis → Master → Child.
- Trả `PresentationSpec` cho browser.
- Seed default recorded và LAM character.
- Typed compatibility/admission errors.

### Phase 1C: static client renderer

- Frontend build pipeline và pinned LAM renderer package.
- Default `avatar.zip`.
- Fake/static ARKit52.
- State mapping, cleanup và WebGL fallback.

### Phase 1D: realtime transport

- Fake motion generator có PTS/sequence/generation.
- LiveKit data transport.
- `ExpressionBuffer` và `AudioPlaybackClock`.
- Loss, reconnect, restart, late-join tests.

### Phase 1E: LAM-A2E

- Stateful 48 kHz → 16 kHz resampler.
- Rolling context, overlap/dedup và async output.
- Warmup, flush và deterministic close.
- Idle/thinking silence policy.
- Optional thinking voice prompt.
- A/V drift và first-motion benchmark.

### Phase 2A: character resources

- Character DB entity, ACL và shares.
- Artifact/version repositories.
- Object storage và signed access.

### Phase 2B: builder worker

- `avatar-builder` worker capability.
- Build scheduler, lease, fencing và retry.
- FLAME, pretrained LAM, export, validation và upload.

### Phase 2C: product lifecycle

- Upload/build/progress UI.
- Rebuild/version selection.
- Quota, cancellation, deletion và orphan cleanup.
- Consent and character-source policy.

## 11. Acceptance criteria

Giai đoạn 1 chỉ hoàn thành khi:

- Recorded character hiện tại vẫn chạy ở aiortc và LiveKit mode được support.
- User chọn được system LAM character.
- Browser render được default `avatar.zip`.
- VieNeu audio thay thế hoàn toàn debug beep trong normal path.
- Cùng audio timeline cấp LiveKit AudioTrack và A2E.
- `idle` và `thinking` duy trì continuous silence/lip-sync clock.
- Thinking prompt chỉ phát theo policy và không chồng với agent answer.
- Kết thúc answer thì motion flush, state trở về idle và không kẹt speaking.
- Hai browser hợp lệ xem cùng session nhận cùng nội dung audio/motion.
- Reconnect hoặc worker restart không render stale generation.
- Session close drain/stop track và giải phóng Child/CUDA resources.
- Master không tạo CUDA context.
- Unit, contract và E2E tests pass.

Giai đoạn 2 chỉ hoàn thành khi:

- User upload portrait và theo dõi build progress.
- Job retry không tạo artifact version trùng hoặc ghi đè artifact READY.
- Character READY có thể được chọn và chạy qua đúng runtime path của system character.
- Owner/public/shared authorization được áp dụng cả lúc chọn character và lúc cấp asset URL.
- Session pin chính xác character artifact version.
- Delete/rebuild không phá session đang chạy.

## 12. Metrics

Realtime metrics:

```text
time_to_first_motion_ms
a2e_inference_ms
a2e_window_ms
motion_frames_generated_total
motion_frames_dropped_total
motion_queue_depth
audio_motion_drift_ms
thinking_prompt_total
thinking_prompt_cancelled_total
thinking_prompt_overlap_prevented_total
browser_render_fps
browser_webgl_context_loss_total
```

Builder metrics:

```text
build_queue_wait_seconds
flame_fitting_seconds
lam_reconstruction_seconds
artifact_export_seconds
artifact_size_bytes
build_attempt_total
build_failure_total
```

Mọi log/event phải mang tối thiểu `trace_id`, `session_id`, `turn_id`, `utterance_id`,
`character_id`, `runtime_revision` và `generation` khi các giá trị tương ứng tồn tại.

## 13. Reference index

| Thành phần | Vai trò trong PersonaForge | Nguồn chính thức |
|---|---|---|
| LAM | Build/reconstruct character asset từ portrait | [GitHub](https://github.com/aigc3d/LAM), [project page](https://aigc3d.github.io/projects/LAM/), [LAM-20K](https://huggingface.co/3DAIGC/LAM-20K) |
| LAM-A2E | Audio 16 kHz thành ARKit facial motion | [GitHub](https://github.com/aigc3d/LAM_Audio2Expression), [model artifacts](https://huggingface.co/3DAIGC/LAM_audio2exp) |
| LAM WebRender | Render LAM ZIP và expression tại browser | [GitHub](https://github.com/aigc3d/LAM_WebRender), [NPM package](https://www.npmjs.com/package/gaussian-splat-renderer-for-lam) |
| LiveKit data tracks | Continuous low-latency motion transport candidate | [Documentation](https://docs.livekit.io/transport/data/data-tracks/) |
| LiveKit data packets | Topic-based lossy/reliable PoC transport | [Documentation](https://docs.livekit.io/transport/data/packets/) |

Các link trên là reference để review và pin dependency. Trước mỗi integration milestone cần ghi
lại commit SHA, package version, model revision, license và checksum thực tế vào implementation
manifest; URL repository không thay thế version pin.
