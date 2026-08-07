#!/usr/bin/env node
/* Pre-render the repo's README + docs/*.md into static pages under docs/.
   Run from website/: node build-docs.cjs */
const fs = require("fs");
const path = require("path");
const markedMod = require("./vendor/marked.min.js");
const parse = (markedMod.marked || markedMod).parse.bind(markedMod.marked || markedMod);

const SITE = "https://www.llmsyscore.com";
const REPO = path.join(__dirname, "..");
const DOCS = [
  { key: "index",        md: path.join(REPO, "README.md"),              title: "Documentation & quick start",
    desc: "LLM Systems Manager overview: top features, installation options, quickstart, inference gateway, architecture, and configuration." },
  { key: "architecture", md: path.join(REPO, "docs", "ARCHITECTURE.md"),  title: "Architecture",
    desc: "How LLM Systems Manager is put together: the manager control plane, per-host agents, and the alarm engine." },
  { key: "components",   md: path.join(REPO, "docs", "COMPONENTS.md"),    title: "Components",
    desc: "The components of LLM Systems Manager and how they interact." },
  { key: "api",          md: path.join(REPO, "docs", "API_REFERENCE.md"), title: "API reference",
    desc: "Every HTTP endpoint exposed by LLM Systems Manager and the Alarm Engine, for operators and integration authors." },
  { key: "deployment",   md: path.join(REPO, "docs", "DEPLOYMENT.md"),    title: "Deployment",
    desc: "Deployment guide for LLM Systems Manager: prerequisites, installation, TLS, and multi-host setups." },
];

/* The site has its own gallery and tour video, so the README's screenshots
   section and its inline references are dropped from the docs page. */
function stripScreenshots(md) {
  const start = md.indexOf("## Screenshots");
  if (start !== -1) {
    const end = md.indexOf("\n## ", start + 1);
    md = md.slice(0, start) + (end !== -1 ? md.slice(end + 1) : "");
  }
  md = md.replace(/ — \[screenshot below\]\(#screenshots\)/gi, "");
  md = md.replace(/ ?\[screenshot below\]\(#screenshots\)\.?/gi, "");
  return md;
}

const LINKMAP = {
  "readme": "index.html", "architecture": "architecture.html", "components": "components.html",
  "api_reference": "api.html", "deployment": "deployment.html",
};

// Strips tags by repeating until stable, then allowlists the surviving
// characters, so no markup can reach the generated id attribute.
function slug(text) {
  let out = text, prev;
  do { prev = out; out = out.replace(/<[^<>]*>/g, " "); } while (out !== prev);
  return out.toLowerCase()
    .replace(/[^\w\s—–-]/g, "").trim()
    .replace(/[\s—–]+/g, "-").replace(/-+/g, "-");
}

function fixup(html) {
  html = html.replace(/src="(?:\.\.\/)?docs\/screenshots\/([^"]+)"/g, 'src="../assets/screenshots/$1"');
  html = html.replace(/href="([^"]*?)(README|ARCHITECTURE|COMPONENTS|API_REFERENCE|DEPLOYMENT)\.md(#[^"]*)?"/gi,
    (m, pre, name, hash) => `href="${LINKMAP[name.toLowerCase()]}${hash || ""}"`);
  html = html.replace(/<h([1-4])>([\s\S]*?)<\/h\1>/g,
    (m, lvl, inner) => `<h${lvl} id="${slug(inner)}">${inner}</h${lvl}>`);
  html = html.replace(/<table>/g, '<div class="tbl-wrap"><table>')
             .replace(/<\/table>/g, "</table></div>");
  html = html.replace(/<a\s+([^>]*href="https?:\/\/[^"]*"[^>]*)>/gi, (m, attrs) => {
    let out = attrs;
    if (!/\btarget\s*=/i.test(out)) out += ' target="_blank"';
    if (!/\brel\s*=/i.test(out)) out += ' rel="noopener"';
    return `<a ${out}>`;
  });
  return html;
}

function page(doc, body) {
  const nav = DOCS.map(d =>
    `      <a href="${d.key}.html"${d.key === doc.key ? ' class="active" aria-current="page"' : ""}>${d.title}</a>`
  ).join("\n");
  const url = `${SITE}/docs/${doc.key === "index" ? "" : doc.key + ".html"}`;
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${doc.title} — LLM SysCore</title>
<meta name="description" content="${doc.desc}">
<link rel="canonical" href="${url}">
<meta property="og:type" content="article">
<meta property="og:site_name" content="LLM SysCore">
<meta property="og:title" content="${doc.title} — LLM Systems Manager">
<meta property="og:description" content="${doc.desc}">
<meta property="og:url" content="${url}">
<meta property="og:image" content="${SITE}/assets/og-card.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" type="image/png" href="../assets/favicon-32.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="../css/site.css">
</head>
<body>

<header class="nav">
  <div class="wrap nav-inner">
    <a class="nav-brand" href="../index.html" aria-label="LLM SysCore home">
      <img src="../assets/logo.svg" alt="">
      <span>LLM&nbsp;Sys<b class="core">Core</b></span>
    </a>
    <nav class="nav-links" aria-label="Main">
      <a href="../index.html#features">Features</a>
      <a href="../index.html#screenshots" class="nav-hide-sm">Screenshots</a>
      <a href="./" aria-current="page">Docs</a>
      <a href="../about.html" class="nav-hide-sm">About</a>
      <a href="../contact.html">Contact</a>
      <a class="btn btn-ghost" href="https://github.com/llmsyscore/llm-systems-manager">
        <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>
        GitHub
      </a>
    </nav>
  </div>
</header>

<main class="wrap docs-layout">
  <aside class="docs-side">
    <h4>Documentation</h4>
    <nav aria-label="Documentation sections">
${nav}
    </nav>
  </aside>

  <article class="md">
${body}
  </article>
</main>

<footer>
  <div class="wrap foot-base" style="border-top:none; padding-top:0">
    <span>© 2026 LLM SysCore · AGPL-3.0</span>
    <span><a href="mailto:support@llmsyscore.com">support@llmsyscore.com</a></span>
  </div>
</footer>

</body>
</html>
`;
}

fs.mkdirSync(path.join(__dirname, "docs"), { recursive: true });
for (const doc of DOCS) {
  let md = fs.readFileSync(doc.md, "utf8");
  if (doc.key === "index") md = stripScreenshots(md);
  const body = fixup(parse(md));
  fs.writeFileSync(path.join(__dirname, "docs", doc.key + ".html"), page(doc, body));
  console.log("built docs/" + doc.key + ".html");
}
