#!/usr/bin/env python3
"""Generate docs/view/index.html from docs/architecture.md.

Projection rule: the HTML contains nothing that is not in architecture.md.
Uses only Python standard library + Mermaid CDN for diagram rendering.
"""

import re
import html
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent.parent
MD_PATH = DOCS_DIR / "architecture.md"
OUT_PATH = DOCS_DIR / "view" / "index.html"


def parse_markdown(text: str) -> list[dict]:
    """Parse markdown into sections by h2 headings."""
    sections = []
    current = {"id": "top", "title": "", "level": 1, "content": ""}
    for line in text.split("\n"):
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if level <= 2:
                if current["title"] or current["content"].strip():
                    sections.append(current)
                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                current = {"id": slug, "title": title, "level": level, "content": ""}
            else:
                current["content"] += line + "\n"
        else:
            current["content"] += line + "\n"
    if current["title"] or current["content"].strip():
        sections.append(current)
    return sections


def render_inline(text: str) -> str:
    """Render inline markdown: bold, italic, code, links."""
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    t = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', t)
    return t


def render_content(content: str) -> str:
    """Render markdown content to HTML."""
    lines = content.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Fenced code / mermaid blocks
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            block_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            block_text = "\n".join(block_lines)
            if lang == "mermaid":
                out.append(f'<div class="mermaid-container" onclick="this.classList.toggle(\'zoomed\')">'
                           f'<pre class="mermaid">{html.escape(block_text)}</pre></div>')
            elif lang:
                out.append(f"<pre><code class=\"language-{html.escape(lang)}\">"
                           f"{html.escape(block_text)}</code></pre>")
            else:
                out.append(f"<pre><code>{html.escape(block_text)}</code></pre>")
            continue

        # Headings (h3+)
        m = re.match(r"^(#{3,6})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            out.append(f"<h{level} id=\"{slug}\">{render_inline(title)}</h{level}>")
            i += 1
            continue

        # Tables
        if "|" in line and i + 1 < len(lines) and re.match(r"^\|[\s\-:|]+\|", lines[i + 1]):
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2  # skip header + separator
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            out.append("<div class=\"table-wrap\"><table><thead><tr>")
            for h in headers:
                out.append(f"<th>{render_inline(h)}</th>")
            out.append("</tr></thead><tbody>")
            for row in rows:
                out.append("<tr>")
                for cell in row:
                    out.append(f"<td>{render_inline(cell)}</td>")
                out.append("</tr>")
            out.append("</tbody></table></div>")
            continue

        # Blockquotes
        if line.strip().startswith("> "):
            bq_lines = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                bq_lines.append(lines[i].strip()[2:])
                i += 1
            out.append(f"<blockquote>{render_inline(' '.join(bq_lines))}</blockquote>")
            continue

        # Unordered lists
        if re.match(r"^\s*[-*]\s+", line):
            out.append("<ul>")
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                item = re.sub(r"^\s*[-*]\s+", "", lines[i])
                out.append(f"<li>{render_inline(item)}</li>")
                i += 1
            out.append("</ul>")
            continue

        # Ordered lists
        if re.match(r"^\s*\d+\.\s+", line):
            out.append("<ol>")
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                item = re.sub(r"^\s*\d+\.\s+", "", lines[i])
                out.append(f"<li>{render_inline(item)}</li>")
                i += 1
            out.append("</ol>")
            continue

        # Horizontal rule
        if re.match(r"^---+$", line.strip()):
            out.append("<hr>")
            i += 1
            continue

        # Paragraph
        if line.strip():
            out.append(f"<p>{render_inline(line)}</p>")

        i += 1

    return "\n".join(out)


