# PersonaForge

**Build AI agents that think, speak and appear.**

PersonaForge là nền tảng tạo và vận hành AI Agent theo từng session. Mỗi Agent có thể được
cấu hình kiến thức, hành vi, giọng nói và nhân vật avatar riêng; người dùng tương tác với Agent
qua hội thoại và luồng audio/video realtime.

Tài liệu kiến trúc:

- Tổng hợp đầy đủ pipeline hiện tại: [docs/current-pipeline.md](docs/current-pipeline.md).
- Xác thực JWT và refresh rotation: [docs/authentication.md](docs/authentication.md).
- Luồng auth, phân quyền, chat/reconnect và media hiện tại: [docs/server-flow.md](docs/server-flow.md).
- Thiết kế Render Worker/SFU và lộ trình model GPU:
  [docs/direct-worker-streaming.md](docs/direct-worker-streaming.md).
- Agent Runtime, gRPC streaming và delegated authorization:
  [docs/agent-runtime.md](docs/agent-runtime.md).
- Luồng mục tiêu Core, composite TTS/Avatar GPU slot, audio timeline và LiveKit:
  [docs/tts-avatar-render-flow.md](docs/tts-avatar-render-flow.md).
- Worker Application Core, TTS/lip-sync interfaces và adapter boundaries:
  [docs/persona-worker-core-architecture.md](docs/persona-worker-core-architecture.md).
- Worker Master–Child, gRPC bidirectional stream qua Unix Domain Socket và multi-GPU:
  [docs/worker-master-child-grpc.md](docs/worker-master-child-grpc.md).
- Kế hoạch tích hợp VieNeu-TTS theo session, system voices và design gate cho custom voice/ACL:
  [docs/vieneu-tts-integration-plan.md](docs/vieneu-tts-integration-plan.md).
- Control Plane đã triển khai, data ownership, enrollment và Docker runbook:
  [docs/control-plane-implementation.md](docs/control-plane-implementation.md).

Phiên bản hiện tại là nền tảng MVP của PersonaForge, được triển khai theo Hexagonal
Architecture. FastAPI nhận signaling/chat; media có thể chạy
trực tiếp bằng aiortc hoặc qua Render Worker + LiveKit SFU. Video chuyển giữa các trạng thái
`idle`, `thinking`, `speaking`, và core không phụ thuộc framework.
Trong LiveKit worker mode, text đi qua fake `CompositeSpeechPipeline`: tone PCM 440 Hz và frame
lip-sync lấy từ `speaking.mp4` dùng chung audio master timeline. Tin nhắn bỏ cursor ngay khi
Agent sinh xong text; avatar tiếp tục `speaking` cho đến khi audio/video playback hoàn tất.
Các application port này là điểm thay thế dành cho model TTS/avatar thật.
Mỗi frame video có bộ đếm sáu chữ số ở góc trái để quan sát stream còn đang chạy.
Trạng thái `thinking` được giữ tối thiểu một giây và có thể chỉnh bằng
`THINKING_MIN_DURATION`.
Chế độ chẩn đoán `AV_SYNC_DEBUG=true` phát beep 1 kHz và nháy ô trắng đồng thời mỗi hai giây;
bấm “Bật âm thanh kiểm tra” trên video để nghe do giới hạn autoplay của trình duyệt.

Mỗi chat turn hiện có thể được giao cho Agent Runtime độc lập qua server-streaming gRPC.
Core API vẫn là nơi quyết định quyền truy cập, lưu transcript và trạng thái turn; Agent chỉ
nhận JWT ủy quyền ngắn hạn, giới hạn đúng user, session, turn, tool và knowledge scope.
Provider `local` vẫn được giữ để demo không cần thêm service.

## Chạy local

### Makefile for development and demo operations

The repository provides a Makefile as the consistent entry point for common development and
operations workflows. List all targets and supported overrides with:

```bash
make help
make doctor
make env-check
```

Main startup workflows:

```bash
make up            # aiortc + PostgreSQL + Redis
make up-agent      # add the gRPC Agent Runtime
make up-control    # first boot: start Control/LiveKit before Worker
make enroll-worker # issue a one-time token and update .env without logging it
make up-livekit    # full pipeline with a CPU Worker
make up-gpu        # full pipeline with an NVIDIA Worker
```

Startup targets use an existing image and do not force a rebuild. Docker Compose builds an image
only when it is missing. Choose the operation from the type of change:

| Change | Command | Docker build |
|---|---|---|
| Start/stop an existing stack | `make up-gpu`, `make up-livekit`, `make up-control` | No |
| Change only `.env` or a Compose environment value | `make recreate-worker-gpu`, `make recreate-chat`, etc. | No |
| Restart a process without reloading `.env` | `make restart-worker`, `make restart-agent` | No |
| Change Core/frontend/migrations/shared API source | `make rebuild-chat` and, when used, `make rebuild-control` | Yes, selected images only |
| Change Agent source or dependencies | `make rebuild-agent` | Yes, Agent only |
| Change Worker source, worker requirements, entrypoint, or Worker Dockerfile | `make rebuild-worker-gpu` or `make rebuild-worker` | Yes, Worker only |
| Change only docs, Makefile, or host-side `scripts/devops.py` | Run the command directly | No |
| Intentionally rebuild the whole stack | `make build` or `make build-gpu` | Yes, all selected images |

`make restart-worker` does not reload `.env`; use `make recreate-worker` or
`make recreate-worker-gpu` for Worker configuration changes. The `worker-status`, `capacity`, and
`telemetry` targets summarize heartbeat data without exposing credentials. `make reset-demo`
deletes PostgreSQL, Redis, and Worker identity volumes, so it requires `CONFIRM=YES`.

Local and CI quality gates:

```bash
make check
make unit PYTEST_ARGS="-x -vv"
make e2e
```

Yêu cầu Python 3.10+ và FFmpeg:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
bash scripts/generate_demo_videos.sh
uvicorn personaforge_chat.main:app --reload
```

Mở <http://localhost:8000>. Trên máy khác cần HTTPS và cấu hình STUN/TURN trong `.env`.

Khi chạy Python trực tiếp, nếu không đặt `DATABASE_URL` thì dữ liệu nằm trong memory. Để kết
nối PostgreSQL đang được Docker publish ra localhost:

```env
DATABASE_URL=postgresql+asyncpg://avatar:avatar@localhost:5432/avatar
```

Khi chạy Python local với `AUTO_CREATE_SCHEMA=true`, SQLAlchemy adapter tự tạo schema lúc ứng
dụng khởi động. Có thể dùng `sqlite+aiosqlite:///./avatar.db` để phát triển local.

Với database lâu dài, nên tắt `AUTO_CREATE_SCHEMA` và chạy migration:

```bash
alembic upgrade head
```

Trong Docker, image chứa Alembic và Compose tự migrate trước khi start API. Lệnh thủ công sau
chỉ cần khi muốn chạy migration riêng:

```bash
docker compose run --rm avatar-chat alembic upgrade head
```

## Xác thực JWT và refresh token

Frontend hỗ trợ register, login, logout và tự gia hạn đăng nhập. Server phát:

- access JWT mặc định 15 phút, frontend chỉ giữ trong memory và gửi bằng
  `Authorization: Bearer <token>`;
- refresh JWT mặc định 7 ngày, chỉ nằm trong cookie `HttpOnly`, `SameSite=Strict`;
- CSRF token ở cookie đọc được, phải khớp header `X-CSRF-Token` khi refresh hoặc mutation.

Mỗi lần refresh sẽ thu hồi refresh token cũ và phát một cặp token mới. Việc thay token được
thực hiện atomic ở repository; tái sử dụng token cũ sẽ thu hồi toàn bộ phiên của user. Logout
thu hồi bản ghi server-side nên access JWT và WebSocket hiện tại mất quyền ngay, không phải
đợi JWT hết hạn. WebSocket truyền access JWT qua subprotocol, không đưa token vào URL.

Cấu hình production tối thiểu:

```env
JWT_SECRET=<random-secret-at-least-32-bytes>
JWT_ISSUER=avatar-chat
JWT_AUDIENCE=avatar-chat-browser
ACCESS_TOKEN_TTL_MINUTES=15
REFRESH_TOKEN_TTL_DAYS=7
REFRESH_COOKIE_NAME=avatar_refresh
AUTH_COOKIE_SECURE=true
ALLOWED_ORIGINS=https://avatar.example.com
```

Giá trị `JWT_SECRET` mặc định chỉ dành cho local. Production bắt buộc dùng HTTPS, secret
manager và một secret ngẫu nhiên riêng cho từng môi trường.

## Chạy Docker với PostgreSQL

```bash
docker compose up --build
```

Docker Compose mặc định chạy PostgreSQL 16, chờ healthcheck, tự chạy `alembic upgrade head`
rồi mới start Uvicorn. User, refresh session, avatar session, share, audit và chat message được
lưu trong named volume `postgres_data`, không còn dùng repository in-memory.

```bash
docker compose ps
docker compose logs -f postgres avatar-chat
```

`docker compose down` giữ nguyên database. Chỉ `docker compose down -v` mới xóa volume và
toàn bộ dữ liệu PostgreSQL. Các credential `avatar/avatar` chỉ dành cho local. PostgreSQL chỉ
dùng biến khởi tạo user/password khi volume còn trống; đổi credential sau đó cần đổi role trong
database hoặc tạo lại volume đối với dữ liệu demo có thể xóa.

Dữ liệu từng nằm trong repository in-memory không thể migrate vì nó đã mất khi process cũ
dừng; từ lần chạy Compose mới, dữ liệu sẽ được giữ trong volume PostgreSQL.

Image tự tạo ba nhân vật demo `default`, `violet`, `coral`; frontend cho chọn nhân vật trước
khi kết nối. Muốn thêm nhân vật, tạo thư mục `assets/characters/<character_id>/` chứa đủ
`idle.mp4`, `thinking.mp4`, `speaking.mp4`. Có thể mount catalog ngoài và cấu hình
`AVATAR_CHARACTERS_ROOT`; ba biến `AVATAR_*_VIDEO` vẫn override nhân vật `default`.

## Chạy Agent Runtime độc lập

Để chạy Core API và Agent service qua gRPC:

```bash
cp .env.example .env
sed -i 's/AGENT_RUNTIME_PROVIDER=local/AGENT_RUNTIME_PROVIDER=grpc/' .env
docker compose --profile agent up --build
```

Mở <http://localhost:8000>, tạo/kết nối session rồi chat như bình thường. Core gọi
`ExecuteTurn` và nhận event `turn_started`, `heartbeat`, `text_delta`, `turn_completed` qua
cùng một HTTP/2 stream. Healthcheck của Compose gọi chuẩn gRPC Health Checking.

Agent mặc định chạy tối đa 20 turn, queue 100 turn, tối đa 3 turn/user và 1 turn/session.
Queue đầy hoặc timeout trả `RESOURCE_EXHAUSTED`; health chuyển `NOT_SERVING` khi không còn
execution/queue capacity. Điều chỉnh bằng nhóm biến `AGENT_MAX_*` và `AGENT_QUEUE_TIMEOUT`
trong `.env`.

Agent demo dùng `ScriptedLanguageModelAdapter`. Khi tích hợp LLM thật, implement
`LanguageModelPort` trong `personaforge_agent/adapters/outbound` và wire provider trong
`personaforge_agent/main.py`; Core API không cần thay đổi.

## Thay model chat local cũ

Mặc định dùng `ScriptedReplyAdapter`. Để dùng HTTP model service:

```env
REPLY_PROVIDER=http
REPLY_HTTP_URL=http://model-service:9000/replies/stream
REPLY_HTTP_TOKEN=optional-token
```

Adapter gửi:

```json
{"session_id":"...","messages":[{"role":"user","content":"Xin chào"}]}
```

Service trả NDJSON hoặc SSE, mỗi event có dạng:

```text
data: {"text":"Xin "}
data: {"text":"chào"}
data: [DONE]
```

Luồng này được bọc bởi `LegacyReplyAgentAdapter` khi `AGENT_RUNTIME_PROVIDER=local` nhằm
giữ tương thích cho demo. Triển khai mới nên dùng Agent Runtime gRPC.

## Thay media runtime

`AiortcAvatarStreamAdapter` implement `AvatarStreamPort`. Có thể bổ sung adapter LiveKit
hoặc model avatar mới mà không sửa domain/use case. Trong aiortc adapter,
`SwitchableAvatarVideoTrack` giữ nguyên track WebRTC và đổi clip phía sau nên không cần
renegotiate khi avatar đổi trạng thái.

### Render Worker publish trực tiếp qua LiveKit

Chế độ này tách media khỏi Core API:

```text
Browser <-- WebRTC --> LiveKit SFU <-- WebRTC --> avatar-worker
Browser <-- HTTP/WS --> avatar-chat <-- Redis Streams --> avatar-worker
```

### Khởi động LiveKit lần đầu với Worker enrollment

Enrollment không thể dùng lệnh `up` tất cả ngay lần đầu vì Worker cần bootstrap token do
Control Plane cấp trước khi nó khởi động. Chuẩn bị `.env`:

```bash
cp .env.example .env
sed -i 's/MEDIA_PROVIDER=aiortc/MEDIA_PROVIDER=livekit/' .env
```

Thay các development secret trong `.env`, đồng thời giữ các URL nội bộ và `host_id`:

```env
CONTROL_OPERATOR_TOKEN=<operator-secret>
CONTROL_CORE_TOKEN=<different-core-secret>
CONTROL_ENROLLMENT_JWT_SECRET=<random-secret-at-least-32-bytes>
CONTROL_PLANE_URL=http://personaforge-control:8090
AVATAR_REGISTRATION_URL=http://personaforge-control:8090
AVATAR_HOST_ID=demo-host
AVATAR_WORKER_BOOTSTRAP_TOKEN=
```

Khởi động storage, SFU, Control Plane và Agent trước; chưa start Worker:

```bash
docker compose --profile livekit --profile agent up -d --build \
  postgres redis livekit personaforge-control personaforge-agent
curl -fsS http://127.0.0.1:8090/health/live
```

Cấp bootstrap token bằng đúng `CONTROL_OPERATOR_TOKEN` và `AVATAR_HOST_ID` trong `.env`:

```bash
export CONTROL_OPERATOR_TOKEN='<same-operator-secret-as-env>'
curl -fsS http://127.0.0.1:8090/internal/worker-enrollment-tokens \
  -H "Authorization: Bearer ${CONTROL_OPERATOR_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{
    "worker_type":"persona-render",
    "cluster_id":"development",
    "host_id":"demo-host",
    "max_gpu_count":1,
    "ttl_seconds":600
  }'
```

Copy `bootstrap_token` trong response vào `.env`, rồi start Core và Worker CPU:

```env
AVATAR_WORKER_BOOTSTRAP_TOKEN=<returned-bootstrap-token>
```

```bash
docker compose --profile livekit --profile agent up -d --build \
  avatar-chat avatar-worker
```

Đối với Worker GPU, dùng file override ở bước cuối:

```bash
docker compose -f compose.yaml -f compose.gpu.yaml \
  --profile livekit --profile agent up -d --build avatar-chat avatar-worker
```

Kiểm tra Worker đã register và đang gửi hardware heartbeat:

```bash
curl -fsS http://127.0.0.1:8090/internal/workers \
  -H "Authorization: Bearer ${CONTROL_OPERATOR_TOKEN}"
docker compose --profile livekit logs -f avatar-worker
```

Kết quả cần có `status=ready`, `worker_id`, GPU capability và `heartbeat`. Sau lần enrollment
đầu, Worker lưu management token trong volume `avatar_worker_identity`; có thể xóa
`AVATAR_WORKER_BOOTSTRAP_TOKEN` khỏi `.env`. Khi restart, Worker dùng management token để
`reregister` và nhận Redis ACL credential mới, không cần bootstrap token mới. Chỉ phát token
mới khi identity bị revoke hoặc volume identity bị xóa có chủ đích.

