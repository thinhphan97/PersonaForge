const $ = (selector) => document.querySelector(selector);
const authView = $("#auth-view");
const appView = $("#app-view");
const video = $("#avatar");
const stateLabel = $("#state");
const connectionLabel = $("#connection");
const messages = $("#messages");
const form = $("#chat-form");
const input = $("#message-input");
const submit = form.querySelector("button");

let currentUser;
let csrfToken;
let accessToken;
let refreshPromise;
let refreshTimer;
let currentSession;
let peer;
let livekitRoom;
let chatSocket;
let streamingBubble;
let chatReady = false;
let mediaReady = false;
const remoteStream = new MediaStream();

async function api(path, options = {}, mutation = false, retry = true) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  if (mutation && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (response.status === 401 && retry && accessToken && await refreshAccessToken()) {
    return api(path, options, mutation, false);
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(payload.detail || response.statusText);
  }
  return response;
}

async function bootstrap() {
  const forceLogin = new URLSearchParams(location.search).get("login") === "1";
  if (forceLogin) {
    authView.hidden = false;
    appView.hidden = true;
    setAuthMode("login");
    return;
  }
  if (await refreshAccessToken()) {
    await enterApplication(currentUser);
  } else {
    authView.hidden = false;
  }
}

async function refreshAccessToken() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = performTokenRefresh();
  try {
    return await refreshPromise;
  } finally {
    refreshPromise = undefined;
  }
}

async function performTokenRefresh() {
  const csrf = readCookie("avatar_csrf");
  if (!csrf) return false;
  const response = await fetch("/api/auth/refresh", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf },
  });
  if (!response.ok) return false;
  const reconnectChatPath = chatSocket && currentSession
    ? `/api/sessions/${currentSession.session_id}/chat`
    : undefined;
  applyAuth(await response.json());
  if (reconnectChatPath) {
    chatSocket.onclose = null;
    chatSocket.close();
    connectChat(reconnectChatPath);
  }
  return true;
}

function applyAuth(auth) {
  currentUser = auth;
  csrfToken = auth.csrf_token;
  accessToken = auth.access_token;
  clearTimeout(refreshTimer);
  if (auth.expires_in) {
    const refreshAfter = Math.max(auth.expires_in - 30, 1) * 1000;
    refreshTimer = setTimeout(refreshAccessToken, refreshAfter);
  }
}

function readCookie(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const item = document.cookie.split("; ").find((cookie) => cookie.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : undefined;
}

async function enterApplication(auth) {
  applyAuth(auth);
  if (location.search) history.replaceState(null, "", "/");
  authView.hidden = true;
  appView.hidden = false;
  $("#current-user").textContent = `@${auth.username}`;
  await loadCharacters();
  await loadSessions("mine");
}

async function submitAuth(path, body) {
  $("#auth-error").textContent = "";
  try {
    accessToken = undefined;
    const auth = await (await api(path, { method: "POST", body: JSON.stringify(body) })).json();
    await enterApplication(auth);
  } catch (error) {
    $("#auth-error").textContent = error.message;
  }
}

$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuth("/api/auth/login", {
    identifier: $("#login-identifier").value,
    password: $("#login-password").value,
  });
});

$("#register-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submitAuth("/api/auth/register", {
    email: $("#register-email").value,
    username: $("#register-username").value,
    password: $("#register-password").value,
  });
});

function setAuthMode(mode) {
  const login = mode === "login";
  $("#login-form").hidden = !login;
  $("#register-form").hidden = login;
  $("#show-login").classList.toggle("is-active", login);
  $("#show-login").classList.toggle("secondary", !login);
  $("#show-register").classList.toggle("is-active", !login);
  $("#show-register").classList.toggle("secondary", login);
}

$("#show-login").addEventListener("click", () => setAuthMode("login"));
$("#show-register").addEventListener("click", () => setAuthMode("register"));

$("#logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" }, true);
  accessToken = undefined;
  csrfToken = undefined;
  clearTimeout(refreshTimer);
  await disconnectCurrent();
  location.reload();
});

async function loadCharacters() {
  const characters = await (await api("/api/characters")).json();
  $("#character-select").replaceChildren(
    ...characters.map((character) => new Option(character.display_name, character.character_id)),
  );
}

