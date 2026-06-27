// ---------------------------------------------------------------------------
// Embedded terminal (xterm.js + SSE output + POST input)
// ---------------------------------------------------------------------------
let _term     = null;
let _termFit  = null;
let _termSid  = null;   // current session ID
let _termEvt  = null;   // EventSource for output
let _termOpen = false;

function _termMkXterm(mountEl) {
  if (_term) { _term.dispose(); _term = null; }
  _term = new Terminal({
    theme: { background: '#0d0d0d', foreground: '#cccccc', cursor: '#7af', selectionBackground: '#3a5a7a' },
    fontFamily: '"Cascadia Code", "Fira Code", monospace',
    fontSize: 13, lineHeight: 1.3, cursorBlink: true, scrollback: 5000,
  });
  _termFit = new FitAddon.FitAddon();
  _term.loadAddon(_termFit);
  _term.open(mountEl);
  _termFit.fit();
}

function _termCloseSession() {
  // Close the SSE + tell the agent to kill the PTY, then drop the sid.
  if (_termEvt) { _termEvt.close(); _termEvt = null; }
  if (_termSid) {
    fetch(`/api/terminal/close/${_termSid}`, {method:'POST'}).catch(()=>{});
    _termSid = null;
  }
}

async function _termStart(mountEl) {
  _termCloseSession();   // kill any existing session before opening a new one
  _termMkXterm(mountEl);
  _term.write('\r\n\x1b[90mConnecting…\x1b[0m\r\n');
  try {
    const r = await _jsonOrThrow(await fetch('/api/terminal/create', {method:'POST'}));
    if (!r.ok) throw new Error(r.error || 'create failed');
    _termSid = r.sid;
  } catch(e) {
    _term.write(`\r\n\x1b[31m● Failed: ${e.message}\x1b[0m\r\n`);
    return;
  }
  // SSE output stream
  _termEvt = new EventSource(`/api/terminal/output/${_termSid}`);
  _termEvt.onmessage = e => { if (_term) _term.write(JSON.parse(e.data)); };
  _termEvt.onerror   = () => { if (_term) _term.write('\r\n\x1b[31m● Stream error\x1b[0m\r\n'); };
  // Keyboard → POST input
  _term.onData(data => {
    if (!_termSid) return;
    fetch(`/api/terminal/input/${_termSid}`, {
      method: 'POST', body: data,
      headers: {'Content-Type': 'application/octet-stream'},
    }).catch(()=>{});
  });
  // Resize → POST resize
  _term.onResize(({rows, cols}) => {
    if (!_termSid) return;
    fetch(`/api/terminal/resize/${_termSid}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rows, cols}),
    }).catch(()=>{});
  });
  _term.write('\x1b[32m● Connected\x1b[0m\r\n');
}

function toggleTerminal() {
  const panel = document.getElementById('terminalPanel');
  _termOpen = !_termOpen;
  panel.style.display = _termOpen ? '' : 'none';
  if (_termOpen) {
    const mount = document.getElementById('terminalMount');
    if (!_termSid) {
      _termStart(mount);
    } else {
      setTimeout(() => _termFit && _termFit.fit(), 50);
    }
  } else {
    // Don't kill the session on hide — user may toggle back
  }
}

function reconnectTerminal() {
  _termStart(document.getElementById('terminalMount'));
}

function closeTerminal() {
  // Full close: kill the session AND hide the panel, so the terminal only
  // reopens (on the selected agent) when the user clicks the button again.
  _termCloseSession();
  _termOpen = false;
  const panel = document.getElementById('terminalPanel');
  if (panel) panel.style.display = 'none';
}

function popOutTerminal() {
  // Pop-out builds absolute URLs the fetch wrapper skips, so inject the picker
  // selection into create directly (sid-routed IO follows the owning agent).
  const agentParam = (typeof _selectedAgent === 'function' && _selectedAgent('llama'))
    ? ('?agent=' + encodeURIComponent(_selectedAgent('llama'))) : '';
  const w = window.open('', 'llmterm', 'width=900,height=540,resizable=yes,scrollbars=no,toolbar=no,menubar=no');
  w.document.write(`<!DOCTYPE html><html><head>
    <title>Terminal — Server</title>
    <link rel="stylesheet" href="${location.origin}/static/vendor/xterm.min.css?v=5.3.0"/>
    <script src="${location.origin}/static/vendor/xterm.min.js?v=5.3.0"><\/script>
    <script src="${location.origin}/static/vendor/xterm-addon-fit.min.js?v=0.8.0"><\/script>
    <style>html,body{margin:0;background:var(--bg-tabnav);height:100%;} #t{height:100%;}</style>
  </head><body><div id="t"></div><script>
  (async function(){
    const base = '${location.origin}';
    const term = new Terminal({theme:{background:'#0d0d0d',foreground:'#ccc',cursor:'#7af'},fontFamily:'"Cascadia Code","Fira Code",monospace',fontSize:13,cursorBlink:true,scrollback:5000});
    const fit  = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(document.getElementById('t'));
    fit.fit();
    term.write('\\r\\n\\x1b[90mConnecting\\u2026\\x1b[0m\\r\\n');
    try {
      const r = await fetch(base+'/api/terminal/create${agentParam}',{method:'POST'}).then(r=>r.json());
      if(!r.ok) throw new Error(r.error||'create failed');
      const sid = r.sid;
      const evt = new EventSource(base+'/api/terminal/output/'+sid);
      evt.onmessage = e => term.write(JSON.parse(e.data));
      evt.onerror   = () => term.write('\\r\\n\\x1b[31m● Stream error\\x1b[0m\\r\\n');
      term.onData(d => fetch(base+'/api/terminal/input/'+sid,{method:'POST',body:d,headers:{'Content-Type':'application/octet-stream'}}).catch(()=>{}));
      term.onResize(({rows,cols})=>fetch(base+'/api/terminal/resize/'+sid,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows,cols})}).catch(()=>{}));
      term.write('\\x1b[32m● Connected\\x1b[0m\\r\\n');
      window.addEventListener('resize',()=>fit.fit());
      window.addEventListener('beforeunload',()=>fetch(base+'/api/terminal/close/'+sid,{method:'POST'}).catch(()=>{}));
    } catch(e) { term.write('\\r\\n\\x1b[31m● '+e.message+'\\x1b[0m\\r\\n'); }
  })();
  <\/script></body></html>`);
  w.document.close();
}

// ── LMS SSH Terminal Agent ──────────────────────────────
let _lmsTerm = null, _lmsTermSid = null, _lmsTermEvt = null, _lmsTermFit = null;

function _lmsTermInit(mountEl) {
  if (_lmsTerm) { _lmsTerm.dispose(); _lmsTerm = null; }
  _lmsTerm = new Terminal({
    theme: {background:'#0d0d0d', foreground:'#ccc', cursor:'#7af'},
    fontFamily: '"Cascadia Code","Fira Code",monospace',
    fontSize: 13, cursorBlink: true, scrollback: 5000,
  });
  _lmsTermFit = new FitAddon.FitAddon();
  _lmsTerm.loadAddon(_lmsTermFit);
  _lmsTerm.open(mountEl);
  _lmsTermFit.fit();
}

function _lmsTermCloseSession() {
  if (_lmsTermEvt) { _lmsTermEvt.close(); _lmsTermEvt = null; }
  if (_lmsTermSid) {
    fetch(`/api/terminal/close/${_lmsTermSid}`, {method:'POST'}).catch(()=>{});
    _lmsTermSid = null;
  }
}

async function _lmsTermStart(mountEl) {
  _lmsTermCloseSession();   // kill any existing session before opening a new one
  _lmsTermInit(mountEl);
  _lmsTerm.write('\r\n\x1b[90mConnecting to the LM Studio host…\x1b[0m\r\n');
  try {
    const r = await _jsonOrThrow(await fetch('/api/lms/terminal/create', {method:'POST'}));
    _lmsTermSid = r.sid;
    _lmsTermEvt = new EventSource(`/api/terminal/output/${_lmsTermSid}`);
    _lmsTermEvt.onmessage = e => { if (_lmsTerm) _lmsTerm.write(JSON.parse(e.data)); };
    _lmsTermEvt.onerror   = () => { if (_lmsTerm) _lmsTerm.write('\r\n\x1b[31m● Stream error\x1b[0m\r\n'); };
    _lmsTerm.onData(data => {
      fetch(`/api/terminal/input/${_lmsTermSid}`, {
        method:'POST', body:data, headers:{'Content-Type':'application/octet-stream'},
      }).catch(()=>{});
    });
    _lmsTerm.onResize(({rows, cols}) => {
      fetch(`/api/terminal/resize/${_lmsTermSid}`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({rows, cols}),
      }).catch(()=>{});
    });
  } catch(e) {
    if (_lmsTerm) _lmsTerm.write(`\r\n\x1b[31mFailed: ${e.message}\x1b[0m\r\n`);
  }
}

function toggleLmsTerminal() {
  const panel = document.getElementById('lmsTerminalPanel');
  const open  = panel.style.display === 'none';
  panel.style.display = open ? '' : 'none';
  if (open) {
    const mount = document.getElementById('lmsTerminalMount');
    if (!_lmsTerm) _lmsTermStart(mount);
    else { requestAnimationFrame(() => { if (_lmsTermFit) _lmsTermFit.fit(); }); }
  }
}

function reconnectLmsTerminal() {
  _lmsTermStart(document.getElementById('lmsTerminalMount'));
}

function closeLmsTerminal() {
  _lmsTermCloseSession();
  // Dispose the xterm so toggleLmsTerminal's `if (!_lmsTerm)` re-creates a
  // fresh session on the next click (it only starts when _lmsTerm is null).
  if (_lmsTerm) { try { _lmsTerm.dispose(); } catch(_) {} _lmsTerm = null; }
  const panel = document.getElementById('lmsTerminalPanel');
  if (panel) panel.style.display = 'none';
}

function popOutLmsTerminal() {
  const agentParam = (typeof _selectedAgent === 'function' && _selectedAgent('lms'))
    ? ('?agent=' + encodeURIComponent(_selectedAgent('lms'))) : '';
  const w = window.open('', 'lmsterm', 'width=900,height=540,resizable=yes,scrollbars=no,toolbar=no,menubar=no');
  w.document.write(`<!DOCTYPE html><html><head>
    <title>Terminal — Agent</title>
    <link rel="stylesheet" href="${location.origin}/static/vendor/xterm.min.css?v=5.3.0"/>
    <script src="${location.origin}/static/vendor/xterm.min.js?v=5.3.0"><\/script>
    <script src="${location.origin}/static/vendor/xterm-addon-fit.min.js?v=0.8.0"><\/script>
    <style>html,body{margin:0;background:var(--bg-tabnav);height:100%;} #t{height:100%;}</style>
  </head><body><div id="t"></div><script>
  (async function(){
    const base = '${location.origin}';
    const term = new Terminal({theme:{background:'#0d0d0d',foreground:'#ccc',cursor:'#7af'},fontFamily:'"Cascadia Code","Fira Code",monospace',fontSize:13,cursorBlink:true,scrollback:5000});
    const fit  = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(document.getElementById('t'));
    fit.fit();
    term.write('\\r\\n\\x1b[90mConnecting to Agent\\u2026\\x1b[0m\\r\\n');
    try {
      const r = await fetch(base+'/api/lms/terminal/create${agentParam}',{method:'POST'}).then(r=>r.json());
      if(!r.ok) throw new Error(r.error||'create failed');
      const sid = r.sid;
      const evt = new EventSource(base+'/api/terminal/output/'+sid);
      evt.onmessage = e => term.write(JSON.parse(e.data));
      evt.onerror   = () => term.write('\\r\\n\\x1b[31m● Stream error\\x1b[0m\\r\\n');
      term.onData(d => fetch(base+'/api/terminal/input/'+sid,{method:'POST',body:d,headers:{'Content-Type':'application/octet-stream'}}).catch(()=>{}));
      term.onResize(({rows,cols})=>fetch(base+'/api/terminal/resize/'+sid,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows,cols})}).catch(()=>{}));
      window.addEventListener('resize',()=>fit.fit());
      window.addEventListener('beforeunload',()=>fetch(base+'/api/terminal/close/'+sid,{method:'POST'}).catch(()=>{}));
    } catch(e) { term.write('\\r\\n\\x1b[31m● '+e.message+'\\x1b[0m\\r\\n'); }
  })();
  <\/script></body></html>`);
  w.document.close();
}

function toggleServerLog() {
  const panel = document.getElementById('serverLogPanel');
  _logPanelOpen = !_logPanelOpen;
  panel.style.display = _logPanelOpen ? '' : 'none';
  if (_logPanelOpen) startLogStream();
}

let _logRetryTimer = null;
let _logStreamGen  = 0;   // bumped by stopLogStream — lets in-flight starts cancel themselves
let _logRetryDelay = 5000; // exponential backoff; reset on a healthy message

async function startLogStream() {
  if (_logEventSrc) return; // already streaming
  const gen = ++_logStreamGen;
  const box = document.getElementById('serverLogBox');
  // Pre-load last 50 lines so box is never blank
  try {
    const r = await fetch('/api/llm/server/log/tail').then(r => r.json());
    if (gen !== _logStreamGen) return;   // cancelled while fetching
    if (r.lines && r.lines.length) {
      box.textContent = r.lines.join('\n') + '\n';
      box.scrollTop = box.scrollHeight;
    } else {
      box.textContent = '';
    }
  } catch(e) {
    if (gen !== _logStreamGen) return;
    box.textContent = '';
  }
  const es = await openAgentSse(
    '/api/llm/server/log/stream-info',
    '/api/llm/server/log/stream',
  );
  // If stopLogStream ran while we were awaiting, drop this stale stream
  // on the floor — otherwise it'll start appending log lines on top of
  // whatever replaced the box (e.g. systemctl status output).
  if (gen !== _logStreamGen) {
    try { es.close(); } catch (_) {}
    return;
  }
  _logEventSrc = es;
  _logEventSrc.onmessage = e => {
    const msg = JSON.parse(e.data);
    _logRetryDelay = 5000;   // healthy stream — reset backoff
    if (msg.keepalive) return;
    box.textContent += msg.line + '\n';
    box.scrollTop = box.scrollHeight;
  };
  _logEventSrc.onerror = () => {
    if (_logEventSrc) { _logEventSrc.close(); _logEventSrc = null; }
    // Only retry while the user is actually on the LLM tab AND the panel
    // is still open. Otherwise we'd quietly poll /llama/log/tail forever
    // after the user navigates away. Capped exponential backoff so a manager
    // 503 (at stream capacity) doesn't turn into a 5s reconnect storm.
    if (_logPanelOpen && _activeTab === 'llm' && _subTabState.llm === 'llamacpp') {
      const delay = _logRetryDelay;
      _logRetryDelay = Math.min(_logRetryDelay * 2, 60000);
      _logRetryTimer = setTimeout(() => { _logRetryTimer = null; startLogStream(); }, delay);
    }
  };
}

async function restartLogStream() {
  stopLogStream();
  const box = document.getElementById('serverLogBox');
  if (box) box.textContent = '── reconnecting…\n';
  await startLogStream();
}

function popOutLog() {
  // Carry the picker selection into the standalone page so it streams the
  // selected agent's log, not the default (the page reads ?agent= itself).
  const ag = (typeof _selectedAgent === 'function' && _selectedAgent('llama'))
    ? ('?agent=' + encodeURIComponent(_selectedAgent('llama'))) : '';
  const win = window.open('/llm/log' + ag, 'llamalog',
    'width=900,height=600,resizable=yes,scrollbars=yes,toolbar=no,menubar=no');
  if (!win) alert('Pop-out blocked — allow pop-ups for this page.');
}

function fullscreenLog() {
  const box = document.getElementById('serverLogBox');
  if (box.requestFullscreen) box.requestFullscreen();
  else if (box.webkitRequestFullscreen) box.webkitRequestFullscreen();
}

function stopLogStream() {
  if (_logEventSrc) { _logEventSrc.close(); _logEventSrc = null; }
  if (_logRetryTimer) { clearTimeout(_logRetryTimer); _logRetryTimer = null; }
  _logRetryDelay = 5000;
  // Invalidate any in-flight startLogStream so it doesn't install a fresh
  // EventSource and resume appending lines after we've cleared the box.
  _logStreamGen++;
}
