# Pipeline Avatar Chat WebRTC hiện tại

Tài liệu này là bản tổng hợp đầy đủ về giải pháp đang được triển khai trong repository:
kiến trúc source code, luồng end-to-end, state machine, hai media runtime, giao thức giữa
Core và Render Worker, cách giữ FPS/A-V sync, cấu hình, vận hành, debug, giới hạn và hướng
mở rộng model thật.

> Phạm vi hiện tại: người dùng chat với một nhân vật ảo; hệ thống stream các video thu sẵn
> `idle`, `thinking`, `speaking` qua WebRTC. Audio hiện là PCM im lặng, có thể bật beep để
> kiểm tra đồng bộ. Model sinh avatar và TTS thật chưa được tích hợp.

## 1. Kết quả mà pipeline đang cung cấp

Pipeline hiện giải quyết được các yêu cầu sau:

- Browser tạo một avatar session qua REST.
- Browser duy trì kênh chat hai chiều qua WebSocket.
- Trạng thái avatar chuyển theo vòng đời `idle → thinking → speaking → idle`.
- Text trả lời được stream theo từng chunk để giao diện hiển thị dần.
- Video được phát liên tục và đổi nội dung theo trạng thái mà không tạo lại UI session.
- Mỗi video frame có số thứ tự sáu chữ số ở góc trái để phát hiện stream bị đứng.
- Một audio track 48 kHz được gửi liên tục, sẵn sàng để thay bằng TTS.
- Chế độ debug tạo beep 1 kHz và flash trắng trên video cùng một timeline.
- Có hai cách triển khai media:
  - `aiortc`: API process trực tiếp làm WebRTC peer và phát media.
  - `livekit`: Render Worker publish media vào LiveKit SFU, API chỉ điều phối.
- Core không import FastAPI, aiortc, AV, HTTPX hoặc SQLAlchemy.
- Chat provider, database, media runtime và renderer có thể thay qua port/adapter.

## 2. Kiến trúc tổng thể

### 2.1. Chế độ LiveKit Worker — kiến trúc khuyến nghị

```mermaid
flowchart LR
    U[Browser]
    API[avatar-chat\nFastAPI + application core]
    R[(Redis Streams)]
    W[avatar-worker\nrender + A/V publisher]
    SFU[LiveKit SFU]
    DB[(Memory / SQL database)]
    LLM[Scripted / HTTP model service]

    U -->|REST: session/media access| API
    U <-->|WebSocket: chat/events| API
    API <-->|session + messages| DB
    API -->|stream prompt/history| LLM
    LLM -->|reply chunks| API
    API -->|start/state/stop commands| R
    R -->|consumer group| W
    W -->|WebRTC publish audio + video| SFU
    SFU -->|WebRTC subscribe audio + video| U
```

Điểm quan trọng nhất là control plane và media plane đã được tách:

| Plane | Thành phần | Nhiệm vụ |
|---|---|---|
| Control plane | Browser, FastAPI, Redis, model adapter | Session, chat, state, token, command |
| Media plane | Render Worker, LiveKit SFU, browser | Decode/render, pacing, encode, RTP/WebRTC |

Model/render nặng chạy trong `avatar-worker`, vì vậy không block event loop xử lý REST và
WebSocket của Core API. Có thể scale worker theo GPU trong tương lai mà không thay domain.

### 2.2. Chế độ aiortc — đơn giản cho local/MVP

```mermaid
flowchart LR
    U[Browser]
    API[avatar-chat FastAPI]
    CORE[Application core]
    AIO[AiortcAvatarStreamAdapter]
    CLIP[Recorded clips]

    U -->|REST + WebSocket| API
    API --> CORE
    U <-->|SDP + ICE + WebRTC| AIO
    CORE -->|change_state| AIO
    CLIP --> AIO
```

Chế độ này ít service hơn, thuận tiện để phát triển local. Đổi lại, decode/encode và network
media chạy chung process với API, khó scale ngang và dễ cạnh tranh CPU với chat.

### 2.3. Hướng dependency

```mermaid
flowchart TB
    IN[Inbound adapters\nFastAPI + frontend]
    APP[Application\nuse cases + ports + DTO]
    DOMAIN[Domain\nentities + state rules]
    OUT[Outbound adapters\naiortc / Redis / LiveKit token / DB / HTTP model]
    BOOT[Bootstrap\nsettings + dependency wiring]

    IN --> APP
    APP --> DOMAIN
    OUT -. implements ports .-> APP
    BOOT --> IN
    BOOT --> APP
    BOOT --> OUT
```

Quy tắc được kiểm tra trong `tests/test_architecture.py`: `domain` và `application` không
được import framework hoặc adapter. Khi thay hệ thống bên ngoài, phần lớn công việc là viết
adapter implement `Protocol`, sau đó wire adapter trong `bootstrap/container.py`.

## 3. Bản đồ source code

```text
src/
├── avatar_chat/
│   ├── domain/
│   │   ├── models.py                 # Entity, enum, state transition
│   │   └── errors.py                 # Domain errors
│   ├── application/
│   │   ├── dto.py                    # DTO đi qua boundary
│   │   ├── ports.py                  # Interface/Protocol
│   │   └── use_cases.py              # Business orchestration
│   ├── adapters/
│   │   ├── inbound/
│   │   │   └── fastapi_app.py        # REST, WS, static frontend
│   │   └── outbound/
│   │       ├── memory.py             # Repository trong RAM
│   │       ├── sqlalchemy_repositories.py
│   │       ├── replies.py            # Scripted + delay decorator
│   │       ├── http_reply.py          # SSE/NDJSON model adapter
│   │       └── media/
│   │           ├── aiortc_stream.py
│   │           ├── livekit_room.py
│   │           ├── redis_render.py
│   │           ├── switchable_track.py
│   │           ├── silent_audio_track.py
│   │           ├── media_clock.py
│   │           └── recorded_source.py
│   ├── bootstrap/
│   │   ├── settings.py               # Env → typed settings
│   │   └── container.py              # Composition root
│   └── main.py                       # ASGI entrypoint
└── avatar_worker/
    ├── main.py                       # Worker entrypoint
    ├── settings.py                   # Worker env settings
    ├── consumer.py                   # Redis consumer + session manager
    ├── livekit_session.py            # Stable A/V publisher
    └── recorded_frames.py            # Loop/switch/decode clips

frontend/
├── index.html
├── app.js                            # Session, aiortc/LiveKit, chat UI
└── styles.css

assets/characters/default/
├── idle.mp4
├── thinking.mp4
└── speaking.mp4

compose.yaml                          # API, worker, Redis, LiveKit
Dockerfile                            # Python + FFmpeg + generated clips
.env.example                         # Toàn bộ biến cấu hình mẫu
tests/                               # Unit, contract, integration, architecture
```

