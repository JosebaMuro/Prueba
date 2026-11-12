// Renderizador Markdown mínimo y seguro (sin dependencias, para funcionar 100% offline).
(function () {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const safeUrl = (u) => (/^(https?:|\/|data:image\/|\.\/|#)/i.test(u) ? u : "#");

  function inline(s) {
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    s = esc(s);
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_, a, u) => `<img alt="${a}" src="${safeUrl(u)}" loading="lazy">`);
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, t, u) => `<a href="${safeUrl(u)}" target="_blank" rel="noopener">${t}</a>`);
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
  }

  function render(md) {
    const lines = (md || "").replace(/\r/g, "").split("\n");
    let html = "", i = 0;
    while (i < lines.length) {
      const l = lines[i];
      const fence = l.match(/^```(\w*)/);
      if (fence) {
        const buf = []; i++;
        while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
        i++;
        html += `<pre><code class="lang-${fence[1]}">${esc(buf.join("\n"))}</code></pre>`;
        continue;
      }
      const h = l.match(/^(#{1,6})\s+(.*)/);
      if (h) { html += `<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`; i++; continue; }
      if (/^\s*(---|\*\*\*)\s*$/.test(l)) { html += "<hr>"; i++; continue; }
      if (/^\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\|?[\s:-]+\|/.test(lines[i + 1])) {
        const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
        html += "<table><tr>" + cells(l).map((c) => `<th>${c}</th>`).join("") + "</tr>";
        i += 2;
        while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) html += "<tr>" + cells(lines[i++]).map((c) => `<td>${c}</td>`).join("") + "</tr>";
        html += "</table>"; continue;
      }
      if (/^\s*([-*+]|\d+\.)\s+/.test(l)) {
        const ordered = /^\s*\d+\./.test(l);
        html += ordered ? "<ol>" : "<ul>";
        while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
          html += `<li>${inline(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, ""))}</li>`; i++;
        }
        html += ordered ? "</ol>" : "</ul>"; continue;
      }
      if (/^>\s?/.test(l)) {
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
        html += `<blockquote>${render(buf.join("\n"))}</blockquote>`; continue;
      }
      if (!l.trim()) { i++; continue; }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^(```|#{1,6}\s|\s*([-*+]|\d+\.)\s+|>|\|)/.test(lines[i])) buf.push(lines[i++]);
      if (!buf.length) buf.push(lines[i++]);
      html += `<p>${buf.map(inline).join("<br>")}</p>`;
    }
    return html;
  }
  window.renderMarkdown = render;
})();
