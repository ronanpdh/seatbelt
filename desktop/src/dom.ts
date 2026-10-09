// Small helpers for building the window. Text from a ledger, a model or a tool is untrusted:
// it is only ever set as textContent, never as HTML.

export const byId = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function button(text: string, className: string, onClick: () => void, title?: string): HTMLButtonElement {
  const b = el("button", className, text);
  b.type = "button";
  if (title) b.title = title;
  b.addEventListener("click", onClick);
  return b;
}

// errors are ours, but may quote a path: no control characters into a terminal or the page
export const plain = (text: unknown): string => String(text).replace(/[\u0000-\u001f\u007f-\u009f]/g, " ");
export const basename = (path: string): string => path.split(/[\\/]/).filter(Boolean).pop() ?? path;

export const count = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;
export const number = (n: number | null | undefined): string => (n == null ? "" : n.toLocaleString());

export function when(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** A table with a header row; each cell is text, or an element built by the caller. */
export function table(head: string[], rows: (string | Node)[][], className = "grid"): HTMLTableElement {
  const t = el("table", className);
  const tr = el("tr");
  for (const h of head) tr.append(el("th", undefined, h));
  t.append(el("thead"));
  t.tHead!.append(tr);
  const body = el("tbody");
  for (const row of rows) {
    const r = el("tr");
    for (const cell of row) {
      const td = el("td");
      if (typeof cell === "string") td.textContent = cell;
      else td.append(cell);
      r.append(td);
    }
    body.append(r);
  }
  t.append(body);
  return t;
}

/** Text that may hold code fences: fenced parts become code blocks, the rest stays text. */
export function richText(text: string): DocumentFragment {
  const out = document.createDocumentFragment();
  const parts = text.split(/^```[^\n]*\n?/m);
  parts.forEach((part, i) => {
    if (part === "") return;
    if (i % 2 === 1) {
      const pre = el("pre", "code");
      pre.append(el("code", undefined, part.replace(/\n$/, "")));
      out.append(pre);
    } else {
      out.append(el("div", "prose", part.replace(/^\n+|\n+$/g, "")));
    }
  });
  return out;
}