## 4. Domain và state machine

`AvatarSession` là domain entity giữ riêng lifecycle của media và playback state. Tách hai
khái niệm này tránh các transition mơ hồ như dùng cùng một enum cho `created` và `speaking`.

```mermaid
stateDiagram-v2
    state lifecycle {
        [*] --> CREATED: POST /api/sessions
        CREATED --> MEDIA_STARTING: provision/negotiate
        MEDIA_STARTING --> READY: media thực sự sẵn sàng
        MEDIA_STARTING --> FAILED: timeout/lỗi dependency
        FAILED --> MEDIA_STARTING: retry provision
        CREATED --> CLOSED: close
        READY --> CLOSED: close
        FAILED --> CLOSED: close
    }
    state playback {
        [*] --> IDLE
        IDLE --> THINKING: nhận user message
        THINKING --> SPEAKING: bắt đầu reply stream
        THINKING --> IDLE: lỗi/cleanup
        SPEAKING --> IDLE: hoàn tất/lỗi
    }
```

Các invariant chính:

- Playback chỉ được đổi khi lifecycle là `READY`.
- Provision idempotent: session đã `READY` chỉ cấp client token mới, không restart worker.
- `MEDIA_STARTING` từ request khác trả conflict thay vì tạo hai publisher.
- Repository dùng `version` làm optimistic concurrency control; stale update trả conflict.
- `close()` idempotent: gọi nhiều lần vẫn giữ `CLOSED`.
- Một `asyncio.Lock` theo `session_id` ngăn hai message chạy đồng thời.
- Lock được bỏ khỏi lock map sau khi xử lý để không tăng bộ nhớ theo số session lịch sử.
- Message thứ hai khi session đang xử lý trả domain error `SessionBusy`.
- Nếu model/reply stream lỗi, use case cố gắng khôi phục cả domain và renderer về `idle`.

## 5. Các application port

| Port | Trách nhiệm | Adapter hiện có |
|---|---|---|
| `SessionRepository` | Lưu/đọc/xóa session | In-memory, SQLAlchemy |
| `ConversationRepository` | Append và lấy lịch sử chat | In-memory, SQLAlchemy |
| `AvatarStreamPort` | Start, negotiate, đổi state, close media | aiortc, Redis command adapter |
| `MediaRoomPort` | Provision room/token và close room | LiveKit token adapter |
| `ReplyGeneratorPort` | Stream reply chunk | Scripted, HTTP SSE/NDJSON, delay decorator |
| `IdGenerator` | Sinh ID | UUID |
| `Clock` | Cấp thời gian | System clock |

Các use case độc lập framework:

- `CreateSession`
- `NegotiateStream`
- `ProvisionMedia`
- `HandleMessage`
- `GetConversation`
- `CloseSession`

### 5.1. Cách thay dependency bên ngoài

Ví dụ thay model chat:

1. Tạo class implement `ReplyGeneratorPort.stream_reply()`.
2. Chuyển response của provider thành `ReplyChunk`.
3. Không đưa SDK/provider type vào application DTO.
4. Wire adapter trong `build_container()` dựa trên một setting mới.
5. Thêm contract test cho adapter và unit test cho wiring.

Ví dụ thay renderer video:

1. Giữ `AvatarStreamPort` ở Core.
2. Adapter control plane chỉ gửi command/state tới renderer.
3. Renderer mới tự publish một audio track và một video track ổn định vào SFU.
4. Không để Core import CUDA, Torch, model SDK hoặc LiveKit RTC SDK.

## 6. Luồng end-to-end ở chế độ LiveKit

### 6.1. Khởi tạo session và media

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant A as avatar-chat API
    participant C as Application Core
    participant R as Redis Streams
    participant W as avatar-worker
    participant L as LiveKit SFU

    B->>A: POST /api/sessions
    A->>C: CreateSession.execute(default)
    C-->>A: session_id, state=created
    A-->>B: URLs + media_provider=livekit
    B->>A: WS /api/sessions/{id}/chat
    B->>A: POST /api/sessions/{id}/media/access
    A->>C: ProvisionMedia.execute(id)
    C->>C: Persist MEDIA_STARTING (optimistic version)
    C->>C: Tạo client/publisher token
    C->>R: avatar.session.start(command_id)
    R-->>W: start command
    W->>L: Connect bằng publisher token
    W->>L: Publish avatar-audio + avatar-video
    W->>R: avatar.session.ready(command_id)
    R-->>C: correlated ready event
    C->>C: Persist READY
    C->>R: avatar.state.set(idle, state_version)
    C-->>A: client MediaAccess
    A-->>B: server_url + client token + room
    B->>L: Connect bằng subscriber token
    L-->>B: Subscribe audio + video WebRTC
```

Token được tách theo least privilege:

| Identity | TTL | Publish | Subscribe |
|---|---:|---:|---:|
| `worker-{session_id}` | 1 giờ | Có | Không |
| `client-{session_id}` | 30 phút | Không | Có |

Room có tên `avatar-{session_id}`. Publisher token được gửi cho worker trong start command;
client chỉ nhận subscriber token qua endpoint media access.

### 6.2. Luồng xử lý một tin nhắn

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant A as FastAPI WebSocket
    participant H as HandleMessage
    participant DB as Conversation repo
    participant R as Redis Stream
    participant W as Render Worker
    participant M as Reply provider

    B->>A: {type:user_message, content:...}
    A->>H: execute(session_id, content)
    H->>DB: append user message
    H->>R: avatar.state.set(thinking)
    H-->>B: avatar_state=thinking
    H->>H: đợi THINKING_MIN_DURATION
    H->>DB: list_recent(limit=30)
    H->>R: avatar.state.set(speaking)
    H-->>B: avatar_state=speaking
    H->>M: stream_reply(history)
    loop từng chunk
        M-->>H: ReplyChunk
        H-->>B: assistant_chunk
    end
    H->>DB: append assistant full reply
    H->>R: avatar.state.set(idle)
    H-->>B: avatar_state=idle
    H-->>B: assistant_complete
```

`THINKING_MIN_DURATION` đảm bảo clip thinking có thời gian xuất hiện. `REPLY_CHUNK_DELAY`
là decorator áp dụng cho mọi reply provider, giúp kéo dài trạng thái speaking trong demo.

### 6.3. Đóng session

