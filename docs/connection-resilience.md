# PersonaForge — Connection Resilience và Failure Handling

Tài liệu này bao phủ các lỗi phát sinh từ kết nối giữa Core API, Control Plane,
Redis, PostgreSQL, Worker, LiveKit và Browser. Nội dung được tách khỏi cơ chế
GPU/Child failover trong `session-recovery-failover.md`.

## 1. Nguyên tắc chung

- Mọi request/command phải có `request_id`, `trace_id`, `session_id`.
- Mọi command có `command_id` idempotent.
- Mọi runtime có `runtime_generation` và `fencing_token`.
- Không dùng timeout mạng để kết luận ngay rằng process đã chết.
- Phân biệt `DEGRADED`, `RECOVERING`, `FAILED` và `CLOSED`.
- Không retry vô hạn.

## 2. Network partition và split-brain

Worker có thể vẫn chạy nhưng không kết nối được Control. Control cũng có thể
nhìn thấy Worker đã chết trong khi Worker vẫn còn publish LiveKit.

Quy tắc:

```text
Worker mất kết nối Control
    → không nhận command mới
    → không tự nhận session mới
    → tiếp tục gia hạn local lease trong giới hạn ngắn

Control hết lease Worker
    → cấp fencing token mới cho recovery
    → Worker/Child cũ không còn quyền publish hoặc xử lý command
```

Mọi media event phải kiểm tra fencing token. Worker cũ không có token hợp lệ
phải dừng publish dù process vẫn còn sống.

## 3. Distributed lock theo session

Mỗi session chỉ được có một recovery workflow hoạt động:

```text
lock:session:{session_id}:recovery
```

Lock chứa:

```text
recovery_id
owner_id
runtime_generation
expires_at
```

Nếu một recovery đang chạy, các request recovery khác phải trả về trạng thái
đang xử lý thay vì tạo Child thứ hai.

## 4. Redis resilience

### 4.1 Redis command retry

Chỉ retry các lỗi tạm thời:

```text
ConnectionError
TimeoutError
BusyLoadingError
```

Không retry:

```text
AuthenticationError
NoPermissionError
ResponseError do schema/command sai
```

Backoff đề xuất:

```text
250ms → 500ms → 1s → 2s → 5s
```

### 4.2 Redis Stream pending message

Worker phải dùng consumer group và xử lý pending message:

```text
1. XAUTOCLAIM message quá idle timeout.
2. Kiểm tra command_id đã hoàn thành chưa.
3. Nếu đã hoàn thành → ACK và bỏ qua.
4. Nếu chưa → xử lý lại với cùng command_id.
```

Không tạo command mới chỉ vì lần đọc trước bị timeout.

### 4.3 Redis outage

Khi Redis mất kết nối:

```text
Worker → trạng thái DEGRADED
Worker → dừng nhận command mới
Worker → không tự ý tạo session
Control → giữ session RECOVERING nếu lease chưa hết
```

Sau khi Redis hồi phục, Worker phải re-authenticate và re-register/renew
trước khi nhận command mới.

## 5. PostgreSQL outage

PostgreSQL là nguồn sự thật cho session và resource metadata. Khi database lỗi:

- Không tạo session mới.
- Không commit state chuyển đổi quan trọng.
- Không đánh dấu recovery thành công nếu chưa persist được.
- Retry transaction với backoff.
- Dùng Redis state chỉ để tiếp tục quan sát tạm thời, không thay thế commit DB.

Recovery phải dùng transaction và optimistic version:

```text
UPDATE sessions
SET runtime_generation = :new_generation,
    assigned_worker_id = :worker_id,
    version = version + 1
WHERE session_id = :session_id
  AND version = :expected_version
```

Nếu update không ảnh hưởng row, workflow đã stale và phải dừng.

## 6. Worker ↔ Control connection

Worker dùng heartbeat định kỳ và reconnect với exponential backoff. Mỗi lần
reconnect phải:

```text
1. Validate bootstrap/management credential.
2. Renew hoặc re-register identity.
3. Gửi lại GPU inventory.
4. Restore heartbeat stream.
5. Chỉ nhận command sau khi Control xác nhận READY.
```

Không xem HTTP response `200` của registration là đủ; Worker phải xác nhận
Redis ACL credential mới bằng một PING và thao tác stream tối thiểu.

## 7. LiveKit connection

### 7.1 Publisher disconnect

Child phải reconnect theo backoff. Nếu vượt quá reconnect timeout:

