"""Shared MDX conversion primitives for the Reddit for Developers migration.

The source is a Docusaurus 3 site (developers.reddit.com/docs) whose published
version is 0.14. Every page is mirrored from the live `.md` endpoints into
`.crawl/mirror/` and rewritten here. See scripts/migrate.py for the driver.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Tag allow-list.
#
# MDX runs every non-fenced line through acorn, so `<Foo>` in prose becomes a
# JSX parse error and `<YOUR_SUBREDDIT_NAME>` becomes an unknown component.
# Only names in this set are treated as markup; every other `<` is escaped.
# ---------------------------------------------------------------------------
HTML_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "button", "caption", "center", "code",
    "col", "colgroup", "dd", "del", "details", "div", "dl", "dt", "em",
    "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i",
    "iframe", "img", "input", "ins", "kbd", "li", "mark", "ol", "p", "path",
    "picture", "pre", "q", "s", "samp", "section", "small", "source", "span",
    "strong", "sub", "summary", "sup", "svg", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "u", "ul", "var", "video",
}

MINTLIFY_COMPONENTS = {
    "Accordion", "AccordionGroup", "Card", "CardGroup", "Check", "CodeGroup",
    "Columns", "Danger", "Expandable", "Frame", "Icon", "Info", "Note",
    "ParamField", "RequestExample", "ResponseExample", "ResponseField", "Step",
    "Steps", "Tab", "Tabs", "Tip", "Tooltip", "Update", "Visibility", "Warning",
}

KNOWN_TAGS = HTML_TAGS | MINTLIFY_COMPONENTS
VOID_TAGS = {"br", "col", "hr", "img", "input", "source"}

TAG_START = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9._-]*)")
FENCE_OPEN = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
FENCE_CLOSE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*$")


def find_tag_end(text: str, start: int) -> int:
    """Return the index just past the `>` closing the tag starting at `start`.

    Tracks quote state and JSX brace depth so `style={{ a: '>' }}` does not end
    the tag early. Returns -1 when the tag never closes.
    """
    i = start + 1
    depth = 0
    quote = ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'" and depth > 0:
            quote = ch
        elif ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == ">" and depth == 0:
            return i + 1
        i += 1
    return -1


class Protector:
    """Swaps regions out for sentinels so text transforms cannot corrupt them."""

    def __init__(self, token: str = "MDXSEG") -> None:
        self.token = token
        self.store: list[str] = []

    def _stash(self, chunk: str) -> str:
        self.store.append(chunk)
        return f"\x00{self.token}{len(self.store) - 1}\x00"

    def protect_code(self, text: str) -> str:
        """Protect fenced blocks first, then inline code spans.

        Fences are matched line-by-line per CommonMark rather than with a regex:
        the source indents fences inside list items, and a closing fence may use
        different indentation from its opener, so indentation-sensitive matching
        pairs the wrong fences and swallows real markup as code.
        """
        lines = text.split("\n")
        out: list[str] = []
        block: list[str] | None = None
        marker = ""
        for line in lines:
            if block is None:
                opener = FENCE_OPEN.match(line)
                if opener:
                    marker = opener.group(1)
                    block = [line]
                else:
                    out.append(line)
                continue
            block.append(line)
            closer = FENCE_CLOSE.match(line)
            if closer and closer.group(1)[0] == marker[0] and len(closer.group(1)) >= len(marker):
                out.append(self._stash("\n".join(block)))
                block = None
        if block is not None:
            out.append(self._stash("\n".join(block)))
        text = "\n".join(out)
        return re.sub(r"(`+)(?:(?!\1).)+?\1", lambda m: self._stash(m.group(0)), text)

    def protect_comments(self, text: str) -> str:
        """Protect MDX expression comments so brace escaping leaves them alone."""
        return re.sub(
            r"\{\s*/\*[\s\S]*?\*/\s*\}", lambda m: self._stash(m.group(0)), text
        )

    def protect_markup(self, text: str) -> str:
        """Protect every well-formed known tag; escape everything else.

        A `<h3>` that never closes is prose (TypeDoc writes "such as <h1>, <h2>,
        <h3>"), not markup, and acorn rejects it. So an opening tag only counts
        as markup when it is void, self-closing, or actually closed later.
        """
        out: list[str] = []
        i = 0
        while True:
            m = TAG_START.search(text, i)
            if not m:
                out.append(text[i:])
                break
            out.append(text[i : m.start()])
            if m.start() > 0 and text[m.start() - 1] == "\\":
                # Already a markdown escape (`\<T\>` from TypeDoc generics):
                # leave it alone or it becomes a visible `\&lt;`.
                out.append("<")
                i = m.start() + 1
                continue
            closing, name = m.group(1), m.group(2)
            end = find_tag_end(text, m.start())
            if name in KNOWN_TAGS and end != -1 and (
                closing
                or name in VOID_TAGS
                or text[end - 2 : end] == "/>"
                or f"</{name}" in text[end:]
            ):
                out.append(self._stash(text[m.start() : end]))
                i = end
            else:
                out.append("&lt;")
                i = m.start() + 1
        return "".join(out)

    def restore(self, text: str) -> str:
        pattern = re.compile(rf"\x00{self.token}(\d+)\x00")
        # Nested sentinels are possible (inline code inside a protected tag).
        for _ in range(8):
            new = pattern.sub(lambda m: self.store[int(m.group(1))], text)
            if new == text:
                break
            text = new
        return text


# ---------------------------------------------------------------------------
# Docusaurus -> Mintlify block conversions
# ---------------------------------------------------------------------------

ADMONITION_MAP = {
    "note": "Note",
    "tip": "Tip",
    "info": "Info",
    "warning": "Warning",
    "caution": "Warning",
    "danger": "Warning",
    "important": "Info",
}


def convert_admonitions(text: str) -> str:
    """`:::warning Title` ... `:::` -> `<Warning>` ... `</Warning>`."""
    lines = text.split("\n")
    out: list[str] = []
    stack: list[str] = []
    for line in lines:
        opener = re.match(r"^\s*:::(\w+)(?:\[[^\]]*\])?\s*(.*?)\s*$", line)
        closer = re.match(r"^\s*:::\s*$", line)
        if opener and opener.group(1).lower() in ADMONITION_MAP:
            kind = ADMONITION_MAP[opener.group(1).lower()]
            title = opener.group(2).strip()
            stack.append(kind)
            out.append("")
            if title:
                out.append(f'<{kind} title="{title.replace(chr(34), chr(39))}">')
            else:
                out.append(f"<{kind}>")
            out.append("")
        elif closer and stack:
            kind = stack.pop()
            out.append("")
            out.append(f"</{kind}>")
            out.append("")
        else:
            out.append(line)
    while stack:
        out.append(f"</{stack.pop()}>")
    return "\n".join(out)


def convert_tabs(text: str) -> str:
    """Docusaurus `<Tabs>`/`<TabItem>` -> Mintlify `<Tabs>`/`<Tab>`.

    Docusaurus lets the human-readable labels live on the parent in
    `values={[{ label: 'Hono', value: 'hono' }]}` while each `<TabItem>` carries
    only `value`. Collect that map per `<Tabs>` block so the Mintlify tab titles
    read "Hono"/"Express" rather than the raw slugs.
    """

    def convert_block(m: re.Match[str]) -> str:
        open_tag, body = m.group(1), m.group(2)
        labels = dict(
            (value, label)
            for label, value in re.findall(
                r"label:\s*['\"]([^'\"]*)['\"]\s*,\s*value:\s*['\"]([^'\"]*)['\"]",
                open_tag,
            )
        )
        labels.update(
            (value, label)
            for value, label in re.findall(
                r"value:\s*['\"]([^'\"]*)['\"]\s*,\s*label:\s*['\"]([^'\"]*)['\"]",
                open_tag,
            )
        )

        def tab_open(item: re.Match[str]) -> str:
            attrs = item.group(1)
            label = re.search(r'label=["\']([^"\']*)["\']', attrs)
            value = re.search(r'value=["\']([^"\']*)["\']', attrs)
            title = (
                label.group(1)
                if label
                else labels.get(value.group(1) if value else "", "")
                or (value.group(1) if value else "Tab")
            )
            return f'<Tab title="{title.replace(chr(34), chr(39))}">'

        body = re.sub(r"<TabItem([^>]*?)>", tab_open, body)
        body = body.replace("</TabItem>", "</Tab>")
        return f"<Tabs>{body}</Tabs>"

    text = re.sub(r"<Tabs([^>]*?)>([\s\S]*?)</Tabs>", convert_block, text)
    # Any stragglers outside a matched block.
    text = re.sub(r"<Tabs[^>]*?>", "<Tabs>", text)
    text = re.sub(r"<TabItem[^>]*?>", "<Tab>", text)
    return text.replace("</TabItem>", "</Tab>")


def convert_details(text: str) -> str:
    """`<details><summary>X</summary>...</details>` -> `<Accordion title="X">`."""

    def repl(m: re.Match[str]) -> str:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip().replace('"', "'")
        body = m.group(2).strip("\n")
        return f'\n<Accordion title="{title}">\n\n{body}\n\n</Accordion>\n'

    return re.sub(
        r"<details[^>]*>\s*<summary[^>]*>([\s\S]*?)</summary>([\s\S]*?)</details>",
        repl,
        text,
    )


def normalize_code_fences(text: str) -> str:
    """Normalize Docusaurus fence metadata for Mintlify.

    Mintlify accepts `title="..."` natively, so titles pass through; this only
    repairs the source's unterminated quotes, quotes bare titles, maps the
    `prism-code` pseudo-language, and translates `showLineNumbers`.
    """

    def repl(m: re.Match[str]) -> str:
        indent, fence, lang, meta = (
            m.group(1), m.group(2), m.group(3) or "", (m.group(4) or "").strip()
        )
        if lang == "prism-code":
            lang = "text"
        if not meta:
            return f"{indent}{fence}{lang}"
        meta = meta.replace("showLineNumbers", "lines")
        title = re.search(r"""title=(?:"([^"]*)"?|'([^']*)'?|(\S+))""", meta)
        if title:
            value = next(g for g in title.groups() if g is not None)
            meta = (meta[: title.start()] + meta[title.end() :]).strip()
            meta = (f'title="{value}" ' + meta).strip()
        return f"{indent}{fence}{lang} {meta}".rstrip()

    return re.sub(
        r"^([ \t]{0,3})(`{3,})([\w+-]*)[ \t]*([^\n]*)$", repl, text, flags=re.M
    )


