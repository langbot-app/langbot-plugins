// Runs the console page's inline script inside a fake DOM with manually resolved
// API responses, so response-to-session attribution (review finding F1) can be
// observed objectively: interleaved session loads, replies while switching, and
// polling/stale writebacks.
//
// usage: node console_attribution_harness.mjs <path/to/index.html>
// prints one line: __RESULT__<json>
import fs from 'node:fs';
import vm from 'node:vm';

const htmlPath = process.argv[2];
if (!htmlPath) {
  console.error('usage: node console_attribution_harness.mjs <index.html>');
  process.exit(2);
}
const html = fs.readFileSync(htmlPath, 'utf8');
const inline = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)]
  .filter((m) => !/\bsrc\s*=/.test(m[1]))
  .map((m) => m[2])
  .join('\n;\n');
if (!inline.trim()) {
  console.error('no inline <script> found in ' + htmlPath);
  process.exit(2);
}

function makeElement(id) {
  const el = {
    id,
    style: {},
    dataset: {},
    value: '',
    textContent: '',
    innerHTML: '',
    className: '',
    disabled: false,
    scrollTop: 0,
    scrollHeight: 0,
    files: [],
    _classes: new Set(),
    _listeners: {},
  };
  el.classList = {
    add: (...names) => names.forEach((n) => el._classes.add(n)),
    remove: (...names) => names.forEach((n) => el._classes.delete(n)),
    toggle: (n) => (el._classes.has(n) ? el._classes.delete(n) : el._classes.add(n)),
    contains: (n) => el._classes.has(n),
  };
  el.addEventListener = (ev, fn) => {
    (el._listeners[ev] || (el._listeners[ev] = [])).push(fn);
  };
  el.removeEventListener = () => {};
  el.appendChild = (child) => {
    (el._children || (el._children = [])).push(child);
    return child;
  };
  el.removeChild = () => {};
  el.querySelector = () => makeElement(id + ' > child');
  el.querySelectorAll = () => [];
  el.getAttribute = () => null;
  el.setAttribute = () => {};
  el.click = () => {};
  el.focus = () => {};
  return el;
}

function loadPage() {
  const elements = new Map();
  const documentStub = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    createElement(tag) {
      return makeElement('<' + tag + '>');
    },
    querySelector: () => makeElement('query'),
    querySelectorAll: () => [],
    addEventListener: () => {},
  };

  // /sessions is not part of the attribution scenarios, so it resolves at once;
  // every other endpoint stays pending until the scenario resolves it explicitly.
  const inflight = [];
  const intervals = [];
  const api = (path, body, method) => {
    if (path === '/sessions') return Promise.resolve({ sessions: [] });
    return new Promise((resolve, reject) => {
      inflight.push({ path, body, method, resolve, reject });
    });
  };

  const sandbox = {
    document: documentStub,
    window: {},
    console,
    langbot: {
      api,
      t: (key, fallback) => (fallback === undefined ? key : fallback),
      applyI18n() {},
      onReady() {},
      onLanguageChange() {},
    },
    setInterval: (fn, ms) => {
      const handle = { fn, ms, cleared: false };
      intervals.push(handle);
      return handle;
    },
    clearInterval: (handle) => {
      if (handle) handle.cleared = true;
    },
    setTimeout: (fn, ms) => ({ fn, ms }),
    clearTimeout: () => {},
  };
  const ctx = vm.createContext(sandbox);
  vm.runInContext(inline, ctx, { filename: 'console-inline.js' });
  return { ctx, document: documentStub, elements, inflight, intervals };
}

const settle = async (rounds = 60) => {
  for (let i = 0; i < rounds; i += 1) await new Promise((r) => setImmediate(r));
};

function pending(page, path, key) {
  const found = page.inflight.find(
    (r) => r.path === path && (key === undefined || (r.body && r.body.session_key === key)),
  );
  if (!found) {
    const seen = page.inflight.map((r) => [r.path, r.body && r.body.session_key].join(':'));
    throw new Error(`no pending ${path} (key=${key}); pending=[${seen.join(', ')}]`);
  }
  page.inflight.splice(page.inflight.indexOf(found), 1);
  return found;
}

function messagesPayload(key, extra = {}) {
  return {
    session_key: key,
    type: 'person',
    name: 'name-' + key,
    messages: [],
    members: {},
    takeover_active: false,
    takeover_remaining: 0,
    takeover_timeout: 600,
    ...extra,
  };
}

function snapshot(page) {
  const { ctx } = page;
  const data = ctx.state.currentData;
  return {
    current: ctx.state.current,
    currentKey: ctx.state.currentKey === undefined ? null : ctx.state.currentKey,
    displayedKey: data ? data.session_key : null,
    takeoverActive: data ? !!data.takeover_active : null,
    titleKey: page.document.getElementById('chatTitle').textContent,
    buttonText: page.document.getElementById('takeoverBtn').textContent,
    countdownShown: page.document.getElementById('countdown')._classes.has('show'),
    sendDisabled: page.document.getElementById('sendBtn').disabled === true,
  };
}

