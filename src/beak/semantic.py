from __future__ import annotations

import json
from typing import Any


SEMANTIC_DOM_SCRIPT = r"""() => {
  const selector = [
    "main", "article", "section", "nav", "header", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "li",
    "a", "button", "input", "textarea", "select", "label",
    "table", "caption", "th", "td", "blockquote", "pre", "code",
    "img", "figure", "figcaption", "summary", "details",
    "[role]", "[aria-label]", "[alt]", "[title]"
  ].join(",");

  const blockTags = new Set(["MAIN", "ARTICLE", "SECTION", "NAV", "HEADER", "FOOTER", "FIGURE", "DETAILS"]);
  const contentTags = new Set([
    "H1", "H2", "H3", "H4", "H5", "H6", "P", "LI", "A", "BUTTON", "INPUT",
    "TEXTAREA", "SELECT", "LABEL", "CAPTION", "TH", "TD", "BLOCKQUOTE",
    "PRE", "CODE", "IMG", "FIGCAPTION", "SUMMARY"
  ]);

  function visible(el) {
    if (!(el instanceof HTMLElement)) return false;
    if (el.closest("[aria-hidden='true'],script,style,noscript,template")) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    const rects = el.getClientRects();
    if (!rects || rects.length === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function clean(text) {
    return (text || "").replace(/\s+/g, " ").trim();
  }

  function pathFor(el) {
    const parts = [];
    let current = el;
    while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
      let part = current.tagName.toLowerCase();
      if (current.id) part += "#" + current.id;
      else if (current.classList && current.classList.length) part += "." + Array.from(current.classList).slice(0, 2).join(".");
      parts.unshift(part);
      current = current.parentElement;
    }
    return parts.join(" > ");
  }

  return Array.from(document.querySelectorAll(selector))
    .filter(visible)
    .map((el, index) => {
      const tag = el.tagName.toLowerCase();
      const role = el.getAttribute("role") || null;
      const ariaLabel = clean(el.getAttribute("aria-label"));
      const title = clean(el.getAttribute("title"));
      const alt = clean(el.getAttribute("alt"));
      const href = el.href || el.getAttribute("href") || null;
      const src = el.currentSrc || el.src || el.getAttribute("src") || null;
      const name = clean(el.getAttribute("name") || el.getAttribute("placeholder"));
      const value = ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) ? clean(el.value) : "";
      let text = clean(el.innerText || el.textContent);

      if (blockTags.has(el.tagName)) {
        text = ariaLabel || title || role || "";
      }
      if (!contentTags.has(el.tagName) && !role && !ariaLabel && !title) {
        return null;
      }

      const item = {
        index,
        tag,
        role,
        text: text.slice(0, 4000),
        aria_label: ariaLabel || null,
        title: title || null,
        alt: alt || null,
        href,
        src,
        name: name || null,
        value: value || null,
        path: pathFor(el)
      };
      const level = /^h[1-6]$/.test(tag) ? Number(tag.slice(1)) : null;
      if (level) item.level = level;
      return item;
    })
    .filter((item) => item && (item.text || item.aria_label || item.alt || item.href || item.src || item.role));
}"""


def semantic_items_to_jsonl(items: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in items) + ("\n" if items else "")


def semantic_items_to_markdown(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for item in items:
        tag = str(item.get("tag") or "")
        text = _display_text(item)
        if not text and tag != "img":
            continue

        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            level = max(1, min(int(tag[1]), 6))
            lines.append(f"{'#' * level} {text}")
        elif tag == "li":
            lines.append(f"- {text}")
        elif tag == "a":
            href = item.get("href")
            lines.append(f"[{text}]({href})" if href else text)
        elif tag == "img":
            alt = text or "image"
            src = item.get("src") or ""
            lines.append(f"![{_escape_brackets(alt)}]({src})")
        elif tag == "button":
            lines.append(f"[button] {text}")
        elif tag in {"input", "textarea", "select"}:
            name = item.get("name") or tag
            value = item.get("value")
            lines.append(f"[{tag}: {name}]" + (f" {value}" if value else ""))
        elif tag == "blockquote":
            lines.append("> " + text.replace("\n", "\n> "))
        elif tag == "pre":
            lines.append(f"```\n{text}\n```")
        elif item.get("role") and tag not in {"p", "td", "th", "caption"}:
            lines.append(f"[{item['role']}] {text}")
        else:
            lines.append(text)
    return "\n\n".join(_dedupe_adjacent(lines)).strip() + ("\n" if lines else "")


def _display_text(item: dict[str, Any]) -> str:
    for key in ("text", "aria_label", "alt", "title", "name", "value"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _escape_brackets(value: str) -> str:
    return value.replace("[", "\\[").replace("]", "\\]")


def _dedupe_adjacent(lines: list[str]) -> list[str]:
    deduped: list[str] = []
    previous = None
    for line in lines:
        if line == previous:
            continue
        deduped.append(line)
        previous = line
    return deduped