Khi browser đóng WebSocket hoặc gọi `DELETE /api/sessions/{id}`:

1. Domain session chuyển sang `closed` nếu còn tồn tại.
2. `RedisAvatarStreamAdapter` gửi `avatar.session.stop`.
3. Worker đặt stop event, chờ render task tối đa 5 giây.
4. Nếu task chưa dừng, worker cancel task.
5. LiveKit publisher disconnect.
6. Room tự biến mất sau khi participant cuối cùng rời room.

`LiveKitMediaRoomAdapter.close()` hiện chưa gọi RoomService để xóa room cưỡng bức.

## 7. API và WebSocket contract

### 7.1. Health check

```http
GET /api/health
```

```json
{"status":"ok"}
```

Endpoint này mới kiểm tra process API, chưa kiểm tra Redis, worker hoặc LiveKit readiness.

### 7.2. Tạo session

```http
POST /api/sessions
Content-Type: application/json

{"character_id":"default"}
```

Response:

```json
{
  "session_id": "<uuid>",
  "character_id": "default",
  "offer_url": "/api/sessions/<uuid>/webrtc/offer",
  "media_provider": "livekit",
  "media_access_url": "/api/sessions/<uuid>/media/access",
  "chat_url": "/api/sessions/<uuid>/chat"
}
```

Trong aiortc mode, `media_access_url` là `null`; frontend dùng `offer_url`.

### 7.3. Provision LiveKit media

```http
POST /api/sessions/{session_id}/media/access
```

```json
{
  "provider": "livekit",
  "server_url": "ws://server:7880",
  "access_token": "<subscriber JWT>",
  "room_name": "avatar-<session_id>",
  "avatar_state": "idle"
}
```

Gọi endpoint này trong aiortc mode trả `409`.

### 7.4. aiortc SDP signaling

```http
POST /api/sessions/{session_id}/webrtc/offer
Content-Type: application/json

{"sdp":"...","type":"offer"}
```

Response chứa SDP answer và `avatar_state=idle`. Gọi endpoint trong LiveKit mode trả `409`.

### 7.5. Chat WebSocket

Client gửi:

```json
{"type":"user_message","content":"Xin chào"}
```

Server có thể gửi:

```json
{"type":"avatar_state","content":null,"avatar_state":"thinking"}
{"type":"avatar_state","content":null,"avatar_state":"speaking"}
{"type":"assistant_chunk","content":"Xin ","avatar_state":null}
{"type":"assistant_chunk","content":"chào!","avatar_state":null}
{"type":"avatar_state","content":null,"avatar_state":"idle"}
{"type":"assistant_complete","content":"Xin chào!","avatar_state":null}
```

Domain error được trả dưới dạng:

```json
{"type":"error","detail":"..."}
```

### 7.6. Lịch sử và cleanup

```http
GET /api/sessions/{session_id}/messages?limit=50
DELETE /api/sessions/{session_id}
```

`limit` bị giới hạn tối đa 100 ở inbound adapter.

## 8. Redis command contract

Mỗi worker có stream riêng:

```text
avatar:commands:{worker_id}
```

Consumer group:

```text
avatar-render-workers
```

Redis entry có field `data`, bên trong là JSON:

```json
{
  "schema_version": 1,
  "type": "avatar.state.set",
  "command_id": "<uuid>",
  "session_id": "<uuid>",
  "created_at": "2026-08-02T15:09:03+00:00",
  "payload": {"state":"speaking", "state_version":4}
}
```

Ba command hiện được hỗ trợ:

### 8.1. `avatar.session.start`

```json
{
  "schema_version": 1,
  "type": "avatar.session.start",
  "command_id": "<uuid>",
  "session_id": "<uuid>",
  "created_at": "<UTC ISO-8601>",
  "payload": {
    "character_id": "default",
    "room_name": "avatar-<uuid>",
    "livekit_url": "ws://livekit:7880",
    "publisher_token": "<JWT>"
  }
}
```

Nếu cùng `session_id` đã tồn tại, manager dừng session cũ trước khi tạo session mới.

### 8.2. `avatar.state.set`

```json
{
  "type": "avatar.state.set",
  "session_id": "<uuid>",
  "payload": {"state":"idle|thinking|speaking", "state_version":5}
}
```

Worker chỉ cho phép ba render state trên. Worker bỏ qua command có `state_version` thấp hơn
version đã áp dụng, nhờ đó retry hoặc delivery lệch thứ tự không làm avatar quay về state cũ.
State command cho session chưa tồn tại bị lỗi.

### 8.3. `avatar.session.stop`

```json
{
  "type": "avatar.session.stop",
  "session_id": "<uuid>",
  "payload": {}
}
```

Stop idempotent nếu session không tồn tại.

### 8.4. Retry, ACK và heartbeat

- Worker đọc tối đa 10 message/lần, block tối đa 1 giây.
- Mỗi message được thử tối đa 3 lần.
- Backoff lần lượt khoảng 250 ms và 500 ms.
- Chỉ `XACK` sau khi dispatch thành công.
- `command_id` được lưu 24 giờ; command đã xử lý được ACK mà không dispatch lại.
- Message lỗi sau lần thứ ba vẫn nằm trong Pending Entries List.
- Hiện chưa có dead-letter stream và chưa có job reclaim pending message.
- Heartbeat key: `avatar:worker:{worker_id}`.
- Heartbeat cập nhật mỗi 5 giây, TTL 15 giây.
- Payload gồm `worker_id`, `status=ready`, `active_sessions`.

Heartbeat hiện phục vụ quan sát. Với lệnh start, API chờ tối đa 15 giây cho event
`avatar.session.ready` có cùng `command_id`; worker chỉ phát event này sau khi publish đủ hai
track. Timeout/lỗi đưa lifecycle về `FAILED` và cleanup room/renderer.

## 9. Render Worker và LiveKit media pipeline

### 9.1. Một publisher ổn định cho mỗi session

`LiveKitRenderSession` tạo đúng một room connection, một local audio track và một local video
track cho toàn bộ vòng đời session. Khi trạng thái đổi, nguồn frame đổi nhưng track identity
không đổi. Browser không phải renegotiate hoặc tạo lại video element.

Track publish:

| Track | Tên | Source | Cấu hình mặc định |
|---|---|---|---|
| Audio | `avatar-audio` | microphone | PCM mono, 48 kHz, 20 ms/frame |
| Video | `avatar-video` | camera | 640×360, 25 FPS, max 1.5 Mbps |