`max_gpu_count` trong enrollment token và `AVATAR_WORKER_MAX_GPU_COUNT` phải bằng nhau. Control
từ chối Worker advertise nhiều GPU hơn token. Một Master có thể quản lý nhiều GPU; Scheduler
reserve theo key `(worker_id, gpu_uuid)` và lease trả về cả hai ID. Nếu identity cũ đã enrollment
với một GPU, cần deregister identity cũ và cấp bootstrap token mới trước khi mở rộng inventory.

Heartbeat được append vào Redis Stream và Control lưu raw samples vào PostgreSQL. Kiểm tra:

```bash
docker compose exec -T postgres psql -U avatar -d avatar -c \
  "select worker_id,gpu_uuid,sampled_at,used_vram_mb,gpu_utilization \
   from gpu_heartbeat_samples order by sampled_at desc limit 10;"
```

Cấu hình safety floor cho một cặp nhân vật/giọng đọc bằng operator API:

```bash
curl -fsS -X PUT http://127.0.0.1:8090/internal/resource-configs \
  -H "Authorization: Bearer ${CONTROL_OPERATOR_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "character_id":"default",
    "avatar_model_id":"recorded-avatar",
    "avatar_revision":"default",
    "voice_id":"default",
    "tts_model_id":"scripted-tone",
    "tts_revision":"default",
    "runtime":"scripted",
    "runtime_version":"default",
    "precision":"fp32",
    "resolution":"640x360",
    "fps":25,
    "isolation_mode":"subprocess",
    "gpu_name":"NVIDIA GeForce RTX 3090",
    "minimum_compute_capability":"8.0",
    "manifest_peak_vram_mb":6000,
    "manifest_gpu_duty_cycle":0.30,
    "bootstrap_vram_margin_mb":512,
    "bootstrap_compute_margin":0.05,
    "active":true
  }'
```

Scheduler đối chiếu đủ tuple nhân vật + avatar revision + voice + TTS revision trong PostgreSQL.
Cấu hình này luôn là safety floor. Nếu có profile đo đúng cặp trên đúng GPU thì dùng P99/high
watermark; nếu chưa có, Control có thể dùng profile tương thích cùng avatar hoặc cùng voice/TTS
trên đúng GPU/runtime và cộng `CONTROL_RESOURCE_COMPATIBLE_VRAM_MARGIN_MB`. Chỉ khi không có cả
profile lẫn config mới dùng platform default. Đặt `gpu_name: null` để manifest áp dụng cho mọi
GPU; Control lưu nó bằng khóa chuẩn `*` để upsert idempotent.

`GET /internal/resource-profiles`, `GET /internal/resource-configs` và
`GET /internal/telemetry/status` lần lượt cho phép operator xem P50/P95/P99, manifest đang dùng
và trạng thái ingest. Mỗi lease trả `resource_source`/`resource_profile_id` để audit quyết định.

Muốn chạy legacy không enrollment, đặt `CONTROL_PLANE_URL=` và `AVATAR_REGISTRATION_URL=`;
Worker khi đó kết nối trực tiếp bằng `REDIS_URL` và không có dynamic Redis ACL identity.

Worker được tách khỏi image API: `Dockerfile.worker` dành cho fake pipeline CPU/local/CI.
Trên NVIDIA host đã cài driver và NVIDIA Container Toolkit, dùng CUDA/cuDNN Worker bằng file
override có sẵn:

```bash
docker compose -f compose.yaml -f compose.gpu.yaml \
  --profile livekit --profile agent up -d --build
```

`WORKER_CUDA_IMAGE` trong `.env` cho phép pin CUDA/cuDNN base image phù hợp với framework model.
Image GPU chưa cài PyTorch/ONNX/TensorRT cho đến khi adapter model thật được chọn.

