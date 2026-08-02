# Luồng xử lý Avatar Chat WebRTC Server

Tài liệu này mô tả luồng xử lý hiện tại của server theo kiến trúc Hexagonal
(Ports & Adapters). Core không phụ thuộc FastAPI, aiortc, SQLAlchemy hay model service.

## 1. Kiến trúc tổng thể

```mermaid
flowchart LR
    Browser[Browser client]

    subgraph Inbound[Driving adapters]
        HTTP[FastAPI REST]
        WS[FastAPI WebSocket]
    end

    subgraph Core[Application core]
        UC1[CreateSession]
        UC2[NegotiateStream]
        UC3[HandleMessage]
        UC4[GetConversation]
        UC5[CloseSession]
        Domain[AvatarSession state machine]

        P1[[SessionRepository]]
        P2[[ConversationRepository]]
        P3[[ReplyGeneratorPort]]
        P4[[AvatarStreamPort]]
    end

    subgraph Outbound[Driven adapters]
        Memory[In-memory repositories]
        SQL[SQLAlchemy repositories]
        Scripted[Scripted reply]
        Model[HTTP streaming model]
        WebRTC[AiortcAvatarStreamAdapter]
    end

    Browser -->|REST| HTTP
    Browser <-->|Chat events| WS
    HTTP --> UC1
    HTTP --> UC2
    HTTP --> UC4
    HTTP --> UC5
    WS --> UC3

    UC1 --> Domain
    UC2 --> Domain
    UC3 --> Domain
    UC5 --> Domain

    UC1 --> P1
    UC2 --> P1
    UC3 --> P1
    UC3 --> P2
    UC3 --> P3
    UC2 --> P4
    UC3 --> P4
    UC5 --> P4

    Memory -.-> P1
    Memory -.-> P2
    SQL -.-> P1
    SQL -.-> P2
    Scripted -.-> P3
    Model -.-> P3
    WebRTC -.-> P4

    WebRTC -->|SRTP audio/video| Browser
```

Dependency được nối duy nhất tại `bootstrap/container.py`. Thay database, model service
hoặc WebRTC runtime chỉ cần thêm adapter và đổi composition root.

## 2. Khởi tạo session và WebRTC

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant API as FastAPI adapter
    participant CS as CreateSession
    participant Repo as SessionRepository
    participant NS as NegotiateStream
    participant Avatar as AvatarStreamPort / aiortc

    B->>API: POST /api/sessions
    API->>CS: execute(character_id)
    CS->>CS: Tạo AvatarSession(CREATED)
    CS->>Repo: save(session)
    Repo-->>CS: saved
    CS-->>API: session_id + character_id
    API-->>B: offer_url + chat_url

    B->>B: Tạo RTCPeerConnection
    B->>B: addTransceiver(video, recvonly)
    B->>B: addTransceiver(audio, recvonly)
    B->>B: createOffer + ICE gathering
    B->>API: POST /webrtc/offer (SDP offer)
    API->>NS: execute(session_id, offer)
    NS->>Repo: get(session_id)
    Repo-->>NS: AvatarSession(CREATED)
    NS->>Avatar: negotiate(session_id, offer)

    Avatar->>Avatar: Tạo RTCPeerConnection
    Avatar->>Avatar: Tạo shared MediaClock
    Avatar->>Avatar: Mở idle.mp4 và preload frame đầu
    Avatar->>Avatar: Thêm video track + audio track
    Avatar->>Avatar: setRemoteDescription
    Avatar->>Avatar: createAnswer + setLocalDescription
    Avatar-->>NS: SDP answer

    NS->>NS: session.connected() → IDLE
    NS->>Avatar: change_state(IDLE)
    NS->>Repo: save(session)
    NS-->>API: SDP answer
    API-->>B: SDP answer + avatar_state=idle
    B->>B: setRemoteDescription
    B->>B: Badge chuyển thành idle
    Avatar-->>B: WebRTC video + audio
```

## 3. Luồng chat và trạng thái avatar

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant WS as WebSocket adapter
    participant HM as HandleMessage
    participant Sessions as SessionRepository
    participant History as ConversationRepository
    participant Reply as ReplyGeneratorPort
    participant Media as AvatarStreamPort

    B->>WS: user_message
    WS->>HM: execute(session_id, content)
    HM->>Sessions: get(session_id)
    HM->>History: append(USER message)

    HM->>HM: IDLE → THINKING
    HM->>Sessions: save(THINKING)
    HM->>Media: change_state(THINKING)
    HM-->>WS: avatar_state=thinking
    WS-->>B: avatar_state=thinking
    HM->>HM: Chờ THINKING_MIN_DURATION

    HM->>History: list_recent()
    HM->>HM: THINKING → SPEAKING
    HM->>Sessions: save(SPEAKING)
    HM->>Media: change_state(SPEAKING)
    HM-->>WS: avatar_state=speaking
    WS-->>B: avatar_state=speaking

    loop Mỗi reply chunk
        HM->>Reply: stream_reply(...)
        Reply-->>HM: ReplyChunk
        HM-->>WS: assistant_chunk
        WS-->>B: assistant_chunk
    end

    HM->>History: append(ASSISTANT message)
    HM->>HM: SPEAKING → IDLE
    HM->>Sessions: save(IDLE)
    HM->>Media: change_state(IDLE)
    HM-->>WS: avatar_state=idle
    WS-->>B: avatar_state=idle
    HM-->>WS: assistant_complete
    WS-->>B: assistant_complete
```