Video được publish với `simulcast=false` explicit. LiveKit vì vậy chỉ nhận một layer có đúng
`WORKER_VIDEO_WIDTH × WORKER_VIDEO_HEIGHT`; resolution không bị hạ sau frame đầu bởi
adaptive layer selection. Khi cần adaptive bitrate production, có thể bật simulcast trở lại
nhưng UI phải chấp nhận resolution thực tế thay đổi theo bandwidth hoặc kích thước phần tử.

LiveKit/WebRTC chịu trách nhiệm encode, packetization, congestion control và chuyển tiếp RTP.

### 9.2. Timeline làm audio master clock

Worker không dựa vào FPS gốc của file video để quyết định cadence outbound. Nó tạo một
content timeline mới:

```text
audio_time = audio_sample_index / audio_sample_rate
video_time = video_frame_number / configured_video_fps
```

Với mặc định:

```text
audio_samples_per_frame = 48000 × 0.02 = 960 samples
audio cadence           = 50 frames/second
video cadence           = 25 frames/second
video frame duration    = 40 ms
```

Vòng lặp:

1. Tính thời điểm bắt đầu audio frame hiện tại.
2. Push mọi video frame có timestamp nhỏ hơn hoặc bằng thời điểm đó.
3. Sinh/push audio frame 960 sample.
4. Tăng audio sample counter.
5. `rtc.AVSynchronizer` pace frame theo timestamp và FPS cấu hình.

Đây là lý do FPS không phụ thuộc tốc độ decode loop và audio/video dùng cùng timeline số học,
tránh việc lấy hai `time.monotonic()` độc lập rồi drift dần.

### 9.3. Buffer và backpressure

- `AudioSource` dùng queue 50 ms.
- `AVSynchronizer` dùng `WORKER_VIDEO_QUEUE_MS`, mặc định 80 ms.
- Queue hữu hạn tạo backpressure khi publisher/network chậm.
- Không nên tăng queue quá lớn chỉ để che tải render, vì latency chat-to-avatar sẽ tăng.
- Model thật nên render trước một buffer nhỏ hoặc drop frame theo policy, không block control
  plane.

### 9.4. Chuyển clip và xử lý EOF

`LoopingRecordedFrameSource`:

1. Khi state thay đổi, đóng AV container cũ.
2. Mở clip tương ứng và tạo decoder mới.
3. Decode frame kế tiếp.
4. Reformat về kích thước worker và `yuv420p`.
5. Khi EOF, mở lại chính clip đó và tiếp tục.
6. Vẽ frame counter và optional sync flash.

Vì outbound LiveKit track không đổi, đổi `idle/thinking/speaking` không làm stream dừng.

### 9.5. Chỉ số runtime

Mỗi 5 giây worker log:

```text
Media cadence session=<id> fps=25.00 av_drift_ms=-23.50
```

- `fps`: FPS thực tế do synchronizer quan sát.
- `av_drift_ms = last_video_time - last_audio_time`.
- Drift âm nhỏ khoảng một phần video frame là bình thường vì audio là master và video được
  push tới mốc gần nhất.
- Cần cảnh báo nếu FPS thấp liên tục hoặc trị tuyệt đối drift tăng không giới hạn.

## 10. Chế độ aiortc chi tiết

### 10.1. SDP và peer lifecycle

`AiortcAvatarStreamAdapter.negotiate()`:

1. Đóng peer cũ cùng `session_id` nếu có.
2. Tạo `RTCPeerConnection` với STUN/TURN config.
3. Tạo một shared `MediaClock`.
4. Tạo `SwitchableAvatarVideoTrack` và `SilentAudioTrack` dùng chung clock.
5. Khởi tạo video ở state `idle`.
6. Add cả hai track vào peer.
7. Apply remote offer, tạo local answer và trả SDP.
8. Cleanup khi peer `failed` hoặc `closed`.

### 10.2. Stable switchable video track

Khi đổi state:

- Mở `MediaPlayer(loop=True)` cho clip mới.
- Đọc trước frame đầu tiên để phát hiện clip hỏng trước khi swap.
- Tăng `generation` để các read cũ biết mình đã lỗi thời.
- Read đang chạy trên source cũ được theo dõi bằng reference count.
- Source cũ chỉ bị stop khi không còn active read.
- Nếu gặp EOF ngoài dự kiến, track tự mở lại clip hiện tại.
- PTS luôn monotonic: `max(clock_pts, last_pts + 1)` trên time base 90 kHz.

Cơ chế generation + retired source xử lý race condition khi chat đổi trạng thái đúng lúc
`recv()` đang đọc frame, là lỗi từng khiến video đứng khi `idle → speaking`.

### 10.3. Audio track

`SilentAudioTrack` phát:

- PCM signed 16-bit.
- Mono.
- 48.000 sample/giây.
- 960 sample/frame.
- 20 ms/frame.
- PTS trên time base `1/48000`.

Ngoài cửa sổ debug, sample bằng 0. Track vẫn chạy liên tục để transceiver/audio clock không bị
ngắt khi avatar không nói.

## 11. A/V sync debug và frame counter

### 11.1. Frame counter

Mỗi outbound frame được vẽ số dạng:

```text
000001, 000002, 000003, ...
```

Counter nằm ở góc trái, vẽ trực tiếp vào luma plane YUV để không phụ thuộc OpenCV/Pillow.
Nếu số không tăng, vấn đề có thể nằm ở decoder, renderer, publisher, SFU subscription hoặc
browser playback.

### 11.2. Beep + flash

Khi `AV_SYNC_DEBUG=true`, cứ mỗi `AV_SYNC_INTERVAL` giây:

- Audio phát sine beep 1 kHz trong `AV_SYNC_PULSE_DURATION`.
- Video hiển thị ô trắng 64×64 ở góc phải trong cùng cửa sổ timeline.

Mặc định:

```text
interval = 2.0 giây
duration = 0.1 giây
```

Browser mở video ở trạng thái muted vì autoplay policy. Người dùng phải bấm **Bật âm thanh
kiểm tra** để bỏ mute và nghe beep.

### 11.3. Cách kiểm tra sync

1. Bật audio trên UI.
2. Quan sát flash và nghe beep.
3. Chúng phải xuất hiện gần như đồng thời.
4. Đối chiếu log `av_drift_ms` của worker.
5. Có thể quay màn hình kèm system audio để đo frame-level chính xác hơn.

## 12. Frontend pipeline

`frontend/app.js` tự chạy `start()` khi trang tải:

1. `POST /api/sessions`.
2. Mở WebSocket chat cùng origin.
3. Chọn media client theo `session.media_provider`.
4. Chỉ enable input khi cả `chatReady` và `mediaReady` đều true.

