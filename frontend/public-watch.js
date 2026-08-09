const video = document.querySelector("#public-avatar");
const status = document.querySelector("#public-status");
const sessionId = decodeURIComponent(location.pathname.split("/").filter(Boolean).at(-1));
let room;
let conversationActive = true;
let sessionClosed = false;
let migrating = false;
let migrationTimer;
let migrationAttempt = 0;
let migrationInFlight = false;
let migrationGeneration = 0;
let intentionalDisconnect = false;
let frameWatchdogTimer;
let lastVideoFrameAt = 0;
let messageCursor = { sequence: 0, revision: 0 };
const messageStates = new Map();

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
  if (sessionClosed) return;
  const infoResponse = await fetch(`/api/public/sessions/${encodeURIComponent(sessionId)}`);
  if (!infoResponse.ok) throw new Error("Session không tồn tại hoặc không còn public");
  const info = await infoResponse.json();
  document.querySelector("#public-title").textContent = `Nhân vật: ${info.character_id}`;
  if (info.status === "closed") {
    showPublicClosed();
    return;
  }
  if (info.media_provider !== "livekit") {
    throw new Error("Public viewer yêu cầu server chạy MEDIA_PROVIDER=livekit");
  }

  const accessResponse = await fetch(
    `/api/public/sessions/${encodeURIComponent(sessionId)}/media/access`,
    { method: "POST" },
  );
  if (accessResponse.status === 409) {
    if (migrating) throw new Error("Worker đang migrate hoặc chưa publish track");
    status.textContent = "Đang chờ owner khởi tạo stream…";
    setTimeout(() => connect().catch(showError), 2000);
    return;
  }
  if (!accessResponse.ok) throw new Error("Không thể cấp quyền xem public");
  const access = await accessResponse.json();
  await loadLiveKitClient();
  const { Room, RoomEvent } = window.LivekitClient;
  if (room) {
    intentionalDisconnect = true;
    await room.disconnect();
    intentionalDisconnect = false;
  }
  const connectedRoom = new Room({ adaptiveStream: false, dynacast: false });
  room = connectedRoom;
  const stream = new MediaStream();
  connectedRoom.on(RoomEvent.TrackSubscribed, (track) => {
    if (room !== connectedRoom) return;
    stream.addTrack(track.mediaStreamTrack);
    video.srcObject = stream;
    if (String(track.kind) === "video") {
      lastVideoFrameAt = performance.now();
      track.mediaStreamTrack.onmute = () => {
        if (room === connectedRoom) beginMigration("Video track bị mute, đang migrate stream…");
      };
      track.mediaStreamTrack.onunmute = () => {
        lastVideoFrameAt = performance.now();
      };
      startFrameWatchdog();
    }
    finishMigration("Stream đã được migrate và hoạt động trở lại.");
  });
  connectedRoom.on(RoomEvent.TrackUnsubscribed, (track) => {
    if (room !== connectedRoom) return;
    if (String(track.kind) === "video") stopFrameWatchdog();
    beginMigration("Mất track avatar, đang migrate stream…");
  });
  connectedRoom.on(RoomEvent.ParticipantDisconnected, () => {
    if (room === connectedRoom) beginMigration("Publisher đã ngắt kết nối, đang migrate stream…");
  });
  connectedRoom.on(RoomEvent.ConnectionStateChanged, (state) => {
    if (room !== connectedRoom) return;
    if (state === "reconnecting" || state === "disconnected") {
      beginMigration("LiveKit đang kết nối lại stream…");
    } else if (!migrating) {
      status.textContent = `LiveKit: ${state}`;
    }
  });
  connectedRoom.on(RoomEvent.Disconnected, () => {
    if (room !== connectedRoom || intentionalDisconnect) return;
    beginMigration("Stream đã ngắt, đang migrate…");
  });
  await connectedRoom.connect(access.server_url, access.access_token);
  if (!migrating) {
    status.textContent = "Đã kết nối stream; đang chờ video…";
  }
}

function beginMigration(reason) {
  if (sessionClosed) return;
  migrating = true;
  migrationGeneration += 1;
  status.classList.add("migrating");
  status.textContent = `Đang migrate stream… ${reason}`;
  scheduleMigration(migrationGeneration, 0);
}

function startFrameWatchdog() {
  stopFrameWatchdog();
  const observeFrame = () => {
    if (!video.requestVideoFrameCallback || migrating) return;
    video.requestVideoFrameCallback(() => {
      lastVideoFrameAt = performance.now();
      observeFrame();
    });
  };
  observeFrame();
  frameWatchdogTimer = setInterval(() => {
    if (document.hidden || migrating || !lastVideoFrameAt) return;
    if (performance.now() - lastVideoFrameAt > 1800) {
      beginMigration("Video không phát sinh frame mới, đang migrate stream…");
    }
  }, 500);
}

