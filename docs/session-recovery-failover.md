# PersonaForge — Session Recovery và Worker Failover

Tài liệu này mô tả cơ chế phục hồi session khi Composite Child, Worker Master,
GPU hoặc toàn bộ host vật lý gặp lỗi. Mục tiêu là tạo lại runtime và LiveKit
stream trên Worker/GPU khác mà không làm mất session chat.

## 1. Phạm vi

Các trường hợp cần xử lý:

- Composite Child bị crash hoặc bị kill.
- Child mất heartbeat hoặc socket/gRPC bị ngắt.
- Worker Master bị restart.
- Worker mất heartbeat với Control Plane.
- GPU bị `busy`, `unavailable`, `device lost` hoặc CUDA context failure.
- Host vật lý mất kết nối.
- LiveKit publisher bị disconnect.

Session chỉ được phục hồi tự động nếu chưa ở trạng thái `CLOSED` hoặc `FAILED`.

## 2. Hành vi hiện tại

Khi stop một Worker hiện tại:

```text
Worker bị stop
    ↓
Child mất kết nối
    ↓
LiveKit stream dừng
    ↓
Control phát hiện heartbeat timeout
    ↓
Lease hết hạn và được release
```

Pipeline hiện tại có thể phát hiện lỗi và giải phóng resource, nhưng chưa có
`SessionRecoveryCoordinator` để tự động provision lại session trên Worker khác.
Vì vậy stream chưa tự động xuất hiện lại.

## 3. Kiến trúc mục tiêu

```text
                         Control Plane
                              │
                 SessionRecoveryCoordinator
                              │
                    SessionMigrationService
                              │
                ┌─────────────┴─────────────┐
                │                           │
          Worker A                    Worker B
          unhealthy                   available
                │                           │
            Child cũ                  Child mới
                │                           │
                └────────── LiveKit Room ──┘
```

Session state phải được lưu bền vững trong PostgreSQL/Redis, không phụ thuộc
vào memory của Child.

## 4. Durable session runtime state

PostgreSQL nên lưu:

```text
session_id
room_name
character_id
voice_id
runtime_spec
last_completed_turn
last_audio_pts
last_sequence
last_state
assigned_worker_id
assigned_gpu_uuid
runtime_generation
recovery_attempt
status
updated_at
```

Redis dùng cho dữ liệu realtime:

```text
session:{session_id}:state
session:{session_id}:lease
session:{session_id}:recovery
session:{session_id}:last-output
```

Trạng thái session:

```text
STARTING → READY → SPEAKING
                   ↓
                THINKING
                   ↓
                  IDLE

READY/SPEAKING/THINKING/IDLE → RECOVERING
RECOVERING → READY
RECOVERING → DEGRADED
RECOVERING → FAILED
mọi trạng thái → CLOSED
```

## 5. Phát hiện lỗi

### 5.1 Child Supervisor

Worker Master phải theo dõi:

- Child process exit code.
- Child heartbeat.
- gRPC/UDS socket health.
- thời điểm command cuối cùng.
- thời điểm media output cuối cùng.
- LiveKit publisher connection.

Khi phát hiện lỗi:

```text
1. Đánh dấu Child = FAILED.
2. Không gửi command mới đến Child.
3. Terminate process nếu process còn treo.
4. Release GPU slot và Child lease.
5. Gửi worker.child.failed.
6. Kích hoạt recovery workflow.
```

### 5.2 Control Plane

Control theo dõi heartbeat Worker. Khi vượt quá timeout:

```text
1. Worker = UNHEALTHY.
2. Các lease của Worker được đánh dấu expired.
3. Session đang chạy trên Worker chuyển sang RECOVERING.
4. Scheduler không cấp session mới cho Worker đó.
5. SessionRecoveryCoordinator bắt đầu migration.
```

## 6. Quy trình phục hồi Child

Nếu Worker vẫn khỏe và chỉ Child lỗi:

```text
Child crash
    ↓
Master phát hiện
    ↓
Session = RECOVERING
    ↓
Release Child/GPU lease
    ↓
Spawn Child mới trên GPU đã cấp
    ↓
Restore runtime spec
    ↓
Join LiveKit room
    ↓
Publish track mới
    ↓
Session = IDLE/READY
```

Mặc định không phát lại câu đang nói dở. Session chuyển về `IDLE` để tránh
audio/video bị lặp hoặc lệch timeline. Chính sách resume có thể bổ sung sau.