Trong aiortc mode:

- Tạo `RTCPeerConnection`.
- Add audio/video recv-only transceiver.
- Tạo offer, chờ ICE gathering complete.
- POST offer và apply answer.
- Gom remote audio/video track vào một `MediaStream` gắn với `<video>`.

Trong LiveKit mode:

- Tải LiveKit UMD browser SDK từ jsDelivr.
- Gọi media access endpoint để provision worker/room/token.
- Tạo `Room({adaptiveStream: false, dynacast: false})` để subscriber không tự yêu cầu quality
  layer nhỏ hơn resolution đã cấu hình.
- Connect bằng subscriber token.
- Bắt `TrackSubscribed` và add media track vào video element.
- Hiển thị `ConnectionStateChanged` lên UI.

Khi unload, frontend đóng chat socket, aiortc peer và LiveKit room.

## 13. Reply/model pipeline

### 13.1. Scripted provider

`ScriptedReplyAdapter` lấy user message gần nhất, tạo câu trả lời demo và yield từng token.
`DelayedReplyAdapter` bọc provider để delay giữa các chunk.

### 13.2. HTTP streaming provider

Cấu hình:

```env
REPLY_PROVIDER=http
REPLY_HTTP_URL=http://model-service:9000/replies/stream
REPLY_HTTP_TOKEN=optional-token
```

Request:

```json
{
  "session_id": "<id>",
  "messages": [
    {"role":"user","content":"Xin chào"},
    {"role":"assistant","content":"..."}
  ]
}
```

Provider có thể trả NDJSON:

```text
{"text":"Xin "}
{"text":"chào"}
```

Hoặc SSE:

```text
data: {"text":"Xin "}
data: {"text":"chào"}
data: [DONE]
```

Adapter bỏ dòng trống/comment, xử lý prefix `data:`, dừng ở `[DONE]`, và dùng timeout tổng
60 giây/connect timeout 10 giây.

## 14. Persistence

### 14.1. In-memory

Khi `DATABASE_URL` rỗng:

- Session và conversation nằm trong dictionary của process.
- Repository dùng `asyncio.Lock` và `deepcopy` để tránh mutation ngoài lock.
- Dữ liệu mất khi restart.
- Không phù hợp nhiều API replica vì mỗi replica có state riêng.

### 14.2. SQLAlchemy

Khi có `DATABASE_URL`, composition root dùng repository SQLAlchemy async. Hai table:

- `avatar_sessions`
- `chat_messages`

Schema được `create_all()` khi API startup. Ví dụ:

```env
DATABASE_URL=sqlite+aiosqlite:///./avatar.db
```

Hoặc:

```env
DATABASE_URL=postgresql+asyncpg://avatar:avatar@postgres:5432/avatar
```

Schema session hiện dùng các cột `status`, `avatar_state` và `version`. `create_all()` không
thay đổi table đã tồn tại; database được tạo bởi phiên bản cũ (cột `state`) phải được migrate
hoặc reset trước khi chạy phiên bản này.

Để scale production cần migration tool thay cho `create_all()` và cần xử lý ownership/locking
session giữa nhiều API replica.

## 15. Cấu hình đầy đủ

### 15.1. API và storage

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `PROJECT_ROOT` | tự phát hiện | Root chứa frontend/assets; Docker đặt `/app` |
| `APP_HOST` | `0.0.0.0` | Bind address khi chạy entrypoint |
| `APP_PORT` | `8000` | Port Uvicorn trong process |
| `APP_HTTP_PORT` | `8000` | Port publish ra host trong Compose |
| `DATABASE_URL` | rỗng | Rỗng dùng memory; có giá trị dùng SQLAlchemy |

### 15.2. Reply provider

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `REPLY_PROVIDER` | `scripted` | `scripted` hoặc `http` |
| `REPLY_CHUNK_DELAY` | `0.25` | Delay giữa text chunk |
| `THINKING_MIN_DURATION` | `1.0` | Thời gian thinking tối thiểu |
| `REPLY_HTTP_URL` | rỗng | URL model streaming |
| `REPLY_HTTP_TOKEN` | rỗng | Bearer token tùy chọn |

### 15.3. Media selection và aiortc

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `MEDIA_PROVIDER` | `aiortc` | `aiortc` hoặc `livekit` |
| `WEBRTC_ICE_SERVERS` | rỗng | Danh sách STUN/TURN URL, phân cách dấu phẩy |
| `WEBRTC_ICE_USERNAME` | rỗng | TURN username |
| `WEBRTC_ICE_CREDENTIAL` | rỗng | TURN credential |

### 15.4. LiveKit, Redis và worker affinity

| Biến | Mặc định mẫu | Ý nghĩa |
|---|---|---|
| `LIVEKIT_URL` | `ws://localhost:7880` | URL browser/public dùng connect SFU |
| `LIVEKIT_WORKER_URL` | `ws://livekit:7880` | URL worker nội bộ Docker |
| `LIVEKIT_API_KEY` | `devkey` | Ký JWT, chỉ dành local |
| `LIVEKIT_API_SECRET` | `secret` | Ký JWT, phải đổi production |
| `LIVEKIT_HTTP_PORT` | `7880` | Signaling/API host port |
| `LIVEKIT_RTC_TCP_PORT` | `7881` | WebRTC TCP host và container port |
| `LIVEKIT_RTC_UDP_PORT` | `7882` | WebRTC UDP host và container port |
| `LIVEKIT_NODE_IP` | rỗng | IP SFU quảng bá cho browser ngoài host |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection |
| `RENDER_WORKER_ID` | `avatar-worker-1` | Stream/worker affinity |
| `WORKER_ID` | từ `RENDER_WORKER_ID` | ID worker process |

RTC TCP/UDP target port trong container được cấu hình trùng host port. Điều này quan trọng:
SFU phải quảng bá đúng cặp `LIVEKIT_NODE_IP:LIVEKIT_RTC_*_PORT`; chỉ map host port sang một
container port khác mà không đổi advertised candidate sẽ làm signaling thành công nhưng media
ICE timeout.

### 15.5. Worker media

| Biến | Mặc định | Ý nghĩa |
|---|---:|---|
| `WORKER_VIDEO_WIDTH` | `640` | Chiều rộng outbound |
| `WORKER_VIDEO_HEIGHT` | `360` | Chiều cao outbound |
| `WORKER_VIDEO_FPS` | `25` | Cadence và max framerate |
| `WORKER_VIDEO_BITRATE` | `1500000` | Max video bitrate bit/s |
| `WORKER_AUDIO_SAMPLE_RATE` | `48000` | Audio sample rate |
| `WORKER_AUDIO_FRAME_DURATION` | `0.02` | Audio frame duration giây |
| `WORKER_VIDEO_QUEUE_MS` | `80` | Video queue của synchronizer |

