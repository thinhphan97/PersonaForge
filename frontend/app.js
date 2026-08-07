const $ = (selector) => document.querySelector(selector);
const authView = $("#auth-view");
const appView = $("#app-view");
const video = $("#avatar");
const videoShell = video.closest(".video-shell");
const closedOverlay = $("#session-closed-overlay");
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
let activeSessionList = "mine";
let peer;
let livekitRoom;
let chatSocket;
let chatReconnectTimer;
let chatReconnectAttempt = 0;
let sessionSyncTimer;
let sessionClosePromise;
let chatReady = false;
let mediaReady = false;
let messageCursor = { sequence: 0, revision: 0 };
const messageStates = new Map();
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
    const error = new Error(payload.detail || response.statusText);
    error.status = response.status;
    throw error;
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
  await loadVoices();
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

async function loadVoices() {
  const voices = await (await api("/api/voices")).json();
  $("#voice-select").replaceChildren(
    ...voices.map((voice) => {
      const option = new Option(
        `${voice.display_name} — ${voice.description}`,
        voice.voice_id,
        voice.is_default,
        voice.is_default,
      );
      return option;
    }),
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
          voice_id: $("#voice-select").value,
          visibility: $("#create-visibility").value,
        }),
      },
      true,
    )
  ).json();
  session.access_role = "owner";
  session.can_chat = true;
  session.status = "created";
  session.version = 0;
  await loadSessions("mine");
  await openSession(session);
});

document.querySelectorAll("[data-list]").forEach((button) => {
  button.addEventListener("click", () => loadSessions(button.dataset.list));
});

async function loadSessions(kind) {
  activeSessionList = kind;
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
    button.classList.toggle("is-closed", session.status === "closed");
    button.innerHTML = `<strong>${escapeHtml(session.character_id)}</strong><span>${escapeHtml(session.voice_id)} · ${escapeHtml(session.access_role)} · ${escapeHtml(session.visibility)} · ${escapeHtml(session.status)}</span>`;
    button.addEventListener("click", () => openSession(session));
    list.append(button);
  }
}

async function openSession(session) {
  await disconnectCurrent();
  currentSession = session;
  setSessionError();
  renderSessionActive();
  document.querySelectorAll(".session-card").forEach((card) => {
    card.classList.toggle("is-active", card.dataset.sessionId === session.session_id);
  });
  messages.replaceChildren();
  messageStates.clear();
  messageCursor = { sequence: 0, revision: 0 };
  $("#session-title").textContent = session.character_id;
  $("#session-access").textContent = `${session.access_role} · ${session.visibility}`;
  const isOwner = session.owner_user_id === currentUser.user_id;
  $("#owner-controls").hidden = !isOwner;
  $("#share-controls").hidden = !isOwner;
  $("#session-visibility").value = session.visibility;
  showPublicLink(session.public_url);
  if (isOwner) await loadShares(session.session_id);
  await loadHistory(session.session_id);
  if (session.status === "closed") {
    renderSessionClosed();
    return;
  }
  connectionLabel.textContent = "Đang kết nối media…";
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
  clearTimeout(chatReconnectTimer);
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(
    `${protocol}//${location.host}${chatPath}`,
    ["avatar-chat", `bearer.${accessToken}`],
  );
  chatSocket = socket;
  socket.onopen = () => {
    if (chatSocket !== socket) return;
    chatReconnectAttempt = 0;
    chatReady = true;
    startSessionReconciliation();
    updateControls();
    refreshCurrentSession({ refreshList: true })
      .then(() => catchUpHistory())
      .catch((error) => addMessage(error.message, "assistant"));
  };
  socket.onclose = async ({ code, reason }) => {
    if (chatSocket !== socket) return;
    chatSocket = undefined;
    chatReady = false;
    updateControls();
    if (reason === "Access revoked") {
      await handleSessionAccessRevoked();
      return;
    }
    if (code === 1008) {
      if (!await refreshAccessToken()) {
        addMessage(reason || "Phiên đăng nhập đã hết hạn.", "assistant");
        return;
      }
    }
    scheduleChatReconnect(chatPath);
  };
  socket.onmessage = ({ data }) => handleChatEvent(JSON.parse(data));
}

function scheduleChatReconnect(chatPath) {
  if (!currentSession || !accessToken) return;
  const sessionId = currentSession.session_id;
  const delay = Math.min(500 * (2 ** chatReconnectAttempt), 10000);
  chatReconnectAttempt += 1;
  clearTimeout(chatReconnectTimer);
  chatReconnectTimer = setTimeout(() => {
    if (currentSession?.session_id === sessionId && !chatSocket) connectChat(chatPath);
  }, delay);
}