## 7. Quy trình Worker failover

Khi Worker A bị stop hoặc mất host:

```text
Worker A heartbeat timeout
    ↓
Worker A = UNHEALTHY
    ↓
Session A = RECOVERING
    ↓
Release lease Worker A/GPU A
    ↓
Scheduler chọn Worker B + GPU B
    ↓
Control gửi avatar.session.start đến Worker B
    ↓
Worker B spawn Composite Child
    ↓
Child restore session runtime
    ↓
Child join lại LiveKit room
    ↓
Child publish Audio/Video/Motion track
    ↓
Client nhận track mới
    ↓
Session = READY hoặc IDLE
```

Nếu không có Worker/GPU đủ capacity, session chuyển sang `DEGRADED` và client
hiển thị trạng thái đang chờ resource.

## 8. Runtime generation và fencing token

Mỗi lần tạo runtime mới phải tăng:

```text
runtime_generation
```

Ví dụ:

```text
generation 1: Worker A / Child c1
generation 2: Worker B / Child c2
```

Mọi command, heartbeat và media event phải chứa:

```text
session_id
runtime_generation
recovery_id
fencing_token
```

Chỉ generation mới nhất được phép publish. Nếu Child cũ sống lại hoặc gửi
event trễ, Control/Worker phải từ chối với lỗi:

```text
stale runtime generation
```

Điều này ngăn Child cũ publish ngược vào LiveKit sau khi session đã migrate.

## 9. LiveKit republish

Client giữ nguyên room và session. Child mới dùng participant identity có
generation mới:

```text
avatar:{session_id}:generation-{runtime_generation}
```

Không dùng identity cố định vì participant cũ có thể chưa rời room hoàn toàn.

### Server-render mode

```text
AudioTrack mới
VideoTrack mới
    ↓
LiveKit room hiện tại
```

### Client-render mode

```text
AudioTrack mới
Motion DataTrack mới
    ↓
ExpressionBuffer mới
```

Frontend phải:

1. Xóa track của participant cũ.
2. Subscribe participant/track mới.
3. Giữ nguyên audio/video element hoặc renderer container.
4. Reset ExpressionBuffer theo audio PTS mới.
5. Hiển thị “Đang khôi phục kết nối…” trong thời gian ngắn.

## 10. Khôi phục hội thoại

Child mới không lấy context từ memory Child cũ. Nó restore từ PostgreSQL/Redis:

```json
{
  "session_id": "...",
  "runtime_generation": 4,
  "character_id": "...",
  "voice_id": "...",
  "last_completed_turn": 12,
  "current_state": "idle"
}
```

Chính sách giai đoạn đầu:

```text
Đang nói dở khi lỗi → hủy utterance hiện tại → chuyển IDLE
```

Không phát lại audio cũ trừ khi sau này có yêu cầu resume chính xác.

## 11. GPU failure và quarantine

Các lỗi sau phải được xem là GPU failure:

```text
CUDA-capable device is busy or unavailable
CUDA initialization failure
device lost
out of memory
```

GPU chuyển sang:

```text
QUARANTINED
```

Trong thời gian quarantine:

- Scheduler không cấp session mới.
- Các session đang chạy được migrate nếu có Worker khác.
- Heartbeat tiếp tục thu thập VRAM/utilization/temperature.
- Sau cooldown, GPU được kiểm tra lại bằng nvitop/NVML.

Nếu GPU khỏe lại thì chuyển về `READY`; nếu lỗi lặp lại thì `UNHEALTHY`.

### 11.1 Không cấp lại GPU lỗi

Nếu Child bị lỗi trong lúc sử dụng GPU `gpu_uuid=A`, Child mới phục hồi không
được dùng lại GPU `A` trong cùng recovery workflow.

```text
Child cũ chạy trên GPU A
    ↓
CUDA/GPU failure
    ↓
Master báo lỗi với gpu_uuid=A
    ↓
Control đánh dấu GPU A = QUARANTINED
    ↓
Scheduler loại GPU A khỏi candidate list
    ↓
Child mới được cấp GPU B hoặc Worker khác/GPU C
```

Assignment mới phải thỏa mãn:

```text
new_gpu_uuid != failed_gpu_uuid
```

Điều kiện này phải được kiểm tra ở hai nơi:

1. Scheduler loại GPU lỗi khi lập danh sách candidate.
2. Control kiểm tra lại assignment trước khi ghi lease và gửi command.