Sau đó mở <http://localhost:8000>. Các giá trị `devkey/secret` trong `.env.example` chỉ
dành cho local. Frontend demo tải LiveKit browser SDK từ jsDelivr nên trình duyệt cần truy
cập Internet; production nên pin version và bundle SDK cùng frontend.

Nếu port mặc định đã bị chiếm, đổi đồng thời `APP_HTTP_PORT`, `LIVEKIT_HTTP_PORT` và phần
port trong `LIVEKIT_URL`; `LIVEKIT_WORKER_URL=ws://livekit:7880` vẫn giữ nguyên vì đây là
địa chỉ nội bộ Docker.

Worker hiện dùng lại các clip `idle`, `thinking`, `speaking`, nhưng chạy trong process riêng.
Nó publish một audio track PCM mono 48 kHz theo frame 20 ms và video theo
`WORKER_VIDEO_FPS`. `rtc.AVSynchronizer`, audio queue 50 ms và video queue hữu hạn giữ
cadence/A-V sync; log `Media cadence` xuất `fps` và `av_drift_ms` mỗi năm giây.

Khi owner đặt session thành `public`, API trả `public_url=/watch/<session_id>` và frontend
hiển thị nút copy/mở URL. Trang này không yêu cầu tài khoản, chỉ được cấp LiveKit subscriber
token khi session vẫn public và media đã sẵn sàng; anonymous viewer không có quyền chat hoặc
publish. Trang public hiển thị transcript chỉ-đọc và đồng bộ message mới mỗi giây, nhưng không
render input/nút gửi. `LIVEKIT_API_URL=http://livekit:7880` là endpoint nội bộ dùng để ngắt viewer ngay khi
owner chuyển session về private.

Chạy worker ngoài Docker, khi Redis và LiveKit đã có sẵn:

```bash
REDIS_URL=redis://localhost:6379/0 \
python -m personaforge_worker.main
```

Chi tiết thiết kế và hướng tích hợp model thật nằm trong
[docs/direct-worker-streaming.md](docs/direct-worker-streaming.md).

## Kiểm thử

```bash
source .venv/bin/activate
ruff check .
mypy src
pytest tests/unit tests/contract/test_memory_repositories.py tests/integration/test_api.py
```

Kiểm thử đồng bộ hai browser bằng Playwright sau khi stack đã chạy:

```bash
pip install -e '.[e2e]'
playwright install chromium
E2E_BASE_URL=http://localhost:8000 pytest tests/e2e -q
```

Các thao tác visibility/share/revoke/close ghi domain state, audit log và realtime outbox
trong cùng transaction. Nếu Redis tạm lỗi, dispatcher retry theo cơ chế at-least-once; browser
cũng reconcile snapshot định kỳ để tránh giữ UI cũ.

## Giới hạn hiện tại

- Nếu không cấu hình `DATABASE_URL`, session và lịch sử mất khi process restart.
- Chat event dùng Redis Pub/Sub trong LiveKit mode và browser tự catch-up từ database bằng
  cursor `sequence:revision`. Muốn chạy nhiều API replica bắt buộc dùng database chung và
  `REALTIME_EVENT_BUS=redis`. Redis lease theo session ngăn hai Core replica chạy hai turn
  cùng lúc mà không giữ PostgreSQL connection; `SESSION_TURN_LOCK_PROVIDER=memory` chỉ dành
  local/test.
- Chưa có TURN mặc định; rate limit đăng nhập hiện lưu trong memory nên cần Redis khi scale.
- Audio hiện là track im lặng 48 kHz; chưa có nội dung TTS.
- Agent Runtime đã tách process và bảo vệ bằng delegated JWT, nhưng TTS/Avatar model thật
  chưa được gắn; audio/video vẫn do media runtime demo hiện tại phát.
- Chế độ LiveKit + Control Plane đã chọn động cặp `worker_id + gpu_uuid`; `RENDER_WORKER_ID` chỉ
  còn dùng trong legacy mode khi `CONTROL_PLANE_URL` để trống.
- Redis Streams chưa có pending reclaim/DLQ; readiness đã được xác nhận theo `command_id`
  sau khi worker publish đủ audio và video track.