async function disconnectCurrent() {
  clearTimeout(chatReconnectTimer);
  clearInterval(sessionSyncTimer);
  sessionSyncTimer = undefined;
  chatReconnectAttempt = 0;
  chatReady = false;
  mediaReady = false;
  const socket = chatSocket;
  chatSocket = undefined;
  socket?.close();
  peer?.close();
  peer = undefined;
  if (livekitRoom) await livekitRoom.disconnect();
  livekitRoom = undefined;
  for (const track of remoteStream.getTracks()) remoteStream.removeTrack(track);
  video.srcObject = null;
  updateControls();
}

function startSessionReconciliation() {
  clearInterval(sessionSyncTimer);
  sessionSyncTimer = setInterval(() => {
    if (!currentSession || document.hidden) return;
    refreshCurrentSession({ refreshList: true }).catch((error) => {
      console.warn("Periodic session reconciliation failed", error);
    });
  }, 15000);
}

async function loadHistory(sessionId) {
  const history = await (await api(`/api/sessions/${sessionId}/messages`)).json();
  for (const message of history) reconcileMessage(message);
}

async function catchUpHistory() {
  if (!currentSession) return;
  const sessionId = currentSession.session_id;
  while (currentSession?.session_id === sessionId) {
    const after = `${messageCursor.sequence}:${messageCursor.revision}`;
    const history = await (
      await api(`/api/sessions/${sessionId}/messages?after=${after}&limit=100`)
    ).json();
    for (const message of history) reconcileMessage(message);
    if (history.length < 100) return;
  }
}

function updateControls() {
  const ready = Boolean(
    currentSession?.status !== "closed" && currentSession?.can_chat && chatReady,
  );
  input.disabled = !ready;
  submit.disabled = !ready;
}

function handleChatEvent(event) {
  if (event.type === "session_closed") {
    markCurrentSessionClosed().catch((error) => console.warn(error));
    return;
  }
  if (event.type === "session_updated") {
    refreshCurrentSession({
      minimumVersion: event.session_version,
      refreshList: true,
    }).catch((error) => console.warn(error));
    return;
  }
  if (event.type === "session_shares_updated") {
    refreshCurrentSession({ refreshShares: true, refreshList: true })
      .catch((error) => console.warn(error));
    return;
  }
  if (event.type === "avatar_state") return setAvatarState(event.avatar_state);
  if (event.type === "user_message") {
    reconcileMessage({ ...event, role: "user", status: "complete" });
    return;
  }
  if (
    ["assistant_started", "assistant_chunk", "assistant_complete", "assistant_failed"]
      .includes(event.type)
  ) {
    reconcileMessage({
      ...event,
      role: "assistant",
      status: event.type === "assistant_complete"
        ? "complete"
        : event.type === "assistant_failed" ? "failed" : "streaming",
    });
  } else if (event.type === "error") {
    addMessage(event.detail, "assistant");
  }
}

function reconcileMessage(message) {
  if (!message.message_id || message.sequence == null || message.revision == null) return;
  const current = messageStates.get(message.message_id);
  if (current && current.revision > message.revision) return;
  let element = current?.element;
  if (!element) {
    element = addMessage("", message.role);
    element.dataset.messageId = message.message_id;
  }
  element.dataset.sequence = String(message.sequence);
  placeMessageInOrder(element, message.sequence);
  element.textContent = message.content || "";
  element.classList.toggle("streaming", message.status === "streaming");
  element.classList.toggle("failed", message.status === "failed");
  messageStates.set(message.message_id, {
    revision: message.revision,
    sequence: message.sequence,
    element,
  });
  if (
    message.sequence > messageCursor.sequence
    || (message.sequence === messageCursor.sequence && message.revision > messageCursor.revision)
  ) {
    messageCursor = { sequence: message.sequence, revision: message.revision };
  }
  messages.scrollTop = messages.scrollHeight;
}

function placeMessageInOrder(element, sequence) {
  const next = [...messages.querySelectorAll("[data-sequence]")].find(
    (candidate) => candidate !== element && Number(candidate.dataset.sequence) > sequence,
  );
  messages.insertBefore(element, next || null);
}

function setAvatarState(state) {
  stateLabel.textContent = state;
}

