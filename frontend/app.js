// Studio IA Local: lógica de la interfaz (vanilla JS, sin dependencias externas).
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const state = { conv: null, agents: {}, tools: {}, attachments: [], busy: false, rec: null };

// ---------------------------------------------------------------- utilidades
function store(k, v) { try { v === undefined ? null : localStorage.setItem(k, v); return localStorage.getItem(k); } catch { return null; } }
(function bootstrapToken() {
  const t = new URLSearchParams(location.search).get("token");
  if (t) { document.cookie = `studio_token=${encodeURIComponent(t)}; path=/; max-age=31536000; SameSite=Strict`; history.replaceState(null, "", location.pathname); }
})();

async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) { init.body = JSON.stringify(opts.json); init.headers["Content-Type"] = "application/json"; init.method = init.method || "POST"; }
  const r = await fetch(path, init);
  if (r.status === 401) { askToken(); throw new Error("Token de acceso requerido"); }
  if (!r.ok) { let d = r.statusText; try { d = (await r.json()).detail || d; } catch {} throw new Error(d); }
  return opts.raw ? r : r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}
function askToken() {
  const t = prompt("Este estudio está protegido. Introduce el token de acceso:");
  if (t) { document.cookie = `studio_token=${encodeURIComponent(t)}; path=/; max-age=31536000; SameSite=Strict`; location.reload(); }
}
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) k === "class" ? (n.className = v) : k.startsWith("on") ? n.addEventListener(k.slice(2), v) : n.setAttribute(k, v);
  for (const k of kids) n.append(k);
  return n;
};
const toast = (msg) => { console.warn(msg); alert(msg); };
const fileToB64 = (f) => new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result.split(",")[1]); r.readAsDataURL(f); });

// ---------------------------------------------------------------- drawers (móvil)
function toggle(id, force) {
  const n = $("#" + id); const open = force ?? !n.classList.contains("open");
  $$(".drawer").forEach((d) => d.classList.remove("open"));
  n.classList.toggle("open", open); $("#scrim").classList.toggle("show", open);
}
$$("[data-toggle]").forEach((b) => b.addEventListener("click", () => toggle(b.dataset.toggle)));
$("#scrim").addEventListener("click", () => { $$(".drawer").forEach((d) => d.classList.remove("open")); $("#scrim").classList.remove("show"); });
$("#panelClose").addEventListener("click", () => toggle("panel", false));
$$(".tabs button[data-tab]").forEach((b) => b.addEventListener("click", () => {
  $$(".tabs button[data-tab]").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + b.dataset.tab));
  ({ ws: loadWorkspace, mem: loadMemory, agents: loadAgents, models: loadHealth })[b.dataset.tab]();
}));

// ---------------------------------------------------------------- conversaciones
async function loadConversations() {
  const list = await api("/api/conversations");
  const nav = $("#convList"); nav.innerHTML = "";
  for (const c of list) {
    nav.append(el("div", { class: "conv" + (c.id === state.conv ? " active" : ""), onclick: () => openConversation(c.id) },
      el("span", {}, c.title || "Sin título"),
      el("button", { title: "Borrar", onclick: async (e) => { e.stopPropagation(); if (confirm("¿Borrar la conversación?")) { await api(`/api/conversations/${c.id}`, { method: "DELETE" }); if (state.conv === c.id) newChat(); loadConversations(); } } }, "🗑")));
  }
}
async function openConversation(id) {
  state.conv = id; store("conv", id);
  const msgs = await api(`/api/conversations/${id}`);
  $("#messages").innerHTML = "";
  for (const m of msgs) addMessage(m.role, m.content, { images: (m.images || []).map((p) => `/api/workspace/raw?path=${encodeURIComponent(p)}`), agent: m.agent });
  if (!msgs.length) showWelcome();
  loadConversations(); toggle("sidebar", false);
}
function newChat() { state.conv = null; store("conv", ""); $("#messages").innerHTML = ""; showWelcome(); loadConversations(); }
function showWelcome() { $("#messages").append(welcomeTpl.cloneNode(true)); bindChips(); }
const welcomeTpl = $("#welcome").cloneNode(true);
function bindChips() { $$(".chip").forEach((c) => (c.onclick = () => { $("#input").value = c.textContent; $("#input").focus(); })); }
bindChips();
$("#newChat").onclick = newChat;

