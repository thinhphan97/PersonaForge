# Chạy Persona Render Worker trên GPU host bên ngoài

## 1. Nguyên nhân lỗi Docker DNS

Tên `redis`, `livekit` và `personaforge-control` chỉ được Docker DNS resolve giữa container nằm
trong cùng Compose network. Worker chạy trên host vật lý khác không thể dùng các URL này.

Trước bản sửa này, Registration Service tạo đúng Redis ACL username/password nhưng trả:

```text
redis://redis:6379/0
```

Core cũng có thể gửi URL sau trong `avatar.session.start`:

```text
ws://livekit:7880
```

Remote Worker đăng ký thành công qua Control nhưng sau đó không thể đọc Redis command hoặc publish
LiveKit media. Đăng ký thành công chỉ chứng minh Control HTTP reachable, không chứng minh các data
plane endpoint còn lại reachable.

## 2. Internal URL và advertised URL

Không dùng cùng một biến cho hai network scope.

| Biến | Consumer | Giá trị trên Control host |
|---|---|---|
| `REDIS_URL` | Core và Agent trong Compose | `redis://default:<admin-password>@redis:6379/0` |
| `CONTROL_REDIS_ADMIN_URL` | Control trong Compose | `redis://default:<admin-password>@redis:6379/0` |
| `CONTROL_REDIS_ADVERTISED_URL` | URL Control trả cho enrolled Worker | `redis://<control-host>:<published-redis-port>/0` |
| `CONTROL_PLANE_URL` | Core trong Compose | `http://personaforge-control:8090` |
| `CONTROL_PUBLIC_URL` | Operator CLI và remote diagnostics | `http://<control-host>:<control-port>` |
| `AVATAR_REGISTRATION_URL` | Worker | `http://<control-host>:<control-port>` |
| `LIVEKIT_API_URL` | Core trong Compose | `http://livekit:7880` |
| `LIVEKIT_URL` | Browser | `ws://<livekit-host>:<signaling-port>` |
| `LIVEKIT_WORKER_URL` | URL Core gửi cho Worker | `ws://<livekit-host>:<signaling-port>` |

`CONTROL_REDIS_WORKER_URL` chỉ còn là backward-compatible fallback.
`CONTROL_REDIS_ADVERTISED_URL` có độ ưu tiên cao hơn.

Redis advertised URL không chứa worker password. Registration response trả URL, dynamic ACL
username và password thành ba field riêng; Worker truyền chúng vào Redis client khi connect.

## 3. Port cần mở

Ví dụ Control/LiveKit host là `192.168.20.166`, sử dụng port trong `.env` hiện tại:

| Host port | Protocol | Ai truy cập | Bắt buộc | Mục đích |
|---:|---|---|---|---|
| `8090` | TCP/HTTP | GPU Worker → Control host | Có | Enroll, re-register, renew và deregister |
| `16379` | TCP/Redis | GPU Worker → Control host | Có | Redis Streams command/event/heartbeat bằng dynamic ACL |
| `17880` | TCP/HTTP+WS | GPU Worker và browser → LiveKit | Có | LiveKit signaling và publish/subscribe setup |
| `17882` | UDP | GPU Worker/browser ↔ LiveKit | Có cho media ưu tiên | WebRTC RTP/RTCP media |
| `17881` | TCP | GPU Worker/browser ↔ LiveKit | Khuyến nghị | WebRTC TCP fallback khi UDP bị chặn |
| `18000` | TCP/HTTP+WS | Browser → Core host | Chỉ cần cho browser ngoài host | Frontend, REST và chat WebSocket |

Các port không cần mở cho remote Render Worker:

| Port/resource | Lý do |
|---|---|
| PostgreSQL `5432` | Chỉ Core/Control truy cập trong Compose; Worker không đọc DB trực tiếp |
| Agent gRPC `50051` | Core gọi Agent trong Compose; Render Worker không gọi Agent |
| Child gRPC UDS | Chỉ Master và Child trên cùng Worker host |
| Docker container Redis `6379` | Remote dùng published host port, ví dụ `16379` |

Chỉ allow source IP/subnet của GPU Worker đối với `8090` và `16379`. Không expose Redis hoặc
operator endpoint trực tiếp ra Internet.

## 4. Cấu hình Control/LiveKit host

Ví dụ development LAN:

```env
REMOTE_WORKERS_ENABLED=true

APP_PUBLIC_URL=http://192.168.20.166:18000

CONTROL_HTTP_BIND_HOST=0.0.0.0
CONTROL_HTTP_PORT=8090
CONTROL_PUBLIC_URL=http://192.168.20.166:8090

REDIS_ADMIN_PASSWORD=<long-random-admin-password>
REDIS_URL=redis://default:<url-encoded-admin-password>@redis:6379/0
CONTROL_REDIS_ADMIN_URL=redis://default:<url-encoded-admin-password>@redis:6379/0
REDIS_BIND_HOST=0.0.0.0
REDIS_PORT=16379
CONTROL_REDIS_ADVERTISED_URL=redis://192.168.20.166:16379/0

LIVEKIT_BIND_HOST=0.0.0.0
LIVEKIT_URL=ws://192.168.20.166:17880
LIVEKIT_WORKER_URL=ws://192.168.20.166:17880
LIVEKIT_NODE_IP=192.168.20.166
LIVEKIT_HTTP_PORT=17880
LIVEKIT_RTC_TCP_PORT=17881
LIVEKIT_RTC_UDP_PORT=17882
```

