# Luồng xử lý Avatar Chat WebRTC Server hiện tại

Tài liệu này phản ánh implementation hiện tại trong repository. Hệ thống dùng Hexagonal
Architecture: domain/application không import FastAPI, SQLAlchemy, Redis, aiortc, LiveKit hay
SDK model. Mọi dependency bên ngoài được nối tại `bootstrap/container.py`.

## 1. Thành phần và ranh giới

```mermaid
flowchart LR
    B[Browser]

    subgraph API[FastAPI inbound]
        REST[REST API]
        WS[Authenticated WebSocket]
        HUB[Local connection hub]
    end

    subgraph CORE[Application and Domain]
        AUTH[Auth use cases]
        ACCESS[SessionAccessPolicy]
        CHAT[HandleMessage]
        MEDIAUC[Media use cases]
        SESSION[AvatarSession state machine]
        PORTS[Ports / Protocols]
    end

    subgraph DATA[Persistence adapters]
        MEM[In-memory repositories]
        SQL[SQLAlchemy repositories]
        DB[(PostgreSQL / SQLite)]
    end

    subgraph EVENTS[Control and realtime]
        PUB[(Redis Pub/Sub\nchat fan-out)]
        STREAM[(Redis Streams\nrender commands/events)]
    end

    subgraph MEDIA[Media plane]
        AIO[aiortc in API]
        WORKER[avatar-worker]
        SFU[LiveKit SFU]
    end

    MODEL[Scripted / HTTP reply provider]

    B <-->|REST| REST
    B <-->|chat/events| WS
    REST --> AUTH
    REST --> ACCESS
    REST --> MEDIAUC
    WS --> AUTH
    WS --> ACCESS
    WS --> CHAT
    CHAT --> SESSION
    CHAT --> PORTS
    MEDIAUC --> SESSION
    MEDIAUC --> PORTS
    PORTS --> MEM
    PORTS --> SQL
    PORTS --> MODEL
    SQL --> DB
    WS --> PUB --> HUB --> B
    MEDIAUC --> STREAM --> WORKER --> SFU --> B
    MEDIAUC --> AIO --> B
```

Hai loại Redis có mục đích khác nhau:

- Redis Streams giữ command/event điều khiển Render Worker và có ACK/retry.
- Redis Pub/Sub fan-out chat event giữa API replica; database và cursor mới là nguồn recovery.

## 2. Xác thực browser

```mermaid
sequenceDiagram
    participant B as Browser
    participant API as FastAPI
    participant Auth as Auth use case
    participant Repo as AuthSessionRepository

    B->>API: register hoặc login
    API->>Auth: xác minh user/password
    Auth->>Repo: lưu auth session + hash refresh JWT
    API-->>B: access JWT trong JSON
    API-->>B: refresh JWT HttpOnly + CSRF cookie

    B->>API: REST + Authorization Bearer + CSRF khi mutation
    API->>Auth: decode JWT, kiểm tra sid/user/revoked/expiry

    B->>API: POST /auth/refresh + cookie + X-CSRF-Token
    Auth->>Repo: CAS rotate refresh session
    API-->>B: access JWT và refresh cookie mới

    B->>API: WebSocket subprotocol avatar-chat, bearer.access-JWT
    API->>Auth: xác thực access JWT trước accept và trước mỗi message
```

Access token chỉ nằm trong memory của trang. Refresh token chỉ nằm trong cookie `HttpOnly`,
không được trả cho JavaScript. Logout thu hồi auth session server-side nên REST và WebSocket
mất quyền ngay. Chi tiết nằm trong [authentication.md](authentication.md).

## 3. Quyền session

| Actor | Xem session/transcript/media | Gửi chat | Đổi public/private | Quản lý share | Đóng session |
|---|---:|---:|---:|---:|---:|
| Owner | Có | Có | Có | Có | Có |
| Shared `interactor` | Có | Có | Không | Không | Không |
| Shared `viewer` | Có | Không | Không | Không | Không |
| User đăng nhập xem public | Có | Không, trừ khi có share `interactor` | Không | Không | Không |
| Anonymous qua `/watch/{id}` | Có, chỉ khi public | Không | Không | Không | Không |

`public` không đồng nghĩa có quyền chat. Anonymous watch không render input gửi tin nhắn.
Khi owner chuyển public về private, participant anonymous bị ngắt khỏi LiveKit; các user còn
share hợp lệ vẫn được giữ. Khi thu hồi share, participant tương ứng cũng bị revoke khỏi room.

## 4. Tạo session và khởi tạo media

### 4.1. aiortc mode