$("#create-session").addEventListener("click", async () => {
  const session = await (
    await api(
      "/api/sessions",
      {
        method: "POST",
        body: JSON.stringify({
          character_id: $("#character-select").value,
          visibility: $("#create-visibility").value,
        }),
      },
      true,
    )
  ).json();
  session.access_role = "owner";
  session.can_chat = true;
  session.status = "created";
  await loadSessions("mine");
  await openSession(session);
});

document.querySelectorAll("[data-list]").forEach((button) => {
  button.addEventListener("click", () => loadSessions(button.dataset.list));
});

async function loadSessions(kind) {
  document.querySelectorAll("[data-list]").forEach((button) => {
    const active = button.dataset.list === kind;
    button.classList.toggle("is-active", active);
    button.classList.toggle("secondary", !active);
  });
  const endpoint = {
    mine: "/api/sessions/mine",
    shared: "/api/sessions/shared-with-me",
    public: "/api/sessions/public",
  }[kind];
  const sessions = await (await api(endpoint)).json();
  const list = $("#session-list");
  list.replaceChildren();
  for (const session of sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-card";
    button.dataset.sessionId = session.session_id;
    button.classList.toggle("is-active", session.session_id === currentSession?.session_id);
    button.innerHTML = `<strong>${escapeHtml(session.character_id)}</strong><span>${escapeHtml(session.access_role)} · ${escapeHtml(session.visibility)} · ${escapeHtml(session.status)}</span>`;
    button.addEventListener("click", () => openSession(session));
    list.append(button);
  }
}

async function openSession(session) {
  await disconnectCurrent();
  currentSession = session;
  document.querySelectorAll(".session-card").forEach((card) => {
    card.classList.toggle("is-active", card.dataset.sessionId === session.session_id);
  });
  messages.replaceChildren();
  $("#session-title").textContent = session.character_id;
  $("#session-access").textContent = `${session.access_role} · ${session.visibility}`;
  const isOwner = session.owner_user_id === currentUser.user_id;
  $("#owner-controls").hidden = !isOwner;
  $("#share-controls").hidden = !isOwner;
  $("#session-visibility").value = session.visibility;
  showPublicLink(session.public_url);
  if (isOwner) await loadShares();
  connectionLabel.textContent = "Đang kết nối media…";
  await loadHistory(session.session_id);
  connectChat(`/api/sessions/${session.session_id}/chat`);
  try {
    const mediaProvider = await detectMediaProvider(session);
    if (mediaProvider === "livekit") {
      await connectLiveKit(`/api/sessions/${session.session_id}/media/access`);
    } else if (isOwner) {
      await connectWebRtc(`/api/sessions/${session.session_id}/webrtc/offer`);
    } else {
      throw new Error("Chia sẻ nhiều viewer yêu cầu MEDIA_PROVIDER=livekit");
    }
    updateControls();
  } catch (error) {
    connectionLabel.textContent = `Không thể kết nối: ${error.message}`;
  }
}

async function detectMediaProvider(session) {
  return session.media_provider;
}

async function connectLiveKit(accessUrl) {
  await loadLiveKitClient();
  const access = await (await api(accessUrl, { method: "POST" }, true)).json();
  const { Room, RoomEvent } = window.LivekitClient;
  livekitRoom = new Room({ adaptiveStream: false, dynacast: false });
  livekitRoom.on(RoomEvent.TrackSubscribed, (track) => {
    remoteStream.addTrack(track.mediaStreamTrack);
    video.srcObject = remoteStream;
  });
  livekitRoom.on(RoomEvent.TrackUnsubscribed, (track) => {
    remoteStream.removeTrack(track.mediaStreamTrack);
  });
  livekitRoom.on(RoomEvent.ConnectionStateChanged, (state) => {
    connectionLabel.textContent = `LiveKit: ${state}`;
  });
  livekitRoom.on(RoomEvent.Disconnected, () => {
    mediaReady = false;
    updateControls();
  });
  await livekitRoom.connect(access.server_url, access.access_token);
  setAvatarState(access.avatar_state || "idle");
  mediaReady = true;
}

async function loadLiveKitClient() {
  if (window.LivekitClient) return;
  await new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.dataset.livekit = "true";
    script.src = "https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js";
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
  };
  await peer.setLocalDescription(await peer.createOffer());
  await waitForIceGathering(peer);
  const response = await api(
    offerUrl,
    { method: "POST", body: JSON.stringify(peer.localDescription) },
    true,
  );
  const answer = await response.json();
  await peer.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
  setAvatarState(answer.avatar_state || "idle");
  mediaReady = true;
}