Nếu password có ký tự đặc biệt, phần password nằm trong Redis URL phải được URL-encode. Dynamic
worker password không cần đặt trong `.env`; Control cấp khi enrollment.

Khởi động service phía Control host:

```bash
make env-check
make up-control
```

Khi đổi Redis `requirepass`, phải recreate Redis và các client để nạp environment mới:

```bash
docker compose --env-file .env up -d --force-recreate redis
make recreate-control
make recreate-chat
make recreate-agent
```

## 5. Cấp enrollment token

Trên Control host:

```bash
make enroll-worker
```

`make enroll-worker` dùng `CONTROL_PUBLIC_URL` nếu được cấu hình và không in raw token ra log.
Bootstrap token phải được chuyển an toàn sang `.env` của đúng GPU host. `host_id` và
`max_gpu_count` trong token phải khớp Worker đích.

Các lệnh `make health`, `make wait-ready`, `make worker-status`, `make capacity`, `make telemetry`
và `make enroll-worker` lấy endpoint từ `APP_PUBLIC_URL`/`CONTROL_PUBLIC_URL`. Chúng chỉ fallback
về `127.0.0.1` để tương thích với file env cũ.

Không copy Redis admin password sang GPU host. Remote Worker chỉ cần dynamic ACL credential nhận
từ Registration Service.

## 6. Cấu hình remote GPU Worker

Các giá trị tối thiểu trên GPU host:

```env
REMOTE_WORKERS_ENABLED=true
CONTROL_PUBLIC_URL=http://192.168.20.166:8090
AVATAR_REGISTRATION_URL=http://192.168.20.166:8090
CONTROL_REDIS_ADVERTISED_URL=redis://192.168.20.166:16379/0
LIVEKIT_WORKER_URL=ws://192.168.20.166:17880

AVATAR_HOST_ID=<gpu-host-id-matching-enrollment-token>
AVATAR_WORKER_BOOTSTRAP_TOKEN=<one-time-token>
AVATAR_WORKER_MAX_GPU_COUNT=<allowed-count>
NVIDIA_VISIBLE_DEVICES=all
```

`CONTROL_REDIS_ADVERTISED_URL` và `LIVEKIT_WORKER_URL` trên remote host chủ yếu giúp `env-check`
và operator tooling xác nhận topology. Redis URL dùng thực tế của Worker vẫn lấy từ authenticated
registration response; LiveKit URL dùng cho session lấy từ provision command của Core.

Build image một lần, sau đó chỉ start Worker và bỏ qua local dependencies:

```bash
make build-worker-gpu
make up-remote-worker-gpu
```

Khi chỉ đổi `.env`:

```bash
make recreate-remote-worker-gpu
```

Các target remote dùng `docker compose up --no-deps avatar-worker`, do đó không start Redis,
Control, LiveKit, PostgreSQL hoặc Agent giả trên GPU host.

## 7. Kiểm tra từ GPU host

Trước khi start Worker:

```bash
curl -fsS http://192.168.20.166:8090/health/live
nc -vz 192.168.20.166 16379
nc -vz 192.168.20.166 17880
nc -vz 192.168.20.166 17881
```

UDP không thể được xác nhận đầy đủ chỉ bằng `nc`; cần tạo session và kiểm tra ICE/LiveKit stats.

Sau khi Worker register:

```bash
make logs-worker
```

Chạy `make worker-status`, `make capacity` và `make telemetry` trên Control host. Các lệnh này
đọc protected inventory endpoint và cần `CONTROL_OPERATOR_TOKEN`; không nên phân phối operator
token sang GPU host chỉ để chạy health check.

Registration response phải chứa dạng:

```json
{
  "credential": {
    "redis_url": "redis://192.168.20.166:16379/0",
    "username": "pf-avatar:...",
    "password": "<redacted>"
  }
}
```

Không được còn các hostname sau trong response hoặc session-start command gửi tới remote Worker:

```text
redis
livekit
personaforge-control
localhost
127.0.0.1
```

## 8. Production security

Cấu hình LAN HTTP/Redis thuần chỉ phù hợp development trên network tin cậy. Production cần:

- private VPN/overlay network hoặc private subnet giữa Control và GPU hosts;
- HTTPS/mTLS cho Registration/Control;
- Redis TLS (`rediss://`) hoặc tunnel bảo mật;
- firewall allowlist theo GPU host;
- rotate development secrets và Redis admin password;
- không log bootstrap token, management token, ACL password hoặc signed credential URL;
- rate limit enrollment/re-registration endpoint;
- giám sát authentication failure và credential reuse.