function renderSessionActive() {
  videoShell.classList.remove("is-closed");
  closedOverlay.hidden = true;
  video.hidden = false;
  video.muted = true;
  $("#audio-toggle").hidden = false;
  $("#session-visibility").disabled = false;
  $("#close-session").disabled = false;
  $("#close-session").textContent = "Đóng session";
  $("#share-username").disabled = false;
  $("#share-permission").disabled = false;
  $("#share-session").disabled = false;
  input.placeholder = "Nhập tin nhắn…";
}

function setSessionError(message = "") {
  $("#session-error").textContent = message;
}

function renderSessionClosed() {
  if (!currentSession) return;
  currentSession.status = "closed";
  currentSession.can_chat = false;
  videoShell.classList.add("is-closed");
  closedOverlay.hidden = false;
  $("#audio-toggle").hidden = true;
  setAvatarState("closed");
  connectionLabel.textContent = "Session đã đóng — stream và mọi thao tác đã dừng.";
  $("#session-access").textContent = `${currentSession.access_role} · ${currentSession.visibility} · closed`;
  $("#session-visibility").disabled = true;
  $("#close-session").disabled = true;
  $("#close-session").textContent = "Đã đóng";
  $("#share-username").disabled = true;
  $("#share-permission").disabled = true;
  $("#share-session").disabled = true;
  document.querySelectorAll("#share-list button").forEach((button) => {
    button.disabled = true;
  });
  showPublicLink(undefined);
  input.value = "";
  input.placeholder = "Session đã đóng — chỉ có thể xem lại hội thoại";
  updateControls();
}

async function refreshCurrentSession({
  minimumVersion,
  refreshShares = false,
  refreshList = false,
} = {}) {
  if (!currentSession) return;
  const sessionId = currentSession.session_id;
  let snapshot;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      snapshot = await (await api(`/api/sessions/${sessionId}`)).json();
    } catch (error) {
      if (error.status === 403 || error.status === 404) {
        await handleSessionAccessRevoked();
        return;
      }
      throw error;
    }
    if (minimumVersion == null || snapshot.version >= minimumVersion) break;
    await delay(150 * (2 ** attempt));
  }
  if (currentSession?.session_id !== sessionId) return;
  if (minimumVersion != null && snapshot.version < minimumVersion) {
    throw new Error("Session snapshot chưa bắt kịp realtime event");
  }
  if ((currentSession.version ?? 0) > snapshot.version) return;
  applySessionSnapshot(snapshot);
  if (snapshot.status === "closed") {
    await markCurrentSessionClosed();
    return;
  }
  if (refreshShares && snapshot.access_role === "owner") {
    await loadShares(snapshot.session_id);
  }
  if (refreshList) await loadSessions(activeSessionList);
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function applySessionSnapshot(snapshot) {
  if (!currentSession || currentSession.session_id !== snapshot.session_id) return;
  if ((currentSession.version ?? 0) > snapshot.version) return;
  currentSession = snapshot;
  const isOwner = snapshot.owner_user_id === currentUser.user_id;
  $("#session-access").textContent = `${snapshot.access_role} · ${snapshot.visibility}`;
  $("#session-visibility").value = snapshot.visibility;
  $("#owner-controls").hidden = !isOwner;
  $("#share-controls").hidden = !isOwner;
  showPublicLink(snapshot.public_url);
  updateSessionCard(snapshot);
  updateControls();
}

function updateSessionCard(session) {
  const card = [...document.querySelectorAll(".session-card")].find(
    (item) => item.dataset.sessionId === session.session_id,
  );
  if (!card) return;
  card.classList.toggle("is-closed", session.status === "closed");
  card.innerHTML = `<strong>${escapeHtml(session.character_id)}</strong><span>${escapeHtml(session.access_role)} · ${escapeHtml(session.visibility)} · ${escapeHtml(session.status)}</span>`;
}

async function handleSessionAccessRevoked() {
  if (!currentSession) return;
  const sessionId = currentSession.session_id;
  currentSession = { ...currentSession, can_chat: false };
  await disconnectCurrent();
  if (currentSession?.session_id !== sessionId) return;
  $("#session-access").textContent = "Quyền truy cập đã bị thu hồi";
  $("#owner-controls").hidden = true;
  $("#share-controls").hidden = true;
  showPublicLink(undefined);
  input.placeholder = "Bạn không còn quyền truy cập session này";
  addMessage("Quyền truy cập phiên đã bị thu hồi.", "assistant");
  await loadSessions(activeSessionList);
}