function connectChat(chatPath) {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  chatSocket = new WebSocket(
    `${protocol}//${location.host}${chatPath}`,
    ["avatar-chat", `bearer.${accessToken}`],
  );
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

async function disconnectCurrent() {
  chatReady = false;
  mediaReady = false;
  chatSocket?.close();
  chatSocket = undefined;
  peer?.close();
  peer = undefined;
  if (livekitRoom) await livekitRoom.disconnect();
  livekitRoom = undefined;
  for (const track of remoteStream.getTracks()) remoteStream.removeTrack(track);
  video.srcObject = null;
  updateControls();
}

async function loadHistory(sessionId) {
  const history = await (await api(`/api/sessions/${sessionId}/messages`)).json();
  for (const message of history) addMessage(message.content, message.role);
}

function updateControls() {
  const ready = Boolean(currentSession?.can_chat && chatReady);
  input.disabled = !ready;
  submit.disabled = !ready;
}

function handleChatEvent(event) {
  if (event.type === "avatar_state") return setAvatarState(event.avatar_state);
  if (event.type === "user_message") {
    addMessage(event.content, "user");
    return;
  }
  if (event.type === "assistant_chunk") {
    if (!streamingBubble) streamingBubble = addMessage("", "assistant");
    streamingBubble.textContent += event.content;
    messages.scrollTop = messages.scrollHeight;
  } else if (event.type === "assistant_complete") {
    streamingBubble = undefined;
  } else if (event.type === "error") {
    addMessage(event.detail, "assistant");
  }
}

function setAvatarState(state) {
  stateLabel.textContent = state;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const content = input.value.trim();
  if (!content || chatSocket?.readyState !== WebSocket.OPEN) return;
  chatSocket.send(JSON.stringify({ type: "user_message", content }));
  input.value = "";
});

$("#session-visibility").addEventListener("change", async (event) => {
  const updated = await (
    await api(
      `/api/sessions/${currentSession.session_id}/visibility`,
      { method: "PATCH", body: JSON.stringify({ visibility: event.target.value }) },
      true,
    )
  ).json();
  currentSession = updated;
  $("#session-access").textContent = `owner · ${updated.visibility}`;
  showPublicLink(updated.public_url);
});

function showPublicLink(publicPath) {
  const panel = $("#public-link-panel");
  panel.hidden = !publicPath;
  if (!publicPath) return;
  const absoluteUrl = new URL(publicPath, location.origin).href;
  $("#public-link").value = absoluteUrl;
  $("#open-public-link").href = absoluteUrl;
}

$("#copy-public-link").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("#public-link").value);
  $("#copy-public-link").textContent = "Đã copy";
  setTimeout(() => { $("#copy-public-link").textContent = "Copy URL"; }, 1200);
});

$("#share-session").addEventListener("click", async () => {
  await api(
    `/api/sessions/${currentSession.session_id}/shares`,
    {
      method: "POST",
      body: JSON.stringify({
        username: $("#share-username").value,
        permission: $("#share-permission").value,
      }),
    },
    true,
  );
  $("#share-username").value = "";
  await loadShares();
});

async function loadShares() {
  const shares = await (
    await api(`/api/sessions/${currentSession.session_id}/shares`)
  ).json();
  const list = $("#share-list");
  list.replaceChildren();
  for (const share of shares) {
    const row = document.createElement("div");
    row.innerHTML = `<span>@${escapeHtml(share.username)} · ${escapeHtml(share.permission)}</span>`;
    const revoke = document.createElement("button");
    revoke.type = "button";
    revoke.className = "danger small";
    revoke.textContent = "Thu hồi";
    revoke.addEventListener("click", async () => {
      await api(
        `/api/sessions/${currentSession.session_id}/shares/${share.user_id}`,
        { method: "DELETE" },
        true,
      );
      await loadShares();
    });
    row.append(revoke);
    list.append(row);
  }
}

$("#close-session").addEventListener("click", async () => {
  await api(`/api/sessions/${currentSession.session_id}`, { method: "DELETE" }, true);
  await disconnectCurrent();
  currentSession = undefined;
  await loadSessions("mine");
});

$("#audio-toggle").addEventListener("click", async () => {
  video.muted = false;
  video.volume = 0.65;
  await video.play();
  $("#audio-toggle").hidden = true;
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

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = value;
  return element.innerHTML;
}

window.addEventListener("beforeunload", () => {
  chatSocket?.close();
  peer?.close();
  livekitRoom?.disconnect();
});

bootstrap();