### 15.6. Debug và clip

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `AV_SYNC_DEBUG` | `true` | Bật beep + flash |
| `AV_SYNC_INTERVAL` | `2.0` | Chu kỳ pulse |
| `AV_SYNC_PULSE_DURATION` | `0.1` | Độ dài pulse |
| `AVATAR_IDLE_VIDEO` | asset mặc định | Clip idle |
| `AVATAR_THINKING_VIDEO` | asset mặc định | Clip thinking |
| `AVATAR_SPEAKING_VIDEO` | asset mặc định | Clip speaking |

## 16. Runbook khởi động pipeline đầy đủ

### 16.1. Chuẩn bị `.env`

```bash
cd /home/noname/dev/webrtc-dev
cp .env.example .env
```

Cấu hình tối thiểu cho LiveKit khi browser chạy cùng máy:

```env
APP_HTTP_PORT=18000
MEDIA_PROVIDER=livekit
LIVEKIT_URL=ws://localhost:17880
LIVEKIT_WORKER_URL=ws://livekit:7880
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=secret
LIVEKIT_HTTP_PORT=17880
LIVEKIT_RTC_TCP_PORT=17881
LIVEKIT_RTC_UDP_PORT=17882
REDIS_URL=redis://redis:6379/0
RENDER_WORKER_ID=avatar-worker-1
```

Nếu browser ở máy khác, lấy IP LAN của server:

```bash
hostname -I
```

Ví dụ server IP là `192.168.20.166`:

```env
LIVEKIT_URL=ws://192.168.20.166:17880
LIVEKIT_NODE_IP=192.168.20.166
```

Không đổi `LIVEKIT_WORKER_URL=ws://livekit:7880`, vì worker dùng Docker DNS và cổng HTTP
nội bộ của LiveKit.

### 16.2. Start

```bash
docker compose --profile livekit up --build -d
```

### 16.3. Kiểm tra service

```bash
docker compose --profile livekit ps
curl http://localhost:18000/api/health
```

Mong đợi bốn service running:

- `avatar-chat`
- `avatar-worker`
- `redis` — healthy
- `livekit`

### 16.4. Mở browser

Browser cùng host:

```text
http://localhost:18000
```

Browser khác host:

```text
http://192.168.20.166:18000
```

### 16.5. Kiểm tra end-to-end

1. UI hiển thị LiveKit `connected`.
2. Frame counter tăng liên tục.
3. Gửi chat thấy `thinking → speaking → idle`.
4. Text assistant xuất hiện từng chunk.
5. Bật âm thanh và kiểm tra beep + flash.
6. Worker log gần FPS cấu hình và drift ổn định.
7. LiveKit log cho thấy client subscribe cả audio và video.

### 16.6. Logs

```bash
docker compose --profile livekit logs -f
docker compose --profile livekit logs -f avatar-chat
docker compose --profile livekit logs -f avatar-worker
docker compose --profile livekit logs -f livekit
```

### 16.7. Restart/rebuild/stop

```bash
docker compose --profile livekit restart
docker compose --profile livekit up --build -d
docker compose --profile livekit down
```

## 17. Chạy aiortc local

### 17.1. Python environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
bash scripts/generate_demo_videos.sh
MEDIA_PROVIDER=aiortc uvicorn avatar_chat.main:app --host 0.0.0.0 --port 8000
```

Mở `http://localhost:8000`.

### 17.2. Docker aiortc

```bash
MEDIA_PROVIDER=aiortc APP_HTTP_PORT=18000 docker compose up --build -d avatar-chat
```

Không cần Redis, worker hoặc LiveKit profile.

## 18. Network, ICE và firewall

LiveKit mode cần browser truy cập:

| Port ví dụ | Protocol | Mục đích |
|---:|---|---|
| `18000` | TCP/HTTP | Frontend, REST, chat WebSocket |
| `17880` | TCP/HTTP+WS | LiveKit signaling |
| `17881` | TCP | WebRTC TCP fallback |
| `17882` | UDP | WebRTC media ưu tiên |

Triệu chứng signaling đúng nhưng không có video thường là:

- Browser join room nhưng ICE không thành `connected`.
- SFU quảng bá container IP như `172.x.x.x` hoặc Docker bridge IP.
- Host port khác advertised container port.
- UDP bị firewall/NAT chặn.

Giải pháp local/LAN:

- Set `LIVEKIT_NODE_IP` bằng IP browser truy cập được.
- Giữ RTC host port và container listen port giống nhau như `compose.yaml` hiện tại.
- Mở firewall TCP signaling, RTC TCP và RTC UDP.
- Đặt `LIVEKIT_URL` theo địa chỉ nhìn từ browser, không theo địa chỉ nhìn từ container.
- Đặt `LIVEKIT_WORKER_URL` theo Docker DNS, không theo địa chỉ browser.

Qua Internet production cần TLS (`https/wss`), domain, certificate, public IP đúng và TURN cho
network hạn chế. HTTP page từ remote origin cũng có thể gặp browser secure-context policy.

## 19. Troubleshooting

### 19.1. `address already in use`

Đổi host port trong `.env`:

```env
APP_HTTP_PORT=18000
LIVEKIT_HTTP_PORT=17880
LIVEKIT_RTC_TCP_PORT=17881
LIVEKIT_RTC_UDP_PORT=17882
```

Đồng thời cập nhật port trong `LIVEKIT_URL`.

### 19.2. UI vẫn “đang khởi tạo”

Kiểm tra:

- Browser console/network có tải được `/app.js` không.
- LiveKit SDK từ jsDelivr có tải được không.
- `/media/access` có trả 200 không.
- Connection label có chuyển `connected` không.
- Chat socket và media đều phải ready thì input mới được enable.

### 19.3. LiveKit connected nhưng không có video

```bash
docker compose --profile livekit logs avatar-worker
docker compose --profile livekit logs livekit
```

Tìm:

- `LiveKit media ready session=...`
- `Media cadence ... fps=...`
- `new participant joined` cho cả worker và client.
- `subscribedAudioCount: 1`.
- `subscribedVideoCount: 1`.
- ICE selected candidate dùng đúng `LIVEKIT_NODE_IP` và RTC port.

### 19.4. Video đứng khi đổi trạng thái