// ---------------------------------------------------------------- mensajes
function addMessage(role, content, { images = [], agent = "" } = {}) {
  $("#welcome")?.remove(); $$(".welcome").forEach((w) => w.remove());
  const wrap = el("div", { class: "msg " + role });
  if (role === "assistant" && agent) wrap.append(el("div", { class: "who" }, agentLabel(agent)));
  if (images.length) wrap.append(el("div", { class: "thumbs" }, ...images.map((src) => el("img", { src }))));
  const bubble = el("div", { class: "bubble" });
  if (role === "user") bubble.textContent = content; else bubble.innerHTML = renderMarkdown(content);
  wrap.append(bubble);
  if (role === "assistant") wrap.append(actions(() => bubble.innerText));
  $("#messages").append(wrap); scrollDown();
  return { wrap, bubble };
}
function agentLabel(id) {
  if (id === "supervisor" || id === "auto") return "🧭 Equipo";
  const a = state.agents[id]; return a ? `${a.emoji || "🤖"} ${a.name}` : id;
}
function actions(getText) {
  return el("div", { class: "msg-actions" },
    el("button", { onclick: () => navigator.clipboard?.writeText(getText()) }, "Copiar"),
    el("button", { onclick: () => speak(getText()) }, "🔊 Escuchar"));
}
const scrollDown = () => { const m = $("#messages"); m.scrollTop = m.scrollHeight; };