function stopFrameWatchdog() {
  clearInterval(frameWatchdogTimer);
  frameWatchdogTimer = undefined;
  lastVideoFrameAt = 0;
}

function finishMigration(message) {
  if (!migrating) return;
  migrating = false;
  migrationAttempt = 0;
  migrationInFlight = false;
  clearTimeout(migrationTimer);
  status.classList.remove("migrating");
  status.textContent = message;
}

function scheduleMigration(generation, delayMs) {
  clearTimeout(migrationTimer);
  migrationTimer = setTimeout(() => recoverMigration(generation), delayMs);
}

async function recoverMigration(generation) {
  if (generation !== migrationGeneration || !migrating || migrationInFlight || sessionClosed) return;
  migrationInFlight = true;
  try {
    if (room && String(room.state) === "connected") {
      migrationAttempt += 1;
      scheduleMigration(generation, Math.min(1000 * (2 ** migrationAttempt), 10000));
      return;
    }
    await connect();
    migrationAttempt = 0;
  } catch (error) {
    migrationAttempt += 1;
    status.textContent = `Đang migrate stream… lần thử ${migrationAttempt}`;
    scheduleMigration(generation, Math.min(1000 * (2 ** migrationAttempt), 10000));
    console.warn("Public media migration retry failed", error);
  } finally {
    migrationInFlight = false;
  }
}

function showPublicClosed() {
  if (sessionClosed) return;
  sessionClosed = true;
  migrating = false;
  migrationGeneration += 1;
  clearTimeout(migrationTimer);
  stopFrameWatchdog();
  document.querySelector("#public-avatar").closest(".video-shell").classList.add("is-closed");
  document.querySelector("#public-closed-overlay").hidden = false;
  document.querySelector("#public-audio").hidden = true;
  status.classList.remove("migrating");
  status.textContent = "Session đã đóng — stream đã kết thúc.";
  room?.disconnect();
}

async function monitorSession() {
  if (sessionClosed) return;
  try {
    const response = await fetch(`/api/public/sessions/${encodeURIComponent(sessionId)}`);
    if (response.status === 404) {
      status.textContent = "Session không còn public.";
      return;
    }
    if (response.ok && (await response.json()).status === "closed") {
      showPublicClosed();
      return;
    }
  } catch (error) {
    console.warn(error);
  }
  setTimeout(monitorSession, 2000);
}

function showError(error) {
  status.textContent = error.message;
}

async function syncConversation() {
  if (!conversationActive) return;
  try {
    const response = await fetch(
      `/api/public/sessions/${encodeURIComponent(sessionId)}/messages?limit=100&after=${messageCursor.sequence}:${messageCursor.revision}`,
    );
    if (response.status === 404) {
      conversationActive = false;
      return;
    }
    if (!response.ok) throw new Error("Không thể tải hội thoại");
    const history = await response.json();
    const container = document.querySelector("#public-messages");
    for (const message of history) {
      const current = messageStates.get(message.message_id);
      if (current && current.revision > message.revision) continue;
      let bubble = current?.element;
      if (!bubble) {
        bubble = document.createElement("div");
        bubble.className = `message ${message.role}`;
        container.append(bubble);
      }
      bubble.dataset.sequence = String(message.sequence);
      const next = [...container.querySelectorAll("[data-sequence]")].find(
        (candidate) => (
          candidate !== bubble && Number(candidate.dataset.sequence) > message.sequence
        ),
      );
      container.insertBefore(bubble, next || null);
      bubble.textContent = message.content || "";
      bubble.classList.toggle("streaming", message.status === "streaming");
      bubble.classList.toggle("failed", message.status === "failed");
      messageStates.set(message.message_id, { revision: message.revision, element: bubble });
      if (
        message.sequence > messageCursor.sequence
        || (message.sequence === messageCursor.sequence && message.revision > messageCursor.revision)
      ) {
        messageCursor = { sequence: message.sequence, revision: message.revision };
      }
    }
    container.scrollTop = container.scrollHeight;
  } catch (error) {
    console.warn(error);
  } finally {
    if (sessionClosed) conversationActive = false;
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
  clearTimeout(migrationTimer);
  intentionalDisconnect = true;
  room?.disconnect();
});
connect().catch(showError);
syncConversation();
monitorSession();
