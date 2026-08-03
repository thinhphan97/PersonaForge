const video = document.querySelector("#public-avatar");
const status = document.querySelector("#public-status");
const sessionId = decodeURIComponent(location.pathname.split("/").filter(Boolean).at(-1));
let room;
let conversationActive = true;
const seenMessages = new Set();

async function loadLiveKitClient() {
  if (window.LivekitClient) return;
  await new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js";
    script.onload = resolve;
    script.onerror = () => reject(new Error("Không tải được LiveKit browser SDK"));
    document.head.append(script);
  });
}

async function connect() {
  const infoResponse = await fetch(`/api/public/sessions/${encodeURIComponent(sessionId)}`);
  if (!infoResponse.ok) throw new Error("Session không tồn tại hoặc không còn public");
  const info = await infoResponse.json();
  document.querySelector("#public-title").textContent = `Nhân vật: ${info.character_id}`;
  if (info.media_provider !== "livekit") {
    throw new Error("Public viewer yêu cầu server chạy MEDIA_PROVIDER=livekit");
  }

  const accessResponse = await fetch(
    `/api/public/sessions/${encodeURIComponent(sessionId)}/media/access`,
    { method: "POST" },
  );
  if (accessResponse.status === 409) {
    status.textContent = "Đang chờ owner khởi tạo stream…";
    setTimeout(() => connect().catch(showError), 2000);
    return;
  }
  if (!accessResponse.ok) throw new Error("Không thể cấp quyền xem public");
  const access = await accessResponse.json();
  await loadLiveKitClient();
  const { Room, RoomEvent } = window.LivekitClient;
  room = new Room({ adaptiveStream: false, dynacast: false });
  const stream = new MediaStream();
  room.on(RoomEvent.TrackSubscribed, (track) => {
    stream.addTrack(track.mediaStreamTrack);
    video.srcObject = stream;
  });
  room.on(RoomEvent.ConnectionStateChanged, (state) => {
    status.textContent = `LiveKit: ${state}`;
  });
  room.on(RoomEvent.Disconnected, () => {
    status.textContent = "Stream đã ngắt hoặc session không còn public.";
  });
  await room.connect(access.server_url, access.access_token);
}

function showError(error) {
  status.textContent = error.message;
}

async function syncConversation() {
  if (!conversationActive) return;
  try {
    const response = await fetch(
      `/api/public/sessions/${encodeURIComponent(sessionId)}/messages?limit=100`,
    );
    if (response.status === 404) {
      conversationActive = false;
      return;
    }
    if (!response.ok) throw new Error("Không thể tải hội thoại");
    const history = await response.json();
    const container = document.querySelector("#public-messages");
    for (const message of history) {
      if (seenMessages.has(message.message_id)) continue;
      seenMessages.add(message.message_id);
      const bubble = document.createElement("div");
      bubble.className = `message ${message.role}`;
      bubble.textContent = message.content;
      container.append(bubble);
    }
    container.scrollTop = container.scrollHeight;
  } catch (error) {
    console.warn(error);
  } finally {
    if (conversationActive) setTimeout(syncConversation, 1000);
  }
}

document.querySelector("#public-audio").addEventListener("click", async () => {
  video.muted = false;
  await video.play();
  document.querySelector("#public-audio").hidden = true;
});

window.addEventListener("beforeunload", () => {
  conversationActive = false;
  room?.disconnect();
});
connect().catch(showError);
syncConversation();
