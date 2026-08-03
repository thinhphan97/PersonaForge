const video = document.querySelector("#avatar");
const stateLabel = document.querySelector("#state");
const connectionLabel = document.querySelector("#connection");
const messages = document.querySelector("#messages");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const submit = form.querySelector("button");
const audioToggle = document.querySelector("#audio-toggle");
const characterSelect = document.querySelector("#character-select");
const startAvatar = document.querySelector("#start-avatar");

let sessionId;
let peer;
let livekitRoom;
let chatSocket;
let streamingBubble;
let chatReady = false;
let mediaReady = false;
let avatarState = "initializing";
const remoteStream = new MediaStream();

async function start() {
  const characterId = characterSelect.value;
  characterSelect.disabled = true;
  startAvatar.disabled = true;
  connectionLabel.textContent = "Đang khởi tạo session…";
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ character_id: characterId }),
  });
  if (!response.ok) throw new Error(await response.text());
  const session = await response.json();
  sessionId = session.session_id;

  connectChat(session.chat_url);
  if (session.media_provider === "livekit") {
    await connectLiveKit(session.media_access_url);
  } else {
    await connectWebRtc(session.offer_url);
  }
}

async function loadCharacters() {
  const response = await fetch("/api/characters");
  if (!response.ok) throw new Error(await response.text());
  const characters = await response.json();
  if (characters.length === 0) throw new Error("Server chưa có nhân vật hợp lệ");
  characterSelect.replaceChildren(
    ...characters.map((character) => {
      const option = document.createElement("option");
      option.value = character.character_id;
      option.textContent = character.display_name;
      return option;
    }),
  );
  characterSelect.disabled = false;
  startAvatar.disabled = false;
  connectionLabel.textContent = "Chọn nhân vật rồi bấm Bắt đầu";
}

async function connectLiveKit(accessUrl) {
  await loadLiveKitClient();
  const response = await fetch(accessUrl, { method: "POST" });
  if (!response.ok) throw new Error(await response.text());
  const access = await response.json();

  const { Room, RoomEvent } = window.LivekitClient;
  // The avatar worker publishes one fixed-resolution layer. Do not let the
  // subscriber request a smaller adaptive layer after the first frame.
  livekitRoom = new Room({ adaptiveStream: false, dynacast: false });
  livekitRoom.on(RoomEvent.TrackSubscribed, (track) => {
    if (track.kind !== "audio" && track.kind !== "video") return;
    remoteStream.addTrack(track.mediaStreamTrack);
    video.srcObject = remoteStream;
  });
  livekitRoom.on(RoomEvent.ConnectionStateChanged, (state) => {
    connectionLabel.textContent = `LiveKit: ${state}`;
  });
  livekitRoom.on(RoomEvent.Disconnected, () => {
    mediaReady = false;
    updateControls();
  });

  await livekitRoom.connect(access.server_url, access.access_token);
  setAvatarState(access.avatar_state ?? "idle");
  mediaReady = true;
  updateControls();
}

async function loadLiveKitClient() {
  if (window.LivekitClient) return;
  await new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src =
      "https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js";
    script.onload = resolve;
    script.onerror = () => reject(new Error("Không tải được LiveKit browser SDK"));
    document.head.append(script);
  });
}

async function connectWebRtc(offerUrl) {
  peer = new RTCPeerConnection();
  peer.addTransceiver("video", { direction: "recvonly" });
  peer.addTransceiver("audio", { direction: "recvonly" });
  peer.ontrack = (event) => {
    remoteStream.addTrack(event.track);
    video.srcObject = remoteStream;
  };
  peer.onconnectionstatechange = () => {
    connectionLabel.textContent = `WebRTC: ${peer.connectionState}`;
    if (peer.connectionState === "connected" && avatarState === "initializing") {
      setAvatarState("idle");
    }
  };

  await peer.setLocalDescription(await peer.createOffer());
  await waitForIceGathering(peer);
  const response = await fetch(offerUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(peer.localDescription),
  });
  if (!response.ok) throw new Error(await response.text());
  const answer = await response.json();
  await peer.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
  setAvatarState(answer.avatar_state ?? "idle");
  mediaReady = true;
  updateControls();
}

function connectChat(chatPath) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  chatSocket = new WebSocket(`${protocol}//${location.host}${chatPath}`);
  chatSocket.onopen = () => {
    chatReady = true;
    updateControls();
  };
  chatSocket.onclose = () => {
    chatReady = false;
    updateControls();
  };
  chatSocket.onmessage = ({ data }) => handleChatEvent(JSON.parse(data));
}

function updateControls() {
  const ready = chatReady && mediaReady;
  input.disabled = !ready;
  submit.disabled = !ready;
  if (ready) input.focus();
}

function handleChatEvent(event) {
  if (event.type === "avatar_state") {
    setAvatarState(event.avatar_state);
    return;
  }
  if (event.type === "assistant_chunk") {
    if (!streamingBubble) streamingBubble = addMessage("", "assistant");
    streamingBubble.textContent += event.content;
    messages.scrollTop = messages.scrollHeight;
    return;
  }
  if (event.type === "assistant_complete") {
    streamingBubble = undefined;
    return;
  }
  if (event.type === "error") addMessage(event.detail, "assistant");
}

function setAvatarState(state) {
  avatarState = state;
  stateLabel.textContent = state;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const content = input.value.trim();
  if (!content || chatSocket.readyState !== WebSocket.OPEN) return;
  addMessage(content, "user");
  chatSocket.send(JSON.stringify({ type: "user_message", content }));
  input.value = "";
});

audioToggle.addEventListener("click", async () => {
  video.muted = false;
  video.volume = 0.65;
  await video.play();
  audioToggle.hidden = true;
});

function addMessage(content, role) {
  const element = document.createElement("div");
  element.className = `message ${role}`;
  element.textContent = content;
  messages.append(element);
  messages.scrollTop = messages.scrollHeight;
  return element;
}

function waitForIceGathering(connection) {
  if (connection.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const listener = () => {
      if (connection.iceGatheringState === "complete") {
        connection.removeEventListener("icegatheringstatechange", listener);
        resolve();
      }
    };
    connection.addEventListener("icegatheringstatechange", listener);
  });
}

window.addEventListener("beforeunload", () => {
  chatSocket?.close();
  peer?.close();
  livekitRoom?.disconnect();
});

startAvatar.addEventListener("click", () => {
  start().catch((error) => {
    connectionLabel.textContent = `Không thể khởi động: ${error.message}`;
    console.error(error);
  });
});

loadCharacters().catch((error) => {
  connectionLabel.textContent = `Không tải được nhân vật: ${error.message}`;
  console.error(error);
});