- Xem frame counter có tiếp tục tăng không.
- Xác nhận worker log `Avatar state session=... state=speaking`.
- Kiểm tra clip tồn tại và decode được bằng FFmpeg.
- Trong aiortc mode, xem log EOF/source failure.
- Không thay remote track khi đổi state; chỉ thay frame source phía sau stable track.

### 19.5. Speaking không trở về idle

- Xem reply provider có kết thúc stream hoặc gửi `[DONE]` không.
- Kiểm tra timeout/error của HTTP provider.
- `HandleMessage` có cleanup về idle cả success và exception; nếu UI sai nhưng renderer đúng,
  kiểm tra WebSocket event delivery.

### 19.6. Không nghe beep

- `AV_SYNC_DEBUG=true`.
- Bấm nút bật âm thanh vì autoplay mặc định muted.
- Kiểm tra browser/system volume.
- Xác nhận browser subscribe audio track.

### 19.7. Worker nhận state trước start

Worker sẽ log `Render session not found` và retry. Kiểm tra ordering trên worker-specific stream,
`state_version` và pending entries. API chỉ gửi state idle sau correlated ready acknowledgement,
nên lỗi này thường chỉ xuất hiện do command thủ công hoặc producer khác không theo protocol.

### 19.8. Inspect Redis

```bash
docker compose exec redis redis-cli GET avatar:worker:avatar-worker-1
docker compose exec redis redis-cli XLEN avatar:commands:avatar-worker-1
docker compose exec redis redis-cli XPENDING \
  avatar:commands:avatar-worker-1 avatar-render-workers
```

## 20. Failure behavior hiện tại

| Failure | Hành vi hiện tại | Khoảng trống |
|---|---|---|
| Reply provider lỗi | Use case restore idle, WS gửi lỗi | Chưa retry/circuit breaker |
| Clip EOF | Tự loop/reopen | Model frame source cần policy riêng |
| aiortc source đổi lúc recv | Generation loại frame cũ | Đã có unit/integration coverage |
| aiortc peer failed/closed | Cleanup peer/tracks | Chưa reconnect tự động phía server |
| Redis command lỗi | Retry 3 lần, không ACK | Chưa DLQ/reclaim/alert |
| Render start lỗi | Phát correlated failed event, Core đặt `FAILED` và cleanup | Chưa tự reschedule |
| Stop treo | Chờ 5 giây rồi cancel | Chưa force cleanup GPU resource cụ thể |
| Worker chết | Heartbeat hết hạn sau tối đa 15 giây | API chưa reschedule session |
| LiveKit room close | Client/worker SDK reconnect tùy trạng thái | Chưa session recovery orchestration |
| API restart + memory repo | Mất session/history | Dùng PostgreSQL; vẫn cần ownership design |

## 21. Security hiện tại và yêu cầu production

Đã có:

- Publisher/subscriber token tách quyền.
- Token gắn đúng room và identity.
- Token có TTL.
- HTTP model adapter hỗ trợ Bearer token.

Chưa có và cần bổ sung trước production:

- Authentication/authorization cho REST và WebSocket.
- Kiểm tra user có quyền truy cập `session_id`.
- API key/secret đủ dài, lưu bằng secret manager.
- TLS cho API và LiveKit (`https/wss`).
- TURN credential có TTL.
- Rate limit, quota session, message size và concurrency.
- Redis TLS/auth và network isolation.
- Không nên gửi publisher JWT dài hạn qua command bus dạng plaintext.
- Token exchange ngắn hạn hoặc worker gọi credential broker.
- CSP và bundle/pin LiveKit browser SDK thay vì runtime CDN latest.
- Validation `character_id` tới asset/model registry.
- Audit log không ghi token, prompt nhạy cảm hoặc raw SDP quá mức.

## 22. Observability đề xuất

Log hiện có:

- Avatar state của worker.
- Worker media ready.
- FPS và A/V drift mỗi 5 giây.
- Render task exception.
- Command retry.
- FastAPI access log.
- LiveKit participant, track và ICE log.

Metrics nên bổ sung:

- `avatar_sessions_active{worker_id}`
- `render_command_latency_seconds{type}`
- `render_command_failures_total{type}`
- `render_fps{session_id}` hoặc aggregate theo worker
- `render_av_drift_ms`
- `render_frame_time_seconds`
- `render_queue_depth`
- `worker_gpu_utilization`, memory và temperature
- `livekit_packet_loss`, RTT, jitter, bitrate
- `chat_reply_first_token_seconds`
- `chat_reply_duration_seconds`
- `state_duration_seconds{state}`
- `session_start_to_media_ready_seconds`

Mọi log/event nên có ít nhất `session_id`, `worker_id`, `command_id` và `room_name` khi phù
hợp để trace xuyên API → Redis → worker → SFU.

## 23. Cách tích hợp model sinh video thật

Mục tiêu là giữ nguyên Core và thay phần frame source trong worker.

```mermaid
flowchart LR
    CMD[State/prompt command]
    ORCH[Render session orchestrator]
    TTS[TTS adapter]
    GEN[Avatar model adapter\nGPU]
    BUF[Bounded A/V buffer]
    PUB[Stable LiveKit publisher]
    SFU[LiveKit SFU]

    CMD --> ORCH
    ORCH --> TTS
    TTS -->|PCM + timing| GEN
    GEN -->|video frames + timestamps| BUF
    TTS -->|audio frames + timestamps| BUF
    BUF --> PUB
    PUB --> SFU
```

Đề xuất boundary trong worker:

```python
class AvatarFrameSource(Protocol):
    async def start(self, session: RenderContext) -> None: ...
    async def set_state(self, state: AvatarState) -> None: ...
    async def frames(self) -> AsyncIterator[TimedVideoFrame]: ...
    async def close(self) -> None: ...

class AudioSource(Protocol):
    async def synthesize(self, text_stream: AsyncIterator[str]) \
        -> AsyncIterator[TimedAudioFrame]: ...
```

Renderer model mới có thể là:

- In-process GPU adapter nếu worker độc quyền GPU.
- gRPC adapter tới model server riêng.
- Shared-memory adapter trên cùng node để giảm copy frame.
- Queue/object-storage adapter cho tác vụ không realtime; không khuyến nghị cho lip-sync realtime.

Nguyên tắc:

- Publisher track không đổi trong suốt session.
- Timestamp do timeline chung quyết định, không lấy tùy ý từ thời gian model trả frame.
- Model chậm phải có bounded queue và explicit policy: giữ frame, drop frame, giảm FPS hoặc
  chuyển fallback idle.