```mermaid
sequenceDiagram
    participant B as Browser
    participant API as FastAPI
    participant Core as CreateSession / NegotiateStream
    participant Repo as SessionRepository
    participant AIO as AiortcAvatarStreamAdapter

    B->>API: POST /api/sessions + character_id + visibility
    API->>Core: create(owner_user_id, ...)
    Core->>Repo: save status=created, avatar=idle
    API-->>B: session_id + offer_url + chat_url

    B->>API: POST /sessions/{id}/webrtc/offer
    API->>Core: require owner + CSRF
    Core->>Repo: status created -> media_starting
    Core->>AIO: stable video/audio tracks + SDP answer
    Core->>Repo: status ready, avatar idle
    API-->>B: SDP answer
    AIO-->>B: WebRTC audio/video
```

aiortc mode phù hợp local và owner duy nhất. Viewer chia sẻ nhiều người cần LiveKit mode.

### 4.2. LiveKit mode

```mermaid
sequenceDiagram
    participant B as Authenticated browser
    participant API as FastAPI
    participant Core as ProvisionMedia
    participant DB as SessionRepository
    participant R as Redis Streams
    participant W as avatar-worker
    participant L as LiveKit

    B->>API: POST /sessions/{id}/media/access
    API->>Core: require VIEW, nếu chưa READY thì require MANAGE
    Core->>DB: created/failed -> media_starting
    Core->>L: tạo publisher/subscriber token giới hạn room
    Core->>R: avatar.session.start(command_id)
    R->>W: command cho RENDER_WORKER_ID
    W->>L: publish stable audio + video tracks
    W->>R: avatar.session.ready(command_id)
    Core->>DB: media_starting -> ready
    API-->>B: subscriber token + room URL
    B->>L: subscribe audio/video qua WebRTC
```

Provision idempotent khi session đã `ready`: API chỉ cấp subscriber token mới, không restart
worker. Anonymous chỉ dùng endpoint `/api/public/sessions/{id}/media/access` khi session đang
public và ready.

## 5. Chat, persistent streaming và fan-out

```mermaid
sequenceDiagram
    autonumber
    participant B1 as Browser gửi
    participant API1 as API replica 1
    participant H as HandleMessage
    participant DB as ConversationRepository
    participant M as Reply provider
    participant P as Redis Pub/Sub
    participant API2 as API replica 2
    participant B2 as Browser khác

    B1->>API1: WS user_message
    API1->>API1: xác thực token + quyền CHAT
    API1->>H: execute(actor, session, content)
    H->>DB: append USER và cấp sequence=1
    H->>P: user_message snapshot
    P-->>API1: event
    P-->>API2: event
    API1-->>B1: user_message
    API2-->>B2: user_message

    H->>DB: session idle -> thinking
    H->>P: avatar_state=thinking
    H->>H: chờ THINKING_MIN_DURATION
    H->>DB: append ASSISTANT draft, sequence=2, revision=0, streaming
    H->>P: assistant_started
    H->>DB: session thinking -> speaking
    H->>P: avatar_state=speaking

    H->>M: stream_reply(history)
    loop mỗi chunk
        M-->>H: text chunk
        H->>DB: CAS update full content, revision + 1
        H->>P: assistant_chunk full snapshot
        P-->>B1: cùng message_id/sequence/revision
        P-->>B2: cùng message_id/sequence/revision
    end

    H->>DB: CAS status=complete, revision + 1
    H->>DB: session speaking -> idle
    H->>P: avatar_state=idle + assistant_complete
```

Chunk là toàn bộ content hiện tại, không phải delta. Browser reconcile theo `message_id` và
chỉ nhận revision không cũ hơn; vì thế event trùng hoặc đến lệch thứ tự không nhân đôi text.
Sequence quyết định thứ tự bubble. Một lock theo session chặn message đồng thời trong cùng
process; repository version/revision phát hiện stale write.

Nếu provider lỗi sau khi draft được tạo, core lưu `status=failed`, khôi phục renderer/domain
về `idle`, rồi phát `avatar_state=idle` và `assistant_failed`.

## 6. Reconnect và catch-up

```mermaid
sequenceDiagram
    participant B as Browser
    participant WS as WebSocket
    participant API as REST API
    participant DB as ConversationRepository

    WS--xB: mất mạng / API restart
    B->>B: disable input, backoff 0.5s -> tối đa 10s
    B->>WS: reconnect với access JWT hiện tại
    alt WebSocket trả 1008 do token hết hạn
        B->>API: rotate refresh token
        B->>WS: reconnect bằng access JWT mới
    end
    WS-->>B: connected
    B->>API: GET /sessions/{id}
    API-->>B: actor-specific session snapshot + version
    B->>API: GET messages với cursor sequence:revision, limit 100
    API->>DB: list_after(cursor)
    DB-->>B: snapshot mới hơn cursor
    B->>B: reconcile và lặp page nếu đủ 100 item
```

