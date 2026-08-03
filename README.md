# Avatar Chat WebRTC

Tài liệu kiến trúc:

- Tổng hợp đầy đủ pipeline hiện tại: [docs/current-pipeline.md](docs/current-pipeline.md).
- Sơ đồ luồng server hiện tại: [docs/server-flow.md](docs/server-flow.md).
- Đề xuất Render Worker stream trực tiếp tới client/SFU:
  [docs/direct-worker-streaming.md](docs/direct-worker-streaming.md).

MVP nhân vật ảo theo Hexagonal Architecture. FastAPI nhận signaling/chat, aiortc phát
video theo các trạng thái `idle`, `thinking`, `speaking`, và core không phụ thuộc framework.
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

Mặc định dữ liệu nằm trong memory. Để lưu bằng PostgreSQL:

```env
DATABASE_URL=postgresql+asyncpg://avatar:avatar@localhost:5432/avatar
```

SQLAlchemy adapter tự tạo schema khi ứng dụng khởi động. Có thể dùng
`sqlite+aiosqlite:///./avatar.db` để phát triển local.

## Chạy Docker

```bash
docker compose up --build
```

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

Chạy worker ngoài Docker, khi Redis và LiveKit đã có sẵn:

```bash
REDIS_URL=redis://localhost:6379/0 \
python -m avatar_worker.main
```

Chi tiết thiết kế và hướng tích hợp model thật nằm trong
[docs/direct-worker-streaming.md](docs/direct-worker-streaming.md).

## Giới hạn hiện tại

- Nếu không cấu hình `DATABASE_URL`, session và lịch sử mất khi process restart.
- Một session thuộc một process; chưa hỗ trợ scale ngang.
- Chưa có authentication và TURN mặc định.
- Audio hiện là track im lặng 48 kHz; chưa có nội dung TTS.
- Chế độ LiveKit hiện dùng một worker tĩnh qua `RENDER_WORKER_ID`; chưa có scheduler động.
- Redis Streams chưa có pending reclaim/DLQ; readiness đã được xác nhận theo `command_id`
  sau khi worker publish đủ audio và video track.