Worker Master chỉ được kiểm tra local mapping và từ chối assignment nếu GPU
không tồn tại hoặc không đúng UUID; Master không được tự ý đổi sang GPU khác.

### 11.2 Thành phần chịu trách nhiệm

```text
Composite Child
    phát hiện exception CUDA/model
    ↓
Worker Master
    gắn gpu_uuid vào failure event
    dừng Child và release local process
    ↓
Control Plane
    xác định mức độ GPU failure
    tạo GPU quarantine record
    cập nhật session recovery state
    ↓
Scheduler
    loại failed_gpu_uuid khỏi candidate list
    chọn worker_id + gpu_uuid mới
    tạo lease mới
    ↓
Worker Master mới/cùng Worker
    validate đúng assignment
    spawn Child với CUDA_VISIBLE_DEVICES mới
```

Vì vậy việc chọn GPU thay thế thuộc về **Control Plane/Scheduler**, không thuộc
về Child và cũng không thuộc về Worker Master. Worker Master chỉ thực thi
assignment đã được cấp.

### 11.3 Phạm vi quarantine

Quarantine nên có scope theo mức độ lỗi:

```text
CHILD_ONLY
    chỉ Child/runtime hiện tại bị lỗi
    có thể thử GPU khác cùng Worker

GPU_LOCAL
    GPU A bị lỗi
    cấm cấp GPU A cho mọi Child trên Worker đó

HOST_WIDE
    nhiều GPU hoặc NVIDIA runtime của host lỗi
    cấm toàn bộ Worker host
```

Sự kiện nên lưu:

```text
failed_gpu_uuid
worker_id
session_id
child_id
failure_code
failure_message
quarantine_scope
quarantined_at
retry_after
```

### 11.4 Recovery attempt có GPU exclusion set

Mỗi recovery workflow giữ một exclusion set trong Redis/PostgreSQL:

```json
{
  "recovery_id": "...",
  "excluded_gpu_uuids": [
    "GPU-A"
  ],
  "attempt": 2
}
```

Nếu attempt tiếp theo tiếp tục thất bại trên GPU `B`, set được mở rộng:

```text
attempt 1: exclude {GPU-A}
attempt 2: exclude {GPU-A, GPU-B}
attempt 3: migrate Worker khác, exclude {GPU-A, GPU-B}
```

Không được reset exclusion set trong cùng một recovery workflow. Exclusion chỉ
được xóa khi:

- recovery thành công và session ổn định qua cooldown; hoặc
- Control operator xác nhận GPU đã được kiểm tra lại.

## 12. Retry policy

Đề xuất giới hạn retry:

```text
Attempt 1: restart Child trên cùng GPU.
Attempt 2: chọn GPU khác cùng Worker.
Attempt 3: migrate sang Worker khác.
Attempt 4: session = DEGRADED/FAILED.
```

Backoff:

```text
1s → 3s → 10s → 30s
```

Không retry vô hạn để tránh vòng lặp spawn/restart và làm quá tải GPU.

## 13. Event và API

Control nên phát các event:

```text
worker.heartbeat.timeout
worker.unhealthy
session.recovery.started
session.recovery.worker_assigned
session.recovery.child_spawned
session.recovery.livekit_connected
session.recovery.completed
session.recovery.failed
```

API trạng thái:

```text
GET /api/sessions/{session_id}/status
```

Ví dụ:

```json
{
  "session_id": "...",
  "status": "recovering",
  "runtime_generation": 4,
  "recovery_attempt": 2,
  "worker_id": "gpu-host-02:...",
  "gpu_uuid": "GPU-...",
  "retry_after_ms": 3000
}
```

## 14. Component cần bổ sung

```text
Control Plane
├── WorkerFailureDetector
├── SessionRecoveryCoordinator
├── SessionMigrationService
├── RuntimeGenerationRepository
└── RecoveryRetryPolicy

Worker Master
├── ChildHeartbeatMonitor
├── ChildRestartController
├── GPUQuarantineReporter
└── LiveKitRepublishCoordinator

Frontend
├── ParticipantReplacementHandler
├── TrackReplacementHandler
└── RecoveryStatePresenter
```

## 15. Lộ trình triển khai

### Phase 1 — Child recovery cùng Worker

- Durable runtime state.
- Child heartbeat/exit detection.
- Master restart Child.
- Runtime generation và fencing token.
- LiveKit republish trong cùng room.

### Phase 2 — Worker failover