def strip_imports(text: str) -> str:
    """Remove Docusaurus/MDX import + export statements."""
    text = re.sub(r"^import\s+[\s\S]*?from\s+['\"][^'\"]+['\"];?\s*$", "", text, flags=re.M)
    text = re.sub(r"^import\s+['\"][^'\"]+['\"];?\s*$", "", text, flags=re.M)
    text = re.sub(r"^export\s+(const|default|function)[^\n]*$", "", text, flags=re.M)
    return text


def repair_overlong_fences(text: str) -> str:
    """Repair source fences opened with 4+ backticks but closed with 3.

    Two source pages (capabilities/server/cache-helper, capabilities/client/
    menu-actions) open a tab's code sample with ```` and close it with ```, so
    the surrounding `</TabItem>` / `</Tabs>` tags get swallowed into the code
    block. Demote the opener to the width the author actually closed with, and
    drop the now-orphaned wider closer.
    """
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        opener = FENCE_OPEN.match(lines[i])
        if not opener or len(opener.group(1)) < 4 or opener.group(1)[0] != "`":
            i += 1
            continue
        width = len(opener.group(1))
        inner = outer = None
        for j in range(i + 1, len(lines)):
            closer = FENCE_CLOSE.match(lines[j])
            if not closer or closer.group(1)[0] != "`":
                continue
            if len(closer.group(1)) >= width:
                outer = j
                break
            if inner is None:
                inner = j
        if inner is None or outer is None:
            i += 1
            continue
        tail = [ln.strip() for ln in lines[inner + 1 : outer] if ln.strip()]
        if tail and all(ln.startswith("<") and ln.endswith(">") for ln in tail):
            lines[i] = lines[i].replace("`" * width, "```", 1)
            del lines[outer]
            i = inner
            continue
        i += 1
    return "\n".join(lines)