```text
Child → báo livekit.publisher.failed
Control → session RECOVERING
Master → tạo runtime generation mới hoặc migrate
```

### 7.2 Token hết hạn

Control/API phải cấp token mới trước expiry. Child không tự tạo token bằng
secret cục bộ. Khi refresh thất bại, session chuyển `DEGRADED` thay vì loop
reconnect vô hạn.

### 7.3 Track replacement

Child mới join cùng room với identity có generation mới:

```text
avatar:{session_id}:generation-{n}
```

Browser phải unsubscribe participant cũ và subscribe participant mới. Không tạo
room mới khi chỉ thay runtime.

## 8. Command idempotency

Mỗi command lưu trạng thái:

```text
RECEIVED
RUNNING
SUCCEEDED
FAILED
```

Redis key:

```text
command:{command_id}:result
```

Khi nhận lại cùng `command_id`:

```text
SUCCEEDED → trả lại result cũ
RUNNING   → không spawn thêm Child
FAILED    → chỉ retry nếu policy cho phép
```

Các lệnh `session.start`, `session.stop`, `session.recover` phải idempotent.

## 9. Lease và fencing

Lease cần có:

```text
lease_id
session_id
worker_id
gpu_uuid
runtime_generation
fencing_token
expires_at
```

Trước khi xử lý command hoặc publish media, Worker kiểm tra lease còn hợp lệ.
Lease hết hạn thì dừng xử lý và không được renew nếu token đã bị thay thế.

## 10. Client mất kết nối

Session không đóng ngay khi Browser rời mạng ngắn hạn.

```text
client disconnected
    ↓
giữ session trong grace period
    ↓
client reconnect + refresh token
    ↓
subscribe lại participant/track hiện tại
```

Sau grace period, nếu policy cho phép, session chuyển `IDLE`; chỉ đóng khi
explicit close hoặc idle timeout.

## 11. Retry và circuit breaker

Mỗi loại connection có retry budget riêng:

```text
Redis: 5 attempts
Control HTTP: 5 attempts
LiveKit signaling: 5 attempts
Database transaction: 3 attempts
```

Nếu vượt budget:

```text
DEGRADED → circuit open → chờ cooldown → thử half-open
```

Circuit breaker ngăn việc reconnect/spawn liên tục làm quá tải hệ thống.

## 12. Failure matrix

| Lỗi | Chủ thể phát hiện | Trạng thái | Hành động |
|---|---|---|---|
| Redis timeout | Worker/Control | DEGRADED | Retry rồi reconnect |
| Redis AUTH failure | Worker | FAILED | Không retry, re-register |
| Redis stream pending | Worker | RECOVERING | XAUTOCLAIM + dedupe |
| PostgreSQL timeout | Control | DEGRADED | Retry transaction |
| Worker heartbeat timeout | Control | RECOVERING | Failover Worker |
| LiveKit disconnect | Child | RECOVERING | Reconnect/republish |
| Token expired | Child/Browser | DEGRADED | Refresh token |
| Network partition | Control + Worker | DEGRADED | Fencing sau lease expiry |
| Duplicate recovery | Control | RUNNING | Distributed lock |
| Stale Child publish | Control/LiveKit layer | REJECTED | Fencing token |
| Retry budget exhausted | Circuit breaker | FAILED | Alert/operator action |

## 13. Observability

Mỗi log/event connection phải chứa:

```text
session_id
request_id
command_id
recovery_id
runtime_generation
worker_id
gpu_uuid
connection_target
attempt
backoff_ms
outcome
```

Metrics tối thiểu:

```text
connection_reconnect_total
connection_failure_total
redis_command_retry_total
stream_pending_claim_total
livekit_reconnect_total
session_recovery_duration_ms
session_recovery_failure_total
stale_command_rejected_total
```

## 14. Acceptance tests

- Tắt Redis trong thời gian ngắn rồi bật lại: Worker reconnect và tiếp tục
  heartbeat.
- Làm Redis AUTH sai: Worker không retry vô hạn.
- Để Redis Stream message pending: Worker xử lý lại đúng một lần theo
  `command_id`.
- Chặn mạng Worker nhưng process vẫn sống: fencing ngăn Worker cũ publish.
- Làm LiveKit disconnect: Child reconnect hoặc recovery đúng policy.
- Gửi hai recovery request đồng thời: chỉ một Child mới được tạo.
- Cho Browser offline rồi online: subscribe lại track mà không tạo room mới.
- Làm PostgreSQL unavailable trong recovery: không đánh dấu recovery thành công
  khi chưa persist được state.