Mỗi session có một `asyncio.Lock`. Nếu một message khác đến trong khi message hiện tại
đang xử lý, core trả `SessionBusy` thay vì làm hỏng state machine.

## 4. State machine

```mermaid
stateDiagram-v2
    [*] --> CREATED: POST /sessions
    CREATED --> CONNECTED: SDP negotiation thành công
    CONNECTED --> IDLE: Media tracks sẵn sàng
    IDLE --> THINKING: Nhận user message
    THINKING --> SPEAKING: Hết minimum delay và bắt đầu reply
    SPEAKING --> IDLE: Reply hoàn tất
    THINKING --> IDLE: Reply provider lỗi
    SPEAKING --> IDLE: Reply provider lỗi
    CREATED --> CLOSED: Đóng session
    CONNECTED --> CLOSED: Đóng session
    IDLE --> CLOSED: Đóng session
    THINKING --> CLOSED: Đóng session
    SPEAKING --> CLOSED: Đóng session
    CLOSED --> [*]
```

## 5. Video track, đổi clip và khôi phục EOF

`SwitchableAvatarVideoTrack` là outbound track ổn định. Việc đổi clip không thay track và
không yêu cầu SDP renegotiation.

```mermaid
flowchart TD
    Sender[RTCRtpSender gọi recv]
    Read[Đọc frame từ source hiện tại]
    EOF{MediaStreamError / EOF?}
    Changed{Generation đã đổi?}
    Restart[Mở lại clip của state hiện tại]
    Discard[Bỏ frame cũ]
    Timestamp[Gán PTS từ shared MediaClock 90 kHz]
    Counter[Vẽ frame counter góc trái]
    Pulse{Đang trong sync pulse?}
    Flash[Vẽ ô trắng góc phải]
    Encode[Trả frame cho WebRTC encoder]

    Sender --> Read
    Read --> EOF
    EOF -->|Không| Changed
    EOF -->|Có| Changed
    Changed -->|Source cũ| Discard
    Discard --> Read
    Changed -->|Source hiện tại bị EOF| Restart
    Restart --> Read
    Changed -->|Frame hợp lệ| Timestamp
    Timestamp --> Counter
    Counter --> Pulse
    Pulse -->|Có| Flash
    Pulse -->|Không| Encode
    Flash --> Encode
    Encode --> Sender
```

Khi đổi state:

```mermaid
sequenceDiagram
    participant Core as HandleMessage
    participant Track as SwitchableAvatarVideoTrack
    participant New as MediaPlayer mới
    participant Old as MediaPlayer cũ
    participant Sender as RTCRtpSender

    Core->>Track: switch(new_state)
    Track->>New: Mở clip tương ứng
    Track->>New: recv() preload frame đầu
    New-->>Track: first_frame
    Track->>Track: Swap source + tăng generation
    alt Sender còn đang đọc source cũ
        Track->>Track: Đánh dấu source cũ là retired
        Sender->>Track: Hoàn tất lượt đọc cũ
        Track->>Old: stop() sau khi active read = 0
    else Không còn active read
        Track->>Old: stop() ngay
    end
    Track-->>Core: Source mới đã sẵn sàng
```

Wrapper tự mở lại clip khi PyAV báo EOF ở cuối video 3 giây. Vì vậy frame counter tiếp tục
tăng qua nhiều vòng clip và qua mọi lần đổi trạng thái.

## 6. Audio câm và A/V sync debug

```mermaid
flowchart LR
    Clock[Shared MediaClock]
    VPTS[Video PTS = elapsed × 90,000]
    APTS[Audio PTS = elapsed × 48,000]
    Video[Switchable video track]
    Audio[Silent / debug audio track]
    Flash[Ô trắng 100 ms]
    Beep[Beep 1 kHz 100 ms]
    PC[RTCPeerConnection]
    Browser[Browser MediaStream]

    Clock --> VPTS --> Video
    Clock --> APTS --> Audio
    Clock -->|Mỗi AV_SYNC_INTERVAL| Flash --> Video
    Clock -->|Cùng thời điểm| Beep --> Audio
    Video --> PC
    Audio --> PC
    PC --> Browser
```

Mặc định debug sync được bật:

```env
AV_SYNC_DEBUG=true
AV_SYNC_INTERVAL=2.0
AV_SYNC_PULSE_DURATION=0.1
```

Browser khởi động video ở chế độ muted. Người dùng phải bấm **Bật âm thanh kiểm tra** để
nghe beep do chính sách autoplay của trình duyệt.

## 7. Đóng session và cleanup

```mermaid
sequenceDiagram
    participant B as Browser
    participant API as FastAPI / WebSocket
    participant Close as CloseSession
    participant Repo as SessionRepository
    participant Media as AiortcAvatarStreamAdapter

    alt Browser gọi DELETE
        B->>API: DELETE /api/sessions/{id}
    else WebSocket disconnect
        B--xAPI: WebSocketDisconnect
    end
    API->>Close: execute(session_id)
    Close->>Repo: get(session_id)
    Close->>Close: session.close() → CLOSED
    Close->>Repo: save(CLOSED)
    Close->>Media: close(session_id)
    Media->>Media: Stop current + retired video sources
    Media->>Media: Stop audio track
    Media->>Media: Close RTCPeerConnection
```

Khi ứng dụng shutdown, container gọi `avatar_stream.shutdown()` để đóng toàn bộ peer còn
tồn tại, sau đó mới dispose database engine.
