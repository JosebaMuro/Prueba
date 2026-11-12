// Studio IA · modo autónomo en Android.
// Sustituye al backend del PC: resuelve aquí mismo las rutas /api (agentes, memoria, workspace,
// modelos) usando el puente nativo (AndroidBridge) y el motor llama.cpp que corre en el teléfono.
(function () {
  "use strict";
  const B = window.AndroidBridge;
  if (!B || !B.isLocal || !B.isLocal()) return;
  const PORT_CHAT = 8431, PORT_EMBED = 8432;

  // ------------------------------------------------------------------ puente asíncrono
  const pending = {};
  let seq = 0;
  window.__native = (id, type, data) => { const h = pending[id]; if (h) h(type, data); };
  function native(start, onChunk) {
    return new Promise((resolve, reject) => {
      const id = "n" + (++seq);
      pending[id] = (type, data) => {
        if (type === "chunk" || type === "progress") { onChunk && onChunk(data); return; }
        delete pending[id];
        type === "done" ? resolve(data) : reject(new Error(data || "Error"));
      };
      start(id);
    });
  }
  const kv = {
    get(name, def) { try { const s = B.kvGet(name); return s ? JSON.parse(s) : def; } catch { return def; } },
    set(name, value) { B.kvSet(name, JSON.stringify(value)); },
  };
  const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);

  // ------------------------------------------------------------------ catálogo de modelos
  const HF = "https://huggingface.co/";
  const EMBED = { file: "nomic-embed-text-v1.5.Q4_K_M.gguf", url: HF + "nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/nomic-embed-text-v1.5.Q4_K_M.gguf" };
  const PROFILES = {
    "gemma3-4b-vision": {
      label: "Gemma 3 4B · chat + visión (3,4 GB · móviles con 8 GB de RAM o más)",
      model: { file: "gemma-3-4b-it-Q4_K_M.gguf", url: HF + "ggml-org/gemma-3-4b-it-GGUF/resolve/main/gemma-3-4b-it-Q4_K_M.gguf" },
      mmproj: { file: "mmproj-gemma-3-4b-it.gguf", url: HF + "ggml-org/gemma-3-4b-it-GGUF/resolve/main/mmproj-model-f16.gguf" },
    },
    "qwen3-4b": {
      label: "Qwen3 4B · solo texto, el más fiable con herramientas (2,5 GB · 8 GB de RAM)",
      model: { file: "Qwen3-4B-Q4_K_M.gguf", url: HF + "unsloth/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf" },
    },
    "qwen3-1.7b": {
      label: "Qwen3 1.7B · solo texto, rápido (1,1 GB · móviles con 6 GB de RAM)",
      model: { file: "Qwen3-1.7B-Q4_K_M.gguf", url: HF + "unsloth/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf" },
    },
  };
  const device = () => { try { return JSON.parse(B.deviceInfo()); } catch { return {}; } };
  function settings() {
    const s = kv.get("settings", {});
    if (!s.profile) s.profile = (device().ramGb || 0) >= 7 ? "gemma3-4b-vision" : "qwen3-1.7b";
    return s;
  }
  function activeProfile() {
    const s = settings();
    if (s.profile === "custom" && s.custom) return { label: "Personalizado", model: s.custom };
    return PROFILES[s.profile] || PROFILES["qwen3-1.7b"];
  }
  const filesOf = (p) => [p.model, p.mmproj, EMBED].filter(Boolean);
  const downloaded = () => { try { return JSON.parse(B.modelFiles()); } catch { return {}; } };
  const missingFiles = () => { const have = downloaded(); return filesOf(activeProfile()).filter((f) => !have[f.file]); };

  let starting = null, override = null;
  function ensureEngine() {
    const r = override || resolveActive();
    if (r.error) return Promise.reject(new Error(r.error));
    if (r.engine !== "llamacpp") {
      if (!starting) {
        const ctx = (device().ramGb || 0) >= 7 ? 6144 : 4096;
        const want = JSON.stringify({ engine: r.engine, model: r.path, tokenizer: r.tokenizer || "", placement: r.placement, ctx, temperature: 0.6 });
        if (ensureEngine.loaded === want) return Promise.resolve();
        B.stopEngine();  // libera la RAM de llama-server
        starting = native((id) => B.genLoad(id, want)).then(() => { ensureEngine.loaded = want; }).finally(() => { starting = null; });
      }
      return starting;
    }
    if (ensureEngine.loaded && B.genUnload) { B.genUnload(); ensureEngine.loaded = ""; }
    const st = JSON.parse(B.engineStatus());
    const p = activeProfile(), have = downloaded();
    const file = r.m && r.m.format === "gguf" && r.ref !== p.model.file ? r.path : p.model.file;
    if (!have[file] && !(r.m && r.m.size)) return Promise.reject(new Error("Falta descargar el modelo. Abre ▤ → ⚙️ Modelos."));
    const mm = file === p.model.file && p.mmproj && have[p.mmproj.file] ? p.mmproj.file : "";
    const emb = have[EMBED.file] ? EMBED.file : "";
    if (st.chat && st.model === file + "|" + mm && (st.embed || !emb)) return Promise.resolve();
    if (!starting) {
      const ctx = (device().ramGb || 0) >= 7 ? 6144 : 4096;
      starting = native((id) => B.startEngine(id, file, mm, emb, ctx)).finally(() => { starting = null; });
    }
    return starting;
  }
  const hasVision = () => { if (resolveActive().engine !== "llamacpp") return false; const p = activeProfile(); return !!(p.mmproj && downloaded()[p.mmproj.file]); };

  // ------------------------------------------------------------------ motores de inferencia
  // llama.cpp va como proceso aparte (llama-server). ONNX Runtime GenAI, ExecuTorch, LiteRT-LM y Qualcomm Genie
  // (APK para Snapdragon) se ejecutan dentro de la app (Engines.java). Cada modelo de la carpeta "models" se asigna
  // al motor que lee su formato; con varios posibles gana el que más tokens/s dio en la prueba de velocidad.
  const EINFO = (() => { try { return JSON.parse(B.engineInfo ? B.engineInfo() : "{}"); } catch { return {}; } })();
  const HAS = EINFO.engines || { llamacpp: !!device().engine };
  const ENGINE_ORDER = ["onnx", "openvino", "litert", "executorch", "mlc", "tensorrt", "genie", "llamacpp"];
  const ENGINE_NAMES = { onnx: "ONNX Runtime GenAI", openvino: "OpenVINO GenAI", litert: "LiteRT-LM", executorch: "ExecuTorch",
    mlc: "MLC LLM", tensorrt: "TensorRT-LLM", genie: "Qualcomm Genie (QNN)", llamacpp: "llama.cpp (llama-server)" };
  const PLACEMENTS = { auto: "Automático", gpu: "GPU (memoria compartida)", cpu: "CPU (RAM)", npu: "NPU" };
  const FORMAT_ENGINES = { gguf: ["llamacpp", "genie"], onnx: ["onnx"], executorch: ["executorch"], litert: ["litert"], genie: ["genie"] };
  const FORMAT_LABELS = { gguf: "GGUF", onnx: "ONNX GenAI", executorch: "ExecuTorch .pte", litert: "LiteRT-LM", genie: "Qualcomm Genie" };
  function engineState(id) {
    if (id === "openvino") return [false, "OpenVINO no publica versión para Android (es para PC con Intel)"];
    if (id === "mlc") return [false, "MLC LLM obliga a compilar cada modelo dentro de la propia app: no admite modelos sueltos"];
    if (id === "tensorrt") return [false, "Solo funciona con GPUs NVIDIA de PC o servidor"];
    if (id === "genie") {
      if (!EINFO.qualcomm) return [false, "Necesita un procesador Snapdragon (NPU Hexagon)"];
      if (!HAS.genie) return [false, "Instala la APK para Snapdragon (StudioIA-Android-Snapdragon.apk), que lo incluye"];
      return [true, ""];
    }
    return HAS[id] ? [true, ""] : [false, "Esta APK no incluye este motor"];
  }
  const engineOk = (id) => engineState(id)[0];
  function detectFormat(entry) {
    const n = entry.name.toLowerCase(), files = (entry.files || []).map((f) => f.toLowerCase());
    if (!entry.dir) return n.endsWith(".gguf") ? "gguf" : n.endsWith(".pte") ? "executorch" : /\.(litertlm|task)$/.test(n) ? "litert" : "";
    if (files.includes("genai_config.json")) return "onnx";
    if (files.includes("genie_config.json")) return "genie";
    if (files.some((f) => f.endsWith(".pte"))) return "executorch";
    if (files.some((f) => /\.(litertlm|task)$/.test(f))) return "litert";
    if (files.some((f) => f.endsWith(".gguf") && !f.includes("mmproj"))) return "gguf";
    return "";
  }
  function localModels() {
    let list = [];
    try { list = JSON.parse(B.modelList ? B.modelList() : "[]"); } catch {}
    return list.map((e) => ({ ...e, format: detectFormat(e) }))
      .filter((e) => e.format && e.name !== EMBED.file && !e.name.toLowerCase().includes("mmproj"));
  }
  function modelPaths(m) {   // rutas relativas a la carpeta models del archivo principal y del tokenizer
    if (!m.dir) {
      const tok = m.format === "executorch" ? "" : "";
      return { path: m.name, tokenizer: tok };
    }
    const f = m.files || [], pick = (re) => f.find((x) => re.test(x));
    const main = m.format === "executorch" ? pick(/\.pte$/i) : m.format === "litert" ? pick(/\.(litertlm|task)$/i)
      : m.format === "gguf" ? f.find((x) => /\.gguf$/i.test(x) && !/mmproj/i.test(x)) : "";
    const tok = pick(/^tokenizer\.(json|model)$/i) || pick(/\.tiktoken$/i) || "";
    return { path: main ? `${m.name}/${main}` : m.name, tokenizer: tok ? `${m.name}/${tok}` : "", dir: m.name };
  }
  function placementsFor(engine, m) {
    if (engine === "llamacpp" || engine === "onnx") return ["cpu"];  // llama.cpp estático y ORT de Android: CPU
    if (engine === "executorch") return [/vulkan/i.test(modelPaths(m).path) ? "gpu" : "cpu"];  // lo fija la exportación
    if (engine === "litert") return ["gpu", "cpu"];
    if (engine === "genie") return m.format === "gguf" ? ["gpu", "npu", "cpu"] : ["npu"];
    return ["cpu"];
  }
  const SPEED = { genie: 5, litert: 4, executorch: 3, onnx: 2, llamacpp: 1 };   // estimación sin medir
  const activeRef = () => settings().active || activeProfile().model.file;
  function resolve(ref) {
    const s = settings(), want = (s.models || {})[ref] || {};
    let m = localModels().find((x) => x.name === ref);
    if (!m) m = { name: ref, dir: false, files: [], format: "gguf" };  // modelo del perfil aún sin descargar
    let cands = (FORMAT_ENGINES[m.format] || []).filter(engineOk);
    if (want.engine && want.engine !== "auto" && cands.includes(want.engine)) cands = [want.engine];
    if (!cands.length) return { ref, m, engine: (FORMAT_ENGINES[m.format] || ["llamacpp"])[0], placement: "cpu", error: `Ningún motor de esta APK lee ${FORMAT_LABELS[m.format] || "este formato"}` };
    const bench = (s.bench || {})[ref] || {};
    let best = null;
    for (const e of cands) for (const p of placementsFor(e, m)) {
      if (want.placement && want.placement !== "auto" && p !== want.placement) continue;
      const r = bench[`${e}|${p}`];
      const score = r && r.tps ? 1000 + r.tps : (e === "genie" && m.format === "gguf" ? 0.5 : SPEED[e] || 0) + (p === "npu" ? 0.3 : p === "gpu" ? 0.2 : 0);
      if (!best || score > best.score) best = { engine: e, placement: p, score };
    }
    if (!best) best = { engine: cands[0], placement: placementsFor(cands[0], m)[0] };
    return { ref, m, ...modelPaths(m), engine: best.engine, placement: best.placement };
  }
  const resolveActive = () => resolve(activeRef());

  // ------------------------------------------------------------------ LLM
  const stripThink = (t) => t.replace(/<think>[\s\S]*?<\/think>/g, "").replace(/<think>[\s\S]*$/, "");
  async function chat(messages, opts = {}) {
    const r = override || resolveActive();
    return r.engine === "llamacpp" ? chatLlama(messages, opts) : chatNative(r, messages, opts);
  }

  // Plantillas de chat de cada modelo (Jinja de Hugging Face) para los motores que reciben el prompt ya formateado
  const CHATML = "{% for m in messages %}<|im_start|>{{ m.role }}\n{{ m.content }}<|im_end|>\n{% endfor %}{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}";
  const templates = {};
  async function renderPrompt(r, messages) {
    if (!templates[r.ref]) {
      let tpl = "", special = {};
      const dir = r.dir || "";
      for (const name of ["chat_template.jinja", "tokenizer_config.json"]) {
        const txt = dir ? B.modelRead(`${dir}/${name}`) : "";
        if (!txt) continue;
        if (name.endsWith(".jinja")) { tpl = tpl || txt; continue; }
        try {
          const j = JSON.parse(txt);
          let t = j.chat_template;
          if (Array.isArray(t)) t = (t.find((x) => x.name === "default") || t[0] || {}).template;
          tpl = tpl || t || "";
          for (const k of ["bos_token", "eos_token"]) { const v = j[k]; special[k] = typeof v === "object" && v ? v.content : v || ""; }
        } catch {}
      }
      const { Template } = await import("./jinja.js");
      let compiled;
      try { compiled = new Template(tpl || CHATML); } catch { compiled = new Template(CHATML); }
      templates[r.ref] = { compiled, special };
    }
    const { compiled, special } = templates[r.ref];
    const msgs = mergeUsers(messages.map((m) => ({ role: m.role, content: textOf(m.content) })));
    try { return compiled.render({ messages: msgs, add_generation_prompt: true, enable_thinking: false, bos_token: "", eos_token: "", ...special }); }
    catch { const { Template } = await import("./jinja.js"); return new Template(CHATML).render({ messages: msgs, add_generation_prompt: true }); }
  }
  const textOf = (c) => (Array.isArray(c) ? c.filter((x) => x.type === "text").map((x) => x.text).join("\n") : String(c || ""));
  const mergeUsers = (msgs) => msgs.reduce((out, m) => {
    const last = out[out.length - 1];
    if (last && last.role === m.role && m.role === "user") last.content += "\n\n" + m.content; else out.push({ ...m });
    return out;
  }, []);
  const STOPS = ["<|im_end|>", "<end_of_turn>", "<|eot_id|>", "<|end|>", "<|endoftext|>", "</s>"];
  const cutStops = (t) => STOPS.reduce((acc, s) => acc.split(s)[0], t);

  async function chatNative(r, messages, { onText, maxTokens = 1024, temperature = 0.6 } = {}) {
    await ensureEngine();
    const ctx = (device().ramGb || 0) >= 7 ? 6144 : 4096;
    const req = { maxTokens, temperature, ctx };
    if (r.engine === "onnx" || r.engine === "executorch") req.prompt = await renderPrompt(r, messages);
    else {  // LiteRT-LM y Genie aplican la plantilla del propio modelo
      const msgs = mergeUsers(messages.map((m) => ({ role: m.role, content: textOf(m.content) })));
      req.system = msgs[0] && msgs[0].role === "system" ? msgs.shift().content : "";
      req.message = msgs.length && msgs[msgs.length - 1].role === "user" ? msgs.pop().content : "";
      req.history = msgs.map((m) => ({ role: m.role === "assistant" ? "assistant" : m.role, content: m.content }));
    }
    let text = "";
    await native((id) => B.genRun(id, JSON.stringify(req)), (piece) => {
      text += piece;
      onText && onText(stripThink(cutStops(text)));
    });
    return stripThink(cutStops(text)).trim();
  }

  async function chatLlama(messages, { onText, maxTokens = 1024, temperature = 0.6 } = {}) {
    let text = "";
    const body = { messages, stream: true, temperature, max_tokens: maxTokens, cache_prompt: true,
      chat_template_kwargs: { enable_thinking: false } };
    await native((id) => B.http(id, PORT_CHAT, "/v1/chat/completions", JSON.stringify(body), true), (chunk) => {
      if (chunk === "[DONE]") return;
      try {
        const d = JSON.parse(chunk).choices?.[0]?.delta?.content;
        if (d) { text += d; onText && onText(stripThink(text)); }
      } catch {}
    });
    return stripThink(text).trim();
  }

  // Embeddings: modelo nomic si está descargado; si no, vector léxico (la memoria funciona igual, con menos finura)
  function lexical(text) {
    const v = new Array(256).fill(0);
    for (const w of text.toLowerCase().normalize("NFD").replace(/[^a-z0-9ñ ]/g, " ").split(/\s+/)) {
      if (w.length < 3) continue;
      let h = 0; for (let i = 0; i < w.length; i++) h = (h * 31 + w.charCodeAt(i)) >>> 0;
      v[h % 256] += 1;
    }
    return v;
  }
  async function embed(texts, kind) {
    if (JSON.parse(B.engineStatus()).embed) {
      try {
        const prefix = kind === "query" ? "search_query: " : "search_document: ";
        const r = JSON.parse(await native((id) => B.http(id, PORT_EMBED, "/v1/embeddings",
          JSON.stringify({ input: texts.map((t) => prefix + t.slice(0, 4000)) }), false)));
        return r.data.map((d) => d.embedding);
      } catch {}
    }
    return texts.map(lexical);
  }
  function cosine(a, b) {
    if (!a || !b || a.length !== b.length) return 0;
    let dot = 0, na = 0, nb = 0;
    for (let i = 0; i < a.length; i++) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
    return na && nb ? dot / Math.sqrt(na * nb) : 0;
  }

  // ------------------------------------------------------------------ memoria persistente
  const mem = {
    all() { return kv.get("memory", []); },
    save(items) { kv.set("memory", items); },
    async remember(text, kind = "hecho", source = "usuario", extra = {}) {
      const [vec] = await embed([text], "doc");
      const items = mem.all();
      let best = null, bestScore = 0;
      for (const m of items) if (m.kind === kind) { const s = cosine(vec, m.vec); if (s > bestScore) { bestScore = s; best = m; } }
      const updated_existing = !!best && bestScore >= 0.92;
      const item = updated_existing ? best : { id: uid() };
      Object.assign(item, { text, kind, source, updated: Date.now() / 1000, vec }, extra);
      if (!updated_existing) items.push(item);
      mem.save(items);
      return { ...pub(item), updated_existing };
    },
    async recall(query, limit = 6, kind = null, minScore = 0.35) {
      const [vec] = await embed([query], "query");
      const lex = vec.length === 256;
      return mem.all().filter((m) => !kind || m.kind === kind)
        .map((m) => ({ ...pub(m), score: Math.round(cosine(vec, m.vec) * 1000) / 1000 }))
        .filter((m) => m.score >= (lex ? Math.min(minScore, 0.2) : minScore))
        .sort((a, b) => b.score - a.score).slice(0, limit);
    },
    forget(id) { mem.save(mem.all().filter((m) => m.id !== id)); },
    forgetSource(source) { mem.save(mem.all().filter((m) => m.source !== source)); },
    async index(path, text) {
      mem.forgetSource("workspace:" + path);
      const chunks = [];
      for (let i = 0; i < text.length && chunks.length < 200; i += 1000) chunks.push(text.slice(i, i + 1200));
      if (!chunks.length) return 0;
      const vecs = await embed(chunks, "doc");
      const items = mem.all();
      chunks.forEach((c, i) => items.push({ id: uid(), text: c, kind: "documento", source: "workspace:" + path, path, chunk: i, updated: Date.now() / 1000, vec: vecs[i] }));
      mem.save(items);
      return chunks.length;
    },
  };
  const pub = (m) => { const { vec, ...rest } = m; return rest; };
  const TEXT_RE = /\.(txt|md|py|js|ts|json|csv|html|css|ya?ml|toml|sh|sql|xml|ini|log|java|go|rs|c|cpp|h|rb|php)$/i;

  // ------------------------------------------------------------------ herramientas
  const TOOLS = {
    list_files: { d: "Lista los archivos del workspace.", a: "{}",
      run: async () => JSON.parse(B.wsList()).map((i) => (i.dir ? "📁 " : "📄 ") + i.path).join("\n") || "(workspace vacío)" },
    read_file: { d: "Lee un archivo de texto del workspace.", a: '{"path": "..."}',
      run: async ({ path }) => B.wsRead(path).slice(0, 6000) },
    write_file: { d: "Crea o sobrescribe un archivo de texto en el workspace.", a: '{"path": "...", "content": "..."}',
      run: async ({ path, content }) => { B.wsWrite(path, content || ""); if (TEXT_RE.test(path)) await mem.index(path, content || ""); return `Guardado ${path}`; } },
    search_workspace: { d: "Búsqueda semántica en los documentos del workspace.", a: '{"query": "..."}',
      run: async ({ query }) => (await mem.recall(query, 4, "documento", 0.3)).map((h) => `[${h.path}]\n${h.text}`).join("\n\n") || "Sin resultados." },
    remember: { d: "Guarda en la memoria a largo plazo un hecho importante sobre el usuario.", a: '{"fact": "..."}',
      run: async ({ fact }) => ((await mem.remember(fact, "hecho", "agente")).updated_existing ? "Recuerdo actualizado" : "Recuerdo guardado") },
    recall: { d: "Busca recuerdos relacionados en la memoria a largo plazo.", a: '{"query": "..."}',
      run: async ({ query }) => (await mem.recall(query, 6, "hecho")).map((h) => "- " + h.text).join("\n") || "No recuerdo nada relacionado." },
    calculate: { d: "Calcula una expresión matemática (JavaScript: + - * / ** Math.sqrt…).", a: '{"expression": "..."}',
      run: async ({ expression }) => {
        if (!/^[\d\s+\-*/%().,eE*]*((Math\.\w+)[\d\s+\-*/%().,eE*]*)*$/.test(expression)) return "Expresión no permitida";
        return String(Function('"use strict"; return (' + expression + ")")());
      } },
    look_at_image: { d: "Analiza una imagen del workspace (requiere un modelo con visión).", a: '{"path": "...", "question": "..."}',
      run: async ({ path, question }) => {
        if (!hasVision()) return "El modelo activo no tiene visión. Elige el perfil «Gemma 3 4B» en Modelos.";
        return chat([{ role: "user", content: [{ type: "text", text: question || "Describe la imagen" }, imagePart(B.wsReadB64(path), path)] }], { maxTokens: 500 });
      } },
  };
  const imagePart = (b64, name = "") => ({ type: "image_url", image_url: { url: `data:image/${/\.png$/i.test(name) ? "png" : "jpeg"};base64,${b64}` } });

  const DEFAULT_AGENTS = {
    investigador: { name: "Investigador", emoji: "🔎", model: "", description: "Busca y sintetiza información de los documentos del workspace y de la memoria.",
      system: "Eres un investigador meticuloso. Busca en el workspace y en la memoria antes de responder y cita los archivos de los que sacas cada dato.",
      tools: ["list_files", "read_file", "search_workspace", "recall"] },
    analista: { name: "Analista", emoji: "🧮", model: "", description: "Hace cálculos, escribe código y guarda archivos de datos o scripts.",
      system: "Eres un analista e ingeniero. Usa calculate para las cuentas, no las hagas de cabeza. Guarda el código y los resultados en el workspace.",
      tools: ["list_files", "read_file", "write_file", "calculate"] },
    escritor: { name: "Escritor", emoji: "✍️", model: "", description: "Redacta, resume, traduce y guarda documentos.",
      system: "Eres un redactor experto. Escribes con claridad y estructura. Si te piden un documento, guárdalo en el workspace en Markdown.",
      tools: ["read_file", "write_file", "search_workspace"] },
    observador: { name: "Observador", emoji: "👁️", model: "", description: "Analiza fotos e imágenes guardadas en el workspace.",
      system: "Eres un analista visual. Usa look_at_image para mirar las imágenes y describe con precisión lo que ves.",
      tools: ["list_files", "look_at_image"] },
  };
  const loadAgents = () => kv.get("agents", null) || DEFAULT_AGENTS;

  // ------------------------------------------------------------------ agentes (supervisor + especialistas, bucle ReAct)
  const MAX_STEPS = 5;
  function toolPrompt(names, agents) {
    const lines = names.filter((n) => TOOLS[n]).map((n) => `- ${n}: ${TOOLS[n].d} Argumentos: ${TOOLS[n].a}`);
    if (agents) lines.push(`- delegate: Encarga una subtarea a un agente del equipo y devuelve su resultado. Argumentos: {"agent": "${Object.keys(agents).join("|")}", "task": "instrucciones completas"}`);
    if (!lines.length) return "";
    return "\n\nHERRAMIENTAS DISPONIBLES:\n" + lines.join("\n") +
      '\n\nPara usar una herramienta responde SOLO con una línea con este formato exacto y nada más:\n<tool>{"name": "nombre", "args": {...}}</tool>\n' +
      "Recibirás el resultado y podrás usar otra herramienta o dar la respuesta final. La respuesta final va en texto normal, sin <tool>.";
  }
  function parseTool(text) {
    const m = text.match(/<tool>\s*([\s\S]*?)\s*(<\/tool>|$)/);
    if (!m) return null;
    try { const j = JSON.parse(m[1].replace(/```json|```/g, "").trim()); return j && j.name ? { name: j.name, args: j.args || j.arguments || {} } : null; }
    catch { return { name: "__invalid__", args: {} }; }
  }

  async function react(agentId, system, userContent, toolNames, emit, { agents = null, depth = 0, history = [], live = false } = {}) {
    const messages = [{ role: "system", content: system + toolPrompt(toolNames, agents) }, ...history, { role: "user", content: userContent }];
    for (let step = 0; step <= MAX_STEPS; step++) {
      let shown = false;
      const lastStep = step === MAX_STEPS;
      if (lastStep) messages.push({ role: "user", content: "Da ya la respuesta final, sin usar más herramientas." });
      const text = await chat(messages, { onText: live ? (t) => {
        const head = t.trimStart();
        if (head.length < 6 || head.startsWith("<tool")) return;       // puede ser una llamada a herramienta: no mostrar
        if (t.includes("<tool>")) { if (shown) { emit({ type: "reset" }); shown = false; } return; }
        shown = true; emit({ type: "partial", text: t });
      } : null });
      const call = lastStep ? null : parseTool(text);
      if (!call) return text.replace(/<\/?tool>/g, "");
      if (shown) emit({ type: "reset" });
      messages.push({ role: "assistant", content: text });
      let result;
      if (call.name === "__invalid__") result = "Formato de herramienta inválido. Usa JSON válido dentro de <tool>.";
      else {
        emit({ type: "tool_call", agent: agentId, tool: call.name, args: call.args, depth });
        try {
          if (call.name === "delegate" && agents && depth === 0) result = await specialist(call.args.agent, call.args.task || "", emit, 1);
          else if (TOOLS[call.name] && toolNames.includes(call.name)) result = await TOOLS[call.name].run(call.args);
          else result = "Herramienta desconocida: " + call.name;
        } catch (e) { result = "Error en " + call.name + ": " + e.message; }
        emit({ type: "tool_result", agent: agentId, tool: call.name, result: String(result).slice(0, 3000), depth });
      }
      messages.push({ role: "user", content: `[Resultado de ${call.name}]\n${String(result).slice(0, 4000)}` });
    }
    return "";
  }

  async function specialist(id, task, emit, depth, opts = {}) {
    const a = loadAgents()[id];
    if (!a) return `No existe el agente '${id}'.`;
    emit({ type: "agent", agent: id, name: a.name, emoji: a.emoji || "🤖", task: typeof task === "string" ? task : "(con imágenes)", depth });
    return react(id, a.system + "\nResponde en el idioma de la tarea, de forma concisa.", task, a.tools || [], emit, { depth, ...opts });
  }

  async function runChat(req, emit) {
    const convs = kv.get("conversations", []);
    let cid = req.conversation_id;
    if (!cid || !convs.find((c) => c.id === cid)) {
      cid = cid || uid();
      convs.unshift({ id: cid, title: (req.message || "Imagen").slice(0, 60), created: Date.now() / 1000 });
      kv.set("conversations", convs);
    }
    emit({ type: "conversation", id: cid });
    const msgs = kv.get("conv_" + cid, []);
    // Ventana de historial que avanza a saltos de 4 mensajes (no de 1): así el principio del prompt
    // no cambia en varios turnos seguidos y llama.cpp reutiliza su caché en vez de reprocesarlo todo,
    // que en un móvil es lo que más tarda.
    let from = Math.max(0, msgs.length - 8); from -= from % 4;
    const history = msgs.slice(from).map((m) => ({ role: m.role, content: m.content.slice(0, 1200) }));

    const saved = [];
    for (const img of req.images || []) { const p = `subidas/${Date.now()}_${saved.length}.jpg`; B.wsWriteB64(p, img); saved.push(p); }
    msgs.push({ role: "user", agent: "", content: req.message, images: saved, created: Date.now() / 1000 });
    kv.set("conv_" + cid, msgs);

    emit({ type: "status", text: "Cargando el modelo en memoria…" });
    await ensureEngine();
    emit({ type: "status", text: "Pensando…" });

    let memCtx = "";
    try {
      const facts = await mem.recall(req.message, 5, "hecho");
      if (facts.length) { memCtx = "Lo que recuerdas del usuario (memoria a largo plazo):\n" + facts.map((f) => "- " + f.text).join("\n"); emit({ type: "memory", text: memCtx }); }
    } catch {}

    // La memoria va en el mensaje del usuario y no en el prompt de sistema: el prefijo no cambia y se reutiliza la caché
    let text = req.message + (memCtx ? `\n\n[${memCtx}]` : "");
    let content = text;
    if (saved.length) {
      if (hasVision()) content = [{ type: "text", text: `${text}\n[Imágenes adjuntas guardadas en: ${saved.join(", ")}]` }, ...req.images.map((b) => imagePart(b))];
      else content += `\n[El usuario adjuntó imágenes (${saved.join(", ")}), pero el modelo activo no tiene visión. Díselo.]`;
    }

    const agents = loadAgents();
    let final, used = req.mode;
    if (req.mode !== "auto" && agents[req.mode]) {
      const a = agents[req.mode];
      emit({ type: "agent", agent: req.mode, name: a.name, emoji: a.emoji || "🤖", task: req.message, depth: 0 });
      final = await react(req.mode, a.system, content, a.tools || [], emit, { history, live: true });
    } else {
      used = "supervisor";
      const team = Object.entries(agents).map(([k, a]) => `- ${k}: ${a.name}. ${a.description}`).join("\n");
      const system = "Eres el Supervisor de un estudio de IA que funciona dentro del móvil del usuario. Responde siempre en el idioma del usuario.\n" +
        "Si la petición es sencilla o conversacional, responde tú directamente. Si requiere trabajo especializado, delega con la herramienta delegate.\n" +
        "Usa remember cuando el usuario cuente algo que convenga recordar.\n\nEquipo:\n" + team;
      final = await react("supervisor", system, content, ["remember", "recall", "list_files"], emit, { agents, history, live: true });
    }
    emit({ type: "final", text: final });
    msgs.push({ role: "assistant", agent: used, content: final, images: [], created: Date.now() / 1000 });
    kv.set("conv_" + cid, msgs);

    emit({ type: "done" });  // la interfaz queda libre ya; la memoria automática sigue después

    // Solo se gasta una llamada al modelo si el mensaje parece contar algo personal o duradero
    const PERSONAL = /\b(me llamo|soy|mi|mis|me gusta|prefiero|trabajo|vivo|tengo|estoy|mi nombre|recuerda|nunca|siempre|odio|quiero que)\b/i;
    if (req.auto_memory && final && req.message.length > 12 && PERSONAL.test(req.message)) {
      try {
        const out = await chat([
          { role: "system", content: 'Extrae SOLO hechos duraderos sobre el usuario (nombre, preferencias, proyectos, decisiones) del mensaje. Devuelve JSON {"hechos": ["..."]} con frases autocontenidas en tercera persona; lista vacía si no hay nada.' },
          { role: "user", content: req.message.slice(0, 1500) }], { maxTokens: 160, temperature: 0 });
        const j = JSON.parse(out.slice(out.indexOf("{"), out.lastIndexOf("}") + 1));
        const facts = (j.hechos || []).filter((f) => typeof f === "string" && f.length > 8).slice(0, 3);
        for (const f of facts) await mem.remember(f, "hecho", "auto");
        if (facts.length) emit({ type: "memory_saved", facts });
      } catch {}
    }
  }

  // ------------------------------------------------------------------ descargas de modelos
  async function pull(name, emit) {
    const s = settings();
    let files;
    if (PROFILES[name]) { s.profile = name; delete s.active; kv.set("settings", s); files = filesOf(PROFILES[name]); }
    else if (/^https?:\/\/.+\.gguf(\?.*)?$/i.test(name)) {
      const file = decodeURIComponent(name.split("?")[0].split("/").pop());
      s.profile = "custom"; s.custom = { file, url: name }; delete s.active; kv.set("settings", s);
      files = [s.custom, EMBED];
    } else files = filesOf(activeProfile()).filter((f) => f.file === name);
    if (!files.length) return emit({ error: "Modelo desconocido. Elige un perfil o pega la URL de un archivo .gguf" });
    const have = downloaded();
    for (const f of files) {
      if (have[f.file]) continue;
      emit({ status: `descargando ${f.file}` });
      await native((id) => B.download(id, f.url, f.file), (p) => { const j = JSON.parse(p); emit({ status: `descargando ${f.file} (${(j.completed / 1e9).toFixed(2)} de ${(j.total / 1e9).toFixed(2)} GB)`, completed: j.completed, total: j.total }); });
    }
    emit({ status: "arrancando el motor…" });
    resetEngine();
    await ensureEngine();
    emit({ status: "listo" });
  }

  // ------------------------------------------------------------------ motores: estado, ajustes, medición, descargas HF
  function enginesStatus() {
    const s = settings(), d = device(), roles = { chat: activeRef() };
    const list = localModels(), prof = activeProfile().model.file;
    if (!list.some((m) => m.name === prof)) list.unshift({ name: prof, dir: false, files: [], format: "gguf", size: 0, pending: true });
    const models = list.map((m) => {
      const r = resolve(m.name), want = (s.models || {})[m.name] || {};
      const options = (FORMAT_ENGINES[m.format] || []).filter(engineOk).map((e) => ({ engine: e, name: ENGINE_NAMES[e], placements: placementsFor(e, m) }));
      return { ref: m.name, format: m.format, format_label: FORMAT_LABELS[m.format] + (m.pending ? " · sin descargar" : ""), size_gb: m.size ? Math.round(m.size / 1e8) / 10 : 0,
        options, setting: { engine: want.engine || "auto", placement: want.placement || "auto" }, bench: (s.bench || {})[m.name] || {},
        roles: Object.keys(roles).filter((k) => roles[k] === m.name),
        chosen: r.error ? { error: r.error } : { engine: r.engine, engine_name: ENGINE_NAMES[r.engine], placement: r.placement } };
    });
    const soc = [EINFO.socMaker, EINFO.soc].filter(Boolean).join(" ") || EINFO.hardware || "";
    return {
      hardware: { cpu: (EINFO.model || "Móvil") + (soc ? ` · ${soc}` : ""), ram_gb: d.ramGb || 0,
        gpus: EINFO.vulkan ? [{ name: "GPU con Vulkan" + (EINFO.qualcomm ? " (Adreno)" : ""), vram_gb: 0 }] : [],
        npus: EINFO.qualcomm ? [{ name: "Hexagon (Qualcomm)" }] : [] },
      engines: ENGINE_ORDER.map((id) => { const [ok, reason] = engineState(id); return { id, name: ENGINE_NAMES[id], available: ok, reason, installable: false }; }),
      models, roles, placements: PLACEMENTS, models_dir: "la carpeta de modelos de la app (Android/data/com.studioia.local)",
    };
  }
  function resetEngine() {
    if (B.genUnload) B.genUnload();
    ensureEngine.loaded = "";
    B.stopEngine();
  }
  function updateEngineSettings(body) {
    const s = settings();
    s.models = s.models || {};
    for (const [ref, v] of Object.entries(body.models || {})) s.models[ref] = { ...(s.models[ref] || {}), engine: v.engine || "auto", placement: v.placement || "auto" };
    const roles = body.roles || {};
    if (roles.chat || roles.code) s.active = roles.chat || roles.code;
    kv.set("settings", s);
    resetEngine();
    return enginesStatus();
  }
  async function benchmark(ref, emit, maxTokens = 64) {
    const s = settings(), r0 = resolve(ref);
    const combos = [];
    for (const e of (FORMAT_ENGINES[r0.m.format] || []).filter(engineOk)) for (const p of placementsFor(e, r0.m)) combos.push([e, p]);
    if (!combos.length) return emit({ status: "error", error: "No hay motores disponibles para este modelo" });
    const results = {};
    for (const [e, p] of combos) {
      const label = `${ENGINE_NAMES[e]} · ${PLACEMENTS[p] || p}`;
      emit({ status: "running", combo: `${e}|${p}`, label });
      override = { ...r0, engine: e, placement: p };
      try {
        resetEngine();
        await ensureEngine();
        const t0 = performance.now(); let first = 0, pieces = 0;
        const msgs = [{ role: "user", content: "Escribe un párrafo sobre la historia de la imprenta." }];
        await chat(msgs, { maxTokens, temperature: 0, onText: () => { pieces++; if (!first) first = performance.now(); } });
        const end = performance.now();
        const tps = first && end > first ? Math.round((Math.max(pieces - 1, 1) / ((end - first) / 1000)) * 10) / 10 : 0;
        const res = { tps, ttft: Math.round((first - t0) / 100) / 10 };
        results[`${e}|${p}`] = res;
        emit({ status: "result", combo: `${e}|${p}`, label, ...res });
      } catch (ex) {
        results[`${e}|${p}`] = { error: String(ex.message || ex).slice(0, 400) };
        emit({ status: "result", combo: `${e}|${p}`, label, error: String(ex.message || ex).slice(0, 400) });
      }
    }
    override = null;
    resetEngine();
    const s2 = settings(); s2.bench = s2.bench || {}; s2.bench[ref] = results; kv.set("settings", s2);
    const best = Object.entries(results).filter(([, x]) => x.tps).sort((a, b) => b[1].tps - a[1].tps)[0];
    emit({ status: "done", best: best ? best[0] : null, results });
  }
  // Hugging Face: "org/repo" o "org/repo/subcarpeta"; se baja a models/org--repo[--subcarpeta]/ (archivos de primer nivel)
  async function downloadHF(repo, include, emit) {
    const parts = String(repo || "").replace(/^hf:/, "").replace(/^https:\/\/huggingface\.co\//, "").split("/").filter(Boolean);
    if (parts.length < 2) return emit({ error: "Escribe el repositorio como organización/nombre" });
    const id = parts.slice(0, 2).join("/"), sub = parts.slice(2).join("/");
    const dir = [parts[0], parts[1], ...parts.slice(2)].join("--");
    const listFile = `.hf-${Date.now()}.json`;
    emit({ status: "consultando Hugging Face…" });
    await native((nid) => B.download(nid, `${HF}api/models/${id}/tree/main${sub ? "/" + sub : ""}`, listFile));
    let tree = [];
    try { tree = JSON.parse(B.modelRead(listFile)); } finally { B.deleteModel(listFile); }
    const re = include ? new RegExp(include.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")) : null;
    const files = tree.filter((f) => f.type === "file" && (!re || re.test(f.path.split("/").pop())));
    if (!files.length) return emit({ error: "El repositorio no tiene archivos (o la subcarpeta no existe)" });
    const total = files.reduce((a, f) => a + (f.size || 0), 0);
    let done = 0;
    for (const f of files) {
      const name = f.path.split("/").pop();
      emit({ status: `descargando ${name}`, completed: done, total });
      await native((nid) => B.download(nid, `${HF}${id}/resolve/main/${f.path.split("/").map(encodeURIComponent).join("/")}`, `${dir}/${name}`),
        (p) => { const j = JSON.parse(p); emit({ status: `descargando ${name} (${((done + j.completed) / 1e9).toFixed(2)} de ${(total / 1e9).toFixed(2)} GB)`, completed: done + j.completed, total }); });
      done += f.size || 0;
    }
    emit({ status: "listo", done: true, model: dir });
  }

  // ------------------------------------------------------------------ enrutador /api
  const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
  function streamResponse(type, run) {
    const enc = new TextEncoder();
    return new Response(new ReadableStream({
      start(ctrl) {
        const emit = (ev) => { try { ctrl.enqueue(enc.encode(type === "sse" ? `data: ${JSON.stringify(ev)}\n\n` : JSON.stringify(ev) + "\n")); } catch {} };
        run(emit).catch((e) => emit(type === "sse" ? { type: "error", text: e.message } : { error: e.message })).finally(() => { try { ctrl.close(); } catch {} });
      },
    }), { headers: { "Content-Type": type === "sse" ? "text/event-stream" : "application/x-ndjson" } });
  }
  const fileB64 = (f) => new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result.split(",")[1]); r.readAsDataURL(f); });

  async function route(method, path, q, body, raw) {
    let m;
    if (path === "/api/ping") return json({ app: "studio-ia-local", version: "1.3.0", auth: false, local: true });
    if (path === "/api/health") {
      const st = JSON.parse(B.engineStatus()), p = activeProfile(), have = downloaded(), d = device(), r = resolveActive();
      const inApp = r.engine !== "llamacpp" && !r.error;
      const missing = inApp ? [] : missingFiles().map((f) => f.file);
      const ready = inApp ? !!ensureEngine.loaded : st.chat;
      if (!missing.length && !ready && !starting && (inApp || d.engine)) ensureEngine().catch(() => {});
      return json({ version: "1.3.0", local: true, engine: (ENGINE_NAMES[r.engine] || "Motor") + (r.placement ? ` · ${PLACEMENTS[r.placement] || r.placement}` : ""),
        ollama: ready, starting: !!starting, error: r.error || st.error, device: d,
        models: [...Object.keys(have).map((f) => `${f} (${(have[f] / 1e9).toFixed(2)} GB)`),
          ...localModels().filter((m) => m.dir).map((m) => `${m.name}/ (${(m.size / 1e9).toFixed(2)} GB)`)],
        config: { chat: r.ref, "visión": inApp ? "solo con llama.cpp" : p.mmproj ? p.mmproj.file : "no incluida en este perfil", memoria: EMBED.file },
        missing, multimodal: { stt: true, tts: true, image: false },
        recommended: Object.fromEntries(Object.entries(PROFILES).map(([id, pr]) => [pr.label, [id]])) });
    }
    if (path === "/api/models/pull") return streamResponse("ndjson", (emit) => /^hf:/.test(body.model || "") ? downloadHF(body.model, "", emit) : pull(body.model, emit));
    if (path === "/api/engines" && method === "GET") return json(enginesStatus());
    if (path === "/api/engines/settings") return json(updateEngineSettings(body));
    if (path === "/api/engines/benchmark") return streamResponse("ndjson", (emit) => benchmark(body.model, emit, body.max_tokens || 64));
    if (path === "/api/models/download") return streamResponse("ndjson", (emit) => downloadHF(body.repo, body.include || "", emit));
    if (/^\/api\/engines\/[\w-]+\/install$/.test(path)) return json({ detail: "En el móvil los motores vienen dentro de la APK; no se instalan aparte" }, 501);

    if (path === "/api/conversations" && method === "GET") return json(kv.get("conversations", []));
    if (path === "/api/conversations" && method === "POST") return json({ id: uid() });
    if ((m = path.match(/^\/api\/conversations\/([\w-]+)$/))) {
      if (method === "GET") return json(kv.get("conv_" + m[1], []));
      const convs = kv.get("conversations", []);
      if (method === "DELETE") { kv.set("conversations", convs.filter((c) => c.id !== m[1])); kv.set("conv_" + m[1], []); }
      if (method === "PATCH") { const c = convs.find((x) => x.id === m[1]); if (c) c.title = body.title; kv.set("conversations", convs); }
      return json({ ok: true });
    }
    if (path === "/api/chat") return streamResponse("sse", async (emit) => {
      let acc = "";
      await runChat(body, (ev) => {   // se traduce el texto parcial acumulado a los eventos "token" de la interfaz
        if (ev.type === "partial") { if (ev.text.startsWith(acc)) emit({ type: "token", text: ev.text.slice(acc.length) }); else { emit({ type: "reset" }); emit({ type: "token", text: ev.text }); } acc = ev.text; }
        else if (ev.type === "reset") { acc = ""; emit(ev); }
        else if (ev.type === "final") { if (ev.text !== acc) { emit({ type: "reset" }); emit({ type: "token", text: ev.text }); } }
        else emit(ev);
      });
    });

    if (path === "/api/agents" && method === "GET") return json(loadAgents());
    if (path === "/api/agents" && method === "PUT") { kv.set("agents", body); return json({ ok: true }); }
    if (path === "/api/agents/reset") { kv.set("agents", DEFAULT_AGENTS); return json(DEFAULT_AGENTS); }
    if (path === "/api/tools") return json(Object.fromEntries(Object.entries(TOOLS).map(([k, t]) => [k, t.d])));

    if (path === "/api/memory" && method === "GET") return json(mem.all().filter((x) => x.kind === (q.get("kind") || "hecho")).map(pub).sort((a, b) => b.updated - a.updated));
    if (path === "/api/memory/search") return json(await mem.recall(q.get("q") || "", 10, null, 0.2));
    if (path === "/api/memory" && method === "POST") return json(await mem.remember(body.text, "hecho", "usuario"));
    if ((m = path.match(/^\/api\/memory\/([\w-]+)$/)) && method === "DELETE") { mem.forget(m[1]); return json({ ok: true }); }

    if (path === "/api/workspace" && method === "GET") return json(JSON.parse(B.wsList()));
    if (path === "/api/workspace" && method === "DELETE") { B.wsDelete(q.get("path")); mem.forgetSource("workspace:" + q.get("path")); return json({ ok: true }); }
    if (path === "/api/workspace/file" && method === "GET") return json({ path: q.get("path"), content: B.wsRead(q.get("path")) });
    if (path === "/api/workspace/file" && method === "PUT") { B.wsWrite(body.path, body.content); return json({ path: body.path, indexed: TEXT_RE.test(body.path) ? await mem.index(body.path, body.content) : 0 }); }
    if (path === "/api/workspace/upload") {
      const out = [], folder = (q.get("folder") || "").replace(/^\/|\/$/g, "");
      for (const f of raw.getAll("files")) {
        const p = (folder ? folder + "/" : "") + f.name;
        B.wsWriteB64(p, await fileB64(f));
        out.push({ path: p, indexed: TEXT_RE.test(p) ? await mem.index(p, await f.text()) : 0 });
      }
      return json(out);
    }
    if (path === "/api/workspace/reindex") {
      let chunks = 0;
      for (const it of JSON.parse(B.wsList())) if (!it.dir && TEXT_RE.test(it.path)) chunks += await mem.index(it.path, B.wsRead(it.path));
      return json({ chunks });
    }
    return json({ detail: "No disponible en modo autónomo" }, 501);
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (!url.pathname.startsWith("/api/") || url.pathname === "/api/workspace/raw") return realFetch(input, init);
    const method = (init.method || "GET").toUpperCase();
    let body = null;
    if (typeof init.body === "string") { try { body = JSON.parse(init.body); } catch {} }
    try { return await route(method, url.pathname, url.searchParams, body || {}, init.body); }
    catch (e) { return json({ detail: e.message }, 500); }
  };

  // ------------------------------------------------------------------ ajustes de la interfaz tras cargar app.js
  document.addEventListener("DOMContentLoaded", () => {
    const input = document.querySelector("#input"), mic = document.querySelector("#micBtn");
    let listening = false, base = "";
    pending.speech = (type, data) => {
      if (type === "partial") { input.value = (base + " " + data).trim(); return; }
      listening = false; mic.classList.remove("rec");
      if (type === "done") input.value = (base + " " + data).trim();
      else if (data) alert(data);
    };
    window.androidMic = () => {
      if (listening) { B.stopListening(); return; }
      base = input.value; listening = true; mic.classList.add("rec"); B.listen();
    };
    window.speak = (text) => B.speak(String(text).replace(/[#*`>|_]/g, "").slice(0, 3900));
    document.querySelector("#pullName")?.setAttribute("placeholder", "URL de un modelo .gguf (opcional)");
    const artist = [...document.querySelectorAll(".chip")].find((c) => /ilustración/.test(c.textContent));
    if (artist) artist.textContent = "Hazme un plan de estudio y guárdalo en plan.md";
  });
})();
