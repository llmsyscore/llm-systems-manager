// Loads demo runtime scripts into a vm context whose self is the context; absent files are skipped.
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const RUNTIME = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'runtime');

function runtimeFile(url) {
  const path = new URL(url, 'http://demo.example/').pathname;
  if (!path.startsWith('/demo/')) throw new Error(`load: not a runtime path: ${url}`);
  return join(RUNTIME, path.slice('/demo/'.length));
}

// Returns the context; extra globals (caches, clients, DEMO_RELEASE, fetch...) come from `env`.
export function load(scripts, env = {}) {
  const listeners = {};
  const sandbox = {
    Request, Response, Headers, URL, URLSearchParams, ReadableStream, TextEncoder, TextDecoder,
    setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask, console,
    location: new URL('http://demo.example/demo-sw.js'),
    listeners,
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    skipWaiting() { sandbox.skipped = true; return Promise.resolve(); },
    ...env,
  };
  sandbox.self = sandbox;
  const ctx = vm.createContext(sandbox);
  sandbox.importScripts = (...urls) => {
    for (const u of urls) {
      const file = runtimeFile(u);
      if (!existsSync(file)) continue;
      vm.runInContext(readFileSync(file, 'utf8'), ctx, { filename: file });
    }
  };
  for (const s of scripts) sandbox.importScripts(`/demo/${s}`);
  return sandbox;
}
