// Markdown for agent replies, built as elements: headings, paragraphs, emphasis, inline code,
// code blocks, lists (with task boxes), quotes, rules and GFM tables. Nothing in the text is
// ever parsed as HTML. A link is shown as its text; clicking it asks the Rust side to open it
// in the browser, which only accepts http and https.

import { invoke } from "@tauri-apps/api/core";
import { el } from "./dom";

const LIST = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const FENCE = /^\s{0,3}(`{3,}|~{3,})\s*([^\s`]*)[^`]*$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const TABLE_DELIM = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

const lead = (line: string): number => line.length - line.trimStart().length;
const blank = (line: string): boolean => line.trim() === "";

function startsBlock(line: string): boolean {
  return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || LIST.test(line);
}

export function markdown(text: string): DocumentFragment {
  const out = document.createDocumentFragment();
  blocks(text.replace(/\r\n?/g, "\n").split("\n"), out);
  return out;
}

function blocks(lines: string[], out: Node & ParentNode): void {
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (blank(line)) {
      i++;
      continue;
    }
    let m = FENCE.exec(line);
    if (m) {
      const fence = m[1];
      const closing = new RegExp(`^\\s{0,3}${fence[0] === "`" ? "`" : "~"}{${fence.length},}\\s*$`);
      const body: string[] = [];
      i++;
      while (i < lines.length && !closing.test(lines[i])) body.push(lines[i++]);
      i++; // the closing fence, or the end: an unclosed block runs to the end, as while streaming
      out.append(codeBlock(body.join("\n"), m[2] ?? ""));
      continue;
    }
    m = HEADING.exec(line);
    if (m) {
      const h = el(`h${m[1].length}` as "h1");
      inline(m[2], h);
      out.append(h);
      i++;
      continue;
    }
    if (RULE.test(line)) {
      out.append(el("hr"));
      i++;
      continue;
    }
    if (QUOTE.test(line)) {
      const inner: string[] = [];
      while (i < lines.length && !blank(lines[i]) && (QUOTE.test(lines[i]) || !startsBlock(lines[i]))) {
        const q = QUOTE.exec(lines[i]);
        inner.push(q ? q[1] : lines[i]);
        i++;
      }
      const quote = el("blockquote");
      blocks(inner, quote);
      out.append(quote);
      continue;
    }
    if (LIST.test(line)) {
      i = list(lines, i, out);
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_DELIM.test(lines[i + 1]) && lines[i + 1].includes("-")) {
      i = table(lines, i, out);
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && !blank(lines[i]) && (para.length === 0 || !startsBlock(lines[i]))) {
      para.push(lines[i].trim());
      i++;
    }
    const p = el("p");
    inline(para.join("\n"), p);
    out.append(p);
  }
}

function list(lines: string[], start: number, out: Node & ParentNode): number {
  const first = LIST.exec(lines[start])!;
  const indent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const node = el(ordered ? "ol" : "ul");
  if (ordered) {
    const n = parseInt(first[2], 10);
    if (n !== 1) (node as HTMLOListElement).start = n;
  }
  let i = start;
  while (i < lines.length) {
    const m = LIST.exec(lines[i]);
    if (!m || m[1].length !== indent || /\d/.test(m[2]) !== ordered) break;
    const inner = m[1].length + m[2].length + 1;
    const item: string[] = [m[3]];
    i++;
    while (i < lines.length) {
      const line = lines[i];
      if (blank(line)) {
        let j = i + 1;
        while (j < lines.length && blank(lines[j])) j++;
        if (j < lines.length && lead(lines[j]) > indent) {
          item.push("");
          i++;
          continue;
        }
        break;
      }
      if (lead(line) > indent) {
        item.push(line.slice(Math.min(lead(line), inner)));
        i++;
        continue;
      }
      if (startsBlock(line)) break;
      item.push(line.trim()); // a paragraph's lazy continuation
      i++;
    }
    const li = el("li");
    const task = /^\[([ xX])\]\s+/.exec(item[0]);
    if (task) item[0] = item[0].slice(task[0].length);
    blocks(item, li);
    if (task) {
      // the box goes at the start of the item's first line, not on a line of its own
      li.classList.add("task");
      const first = li.firstElementChild;
      const box = el("span", "task-box", task[1] === " " ? "☐ " : "☑ ");
      if (first?.tagName === "P") first.prepend(box);
      else li.prepend(box);
    }
    node.append(li);
  }
  out.append(node);
  return i;
}

/** A table row's cells; `\|` is a pipe inside a cell. (No lookbehind: older macOS web views
 * cannot parse one, and the whole script would fail.) */
function cells(row: string): string[] {
  let text = row.trim();
  if (text.startsWith("|")) text = text.slice(1);
  if (text.endsWith("|") && !text.endsWith("\\|")) text = text.slice(0, -1);
  const out: string[] = [];
  let cell = "";
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "\\" && text[i + 1] === "|") {
      cell += "|";
      i++;
    } else if (text[i] === "|") {
      out.push(cell.trim());
      cell = "";
    } else {
      cell += text[i];
    }
  }
  out.push(cell.trim());
  return out;
}

function table(lines: string[], start: number, out: Node & ParentNode): number {
  const head = cells(lines[start]);
  const align = cells(lines[start + 1]).map((d) =>
    d.startsWith(":") && d.endsWith(":") ? "center" : d.endsWith(":") ? "right" : d.startsWith(":") ? "left" : "",
  );
  const t = el("table");
  const tr = el("tr");
  head.forEach((c, k) => {
    const th = el("th");
    if (align[k]) th.style.textAlign = align[k];
    inline(c, th);
    tr.append(th);
  });
  t.createTHead().append(tr);
  const body = el("tbody");
  let i = start + 2;
  while (i < lines.length && !blank(lines[i]) && lines[i].includes("|")) {
    const row = el("tr");
    const values = cells(lines[i]);
    head.forEach((_, k) => {
      const td = el("td");
      if (align[k]) td.style.textAlign = align[k];
      inline(values[k] ?? "", td);
      row.append(td);
    });
    body.append(row);
    i++;
  }
  t.append(body);
  const wrap = el("div", "table-wrap");
  wrap.append(t);
  out.append(wrap);
  return i;
}

function codeBlock(code: string, lang: string): HTMLElement {
  const box = el("div", "code-block");
  const head = el("div", "code-head");
  head.append(el("span", undefined, lang));
  const copy = el("button", "copy", "Copy");
  copy.type = "button";
  copy.addEventListener("click", () => {
    navigator.clipboard
      .writeText(code)
      .then(() => (copy.textContent = "Copied"))
      .catch(() => (copy.textContent = "Copy failed"))
      .finally(() => setTimeout(() => (copy.textContent = "Copy"), 1500));
  });
  head.append(copy);
  const pre = el("pre");
  pre.append(el("code", undefined, code));
  box.append(head, pre);
  return box;
}

// -- inline ---------------------------------------------------------------------------------

/** The end of a link's `[text](url)` starting at `at`, with its text and url, or null. */
function link(text: string, at: number): { label: string; url: string; end: number } | null {
  let depth = 0;
  let i = at;
  for (; i < text.length; i++) {
    if (text[i] === "\\") {
      i++;
      continue;
    }
    if (text[i] === "[") depth++;
    else if (text[i] === "]" && --depth === 0) break;
  }
  if (i >= text.length || text[i + 1] !== "(") return null;
  const close = text.indexOf(")", i + 2);
  if (close < 0) return null;
  const url = text.slice(i + 2, close).trim().split(/\s+/)[0].replace(/^<|>$/g, "");
  return { label: text.slice(at + 1, i), url, end: close + 1 };
}

function linkNode(label: string, url: string): HTMLElement {
  const a = el("span", "md-link");
  a.title = url;
  a.tabIndex = 0;
  a.setAttribute("role", "link");
  inline(label, a);
  const open = () => void invoke("open_link", { url }).catch(() => undefined);
  a.addEventListener("click", open);
  a.addEventListener("keydown", (e) => {
    if (e.key === "Enter") open();
  });
  return a;
}

const PUNCT = /[\\`*_{}[\]()#+\-.!|~<>]/;
const WORD = /[\p{L}\p{N}]/u;

function inline(text: string, parent: Node & ParentNode): void {
  let buf = "";
  const flush = () => {
    if (!buf) return;
    const parts = buf.split("\n");
    parts.forEach((part, k) => {
      if (k > 0) parent.append(el("br"));
      if (part) parent.append(document.createTextNode(part));
    });
    buf = "";
  };
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === "\\" && i + 1 < text.length && PUNCT.test(text[i + 1])) {
      buf += text[i + 1];
      i += 2;
      continue;
    }
    if (c === "`") {
      let n = 0;
      while (text[i + n] === "`") n++;
      const fence = "`".repeat(n);
      let close = text.indexOf(fence, i + n);
      while (close >= 0 && text[close + n] === "`") close = text.indexOf(fence, close + n + 1);
      if (close >= 0) {
        flush();
        let code = text.slice(i + n, close).replace(/\n/g, " ");
        if (/^ .* $/.test(code) && code.trim()) code = code.slice(1, -1);
        parent.append(el("code", undefined, code));
        i = close + n;
      } else {
        buf += fence;
        i += n;
      }
      continue;
    }
    if (c === "!" && text[i + 1] === "[") {
      const l = link(text, i + 1);
      if (l) {
        flush();
        parent.append(el("span", "md-image", `[image: ${l.label || l.url}]`));
        i = l.end;
        continue;
      }
    }
    if (c === "[") {
      const l = link(text, i);
      if (l && /^https?:\/\//i.test(l.url)) {
        flush();
        parent.append(linkNode(l.label, l.url));
        i = l.end;
        continue;
      }
    }
    if (c === "<") {
      const auto = /^<(https?:\/\/[^\s<>]+)>/i.exec(text.slice(i));
      if (auto) {
        flush();
        parent.append(linkNode(auto[1], auto[1]));
        i += auto[0].length;
        continue;
      }
    }
    if (c === "*" || c === "_" || c === "~") {
      let n = 0;
      while (text[i + n] === c && n < 3) n++;
      const before = text[i - 1] ?? " ";
      const after = text[i + n] ?? " ";
      const opens = !/\s/.test(after) && (c !== "_" || !WORD.test(before)) && (c !== "~" || n === 2);
      if (opens) {
        const run = c.repeat(n);
        let close = text.indexOf(run, i + n);
        while (close >= 0) {
          const prev = text[close - 1];
          const next = text[close + n] ?? " ";
          if (!/\s/.test(prev) && next !== c && (c !== "_" || !WORD.test(next))) break;
          close = text.indexOf(run, close + 1);
        }
        if (close > i + n) {
          flush();
          const inner = text.slice(i + n, close);
          let node: HTMLElement;
          if (c === "~") node = el("del");
          else if (n === 1) node = el("em");
          else if (n === 2) node = el("strong");
          else {
            node = el("strong");
            const em = el("em");
            node.append(em);
            inline(inner, em);
            parent.append(node);
            i = close + n;
            continue;
          }
          inline(inner, node);
          parent.append(node);
          i = close + n;
          continue;
        }
      }
      buf += c.repeat(n);
      i += n;
      continue;
    }
    buf += c;
    i++;
  }
  flush();
}