Redis Pub/Sub không lưu event. Sau reconnect, `SessionRepository` và `ConversationRepository`
mới là source of truth.
Public Watch không dùng chat WebSocket; trang poll endpoint public mỗi giây với cùng cursor.

### 6.1. Đồng bộ cấu hình giữa nhiều browser

```mermaid
sequenceDiagram
    participant A as Browser A
    participant API as FastAPI
    participant DB as SessionRepository
    participant E as Redis Pub/Sub
    participant B as Browser B

    A->>API: PATCH visibility public thành private
    API->>DB: optimistic save và tăng version
    API->>E: session_updated, session_version
    E-->>A: invalidation event
    E-->>B: invalidation event
    B->>API: GET /sessions/{id}
    API-->>B: visibility private, public_url null, version mới
    B->>B: ẩn Copy URL và cập nhật card/select
```

Event chỉ là tín hiệu invalidation, không mang `access_role` hay `can_chat`, vì hai user trên
cùng session có thể có quyền khác nhau. Endpoint snapshot tính lại owner/interactor/viewer/public
theo access token của từng browser. Frontend bỏ snapshot có version cũ hơn state đang giữ.

`session_shares_updated` dùng cùng cơ chế. Owner tab reload danh sách share; interactor/viewer
reload snapshot để bật/tắt chat. Nếu share bị thu hồi trên private session, hub đóng WebSocket
với reason `Access revoked`, frontend ngắt media và xóa các quyền điều khiển.

## 7. Video/audio pipeline

Cả hai media runtime giữ một video track và một audio track ổn định trong suốt session. State
chỉ đổi nguồn frame `idle`, `thinking`, `speaking`, không renegotiate track.

- Mỗi frame được normalize về resolution cấu hình và có frame counter góc trái.
- Audio PCM mono 48 kHz chạy liên tục; hiện là silence hoặc beep debug.
- Shared media clock/`AVSynchronizer` giữ cadence và A/V sync.
- `AV_SYNC_DEBUG` tạo beep 1 kHz và flash trắng trên cùng timeline.
- Worker dùng bounded queue; audio timeline là master clock và video chạy theo FPS cấu hình.

## 8. Đóng session và cleanup

Đóng WebSocket **không đóng session**. Điều này là chủ ý để browser reconnect và catch-up mà
không làm dừng media của các viewer khác.

```mermaid
sequenceDiagram
    participant O as Owner browser
    participant API as FastAPI
    participant C as CloseSession
    participant DB as SessionRepository
    participant Media as aiortc / Redis worker adapter
    participant Room as LiveKit
    participant Events as RealtimeEventBus
    participant V as Connected viewers

    O->>API: DELETE /api/sessions/{id} + Bearer + CSRF
    API->>C: require MANAGE
    C->>DB: status -> closed, avatar -> idle
    C->>Media: close / avatar.session.stop
    C->>Room: close room nếu có
    API->>Events: publish session_closed
    Events-->>O: chuyển sang read-only
    Events-->>V: chuyển sang read-only
    API-->>O: 204
```

Frontend giữ transcript nhưng ngắt WebSocket/media, khóa chat, visibility và share controls,
đồng thời phủ vùng avatar bằng thông báo “Session đã đóng”. Card session được đánh dấu closed;
mở lại card không tạo WebSocket hay WebRTC mới. Public Watch theo dõi session status và chuyển
sang thông báo kết thúc thay vì retry provision vô hạn.

Core cũng xem closed session là immutable: chat, negotiate/provision, đổi visibility và thay
đổi share trả `409 SessionClosed`. Các API xem session và transcript vẫn hoạt động để người có
quyền xem lại lịch sử.

Khi application shutdown, container dừng realtime event bus, media adapter và database theo
thứ tự. Worker process và LiveKit có lifecycle container riêng.

## 9. Nguồn dữ liệu và giới hạn scale hiện tại

- Không có `DATABASE_URL`: toàn bộ user/session/message/share/auth state ở memory và mất khi
  restart; không dùng được cho nhiều replica.
- Có database chung + `REALTIME_EVENT_BUS=redis`: history và realtime fan-out dùng được giữa
  replica.
- Lock xử lý chat vẫn nằm trong process. Hai replica cùng nhận chat cho một session có thể
  cạnh tranh; optimistic version/CAS sẽ từ chối stale write nhưng chưa thay distributed lock.
- Login rate limiter còn in-memory theo replica.
- Redis Streams chưa có pending reclaim/DLQ; worker vẫn dùng một `RENDER_WORKER_ID` tĩnh.
- Health endpoint hiện mới xác nhận API process, chưa aggregate Redis/DB/worker/LiveKit.
