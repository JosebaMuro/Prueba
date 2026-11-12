// Prueba del modo autónomo en Chromium con un AndroidBridge y un motor simulados.
// Uso: node android/tests/local_test.js   (requiere playwright)
const { chromium, devices } = require('playwright');
const http = require('http'), fs = require('fs'), path = require('path');
const root = path.join(__dirname, '..', '..');
const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
const server = http.createServer((req, res) => {
  let p = req.url.split('?')[0]; if (p === '/') p = '/index.html';
  const f = ['/local.js', '/jinja.js'].includes(p) ? path.join(root, 'android/assets/www', p) : path.join(root, 'frontend', p);
  if (!fs.existsSync(f)) { res.writeHead(404); return res.end(); }
  let body = fs.readFileSync(f);
  if (p === '/index.html') body = String(body).replace('<script src="app.js">', '<script src="local.js"></script><script src="app.js">');
  res.writeHead(200, { 'Content-Type': mime[path.extname(f)] || 'text/plain' }); res.end(body);
});
const mock = () => {
  const kv = {}, ws = {}, models = {}; let chat = false, calls = 0;
  const later = (id, type, data, ms = 5) => setTimeout(() => window.__native(id, type, data), ms);
  const sse = (id, text) => { let i = 0; for (const piece of text.match(/[\s\S]{1,7}/g)) later(id, 'chunk', JSON.stringify({ choices: [{ delta: { content: piece } }] }), 5 * ++i); later(id, 'done', '', 5 * (i + 2)); };
  window.AndroidBridge = {
    isLocal: () => true, kvGet: (n) => kv[n] || '', kvSet: (n, v) => { kv[n] = v; },
    wsList: () => JSON.stringify(Object.keys(ws).sort().map((p) => ({ path: p, dir: false, size: ws[p].length }))),
    wsRead: (p) => ws[p], wsReadB64: (p) => btoa(ws[p]), wsWrite: (p, t) => { ws[p] = t; }, wsWriteB64: (p, b) => { ws[p] = atob(b); }, wsDelete: (p) => { delete ws[p]; },
    modelFiles: () => JSON.stringify(models), deleteModel() {},
    deviceInfo: () => JSON.stringify({ ramGb: 5.6, cores: 8, freeGb: 40, engine: true, abi: 'arm64-v8a' }),
    download: (id, url, file) => { later(id, 'progress', '{"completed":500000000,"total":1000000000}'); setTimeout(() => { models[file] = 1e9; window.__native(id, 'done', ''); }, 30); },
    engineStatus: () => JSON.stringify({ chat, embed: false, model: chat ? 'Qwen3-1.7B-Q4_K_M.gguf|' : '', error: '' }),
    startEngine: (id) => { chat = true; later(id, 'done', '', 20); }, stopEngine: () => { chat = false; },
    http: (id, port, p, body) => {
      const msgs = JSON.parse(body).messages, sys = msgs[0].content, last = msgs[msgs.length - 1].content; calls++;
      window.__calls = calls;
      if (sys.startsWith('Extrae SOLO')) return sse(id, '{"hechos": ["El usuario se llama Joseba"]}');
      if (sys.includes('Supervisor')) return sse(id, String(last).startsWith('[Resultado') ? 'El equipo ha terminado: ' + last.split('\n')[1] : 'Voy a delegar. <tool>{"name": "delegate", "args": {"agent": "analista", "task": "Calcula 6*7 y guárdalo en resultado.txt"}}</tool>');
      const n = msgs.filter((m) => String(m.content).startsWith('[Resultado')).length;
      if (n === 0) return sse(id, '<tool>{"name": "calculate", "args": {"expression": "6*7"}}</tool>');
      if (n === 1) return sse(id, '<tool>{"name": "write_file", "args": {"path": "resultado.txt", "content": "6*7 = 42"}}</tool>');
      return sse(id, 'El resultado es 42 y está en resultado.txt');
    },
    speak() {}, listen() {}, stopListening() {},
    // motores dentro de la app (Engines.java)
    engineInfo: () => JSON.stringify({ engines: { llamacpp: true, onnx: true, executorch: true, litert: true, genie: false }, qualcomm: true, vulkan: true, soc: 'SM8650', socMaker: 'QTI', model: 'Xiaomi 14' }),
    modelList: () => JSON.stringify([...Object.keys(models).filter((f) => !f.includes('/')).map((f) => ({ name: f, dir: false, size: models[f], files: [] })),
      { name: 'qwen-onnx', dir: true, size: 9e8, files: ['genai_config.json', 'model.onnx', 'tokenizer.json', 'tokenizer_config.json'] },
      { name: 'gemma.litertlm', dir: false, size: 5e8, files: [] }]),
    modelRead: (rel) => rel === 'qwen-onnx/tokenizer_config.json' ? JSON.stringify({ eos_token: '<|im_end|>', chat_template: "{% for m in messages %}<|im_start|>{{ m.role }}\n{{ m.content }}<|im_end|>\n{% endfor %}{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}" }) : '',
    genLoad: (id, j) => { window.__loads = (window.__loads || []).concat(JSON.parse(j)); later(id, 'done', '', 10); },
    genRun: (id, j) => {
      const req = JSON.parse(j); window.__runs = (window.__runs || []).concat(req);
      const text = req.prompt !== undefined ? 'Hola desde ONNX<|im_end|>basura' : 'Hola desde LiteRT';
      let i = 0; for (const piece of text.match(/[\s\S]{1,4}/g)) later(id, 'chunk', piece, 3 * ++i); later(id, 'done', '', 3 * (i + 2));
    },
    genStop() {}, genUnload() {},
  };
};
(async () => {
  await new Promise((r) => server.listen(8791, r));
  const b = await chromium.launch(); const ctx = await b.newContext({ ...devices['Pixel 7'] }); const p = await ctx.newPage();
  const errors = []; p.on('pageerror', (e) => errors.push(e.message)); p.on('dialog', (d) => { errors.push('dialog: ' + d.message()); d.dismiss(); });
  await p.addInitScript(mock);
  await p.goto('http://localhost:8791/'); await p.waitForTimeout(800);
  const assert = (c, m) => { if (!c) { console.log('FALLO:', m); process.exitCode = 1; } else console.log('ok:', m); };
  assert((await p.textContent('#statusText')).includes('Faltan'), 'primer arranque: faltan modelos');
  assert(await p.isVisible('#tab-models'), 'se abre la pestaña Modelos');
  if (process.argv[2]) await p.screenshot({ path: process.argv[2] + '/local-models.png' });
  await p.click('#health button.primary'); await p.waitForTimeout(700);
  assert((await p.textContent('#statusText')).includes('Listo'), 'tras descargar: motor en marcha');
  await p.click('#panelClose');
  await p.fill('#input', 'Calcula 6*7, me llamo Joseba'); await p.click('#sendBtn'); await p.waitForTimeout(2500);
  const answer = await p.textContent('.msg.assistant .bubble');
  assert(answer.includes('42') && !answer.includes('<tool'), 'respuesta final sin restos de herramientas: ' + answer.trim());
  assert((await p.textContent('.msg.assistant')).includes('Recordado'), 'memoria automática');
  const files = await p.evaluate(() => fetch('/api/workspace').then((r) => r.json()));
  assert(files.some((f) => f.path === 'resultado.txt'), 'el agente escribió en el workspace');
  const mem = await p.evaluate(() => fetch('/api/memory/search?q=como se llama Joseba').then((r) => r.json()));
  assert(mem.length > 0, 'la memoria se recupera por similitud');
  const convs = await p.evaluate(() => fetch('/api/conversations').then((r) => r.json()));
  assert(convs.length === 1, 'conversación guardada');
  if (process.argv[2]) await p.screenshot({ path: process.argv[2] + '/local-chat.png' });

  // Otros motores: ONNX Runtime GenAI y LiteRT-LM (dentro de la app)
  const eng = await p.evaluate(() => fetch('/api/engines').then((r) => r.json()));
  const onnxRow = eng.models.find((m) => m.ref === 'qwen-onnx'), lrt = eng.models.find((m) => m.ref === 'gemma.litertlm');
  assert(eng.engines.find((e) => e.id === 'onnx').available && !eng.engines.find((e) => e.id === 'genie').available, 'motores de la APK detectados (Genie pide la APK Snapdragon)');
  assert(onnxRow && onnxRow.chosen.engine === 'onnx' && onnxRow.chosen.placement === 'cpu', 'carpeta ONNX → ONNX Runtime GenAI en CPU');
  assert(lrt && lrt.chosen.engine === 'litert' && lrt.chosen.placement === 'gpu' && lrt.options[0].placements.join() === 'gpu,cpu', '.litertlm → LiteRT-LM, GPU por defecto y CPU a elegir');
  await p.evaluate(() => fetch('/api/engines/settings', { method: 'PUT', body: JSON.stringify({ roles: { chat: 'qwen-onnx' } }) }));
  const h2 = await p.evaluate(() => fetch('/api/health').then((r) => r.json()));
  assert(h2.config.chat === 'qwen-onnx' && h2.engine.startsWith('ONNX Runtime GenAI') && h2.missing.length === 0, 'health con el modelo ONNX activo: ' + h2.engine);
  await p.evaluate(() => { window.__runs = []; return fetch('/api/chat', { method: 'POST', body: JSON.stringify({ message: 'Hola', mode: 'auto', images: [] }) }).then((r) => r.text()); });
  const runs = await p.evaluate(() => window.__runs), loads = await p.evaluate(() => window.__loads || []);
  assert(loads.some((l) => l.engine === 'onnx' && l.model === 'qwen-onnx' && l.tokenizer === 'qwen-onnx/tokenizer.json'), 'genLoad con la carpeta y el tokenizer');
  assert(runs[0] && runs[0].prompt.includes('<|im_start|>user\nHola<|im_end|>') && runs[0].prompt.endsWith('<|im_start|>assistant\n'), 'prompt con la plantilla Jinja del modelo');
  const last = (await p.evaluate(() => fetch('/api/conversations').then((r) => r.json()).then((c) => fetch('/api/conversations/' + c[0].id)).then((r) => r.json())));
  const ans = last[last.length - 1].content;
  assert(ans === 'Hola desde ONNX', 'respuesta cortada en el token de fin: ' + ans);
  await p.evaluate(() => fetch('/api/engines/settings', { method: 'PUT', body: JSON.stringify({ models: { 'gemma.litertlm': { engine: 'litert', placement: 'cpu' } }, roles: { chat: 'gemma.litertlm' } }) }));
  const bench = await p.evaluate(() => fetch('/api/engines/benchmark', { method: 'POST', body: JSON.stringify({ model: 'gemma.litertlm' }) }).then((r) => r.text()));
  const evs = bench.trim().split('\n').map((l) => JSON.parse(l));
  assert(evs.filter((e) => e.status === 'result' && e.tps > 0).length === 2 && evs.pop().status === 'done', 'medición de velocidad en GPU y CPU');
  await p.evaluate(() => { window.__runs = []; window.__loads = []; return fetch('/api/chat', { method: 'POST', body: JSON.stringify({ message: 'Hola', mode: 'auto', images: [] }) }).then((r) => r.text()); });
  const r2 = await p.evaluate(() => window.__runs), l2 = await p.evaluate(() => window.__loads);
  assert(l2.some((l) => l.engine === 'litert' && l.placement === 'cpu'), 'la ubicación elegida a mano (CPU) manda sobre la medición');
  assert(r2[0] && r2[0].message === 'Hola' && r2[0].system.includes('Supervisor') && r2[0].prompt === undefined, 'LiteRT-LM recibe sistema, historial y mensaje');
  const inst = await p.evaluate(() => fetch('/api/engines/onnx/install', { method: 'POST' }).then((r) => r.status));
  assert(inst === 501, 'instalar motores no aplica en el móvil');
  await p.click('[data-toggle=panel]');
  await p.evaluate(() => document.querySelector('.tabs button[data-tab=models]').click()); await p.waitForTimeout(500);
  assert((await p.textContent('#engines')).includes('LiteRT-LM'), 'la pestaña Modelos muestra los motores');
  if (process.argv[2]) await p.screenshot({ path: process.argv[2] + '/local-engines.png', fullPage: true });
  assert(errors.length === 0, 'sin errores de página ' + errors.join(' | '));
  await b.close(); server.close();
})();
