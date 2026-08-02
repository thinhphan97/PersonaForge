const video = document.querySelector("#avatar");
const stateLabel = document.querySelector("#state");
const connectionLabel = document.querySelector("#connection");
const messages = document.querySelector("#messages");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const submit = form.querySelector("button");
const audioToggle = document.querySelector("#audio-toggle");

let sessionId;
let peer;
let chatSocket;
let streamingBubble;
let chatReady = false;
let mediaReady = false;
let avatarState = "initializing";
const remoteStream = new MediaStream();

async function start() {
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ character_id: "default" }),
  });
  if (!response.ok) throw new Error(await response.text());
  const session = await response.json();
  sessionId = session.session_id;

  connectChat(session.chat_url);
  await connectWebRtc(session.offer_url);
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
});

start().catch((error) => {
  connectionLabel.textContent = `Không thể khởi động: ${error.message}`;
  console.error(error);
});