def convert_html_comments(text: str) -> str:
    """MDX has no HTML comments; drop them rather than leaking `<!--` into acorn."""
    return re.sub(r"<!--[\s\S]*?-->", "", text)


def html_hygiene(text: str) -> str:
    """Make raw HTML JSX-safe: self-close voids, camelCase the DOM attributes."""
    for void in ("br", "hr", "img", "input", "source", "col"):
        text = re.sub(
            rf"<{void}((?:\s[^<>]*?)?)(?<!/)>",
            lambda m, v=void: f"<{v}{m.group(1).rstrip()} />",
            text,
        )
    replacements = {
        "frameborder": "frameBorder",
        "allowfullscreen": "allowFullScreen",
        "referrerpolicy": "referrerPolicy",
        "crossorigin": "crossOrigin",
        "srcset": "srcSet",
        "tabindex": "tabIndex",
        "maxlength": "maxLength",
        "autoplay": "autoPlay",
        "playsinline": "playsInline",
        "class=": "className=",
        "colspan": "colSpan",
        "rowspan": "rowSpan",
        "stroke-width": "strokeWidth",
        "stroke-linecap": "strokeLinecap",
        "stroke-linejoin": "strokeLinejoin",
        "fill-rule": "fillRule",
        "clip-rule": "clipRule",
    }

    def fix_tag(m: re.Match[str]) -> str:
        tag = m.group(0)
        for k, v in replacements.items():
            tag = re.sub(rf"(?<=[\s])({re.escape(k)})(?![A-Za-z])", v, tag)
        # Boolean attributes must carry a JSX value.
        tag = re.sub(r"\ballowFullScreen(?![=\w])", "allowFullScreen={true}", tag)
        tag = re.sub(r"\bautoPlay(?![=\w])", "autoPlay={true}", tag)
        tag = re.sub(r"\bplaysInline(?![=\w])", "playsInline={true}", tag)
        return tag

    text = re.sub(r"<[A-Za-z][^<>]*>", fix_tag, text)
    return text