def generate_html(md_text: str) -> str:
    sections = parse_markdown(md_text)

    # Build sidebar
    sidebar_items = []
    for sec in sections:
        if sec["level"] <= 2 and sec["title"]:
            sidebar_items.append(
                f'<a class="nav-link" href="#{sec["id"]}" data-section="{sec["id"]}">'
                f'{html.escape(sec["title"])}</a>'
            )

    sidebar_html = "\n".join(sidebar_items)

    # Build sections
    sections_html = []
    for sec in sections:
        if sec["level"] == 1 and sec["title"]:
            sections_html.append(
                f'<section id="{sec["id"]}" class="slide">'
                f'<h1>{render_inline(sec["title"])}</h1>'
                f'{render_content(sec["content"])}</section>'
            )
        elif sec["level"] == 2:
            sections_html.append(
                f'<section id="{sec["id"]}" class="slide">'
                f'<h2>{render_inline(sec["title"])}</h2>'
                f'{render_content(sec["content"])}</section>'
            )
        else:
            sections_html.append(render_content(sec["content"]))

    body = "\n".join(sections_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wavebreak Architecture</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<style>
:root {{
  --bg: #ffffff;
  --fg: #1a1a1a;
  --bg-sidebar: #f5f5f5;
  --border: #e0e0e0;
  --accent: #2563eb;
  --accent-light: #dbeafe;
  --code-bg: #f3f4f6;
  --table-stripe: #f9fafb;
  --blockquote-border: #d1d5db;
  --blockquote-bg: #f9fafb;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #0f0f0f;
    --fg: #e5e5e5;
    --bg-sidebar: #1a1a1a;
    --border: #333;
    --accent: #60a5fa;
    --accent-light: #1e3a5f;
    --code-bg: #1e1e1e;
    --table-stripe: #1a1a1a;
    --blockquote-border: #555;
    --blockquote-bg: #1a1a1a;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #0f0f0f;
  --fg: #e5e5e5;
  --bg-sidebar: #1a1a1a;
  --border: #333;
  --accent: #60a5fa;
  --accent-light: #1e3a5f;
  --code-bg: #1e1e1e;
  --table-stripe: #1a1a1a;
  --blockquote-border: #555;
  --blockquote-bg: #1a1a1a;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.6;
}}

/* Sidebar */
.sidebar {{
  position: fixed;
  top: 0;
  left: 0;
  width: 280px;
  height: 100vh;
  overflow-y: auto;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  padding: 16px;
  z-index: 100;
  transition: transform 0.3s;
}}
.sidebar h3 {{
  font-size: 14px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--accent);
  margin-bottom: 12px;
}}
.nav-link {{
  display: block;
  padding: 6px 8px;
  text-decoration: none;
  color: var(--fg);
  font-size: 13px;
  border-radius: 4px;
  margin-bottom: 2px;
}}
.nav-link:hover, .nav-link.active {{
  background: var(--accent-light);
  color: var(--accent);
}}

/* Main */
.main {{
  margin-left: 280px;
  padding: 32px 48px;
  max-width: 960px;
}}
@media (max-width: 768px) {{
  .sidebar {{ transform: translateX(-100%); }}
  .sidebar.open {{ transform: translateX(0); }}
  .main {{ margin-left: 0; padding: 16px; }}
  .menu-toggle {{ display: block !important; }}
}}
.menu-toggle {{
  display: none;
  position: fixed;
  top: 12px;
  left: 12px;
  z-index: 200;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 4px;
  padding: 8px 12px;
  cursor: pointer;
  font-size: 16px;
}}

