// Small DOM and formatting helpers shared by the renderer modules.

/**
 * Creates an element. `attrs` keys: `class`, `text`, `on<Event>` handlers,
 * `dataset` (object), booleans (set when true) and plain attributes.
 */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  el.replaceChildren();
}

export const fmtInt = (n) => Number(n).toLocaleString('en-US');

export function fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, '0');
  return hh > 0 ? `${hh}:${String(mm).padStart(2, '0')}:${ss}` : `${mm}:${ss}`;
}

/** `vol10/Christadelphian 1873.docx` -> `Christadelphian 1873`. */
export function fileStem(relPath) {
  const name = relPath.split(/[\\/]/).pop();
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(0, dot) : name;
}

/** Calls the backend; resolves to the result or throws an Error with `fields`. */
export async function call(cmd, params = {}) {
  const reply = await window.corpusqa.request(cmd, params);
  if (reply.ok) return reply.result;
  const err = new Error(reply.error);
  err.fields = reply.fields || {};
  throw err;
}

/** Reads a JSON value from localStorage, tolerating absence and errors. */
export function loadLocal(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function saveLocal(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable: the setting just is not remembered */
  }
}

/**
 * Text as the book prints it: escapes decoded, *italic* and **bold** styled.
 * @param {string} text stored text
 * @returns {Node[]} nodes to append
 */
export function styledText(text) {
  const { styledRuns, decodeEntities } = window.CorpusReader;
  return styledRuns(text).map((run) => {
    let node = document.createTextNode(decodeEntities(run.text));
    if (run.em) node = h('em', {}, node);
    if (run.strong) node = h('strong', {}, node);
    return node;
  });
}