- TTS audio nên là master clock cho talking avatar/lip-sync.
- State/prompt command không mang tensor/frame lớn qua Redis.
- Frame dữ liệu nặng ở trong worker/model plane; Redis chỉ mang metadata/control.

## 24. Scale worker trong tương lai

Hiện tại API gửi mọi session tới một `RENDER_WORKER_ID` tĩnh. Kiến trúc production nên thêm:

1. Worker registry dựa trên heartbeat/capabilities.
2. Scheduler chọn worker theo GPU memory, active session và model affinity.
3. Session-to-worker mapping bền vững.
4. Worker-ready event sau khi track publish thành công.
5. Timeout và reschedule nếu worker không ready.
6. Graceful drain khi deploy worker.
7. Idempotency/deduplication theo `command_id`.
8. Pending reclaim và dead-letter stream.

Ví dụ mapping:

```text
avatar:session:{session_id}:worker -> worker-7
avatar:worker:worker-7             -> heartbeat/capabilities/load
avatar:commands:worker-7           -> ordered commands
avatar:events:{session_id}         -> ready/error/stopped events
```

## 25. Test và quality gates

Test suite hiện chia thành:

- Unit test cho domain transition và use case.
- Unit test cho silent audio, frame overlay, LiveKit token và worker media.
- Contract test cho memory và SQLAlchemy repositories.
- Integration test cho API và switchable track.
- Architecture test ngăn Core import infrastructure.

Chạy đầy đủ:

```bash
source .venv/bin/activate
ruff check .
mypy src
pytest
docker compose --profile livekit config
git diff --check
```

Smoke test end-to-end cần xác nhận thêm ngoài automated test:

- Browser participant active.
- Audio và video đều subscribed.
- ICE chọn candidate truy cập được.
- FPS gần `WORKER_VIDEO_FPS`.
- A/V drift ổn định.
- State đi đủ `thinking → speaking → idle`.
- Frame counter không dừng khi switch state.

## 26. Giới hạn chính của phiên bản hiện tại

- Video vẫn là ba clip thu sẵn, chưa có avatar generation/lip-sync.
- Audio là silence/beep, chưa có TTS nội dung.
- Một worker ID tĩnh, chưa scheduler/capacity management.
- Worker-ready/error đã correlation theo `command_id`; chưa có scheduler để reschedule.
- Command pending chưa được reclaim và chưa có DLQ.
- Publisher token nằm trong Redis command.
- Room close dựa vào LiveKit tự cleanup.
- Health endpoint chưa aggregate dependency readiness.
- In-memory là mặc định và không hỗ trợ multi-replica API.
- Session lock nằm trong process; optimistic version bảo vệ repository nhưng chưa thay thế
  distributed ownership khi chạy nhiều API replica.
- Chưa auth, TLS, TURN mặc định, quota hoặc production secret handling.
- Frontend LiveKit SDK tải từ CDN và chưa pin/bundle version.
- Chưa có metrics exporter/dashboard/alert.
- Chưa có Kubernetes manifests, autoscaling hoặc GPU scheduling.

## 27. Thứ tự nâng cấp được khuyến nghị

### Giai đoạn A — làm chắc MVP hiện tại

- Hoàn thiện pending reclaim và DLQ (ready/error, timeout và idempotency đã có).
- Thêm transactional outbox để commit domain state và publish command nguyên tử.
- Thêm migration chính thức cho schema `status`, `avatar_state`, `version`.
- Bundle/pin LiveKit JS SDK.
- Thêm readiness check Redis/worker/LiveKit.
- Bổ sung integration test Redis command → worker session.

### Giai đoạn B — TTS và audio thật

- Tạo `TtsPort`/adapter trong worker hoặc model service.
- Stream PCM 48 kHz liên tục, chèn silence khi underrun.
- Dùng audio timeline làm master clock.
- Giữ beep/flash làm debug mode, tắt mặc định production.

### Giai đoạn C — model avatar/GPU

- Tạo frame-source interface.
- Tách model runtime thành worker/GPU service.
- Thêm bounded buffer và overload policy.
- Đo render time, first-frame latency và GPU capacity.
- Fallback về idle/thinking clip khi model chưa ready hoặc lỗi.

### Giai đoạn D — scale và production hardening

- Scheduler + worker registry + session affinity.
- PostgreSQL + migration + distributed ownership.
- TLS, auth, TURN, secret manager và rate limiting.
- Metrics/tracing/dashboard/alert.
- Rolling deploy và graceful drain.

## 28. Checklist vận hành nhanh

### Trước khi start

- [ ] `MEDIA_PROVIDER=livekit` cho full pipeline.
- [ ] `LIVEKIT_URL` nhìn được từ browser.
- [ ] `LIVEKIT_WORKER_URL` nhìn được từ worker container.
- [ ] `LIVEKIT_NODE_IP` đúng nếu browser ở máy khác.
- [ ] RTC TCP/UDP ports không bị chiếm và firewall cho phép.
- [ ] Redis URL đúng.
- [ ] Ba clip avatar tồn tại.
- [ ] Production credentials không dùng `devkey/secret`.

### Sau khi start

- [ ] Bốn container running, Redis healthy.
- [ ] `/api/health` trả 200.
- [ ] Worker heartbeat tồn tại.
- [ ] Browser connection state là connected.
- [ ] Client subscribed audio và video.
- [ ] Frame counter tăng.
- [ ] FPS và drift ổn định.
- [ ] Chat chuyển đủ ba state.
- [ ] Close browser/session làm active session giảm.

## 29. Các quyết định kiến trúc cốt lõi

1. Core chỉ quản lý domain và orchestration, không render media.
2. `AvatarStreamPort` là boundary giữa business flow và media runtime.
3. LiveKit SFU là media router; worker không tạo peer riêng cho từng browser.
4. Worker publish trực tiếp lên SFU để frame nặng không đi qua Core API.
5. Redis Streams là command bus MVP vì giữ ordering theo worker stream và có ACK.
6. Một stable audio/video track được giữ suốt session; state chỉ thay source/content.
7. Audio sample timeline là master clock; video được pace theo configured FPS.
8. Queue phải hữu hạn để kiểm soát latency và backpressure.
9. Beep/flash và frame counter là observability nằm ngay trong media payload.
10. Mọi external provider được thay bằng adapter và composition-root wiring.

Những quyết định này cho phép pipeline hiện tại tiếp tục chạy bằng clip demo, đồng thời mở
đường thay clip decoder bằng model sinh avatar thật mà không viết lại session, chat, API hay
state machine.