/* Content */
h1 {{ font-size: 28px; margin: 24px 0 16px; }}
h2 {{ font-size: 22px; margin: 32px 0 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
h3 {{ font-size: 18px; margin: 24px 0 8px; }}
h4 {{ font-size: 15px; margin: 16px 0 6px; }}
p {{ margin: 8px 0; }}
a {{ color: var(--accent); }}
code {{
  background: var(--code-bg);
  padding: 2px 5px;
  border-radius: 3px;
  font-size: 0.9em;
  font-family: 'Consolas', 'Fira Code', monospace;
}}
pre {{
  background: var(--code-bg);
  padding: 16px;
  border-radius: 6px;
  overflow-x: auto;
  margin: 12px 0;
}}
pre code {{ background: none; padding: 0; }}
blockquote {{
  border-left: 3px solid var(--blockquote-border);
  background: var(--blockquote-bg);
  padding: 12px 16px;
  margin: 12px 0;
  border-radius: 0 4px 4px 0;
}}
ul, ol {{ margin: 8px 0; padding-left: 24px; }}
li {{ margin: 4px 0; }}
hr {{ border: none; border-top: 1px solid var(--border); margin: 24px 0; }}

/* Tables */
.table-wrap {{ overflow-x: auto; margin: 12px 0; }}
table {{
  border-collapse: collapse;
  width: 100%;
  font-size: 14px;
}}
th, td {{
  border: 1px solid var(--border);
  padding: 8px 12px;
  text-align: left;
}}
th {{ background: var(--bg-sidebar); font-weight: 600; }}
tr:nth-child(even) {{ background: var(--table-stripe); }}

/* Mermaid */
.mermaid-container {{
  margin: 16px 0;
  padding: 16px;
  background: var(--code-bg);
  border-radius: 6px;
  cursor: zoom-in;
  overflow: hidden;
  transition: all 0.3s;
}}
.mermaid-container.zoomed {{
  position: fixed;
  top: 0;
  left: 0;
  width: 100vw;
  height: 100vh;
  z-index: 1000;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: zoom-out;
  border-radius: 0;
  overflow: auto;
}}
.mermaid-container.zoomed .mermaid {{
  transform: scale(1.3);
}}

/* Mode badge */
.mode-badge {{
  position: fixed;
  top: 12px;
  right: 12px;
  z-index: 200;
  background: var(--accent);
  color: #fff;
  padding: 4px 12px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}}

/* Present mode */
body.present .sidebar {{ display: none; }}
body.present .main {{
  margin: 0;
  padding: 0;
  max-width: none;
}}
body.present .slide {{
  display: none;
  min-height: 100vh;
  padding: 48px 64px;
  align-items: flex-start;
  justify-content: center;
  flex-direction: column;
}}
body.present .slide.active {{
  display: flex;
}}
body.present .slide h1,
body.present .slide h2 {{
  font-size: 36px;
}}
body.present .mermaid-container {{
  max-height: 70vh;
  overflow: auto;
}}

/* Slide counter */
.slide-counter {{
  display: none;
  position: fixed;
  bottom: 16px;
  right: 16px;
  background: var(--bg-sidebar);
  border: 1px solid var(--border);
  padding: 4px 12px;
  border-radius: 4px;
  font-size: 13px;
  z-index: 200;
}}
body.present .slide-counter {{ display: block; }}
</style>
</head>
<body>
<button class="menu-toggle" onclick="document.querySelector('.sidebar').classList.toggle('open')">&#9776;</button>
<div class="mode-badge" onclick="togglePresent()" title="Press P to toggle">Read</div>
<div class="slide-counter"></div>

<nav class="sidebar">
  <h3>Wavebreak</h3>
  {sidebar_html}
</nav>

<main class="main">
{body}
</main>

<script>
// Mermaid init
mermaid.initialize({{
  startOnLoad: true,
  theme: window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'default',
  flowchart: {{ curve: 'basis' }},
  securityLevel: 'loose'
}});

// Active nav tracking
const sections = document.querySelectorAll('.slide');
const navLinks = document.querySelectorAll('.nav-link');
const observer = new IntersectionObserver((entries) => {{
  entries.forEach(entry => {{
    if (entry.isIntersecting) {{
      navLinks.forEach(l => l.classList.remove('active'));
      const link = document.querySelector(`.nav-link[data-section="${{entry.target.id}}"]`);
      if (link) link.classList.add('active');
    }}
  }});
}}, {{ threshold: 0.3 }});
sections.forEach(s => observer.observe(s));

// Present mode
let currentSlide = 0;
function togglePresent() {{
  document.body.classList.toggle('present');
  const badge = document.querySelector('.mode-badge');
  if (document.body.classList.contains('present')) {{
    badge.textContent = 'Present';
    showSlide(0);
  }} else {{
    badge.textContent = 'Read';
  }}
}}
function showSlide(n) {{
  const slides = document.querySelectorAll('.slide');
  if (n < 0 || n >= slides.length) return;
  currentSlide = n;
  slides.forEach(s => s.classList.remove('active'));
  slides[n].classList.add('active');
  document.querySelector('.slide-counter').textContent = `${{n + 1}} / ${{slides.length}}`;
}}
document.addEventListener('keydown', (e) => {{
  if (e.key === 'p' || e.key === 'P') {{ togglePresent(); return; }}
  if (!document.body.classList.contains('present')) return;
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') showSlide(currentSlide + 1);
  if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') showSlide(currentSlide - 1);
  if (e.key === 'Escape') togglePresent();
}});
</script>
</body>
</html>"""


if __name__ == "__main__":
    md_text = MD_PATH.read_text(encoding="utf-8")
    html_output = generate_html(md_text)
    OUT_PATH.write_text(html_output, encoding="utf-8")
    print(f"Generated {OUT_PATH} ({len(html_output):,} bytes)")