// ---------------------------------------------------------------- envío y streaming
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
$("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !matchMedia("(pointer: coarse)").matches) { e.preventDefault(); if (!state.busy) send(); } });
$("#input").addEventListener("input", (e) => { e.target.style.height = "auto"; e.target.style.height = e.target.scrollHeight + "px"; });

async function send() {
  if (state.busy) { state.abort?.abort(); return; }  // el botón hace de «Parar» mientras responde
  const text = $("#input").value.trim();
  const imgs = state.attachments.filter((a) => a.kind === "image");
  const docs = state.attachments.filter((a) => a.kind === "file");
  if (!text && !imgs.length && !docs.length) return;
  state.busy = true; state.abort = new AbortController();
  $("#sendBtn").textContent = "■ Parar"; $("#sendBtn").title = "Detener la respuesta";

  let message = text;
  if (docs.length) {  // los documentos se suben al workspace y se mencionan en el mensaje
    const fd = new FormData(); docs.forEach((d) => fd.append("files", d.file));
    const up = await api("/api/workspace/upload?folder=subidas", { method: "POST", body: fd }).catch((e) => (toast(e.message), []));
    message += `\n\n[Archivos subidos al workspace: ${up.map((u) => u.path).join(", ")}]`;
  }
  addMessage("user", text || "(adjuntos)", { images: imgs.map((i) => i.url) });
  $("#input").value = ""; $("#input").style.height = "auto";
  const images = imgs.map((i) => i.b64); state.attachments = []; renderAttachments();

  const { wrap, bubble } = addMessage("assistant", "", { agent: $("#mode").value });
  wrap.querySelector(".msg-actions").remove();
  const trace = el("details", { class: "trace" }, el("summary", {}, "Trabajando…"));
  wrap.insertBefore(trace, bubble); bubble.classList.add("typing");
  let final = "", steps = 0, finished = false, frame = 0;
  const summary = trace.querySelector("summary");
  const t0 = performance.now(); let tFirst = 0;
  // Pintar el Markdown como mucho una vez por fotograma (antes se repintaba en cada token: O(n²))
  const paint = () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; bubble.innerHTML = renderMarkdown(final); scrollDown(); }); };
  const finish = () => {
    if (finished) return; finished = true;
    if (frame) { cancelAnimationFrame(frame); frame = 0; }
    if (final) bubble.innerHTML = renderMarkdown(final);
    bubble.classList.remove("typing");
    const secs = ((performance.now() - t0) / 1000).toFixed(1);
    summary.textContent = (steps ? `Proceso del equipo (${steps} pasos)` : "Proceso") + ` · ${secs} s` + (tFirst ? ` (primera palabra en ${((tFirst - t0) / 1000).toFixed(1)} s)` : "");
    if (!trace.querySelector(".step") && !tFirst) trace.remove();
    wrap.append(actions(() => bubble.innerText));
    if ($("#speak").checked && final) speak(final);
    state.busy = false; state.abort = null; $("#sendBtn").textContent = "Enviar"; $("#sendBtn").title = "";
    loadConversations();
  };

  try {
    const r = await api("/api/chat", { json: { conversation_id: state.conv, message, images, mode: $("#mode").value, auto_memory: $("#autoMem").checked }, raw: true, signal: state.abort.signal });
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        switch (ev.type) {
          case "conversation": if (state.conv !== ev.id) { state.conv = ev.id; store("conv", ev.id); } break;
          case "status": summary.textContent = ev.text; break;
          case "agent": steps++; summary.textContent = `${ev.emoji} ${ev.name} trabajando…`;
            trace.append(el("div", { class: "step", style: `--d:${ev.depth}` }, `${ev.emoji} `, el("strong", {}, ev.name), `: ${ev.task}`)); break;
          case "tool_call": steps++; summary.textContent = `🔧 ${ev.tool}…`;
            if (ev.tool !== "delegate") trace.append(el("div", { class: "step", style: `--d:${ev.depth + 1}` }, `🔧 ${ev.tool} `, el("pre", {}, JSON.stringify(ev.args, null, 1)))); break;
          case "tool_result": if (ev.tool !== "delegate") trace.append(el("div", { class: "step", style: `--d:${ev.depth + 1}` }, `↳ resultado`, el("pre", {}, ev.result))); break;
          case "memory": trace.append(el("div", { class: "step" }, "🧠 Memoria relevante", el("pre", {}, ev.text))); break;
          case "memory_saved": wrap.append(el("div", { class: "hint" }, `🧠 Recordado: ${ev.facts.join(" · ")}`)); break;
          case "reset": final = ""; paint(); break;
          case "partial": if (!tFirst) tFirst = performance.now(); final = ev.text; paint(); break;
          case "final": final = ev.text ?? final; paint(); break;
          case "token": if (!tFirst) tFirst = performance.now(); final += ev.text; paint(); break;
          case "error": final = ""; bubble.innerHTML = `<p style="color:var(--danger)">⚠️ ${ev.text}</p>`; break;
          case "done": finish(); break;  // el chat queda libre; la memoria automática sigue en segundo plano
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") { final += final ? "\n\n*(detenido)*" : "*(detenido)*"; }
    else if (!finished) bubble.innerHTML = `<p style="color:var(--danger)">⚠️ ${e.message}</p>`;
  }
  finish();
}

// ---------------------------------------------------------------- adjuntos
$("#attachBtn").onclick = () => $("#fileInput").click();
$("#fileInput").onchange = (e) => { addFiles([...e.target.files]); e.target.value = ""; };
document.addEventListener("paste", (e) => { const f = [...(e.clipboardData?.files || [])]; if (f.length) { e.preventDefault(); addFiles(f); } });
$("#chat").addEventListener("dragover", (e) => e.preventDefault());
$("#chat").addEventListener("drop", (e) => { e.preventDefault(); addFiles([...e.dataTransfer.files]); });
async function addFiles(files) {
  for (const f of files) {
    if (f.type.startsWith("image/")) state.attachments.push({ kind: "image", b64: await fileToB64(f), url: URL.createObjectURL(f), name: f.name });
    else state.attachments.push({ kind: "file", file: f, name: f.name });
  }
  renderAttachments();
}
function renderAttachments() {
  const box = $("#attachments"); box.innerHTML = "";
  state.attachments.forEach((a, i) => box.append(el("div", { class: "att" },
    a.kind === "image" ? el("img", { src: a.url }) : el("span", {}, "📄 " + a.name),
    el("button", { type: "button", onclick: () => { state.attachments.splice(i, 1); renderAttachments(); } }, "✕"))));
}

