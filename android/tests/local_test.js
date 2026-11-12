// Prueba del modo autónomo en Chromium con un AndroidBridge y un motor simulados.
// Uso: node android/tests/local_test.js   (requiere playwright)
const { chromium, devices } = require('playwright');
const http = require('http'), fs = require('fs'), path = require('path');
const root = path.join(__dirname, '..', '..');
const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
const server = http.createServer((req, res) => {
  let p = req.url.split('?')[0]; if (p === '/') p = '/index.html';
  const f = p === '/local.js' ? path.join(root, 'android/assets/www/local.js') : path.join(root, 'frontend', p);
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
  assert(errors.length === 0, 'sin errores de página ' + errors.join(' | '));
  await b.close(); server.close();
})();