def escape_braces(text: str) -> str:
    """Escape curly braces left in prose after markup and code are protected.

    TypeDoc writes inline object types with markdown-escaped braces
    (`\\{ domains: string[]; \\}`). Escaping those again yields `\\&#123;`, which
    renders with a visible stray backslash, so consume the existing escape.
    """
    text = text.replace("\\{", "&#123;").replace("\\}", "&#125;")
    return text.replace("{", "&#123;").replace("}", "&#125;")


def normalize_blocks(text: str) -> str:
    """Guarantee block boundaries around fences and component tags."""
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)
    # Collapse runs of horizontal rules (Pattern H).
    text = re.sub(r"(?:^(?:---|\*\*\*)\s*\n\s*\n){2,}", "***\n\n", text, flags=re.M)
    return text.strip() + "\n"


def convert_markdown(text: str, rewrite_images=None, rewrite_links=None) -> str:
    """Docusaurus markdown body -> Mintlify MDX body.

    Order matters: code spans and MDX comments are protected before any text
    transform runs, so a `<img src="/logo.png" />` example inside backticks and a
    `{/* commented-out image */}` block both survive untouched.
    """
    text = convert_html_comments(text)
    text = repair_overlong_fences(text)
    text = normalize_code_fences(text)

    prot = Protector()
    text = prot.protect_code(text)
    text = prot.protect_comments(text)

    text = convert_admonitions(text)
    text = convert_details(text)
    text = convert_tabs(text)
    text = html_hygiene(text)
    if rewrite_images:
        text = rewrite_images(text)
    if rewrite_links:
        text = rewrite_links(text)

    text = prot.protect_markup(text)
    text = escape_braces(text)
    text = prot.restore(text)
    return normalize_blocks(text)


def yaml_quote(value: str) -> str:
    value = value.replace("\n", " ").strip()
    value = value.replace('"', "'")
    return f'"{value}"'


def jsx_attr(name: str, value: str) -> str:
    """Render a JSX string prop, falling back to an expression when quoting bites.

    Source titles contain both quote characters ("Skyboard Wins Reddit's "Silly
    Sh!t Challenge""), so a quoted attribute cannot hold them verbatim. A JSON
    string inside a JSX expression always can.
    """
    import json

    value = value.replace("\n", " ").strip()
    if '"' not in value:
        return f'{name}="{value}"'
    return f"{name}={{{json.dumps(value)}}}"
