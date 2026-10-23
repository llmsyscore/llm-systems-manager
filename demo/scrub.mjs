// Scrubs captured JSON: hostnames, IPv4 addresses, usernames, paths, fingerprints and agent ids become sample values.
// Agent ids are learned from agent_id fields, agent/target fields and /api/agents/<id> paths.
const HOST_KEYS = /^(hostname|host|host_name|fqdn|server|machine|node|agent_host|agent_name|computer_name)$/i;
const USER_KEYS = /^(user|username|owner|created_by|login|actor|os_user)$/i;
const IPV4 = /\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b/g;
const URL_HOST = /\b([a-z][a-z0-9+.-]*:\/\/)(?:[^\/\s@"']*@)?([^\/\s:?#"'\]]+)/gi;
const PRIVATE_TLD = /\b[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)*\.(?:lan|local|localdomain|internal|home|corp|intranet)\b/gi;
const PATH = /(?:\/home\/[^\s"'\\]+|\/opt\/[^\s"'\\]+|\/Users\/[^\s"'\\]+)/g;
// Real usernames and machine names to replace, from DEMO_SCRUB_NAMES (comma-separated).
const names = () => (process.env.DEMO_SCRUB_NAMES || '').split(',').map((n) => n.trim()).filter(Boolean);
const FINGERPRINT = /\b[0-9a-f]{64}\b/gi;
const DUMMY_FINGERPRINT = '0123456789abcdef'.repeat(4);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const AGENT_PATH = /\/api\/agents\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f-])/gi;
const AGENT_KEYS = /^(agent|target)$/i;
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// True for addresses inside the documentation ranges 192.0.2.0/24 and 198.51.100.0/24.
export function isDocIp(ip) {
  return /^(192\.0\.2|198\.51\.100)\.\d{1,3}$/.test(ip);
}

const isIp = (h) => /^\d{1,3}(\.\d{1,3}){3}$/.test(h);
const keepHost = (h) => !h || /\.example$/i.test(h) || /^localhost$/i.test(h) || isIp(h);

// Returns a scrub function; `fleet` lists the sample hostnames handed out to real hosts in order seen.
export function makeScrubber(fleet) {
  const hostMap = new Map();
  const ipMap = new Map();
  const agentMap = new Map();
  let count = 0;
  let agentCount = 0;
  const ids = fleet.map((h) => h.split('.')[0]);
  const sampleHost = (real) => {
    const k = real.toLowerCase();
    if (!hostMap.has(k)) {
      const fake = count < fleet.length ? fleet[count] : `node${count + 1}.example`;
      count++;
      hostMap.set(k, fake);
      const short = k.split('.')[0];
      if (short !== k && short.length > 2 && !hostMap.has(short)) hostMap.set(short, fake);
    }
    return hostMap.get(k);
  };
  const sampleIp = (ip) => {
    if (!ipMap.has(ip)) ipMap.set(ip, `192.0.2.${(ipMap.size % 254) + 1}`);
    return ipMap.get(ip);
  };
  const sampleAgent = (id, host) => {
    if (agentMap.has(id)) return;
    const fake = host ? sampleHost(host).split('.')[0] : (agentCount < ids.length ? ids[agentCount] : `agent${agentCount + 1}`);
    agentCount++;
    agentMap.set(id, fake);
  };
  // First pass: learns the real hostnames and agent ids without changing anything.
  const collect = (v, key) => {
    if (typeof v === 'string') {
      if (HOST_KEYS.test(key || '')) { const h = v.replace(/:\d+$/, ''); if (!keepHost(h)) sampleHost(h); }
      for (const m of v.matchAll(URL_HOST)) if (!keepHost(m[2])) sampleHost(m[2]);
      if (AGENT_KEYS.test(key || '') && UUID.test(v)) sampleAgent(v, null);
      for (const m of v.matchAll(AGENT_PATH)) sampleAgent(m[1], null);
    } else if (Array.isArray(v)) v.forEach((x) => collect(x, key));
    else if (v && typeof v === 'object') {
      const hk = Object.keys(v).find((k) => HOST_KEYS.test(k) && typeof v[k] === 'string' && !keepHost(v[k].replace(/:\d+$/, '')));
      if (typeof v.agent_id === 'string' && UUID.test(v.agent_id)) sampleAgent(v.agent_id, hk ? v[hk].replace(/:\d+$/, '') : null);
      for (const [k, x] of Object.entries(v)) collect(x, k);
    }
  };
  const str = (s) => {
    let out = s.replace(PATH, (p) => `/opt/models/${p.split('/').filter(Boolean).pop()}`);
    for (const [id, fake] of agentMap) out = out.split(id).join(fake);
    out = out.replace(FINGERPRINT, DUMMY_FINGERPRINT);
    out = out.replace(URL_HOST, (m, scheme, host) => (keepHost(host) ? m : m.replace(host, sampleHost(host))));
    out = out.replace(IPV4, (m, a, b, c, d) => (+a > 255 || +b > 255 || +c > 255 || +d > 255 || isDocIp(m) ? m : sampleIp(m)));
    out = out.replace(PRIVATE_TLD, (t) => sampleHost(t));
    const hosts = [...hostMap].sort((a, b) => b[0].length - a[0].length);
    for (const [real, fake] of hosts) out = out.replace(new RegExp(`(?<![\\w.@])${esc(real)}(?!\\w|\\.example)`, 'gi'), fake);
    for (const n of names()) out = out.replace(new RegExp(esc(n), 'gi'), 'alex');
    return out;
  };
  const walk = (v, key) => {
    if (typeof v === 'string') {
      if (HOST_KEYS.test(key || '')) {
        const h = v.replace(/:\d+$/, '');
        return keepHost(h) ? str(v) : sampleHost(h);
      }
      if (USER_KEYS.test(key || '')) return 'alex';
      return str(v);
    }
    if (Array.isArray(v)) return v.map((x) => walk(x, key));
    if (v && typeof v === 'object') {
      const out = {};
      for (const [k, x] of Object.entries(v)) out[agentMap.get(k) || hostMap.get(k.toLowerCase()) || k] = walk(x, k);
      return out;
    }
    return v;
  };
  return (json) => { collect(json); return walk(json); };
}

export function scrub(json, fleet = ['atlas.example', 'forge.example', 'studio.example', 'quill.example']) {
  return makeScrubber(fleet)(json);
}

export { UUID, DUMMY_FINGERPRINT };