- Worker heartbeat timeout.
- Release stale lease.
- SessionRecoveryCoordinator.
- Scheduler chọn Worker/GPU thay thế.
- Restore session trên Worker mới.

### Phase 3 — GPU quarantine

- Phân loại CUDA error.
- Quarantine GPU.
- Cooldown và health recheck.
- Cập nhật scheduler capacity.

### Phase 4 — Frontend recovery UX

- Subscribe participant mới.
- Track replacement.
- Hiển thị recovery state.
- Không tạo room mới khi session chỉ đang recover.

## 16. Acceptance test

### Child crash

```text
1. Tạo session.
2. Kill Composite Child.
3. Xác nhận Master spawn Child mới.
4. Xác nhận cùng room có participant generation mới.
5. Xác nhận stream hoạt động lại.
```

### Worker failover

```text
1. Có Worker A và Worker B cùng register.
2. Tạo session trên Worker A.
3. Stop Worker A.
4. Chờ heartbeat timeout.
5. Xác nhận session = RECOVERING.
6. Xác nhận Scheduler cấp Worker B/GPU B.
7. Xác nhận Child mới join cùng LiveKit room.
8. Xác nhận browser nhận track mới.
```

### GPU failure

```text
1. Làm GPU được cấp trả về CUDA busy/unavailable.
2. Xác nhận GPU chuyển QUARANTINED.
3. Xác nhận session được migrate nếu còn capacity.
4. Xác nhận scheduler không cấp session mới vào GPU lỗi.
```

## 17. Nguyên tắc bắt buộc

- Không lưu recovery state trong memory của Child.
- Không tạo room LiveKit mới cho cùng session khi chỉ migrate runtime.
- Không để Child cũ publish sau khi đã bị fencing.
- Không cấp lại GPU đang quarantine.
- Không retry vô hạn.
- Mọi recovery phải truy vết được bằng `session_id`, `recovery_id`,
  `runtime_generation`, `worker_id`, `gpu_uuid`.

## 18. Trạng thái triển khai

Các phần đã được nối vào pipeline hiện tại:

- Redis Control Plane lưu `recovery-start` payload để có thể tạo lại runtime.
- Reconciler phát hiện Worker mất heartbeat và kích hoạt migration.
- Scheduler loại Worker lỗi và GPU lỗi khỏi candidate list.
- Child failure được Worker ghi nhận bằng session failure marker có TTL.
- Redis reconciler phát hiện marker và migrate Child sang GPU/Worker khác.
- Recovery có distributed lock theo `session_id` để tránh tạo hai Child.
- Khi chưa có capacity, recovery được lưu vào pending set và retry ở vòng sau.
- Start payload có đủ runtime/resource metadata để rebuild `CompositeRequirement`.
- Lease/generation cũ được release trước khi cấp assignment mới.
- Frontend owner và public viewer phát hiện `TrackUnsubscribed`, publisher
  disconnect hoặc LiveKit mất kết nối; trong lúc đó khóa ô gửi chat và hiển thị
  `Đang migrate stream…`.
- Frontend có watchdog dựa trên `requestVideoFrameCallback` và trạng thái
  `MediaStreamTrack.mute`; nếu video đứng ở frame cuối quá khoảng 1,8 giây,
  UI bắt đầu migrate ngay, không chờ LiveKit phát sự kiện unsubscribe.
- Trong lúc viewer room vẫn còn `connected` nhưng publisher chưa publish lại,
  frontend chỉ chờ và retry nhẹ; không disconnect/reconnect viewer liên tục,
  tránh tạo churn và làm chậm track mới.
- Owner có thể gọi endpoint `POST /api/sessions/{id}/media/recover` trong lúc
  migrate. Endpoint idempotently rehydrate assignment/Child khi session vẫn
  `READY` nhưng Redis lease đã hết hạn; viewer chỉ được cấp access token và
  không tự kích hoạt workload.
- Frontend tự gọi lại media-access với exponential backoff. Khi Child mới
  publish track vào cùng room, UI tự gắn track mới và mở lại ô chat.

Các phần vẫn cần hoàn thiện ở phase tiếp theo:

- API công khai trả về recovery progress chi tiết cho Browser.
- LiveKit participant identity theo `runtime_generation` và fencing phía client.
- Circuit breaker và retry budget cho từng loại connection.
- Persist recovery audit/history vào PostgreSQL.
- GPU quarantine duration và health recheck chuyên biệt cho CUDA device loss.