const result = {};
async function scenario(name, fn) {
  try {
    result[name] = await fn();
  } catch (err) {
    result[name] = { error: String((err && err.stack) || err) };
  }
}

// --- 1. Two interleaved loads; the older response lands last --------------
// Opening A then B must leave B on screen, and the reply must go to whatever
// session is actually displayed.
await scenario('interleaved_loads_and_reply', async () => {
  const page = loadPage();
  const { ctx } = page;
  ctx.openSession('A');
  const requestA = pending(page, '/messages', 'A');
  ctx.openSession('B');
  const requestB = pending(page, '/messages', 'B');
  requestB.resolve(messagesPayload('B'));
  await settle();
  requestA.resolve(messagesPayload('A')); // stale response arrives last
  await settle();

  const afterLoads = snapshot(page);
  const displayedAtTyping = ctx.state.currentData ? ctx.state.currentData.session_key : null;
  page.document.getElementById('msgInput').value = 'answer for the conversation on screen';
  ctx.sendMessage();
  await settle();
  const reply = page.inflight.find((r) => r.path === '/reply') || null;

  const out = {
    afterLoads,
    displayedAtTyping,
    replySent: !!reply,
    replyKey: reply ? reply.body.session_key : null,
    replyText: reply ? reply.body.text : null,
  };
  out.replyKeyMatchesDisplayed = !!reply && reply.body.session_key === displayedAtTyping;

  if (reply) {
    reply.resolve({ takeover_active: false, takeover_remaining: 0 });
    await settle();
  }
  const refresh = page.inflight.find((r) => r.path === '/messages') || null;
  if (refresh) {
    refresh.resolve(messagesPayload(refresh.body.session_key));
    await settle();
  }
  out.afterSend = snapshot(page);
  return out;
});

// --- 2. Switching sessions clears the previous conversation and blocks send
await scenario('switch_while_loading', async () => {
  const page = loadPage();
  const { ctx } = page;
  ctx.openSession('A');
  pending(page, '/messages', 'A').resolve(messagesPayload('A'));
  await settle();
  const loaded = snapshot(page);

  ctx.openSession('B'); // B's /messages is still in flight
  await settle(5);
  const whileLoading = snapshot(page);
  whileLoading.messagesHtml = page.document.getElementById('messages').innerHTML;
  whileLoading.infoHtml = page.document.getElementById('infoPanel').innerHTML;

  page.document.getElementById('msgInput').value = 'typed while B loads';
  ctx.sendMessage();
  await settle();
  whileLoading.replySentWhileLoading = page.inflight.some((r) => r.path === '/reply');

  pending(page, '/messages', 'B').resolve(messagesPayload('B'));
  await settle();
  return { loaded, whileLoading, afterLoad: snapshot(page) };
});

// --- 3. Polling must not write a previous session's snapshot onto the new one
await scenario('poll_stale_writeback', async () => {
  const page = loadPage();
  const { ctx } = page;
  ctx.startPolling();
  const poll = page.intervals.filter((h) => h.ms === 5000).pop();
  ctx.openSession('A');
  pending(page, '/messages', 'A').resolve(messagesPayload('A'));
  await settle();
  const before = snapshot(page);

  poll.fn(); // poll starts for A and stays in flight
  await settle();
  const stale = pending(page, '/takeover_status', 'A');

  ctx.openSession('B');
  pending(page, '/messages', 'B').resolve(messagesPayload('B'));
  await settle();

  stale.resolve({ takeover_active: true, takeover_remaining: 300 }); // A's snapshot, late
  await settle();
  return { before, polledKey: stale.body.session_key, after: snapshot(page) };
});

// --- 4. A takeover response for the previous session must not repaint the new one
await scenario('takeover_stale_response', async () => {
  const page = loadPage();
  const { ctx } = page;
  ctx.openSession('A');
  pending(page, '/messages', 'A').resolve(messagesPayload('A'));
  await settle();

  ctx.toggleTakeover();
  const request = pending(page, '/takeover', 'A');

  ctx.openSession('B');
  pending(page, '/messages', 'B').resolve(messagesPayload('B'));
  await settle();

  request.resolve({ takeover_active: true, takeover_remaining: 300 });
  await settle();
  return { requestKey: request.body.session_key, after: snapshot(page) };
});

// --- 5. Regression guards: the current session still works normally --------
await scenario('current_session_still_works', async () => {
  const page = loadPage();
  const { ctx } = page;
  ctx.openSession('Z');
  pending(page, '/messages', 'Z').resolve(
    messagesPayload('Z', { takeover_active: true, takeover_remaining: 120 }),
  );
  await settle();
  const afterLoad = snapshot(page);

  page.document.getElementById('msgInput').value = 'hello';
  ctx.sendMessage();
  await settle();
  const reply = pending(page, '/reply', 'Z');
  const replyKey = reply.body.session_key;
  reply.resolve({ takeover_active: false, takeover_remaining: 0 });
  await settle();
  const refresh = pending(page, '/messages', 'Z');
  refresh.resolve(messagesPayload('Z'));
  await settle();
  return { afterLoad, replyKey, afterSend: snapshot(page) };
});

console.log('__RESULT__' + JSON.stringify(result));
