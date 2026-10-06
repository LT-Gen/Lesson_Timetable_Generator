# LT-Gen — генератор розкладу уроків для української школи.
# Copyright (C) 2026  Rogala <https://github.com/Rogala>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Мінімальний рендерер підмножини Markdown для вбудованої довідки (тека data/man/).

Свідомо БЕЗ сторонніх бібліотек - тексти довідки наші, тримаємо їх у простій
підмножині, яку покриває цей файл:
  - заголовки  # .. ####
  - абзаци (порожній рядок - роздільник)
  - марковані списки "- " і нумеровані "1. " (+ один рівень вкладення 2-4 пробіли)
  - **жирний**, `код` (інлайн)
  - [текст](посилання)  (http/https -> нова вкладка)
  - > цитата
  - --- горизонтальна лінія
  - ``` огороджені блоки коду
  - прості таблиці з "|" (перший рядок - заголовок, другий - роздільник ---|---)

Увесь текст екранується (html.escape) ПЕРЕД інлайн-розміткою - вставити довільний
HTML із .md-файлу неможливо.
"""

import html
import re

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _inline(text):
    """Інлайн-розмітка. text вже екранований html.escape."""
    # код - першим, щоб зірочки/дужки всередині `..` не чіпались
    out = []
    pos = 0
    for m in _INLINE_CODE.finditer(text):
        out.append(_bold_link(text[pos:m.start()]))
        out.append("<code>" + m.group(1) + "</code>")
        pos = m.end()
    out.append(_bold_link(text[pos:]))
    return "".join(out)


def _bold_link(text):
    text = _BOLD.sub(r"<strong>\1</strong>", text)

    def _lnk(m):
        label, href = m.group(1), m.group(2)
        # href уже екранований; лапки в атрибут не потраплять (html.escape робить &quot;)
        zovnishnye = href.startswith(("http://", "https://"))
        # білий список схем: зовнішнє http(s), внутрішнє "/шлях" або "#якір".
        # Усе інше (насамперед javascript:) віддаємо як звичайний текст - .md-файли
        # довідки може відредагувати будь-хто, у кого є папка застосунку.
        if not (zovnishnye or href.startswith(("/", "#"))):
            return html.escape(m.group(0))
        extra = ' target="_blank" rel="noopener"' if zovnishnye else ""
        return f'<a href="{href}"{extra}>{label}</a>'

    return _LINK.sub(_lnk, text)


def _flush_list(html_parts, stack):
    while stack:
        html_parts.append("</" + stack.pop() + ">")


def render(md_text):
    """Markdown-підмножина -> безпечний HTML-рядок."""
    lines = md_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    list_stack = []        # напр. ["ul"] або ["ul", "ul"]
    para = []
    in_code = False
    code_buf = []
    table_buf = []

    def flush_para():
        if para:
            out.append("<p>" + _inline(html.escape(" ".join(para))) + "</p>")
            para.clear()

    def flush_table():
        if not table_buf:
            return
        rows = [r for r in table_buf if r.strip()]
        table_buf.clear()
        if len(rows) < 2:
            for r in rows:
                para.append(r)
            flush_para()
            return
        def cells(r):
            r = r.strip().strip("|")
            return [c.strip() for c in r.split("|")]
        head = cells(rows[0])
        body = [cells(r) for r in rows[2:]]
        out.append('<div class="table-wrap"><table>')
        out.append("<tr>" + "".join("<th>" + _inline(html.escape(c)) + "</th>" for c in head) + "</tr>")
        for br in body:
            out.append("<tr>" + "".join("<td>" + _inline(html.escape(c)) + "</td>" for c in br) + "</tr>")
        out.append("</table></div>")

    for raw in lines:
        line = raw.rstrip()

        # огороджений блок коду
        if line.strip().startswith("```"):
            if in_code:
                out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")
                code_buf = []
                in_code = False
            else:
                flush_para(); flush_table(); _flush_list(out, list_stack)
                in_code = True
            continue
        if in_code:
            code_buf.append(raw)
            continue

        # таблиця (рядок містить | і ми або вже в таблиці, або наступний рядок -роздільник)
        if "|" in line and line.strip().startswith("|"):
            flush_para(); _flush_list(out, list_stack)
            table_buf.append(line)
            continue
        else:
            flush_table()

        stripped = line.strip()

        if not stripped:
            flush_para(); _flush_list(out, list_stack)
            continue

        if stripped in ("---", "***", "___"):
            flush_para(); _flush_list(out, list_stack)
            out.append("<hr>")
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            flush_para(); _flush_list(out, list_stack)
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>" + _inline(html.escape(m.group(2).strip())) + f"</h{lvl}>")
            continue

        if stripped.startswith("> "):
            flush_para(); _flush_list(out, list_stack)
            out.append("<blockquote>" + _inline(html.escape(stripped[2:])) + "</blockquote>")
            continue

        m = re.match(r"^(\s*)([-*]|\d+[.)])\s+(.*)$", line)
        if m:
            flush_para()
            indent = len(m.group(1))
            kind = "ol" if m.group(2)[0].isdigit() else "ul"
            depth = 1 if indent >= 2 else 0
            # вирівнюємо стек списків під глибину
            while len(list_stack) > depth + 1:
                out.append("</" + list_stack.pop() + ">")
            if len(list_stack) == depth + 1 and list_stack[-1] != kind:
                out.append("</" + list_stack.pop() + ">")
            while len(list_stack) < depth + 1:
                list_stack.append(kind)
                out.append("<" + kind + ">")
            out.append("<li>" + _inline(html.escape(m.group(3).strip())) + "</li>")
            continue

        # рядок з відступом усередині списку й без маркера - продовження
        # попереднього пункту (переноси рядків у .md-файлі не мають рвати <li>)
        if list_stack and raw[:1] in (" ", "\t") and out and out[-1].endswith("</li>"):
            out[-1] = out[-1][:-len("</li>")] + " " + _inline(html.escape(stripped)) + "</li>"
            continue

        # звичайний текст -> абзац
        _flush_list(out, list_stack)
        para.append(stripped)

    if in_code:
        out.append("<pre><code>" + html.escape("\n".join(code_buf)) + "</code></pre>")
    flush_para()
    flush_table()
    _flush_list(out, list_stack)
    return "\n".join(out)


def pershyj_zagolovok(md_text):
    """Перший рядок '# ...' з файлу - для <title>/<h1> сторінки довідки."""
    for raw in md_text.replace("\r\n", "\n").split("\n"):
        s = raw.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return "Довідка"
