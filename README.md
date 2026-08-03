# Avatar Chat WebRTC

Tài liệu kiến trúc:

- Tổng hợp đầy đủ pipeline hiện tại: [docs/current-pipeline.md](docs/current-pipeline.md).
- Xác thực JWT và refresh rotation: [docs/authentication.md](docs/authentication.md).
- Luồng auth, phân quyền, chat/reconnect và media hiện tại: [docs/server-flow.md](docs/server-flow.md).
- Thiết kế Render Worker/SFU và lộ trình model GPU:
  [docs/direct-worker-streaming.md](docs/direct-worker-streaming.md).

MVP nhân vật ảo theo Hexagonal Architecture. FastAPI nhận signaling/chat; media có thể chạy
trực tiếp bằng aiortc hoặc qua Render Worker + LiveKit SFU. Video chuyển giữa các trạng thái
`idle`, `thinking`, `speaking`, và core không phụ thuộc framework.
Server đồng thời phát một audio track PCM im lặng; track này là điểm thay thế dành cho TTS.
Mỗi frame video có bộ đếm sáu chữ số ở góc trái để quan sát stream còn đang chạy.
Trạng thái `thinking` được giữ tối thiểu một giây và có thể chỉnh bằng
`THINKING_MIN_DURATION`.
Chế độ `AV_SYNC_DEBUG` mặc định phát beep 1 kHz và nháy ô trắng đồng thời mỗi hai giây;
bấm “Bật âm thanh kiểm tra” trên video để nghe do giới hạn autoplay của trình duyệt.

## Chạy local

Yêu cầu Python 3.10+ và FFmpeg:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
bash scripts/generate_demo_videos.sh
uvicorn avatar_chat.main:app --reload
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

## Thay model chat bên ngoài

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

Một provider khác chỉ cần implement `ReplyGeneratorPort` và được wire trong
`bootstrap/container.py`.

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

Chạy đầy đủ bằng Docker:

```bash
cp .env.example .env
sed -i 's/MEDIA_PROVIDER=aiortc/MEDIA_PROVIDER=livekit/' .env
docker compose --profile livekit up --build
```

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
python -m avatar_worker.main
```

Chi tiết thiết kế và hướng tích hợp model thật nằm trong
[docs/direct-worker-streaming.md](docs/direct-worker-streaming.md).

## Giới hạn hiện tại

- Nếu không cấu hình `DATABASE_URL`, session và lịch sử mất khi process restart.
- Chat event dùng Redis Pub/Sub trong LiveKit mode và browser tự catch-up từ database bằng
  cursor `sequence:revision`. Muốn chạy nhiều API replica bắt buộc dùng database chung và
  `REALTIME_EVENT_BUS=redis`; lock xử lý message vẫn chưa phải distributed lock.
- Chưa có TURN mặc định; rate limit đăng nhập hiện lưu trong memory nên cần Redis khi scale.
- Audio hiện là track im lặng 48 kHz; chưa có nội dung TTS.
- Chế độ LiveKit hiện dùng một worker tĩnh qua `RENDER_WORKER_ID`; chưa có scheduler động.
- Redis Streams chưa có pending reclaim/DLQ; readiness đã được xác nhận theo `command_id`
  sau khi worker publish đủ audio và video track.