async function markCurrentSessionClosed() {
  if (sessionClosePromise) return sessionClosePromise;
  sessionClosePromise = (async () => {
    if (!currentSession) return;
    const sessionId = currentSession.session_id;
    currentSession = { ...currentSession, status: "closed", can_chat: false };
    await disconnectCurrent();
    if (currentSession?.session_id !== sessionId) return;
    renderSessionClosed();
    await loadSessions(activeSessionList);
  })();
  try {
    await sessionClosePromise;
  } finally {
    sessionClosePromise = undefined;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const content = input.value.trim();
  if (!content || chatSocket?.readyState !== WebSocket.OPEN) return;
  chatSocket.send(JSON.stringify({ type: "user_message", content }));
  input.value = "";
});

$("#session-visibility").addEventListener("change", async (event) => {
  const select = event.target;
  const sessionId = currentSession.session_id;
  const previousVisibility = currentSession.visibility;
  select.disabled = true;
  setSessionError();
  try {
    const updated = await (
      await api(
        `/api/sessions/${sessionId}/visibility`,
        { method: "PATCH", body: JSON.stringify({ visibility: select.value }) },
        true,
      )
    ).json();
    if (currentSession?.session_id !== sessionId) return;
    applySessionSnapshot(updated);
    await loadSessions(activeSessionList);
  } catch (error) {
    if (currentSession?.session_id === sessionId) select.value = previousVisibility;
    setSessionError(error.message);
  } finally {
    if (currentSession?.session_id === sessionId) {
      select.disabled = currentSession.status === "closed";
    }
  }
});

function showPublicLink(publicPath) {
  const panel = $("#public-link-panel");
  panel.hidden = !publicPath;
  if (!publicPath) {
    $("#public-link").value = "";
    $("#open-public-link").removeAttribute("href");
    return;
  }
  const absoluteUrl = new URL(publicPath, location.origin).href;
  $("#public-link").value = absoluteUrl;
  $("#open-public-link").href = absoluteUrl;
}

$("#copy-public-link").addEventListener("click", async () => {
  const button = $("#copy-public-link");
  const field = $("#public-link");
  let copied = false;
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(field.value);
      copied = true;
    } catch (error) {
      console.warn("Clipboard API failed, using selection fallback", error);
    }
  }
  if (!copied) {
    try {
      field.focus();
      field.select();
      field.setSelectionRange(0, field.value.length);
      copied = document.execCommand("copy");
      window.getSelection()?.removeAllRanges();
    } catch (error) {
      console.warn("Clipboard selection fallback failed", error);
    }
  }
  button.textContent = copied ? "Đã copy" : "Nhấn Ctrl+C";
  setTimeout(() => { button.textContent = "Copy URL"; }, 1600);
});

$("#share-session").addEventListener("click", async () => {
  const button = $("#share-session");
  const sessionId = currentSession.session_id;
  button.disabled = true;
  setSessionError();
  try {
    await api(
      `/api/sessions/${sessionId}/shares`,
      {
        method: "POST",
        body: JSON.stringify({
          username: $("#share-username").value,
          permission: $("#share-permission").value,
        }),
      },
      true,
    );
    if (currentSession?.session_id !== sessionId) return;
    $("#share-username").value = "";
    await loadShares(sessionId);
  } catch (error) {
    setSessionError(error.message);
  } finally {
    if (currentSession?.session_id === sessionId) {
      button.disabled = currentSession.status === "closed";
    }
  }
});

async function loadShares(sessionId = currentSession?.session_id) {
  if (!sessionId) return;
  const shares = await (
    await api(`/api/sessions/${sessionId}/shares`)
  ).json();
  if (currentSession?.session_id !== sessionId) return;
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
      revoke.disabled = true;
      setSessionError();
      try {
        await api(
          `/api/sessions/${sessionId}/shares/${share.user_id}`,
          { method: "DELETE" },
          true,
        );
        await loadShares(sessionId);
      } catch (error) {
        setSessionError(error.message);
        revoke.disabled = false;
      }
    });
    row.append(revoke);
    list.append(row);
  }
}

$("#close-session").addEventListener("click", async () => {
  const button = $("#close-session");
  const sessionId = currentSession.session_id;
  button.disabled = true;
  setSessionError();
  try {
    await api(`/api/sessions/${sessionId}`, { method: "DELETE" }, true);
    if (currentSession?.session_id === sessionId) await markCurrentSessionClosed();
  } catch (error) {
    setSessionError(error.message);
    if (currentSession?.session_id === sessionId) button.disabled = false;
  }
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