// ---------------------------------------------------------------- voz
$("#micBtn").onclick = async () => {
  if (window.AndroidBridge) return androidMic();
  if (state.rec) { state.rec.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia) return toast("El micrófono necesita https o localhost en este navegador.");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const rec = new MediaRecorder(stream); const chunks = [];
    rec.ondataavailable = (e) => chunks.push(e.data);
    rec.onstop = async () => { stream.getTracks().forEach((t) => t.stop()); state.rec = null; $("#micBtn").classList.remove("rec"); await transcribe(new Blob(chunks, { type: rec.mimeType })); };
    rec.start(); state.rec = rec; $("#micBtn").classList.add("rec");
  } catch (e) { toast("No se pudo usar el micrófono: " + e.message); }
};
// En la app Android la grabación es nativa (WebView no permite getUserMedia sobre http en la LAN)
function androidMic() {
  if (AndroidBridge.isRecording()) { AndroidBridge.stopRecording(); $("#micBtn").classList.remove("rec"); }
  else if (AndroidBridge.startRecording()) $("#micBtn").classList.add("rec");
}
window.onAndroidAudio = async (b64, mime) => {
  const bin = atob(b64); const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  await transcribe(new Blob([bytes], { type: mime || "audio/mp4" }));
};
async function transcribe(blob) {
  $("#input").placeholder = "Transcribiendo…";
  const fd = new FormData(); fd.append("audio", blob, "voz.webm");
  try { const r = await api("/api/stt", { method: "POST", body: fd }); $("#input").value = ($("#input").value + " " + r.text).trim(); $("#input").focus(); }
  catch (e) { toast("Transcripción no disponible: " + e.message); }
  $("#input").placeholder = "Escribe un mensaje…";
}
let currentAudio = null;
async function speak(text) {
  try {
    currentAudio?.pause();
    const r = await api("/api/tts", { json: { text: text.replace(/[#*`>|_]/g, "") }, raw: true });
    currentAudio = new Audio(URL.createObjectURL(await r.blob())); currentAudio.play();
  } catch (e) {
    if ("speechSynthesis" in window) { const u = new SpeechSynthesisUtterance(text); u.lang = "es-ES"; speechSynthesis.speak(u); }
    else toast("Voz no disponible: " + e.message);
  }
}

// ---------------------------------------------------------------- workspace
let wsCurrent = null;
async function loadWorkspace() {
  const items = await api("/api/workspace").catch(() => []);
  const ul = $("#wsTree"); ul.innerHTML = "";
  if (!items.length) ul.append(el("li", {}, el("span", { class: "hint" }, "Vacío. Sube archivos o pide a los agentes que creen alguno.")));
  for (const it of items) {
    const depth = it.path.split("/").length - 1;
    ul.append(el("li", { style: `padding-left:${8 + depth * 14}px` },
      el("span", { class: "t", onclick: () => !it.dir && openFile(it.path) }, `${it.dir ? "📁" : "📄"} ${it.path.split("/").pop()}`),
      it.dir ? "" : el("small", {}, fmtSize(it.size)),
      it.dir ? "" : el("a", { href: `/api/workspace/raw?path=${encodeURIComponent(it.path)}`, download: "", title: "Descargar" }, "⬇")));
  }
}
const fmtSize = (n) => (n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : n > 1e3 ? (n / 1e3).toFixed(0) + " KB" : n + " B");
async function openFile(path) {
  wsCurrent = path; $("#wsEditor").hidden = false; $("#wsPath").textContent = path;
  const isImg = /\.(png|jpe?g|webp|gif|bmp)$/i.test(path);
  $("#wsImage").hidden = !isImg; $("#wsContent").hidden = isImg; $("#wsSave").hidden = isImg;
  if (isImg) $("#wsImage").src = `/api/workspace/raw?path=${encodeURIComponent(path)}&t=${Date.now()}`;
  else $("#wsContent").value = (await api(`/api/workspace/file?path=${encodeURIComponent(path)}`).catch((e) => ({ content: "⚠️ " + e.message }))).content;
}
$("#wsClose").onclick = () => { $("#wsEditor").hidden = true; wsCurrent = null; };
$("#wsSave").onclick = async () => { await api("/api/workspace/file", { method: "PUT", json: { path: wsCurrent, content: $("#wsContent").value } }); loadWorkspace(); };
$("#wsDelete").onclick = async () => { if (!confirm(`¿Borrar ${wsCurrent}?`)) return; await api(`/api/workspace?path=${encodeURIComponent(wsCurrent)}`, { method: "DELETE" }); $("#wsClose").click(); loadWorkspace(); };
$("#wsNew").onclick = async () => { const p = prompt("Nombre del archivo (p. ej. notas/ideas.md):"); if (p) { await api("/api/workspace/file", { method: "PUT", json: { path: p, content: "" } }); await loadWorkspace(); openFile(p); } };
$("#wsUpload").onclick = () => $("#wsFile").click();
$("#wsFile").onchange = async (e) => { const fd = new FormData(); [...e.target.files].forEach((f) => fd.append("files", f)); await api("/api/workspace/upload", { method: "POST", body: fd }).catch((x) => toast(x.message)); e.target.value = ""; loadWorkspace(); };
$("#wsReindex").onclick = async () => { const r = await api("/api/workspace/reindex", { method: "POST" }).catch((x) => toast(x.message)); if (r) toast(`Indexados ${r.chunks} fragmentos`); };

// ---------------------------------------------------------------- memoria
async function loadMemory(q) {
  const ul = $("#memList"); ul.innerHTML = "";
  let items;
  try { items = q ? await api(`/api/memory/search?q=${encodeURIComponent(q)}`) : await api("/api/memory"); }
  catch (e) { ul.append(el("li", {}, el("span", { class: "hint" }, "⚠️ " + e.message))); return; }
  if (!items.length) ul.append(el("li", {}, el("span", { class: "hint" }, "Aún no hay recuerdos. Se crean solos al conversar o puedes añadirlos.")));
  for (const m of items) ul.append(el("li", {},
    el("span", { class: "t" }, m.text.slice(0, 400), el("br"), el("small", {}, `${m.kind} · ${m.source}${m.score ? " · " + m.score : ""}`)),
    el("button", { title: "Olvidar", onclick: async () => { await api(`/api/memory/${m.id}`, { method: "DELETE" }); loadMemory($("#memSearch").value); } }, "🗑")));
}
$("#memForm").onsubmit = async (e) => { e.preventDefault(); const t = $("#memText").value.trim(); if (!t) return; await api("/api/memory", { json: { text: t } }).catch((x) => toast(x.message)); $("#memText").value = ""; loadMemory(); };
let memTimer; $("#memSearch").oninput = (e) => { clearTimeout(memTimer); memTimer = setTimeout(() => loadMemory(e.target.value.trim()), 300); };

// ---------------------------------------------------------------- agentes
async function fetchAgents() {
  state.agents = await api("/api/agents"); state.tools = await api("/api/tools");
  const sel = $("#mode"); const cur = sel.value;
  sel.innerHTML = '<option value="auto">🧭 Equipo (supervisor)</option>';
  for (const [id, a] of Object.entries(state.agents)) sel.append(el("option", { value: id }, `${a.emoji || "🤖"} ${a.name}`));
  sel.value = state.agents[cur] || cur === "auto" ? cur : "auto";
}
function loadAgents() {
  const box = $("#agentList"); box.innerHTML = "";
  for (const [id, a] of Object.entries(state.agents)) box.append(agentCard(id, a));
}
function agentCard(id, a) {
  const card = el("div", { class: "agent-card", "data-id": id });
  const f = (name, val, tag = "input") => { const n = el(tag, { "data-f": name, placeholder: name }); n.value = val || ""; return n; };
  card.append(
    el("div", { class: "row" }, f("emoji", a.emoji), f("name", a.name), el("button", { class: "small danger", onclick: () => card.remove() }, "🗑")),
    f("description", a.description),
    el("label", { class: "hint" }, "Modelo (vacío = chat, «code» = programador, o un nombre de Ollama)"), f("model", a.model),
    el("label", { class: "hint" }, "Instrucciones del agente"), f("system", a.system, "textarea"),
    el("div", { class: "tools" }, ...Object.keys(state.tools).map((t) => {
      const cb = el("input", { type: "checkbox", value: t, title: state.tools[t] }); cb.checked = (a.tools || []).includes(t);
      return el("label", { title: state.tools[t] }, cb, " " + t);
    })));
  card.querySelector('[data-f="emoji"]').style.width = "52px";
  return card;
}
$("#agentAdd").onclick = () => $("#agentList").append(agentCard("agente" + Date.now().toString(36), { name: "Nuevo agente", emoji: "🤖", description: "", system: "", tools: [] }));
$("#agentSave").onclick = async () => {
  const out = {};
  for (const card of $$(".agent-card")) {
    const g = (k) => card.querySelector(`[data-f="${k}"]`).value.trim();
    const id = card.dataset.id.startsWith("agente") ? g("name").toLowerCase().normalize("NFD").replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "") || card.dataset.id : card.dataset.id;
    out[id] = { name: g("name"), emoji: g("emoji"), description: g("description"), model: g("model"), system: g("system"),
      tools: [...card.querySelectorAll(".tools input:checked")].map((c) => c.value) };
  }
  await api("/api/agents", { method: "PUT", json: out }).then(() => toast("Equipo guardado")).catch((e) => toast(e.message));
  await fetchAgents(); loadAgents();
};
$("#agentReset").onclick = async () => { if (confirm("¿Restaurar el equipo por defecto?")) { await api("/api/agents/reset", { method: "POST" }); await fetchAgents(); loadAgents(); } };

// ---------------------------------------------------------------- modelos y estado
async function loadHealth() {
  const h = await api("/api/health").catch(() => null);
  const dot = $("#statusDot"), txt = $("#statusText");
  if (!h) { dot.className = "dot bad"; txt.textContent = "Servidor no disponible"; return null; }
  const engine = h.engine || "Ollama";
  if (h.missing.length) { dot.className = "dot warn"; txt.textContent = `Faltan ${h.missing.length} modelo(s)`; }
  else if (h.starting) { dot.className = "dot warn"; txt.textContent = "Cargando el modelo…"; }
  else if (!h.ollama) { dot.className = "dot bad"; txt.textContent = `${engine} no está en marcha`; }
  else if (false) { dot.className = "dot warn"; txt.textContent = `Faltan ${h.missing.length} modelo(s)`; }
  else { dot.className = "dot ok"; txt.textContent = "Listo · local"; }
  const box = $("#health"); if (!box) return h;
  const mm = h.multimodal;
  box.innerHTML = "";
  box.append(
    el("h3", {}, "Estado"),
    el("div", {}, badge(engine, h.ollama), badge("Voz→texto", mm.stt), badge("Texto→voz", mm.tts), badge("Generar imágenes", mm.image)),
    el("h3", {}, "Modelos configurados"),
    el("div", {}, ...Object.entries(h.config).map(([k, m]) => badge(`${k}: ${m}`, !h.missing.includes(m)))),
  );
  if (h.device) box.append(el("p", { class: "hint" }, `Este dispositivo: ${h.device.ramGb} GB de RAM · ${h.device.cores} núcleos · ${h.device.freeGb} GB libres` + (h.device.engine ? "" : " · ⚠️ esta APK no incluye el motor")));
  if (h.error) box.append(el("pre", { class: "hint", style: "white-space:pre-wrap;color:var(--danger)" }, h.error));
  if (h.missing.length) box.append(el("p", {}, el("button", { class: "primary small", onclick: () => pullMany(h.missing) }, `⬇ Descargar los que faltan (${h.missing.length})`)));
  box.append(el("h3", {}, "Modelos instalados"), el("div", {}, ...(h.models.length ? h.models.map((m) => el("span", { class: "badge" }, m)) : [el("span", { class: "hint" }, "Ninguno")])));
  if (h.loaded) {  // qué hay cargado en memoria ahora mismo y cuánto va en la GPU
    const gb = (b) => (b / 1e9).toFixed(1) + " GB";
    box.append(el("h3", {}, "Cargados en memoria"), el("div", {}, ...(h.loaded.length ? h.loaded.map((m) => {
      const gpu = m.size ? Math.round((100 * m.vram) / m.size) : 0;
      return el("span", { class: "badge " + (gpu >= 100 ? "ok" : gpu > 0 ? "" : "bad"), title: gpu < 100 ? "Parte del modelo va en CPU: será más lento. Prueba un modelo más pequeño o menos contexto (NUM_CTX)." : "" },
        `${m.name} · ${gb(m.size)} · ${gpu ? gpu + " % GPU" : "CPU"}` + (m.ctx ? ` · ctx ${m.ctx}` : ""));
    }) : [el("span", { class: "hint" }, "Ninguno (se cargan con el primer mensaje)")])));
  }
  if (h.engine_opts) box.append(el("p", { class: "hint" }, `Motor: contexto ${h.engine_opts.num_ctx} tokens · modelos residentes ${h.engine_opts.keep_alive} · razonamiento ${h.engine_opts.think ? "activado" : "desactivado (más rápido)"}`));
  const rec = $("#recommended"); rec.innerHTML = ""; rec.append(el("h3", {}, "Recomendados por hardware"));
  for (const [tier, models] of Object.entries(h.recommended)) {
    const prof = h.profiles?.[tier];
    rec.append(el("div", { class: "row", style: "flex-wrap:wrap;margin:4px 0" }, el("span", { class: "hint" }, tier), ...models.map((m) => el("button", { class: "small", onclick: () => pullMany([m]) }, m)),
      ...(prof ? [el("button", { class: "small", title: "Copia estas líneas en studio.env y reinicia el estudio", onclick: () => {
        const txt = Object.entries(prof).map(([k, v]) => `${k}=${v}`).join("\n");
        (navigator.clipboard?.writeText(txt) || Promise.reject()).then(() => toast("Copiado. Pégalo en studio.env y reinicia el estudio.\n\n" + txt), () => prompt("Copia estas líneas en studio.env:", txt.replace(/\n/g, "  ")));
      } }, "📋 config")] : [])));
  }
  return h;
}
const badge = (t, ok) => el("span", { class: "badge " + (ok ? "ok" : "bad") }, (ok ? "✓ " : "✗ ") + t);
async function pullMany(models) { for (const m of models) await pull(m); loadHealth(); }
async function pull(model) {
  const box = $("#pullProgress"); const bar = el("progress", { max: 100, value: 0 }); const label = el("div", { class: "hint" }, `${model}: iniciando…`);
  box.append(label, bar);
  const r = await api("/api/models/pull", { json: { model }, raw: true });
  const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true }); const lines = buf.split("\n"); buf = lines.pop();
    for (const l of lines) { if (!l) continue; const ev = JSON.parse(l);
      if (ev.error) { label.textContent = `${model}: ⚠️ ${ev.error}`; return; }
      if (ev.total) bar.value = (100 * (ev.completed || 0)) / ev.total;
      label.textContent = `${model}: ${ev.status}`; }
  }
  label.textContent = `${model}: ✓ listo`; bar.value = 100;
}
$("#pullForm").onsubmit = (e) => { e.preventDefault(); const m = $("#pullName").value.trim(); if (m) pullMany([m]); };

// ---------------------------------------------------------------- arranque
(async function init() {
  try { await fetchAgents(); } catch (e) { console.error(e); }
  const h = await loadHealth();
  await loadConversations().catch(() => {});
  const last = store("conv"); if (last) openConversation(last).catch(() => newChat());
  loadWorkspace();
  if (h && (!h.ollama || h.missing.length)) { $$(".tabs button[data-tab]").find((b) => b.dataset.tab === "models").click(); if (innerWidth < 1100) toggle("panel", true); }
  setInterval(loadHealth, 30000);
})();
