#!/usr/bin/env python3
"""Build the Reddit Ads API preview from a crawl of ads-api.reddit.com/docs.

The Ads API site is Docusaurus + docusaurus-plugin-openapi-docs. Two things
follow from that:

* Every endpoint page carries its complete OpenAPI operation in the page's
  `api` frontmatter, base64 + zlib compressed. Decompressing those gives a
  fully resolved spec for all 105 operations, which Mintlify turns into
  endpoint pages with a real interactive playground. That is far more faithful
  than scraping the rendered, partly collapsed schema UI.
* Narrative pages have no raw `.md` endpoint, so they are converted from the
  rendered article DOM via scripts/ads_html_to_md.mjs.

Inputs (produced by .crawl/ads/crawl.mjs):
  .crawl/ads/urls.txt          the 206 sitemap URLs
  .crawl/ads/mirror/**.json    per-page capture: title, h1, sidebar label, html
  .crawl/ads/chunks/**.js      raw route chunks, holding the `api` frontmatter

Outputs:
  openapi/reddit-ads-api-v3.json
  ads-api/**.mdx
  ads-api-parity-manifest.json
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_mdx import convert_markdown, yaml_quote  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CRAWL = REPO / ".crawl" / "ads"
MIRROR = CRAWL / "mirror"
CHUNKS = CRAWL / "chunks"
OUT = REPO / "ads-api"
SPEC = REPO / "openapi" / "reddit-ads-api-v3.json"
SOURCE = "https://ads-api.reddit.com/docs"

API_BLOB = re.compile(r'"?api"?:\s*"([A-Za-z0-9+/=]{200,})"')

# Narrative pages worth migrating, mapped to their preview path. Everything
# else in the sitemap is a generated index (see EXCLUSIONS).
PAGES = {
    # The source sidebar labels this page "Introduction" under its API section,
    # while the page's own title is "Reddit Advertising API".
    "v3/api/reddit-advertising-api": ("ads-api/api/introduction", "Introduction"),
    "v3/history": ("ads-api/whats-new", ""),
}

# Pages kept as hand-written MDX. The generator still maps their slug so links
# resolve and still records them in the parity manifest, but it never writes
# over them.
HAND_AUTHORED = {
    "index": (
        "ads-api",
        "Run ads with\u00a0Reddit",
        "hand-built replica of the source landing page: same section order, "
        "copy, brand tokens and chrome, with the notice bar, hero, cards, help "
        "and Ads Formula bands, and the version and More menus rebuilt as "
        "CSS-only disclosure menus",
    ),
}

GUIDE_PREFIX = "v3/guides/"

# Source URLs that are generated navigation rather than content.
EXCLUSIONS = {
    "v3/guides/programs": "generated guide category index; Mintlify renders the same grouping as sidebar groups",
    "v3/api": "generated API category index",
    "blog": "blog index route, replaced by the consolidated /ads-api/blog Update timeline",
    "blog/archive": "generated blog archive route, replaced by the consolidated timeline",
    "blog/tags": "generated tag index route; tags are preserved as Update tag filters",
    "erd-diagram": "entity-relationship diagram rendered as an interactive canvas with no server-side content to convert",
}

# Program guide areas, in the order the source sidebar lists them, with the
# labels the source uses for each category.
PROGRAM_GROUPS = [
    ("Campaign Management", "campaign"),
    ("Conversions API", "capi"),
    ("Reporting", "reporting"),
    ("Data Deletion", "data-deletion"),
    ("Product Catalogs", "product-catalogs"),
    ("Targeting", "targeting"),
    ("Business Management", "business"),
]


def load_captures() -> dict[str, dict]:
    out = {}
    for path in MIRROR.rglob("*.json"):
        rec = json.loads(path.read_text(encoding="utf-8"))
        out[rec["slug"]] = rec
    return out


# ---------------------------------------------------------------------------
# OpenAPI spec
# ---------------------------------------------------------------------------

def decode_operation(slug: str) -> dict | None:
    chunk = CHUNKS / f"{slug}.js"
    if not chunk.exists():
        return None
    m = API_BLOB.search(chunk.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None
    blob = m.group(1)
    try:
        raw = zlib.decompress(base64.b64decode(blob + "=" * (-len(blob) % 4)))
    except Exception:
        return None
    return json.loads(raw)


RATE_LIMIT_SPLIT = re.compile(r"\n?\s*<h2>\s*Rate Limit\s*</h2>", re.I)


def clean_description(text: str, path_map: dict[str, str]) -> str:
    """Turn an operation description back into plain markdown.

    Each description is markdown prose followed by a block of Stoplight
    Elements HTML -- an `<h2>Rate Limit</h2>` heading, a link to the rate limit
    table, and a `<details>` panel with the policy slug, window, and quota,
    complete with an inline chevron SVG. Mintlify renders that verbatim, so the
    prose is kept and the rate limit panel is rebuilt as a short markdown list.
    """
    parts = RATE_LIMIT_SPLIT.split(text, maxsplit=1)
    prose = parts[0]
    tail = parts[1] if len(parts) > 1 else ""

    out = prose.strip()

    if tail:
        flat = re.sub(r"<[^>]+>", " ", tail)
        flat = re.sub(r"\s+", " ", flat)
        policy = re.search(r"See the rate limits for\s+([A-Za-z0-9 &/-]+?)\s+Policy Slug", flat)
        slug = re.search(r"Policy Slug:\s*([a-z0-9-]+)", flat)
        window = re.search(r"Window:\s*([^:]*?seconds)", flat)
        quota = re.search(r"Quota:\s*([0-9,]+\s*requests)", flat)
        lines = ["", "## Rate Limit", ""]
        if policy:
            name = policy.group(1).strip()
            anchor = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            lines.append(
                f"See the rate limits for "
                f"[{name}](/ads-api/guides/quick-start/rate-limiting#{anchor})."
            )
            lines.append("")
        for label, hit in (("Policy slug", slug), ("Window", window), ("Quota", quota)):
            if hit:
                value = hit.group(1).strip()
                value = f"`{value}`" if label == "Policy slug" else value
                lines.append(f"- **{label}:** {value}")
        out = out + "\n" + "\n".join(lines)

    # Point the source's own doc links at the preview, and drop any stray tags.
    out = rewrite_links(out, path_map)
    out = re.sub(r"<(?!/?(?:code|strong|em|b|i|br)\b)[^>]+>", "", out)
    return out.strip()


def build_spec(captures: dict[str, dict], path_map: dict[str, str]) -> tuple[dict, list[dict]]:
    """Reassemble one OpenAPI document from the per-page operation objects."""
    operations: list[tuple[str, dict]] = []
    for slug, rec in sorted(captures.items()):
        if not rec.get("api"):
            continue
        op = decode_operation(slug)
        if op:
            operations.append((slug, op))

    if not operations:
        raise SystemExit("no operations decoded; re-run .crawl/ads/crawl.mjs")

    first = operations[0][1]
    spec: dict = {
        # The source's schemas are JSON Schema 2020-12 style -- nullability as
        # `type: [..., "null"]` and `examples` arrays -- so the document has to
        # declare 3.1, not 3.0.
        "openapi": "3.1.0",
        "info": {k: v for k, v in first["info"].items() if not k.startswith("x-")},
        "servers": first["servers"],
        "tags": [],
        "paths": {},
        "components": {"securitySchemes": first["securitySchemes"]},
    }

    # Tag descriptions come from the source's own API category pages.
    tag_descriptions: dict[str, str] = {}
    for slug, rec in captures.items():
        if not slug.startswith("v3/api/") or rec.get("api"):
            continue
        text = re.sub(r"<[^>]+>", " ", rec.get("html", ""))
        text = re.sub(r"\s+", " ", text).strip()
        label = rec.get("h1", "").strip()
        if not label:
            continue
        # Drop the breadcrumb and version badge the capture includes ahead of
        # the description, e.g. "API Campaigns Version: v3 Campaigns <desc>".
        text = re.sub(r"^\s*API\s+", "", text)
        text = re.sub(r"Version:\s*v\d+\s*", "", text)
        text = re.sub(r"^\s*" + re.escape(label) + r"\s*", "", text)
        m = re.match(r"\s*" + re.escape(label) + r"\s+(.{10,200}?\.)(?:\s|$)", text)
        if not m:
            m = re.match(r"\s*(.{10,200}?\.)(?:\s|$)", text)
        if m:
            tag_descriptions[label] = m.group(1).strip()

    seen_tags: list[str] = []
    rows: list[dict] = []
    for slug, op in operations:
        path = op["path"]
        method = op["method"].lower()
        entry = {
            "operationId": op["operationId"],
            "summary": captures[slug].get("h1") or op["operationId"],
            "description": clean_description(op.get("description", ""), path_map),
            "tags": op.get("tags", []),
            "responses": op["responses"],
        }
        if op.get("parameters"):
            entry["parameters"] = op["parameters"]
        if op.get("requestBody"):
            entry["requestBody"] = op["requestBody"]
        if op.get("security"):
            entry["security"] = op["security"]
        spec["paths"].setdefault(path, {})[method] = entry
        for tag in entry["tags"]:
            if tag not in seen_tags:
                seen_tags.append(tag)
        rows.append({
            "source_url": f"{SOURCE}/{slug}",
            "source_title": entry["summary"],
            "normalized_path": f"/ads-api/reference/{op['operationId']}",
            "converted_file": "openapi/reddit-ads-api-v3.json",
            "status": "done",
            "notes": f"{method.upper()} {path} generated by Mintlify from the "
                     f"reconstructed OpenAPI document",
        })

    spec["tags"] = [
        {"name": t, **({"description": tag_descriptions[t]} if t in tag_descriptions else {})}
        for t in sorted(seen_tags, key=str.lower)
    ]
    spec["paths"] = order_paths_by_tag(spec["paths"])
    sanitize_descriptions(spec, path_map)
    hoist_shared_schemas(spec)
    return spec, rows


def order_paths_by_tag(paths: dict[str, dict]) -> dict[str, dict]:
    """Group the paths by tag, with the tags in alphabetical order.

    Mintlify builds one sidebar group per tag and orders those groups by the
    order the tags first appear in `paths`, not by the `tags` array. The source
    sidebar lists its categories alphabetically, so the paths have to be
    emitted that way. Operations keep their order within a tag: the source
    doesn't sort those either, it lists them the way its spec does.
    """
    def tag_of(ops: dict) -> str:
        for op in ops.values():
            if isinstance(op, dict) and op.get("tags"):
                return op["tags"][0]
        return ""

    return {
        path: ops
        for path, ops in sorted(
            paths.items(), key=lambda kv: tag_of(kv[1]).lower())
    }


def sanitize_descriptions(node, path_map: dict[str, str]) -> None:
    """Strip leftover HTML from every description and repoint doc links.

    Beyond the rate limit panels, a handful of field descriptions carry empty
    `<span id>` anchors and the info block wraps its terms in a `<details>`,
    none of which mean anything once the content is rendered by Mintlify.
    """
    if isinstance(node, list):
        for item in node:
            sanitize_descriptions(item, path_map)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key == "description" and isinstance(value, str):
            text = re.sub(r"<span\b[^>]*>\s*</span>", "", value)
            text = re.sub(r"</?(?:details|summary|div)\b[^>]*>", "", text)
            node[key] = rewrite_links(text, path_map).strip()
        else:
            sanitize_descriptions(value, path_map)


def hoist_shared_schemas(spec: dict) -> None:
    """Re-introduce `$ref`s for schemas the source repeats across operations.

    The plugin bakes a fully dereferenced schema into every page, so the
    reassembled document repeats the Campaign, Ad, Product and error shapes
    dozens of times -- 3.9 MB in total, enough to run `mint dev` out of heap
    while it expands every endpoint page. Identical titled schemas are hoisted
    into `components.schemas` and referenced, which is both what the upstream
    spec almost certainly looks like and small enough to build.
    """
    counts: dict[tuple[str, str], int] = {}

    def survey(node) -> None:
        if isinstance(node, list):
            for item in node:
                survey(item)
            return
        if not isinstance(node, dict):
            return
        title = node.get("title")
        if isinstance(title, str) and ("properties" in node or "enum" in node):
            key = (title, json.dumps(node, sort_keys=True))
            counts[key] = counts.get(key, 0) + 1
        for value in node.values():
            survey(value)

    survey(spec["paths"])

    # Only hoist repeated shapes; a schema used once is clearer inline.
    names: dict[str, str] = {}
    schemas: dict[str, dict] = {}
    for (title, blob), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n < 2:
            continue
        name = re.sub(r"[^A-Za-z0-9._-]", "_", title)
        candidate, i = name, 2
        while candidate in schemas and schemas[candidate] != json.loads(blob):
            candidate, i = f"{name}_{i}", i + 1
        schemas[candidate] = json.loads(blob)
        names[blob] = candidate

    if not schemas:
        return

    def replace(node):
        if isinstance(node, list):
            return [replace(i) for i in node]
        if not isinstance(node, dict):
            return node
        blob = json.dumps(node, sort_keys=True)
        if blob in names:
            return {"$ref": f"#/components/schemas/{names[blob]}"}
        return {k: replace(v) for k, v in node.items()}

    spec["paths"] = replace(spec["paths"])
    # Hoisted schemas may themselves contain other hoisted shapes.
    spec["components"]["schemas"] = {
        name: {k: replace(v) for k, v in body.items()}
        for name, body in schemas.items()
    }


# ---------------------------------------------------------------------------
# Narrative pages
# ---------------------------------------------------------------------------

def article_html(rec: dict) -> str:
    """Trim Docusaurus page chrome down to the markdown container."""
    html = rec.get("html", "")
    m = re.search(r'<div class="theme-doc-markdown[^"]*"[^>]*>', html)
    if m:
        html = html[m.end():]
    html = re.split(r'<footer class="[^"]*theme-doc-footer', html)[0]
    html = re.split(r'<nav class="[^"]*pagination-nav', html)[0]
    # The H1 becomes frontmatter `title`.
    html = re.sub(r"<header>[\s\S]*?</header>", "", html, count=1)
    return html


def html_to_markdown(pages: dict[str, str]) -> dict[str, str]:
    src = CRAWL / "html-in.json"
    dst = CRAWL / "md-out.json"
    src.write_text(json.dumps(pages), encoding="utf-8")
    subprocess.run(
        ["node", str(Path(__file__).resolve().parent / "ads_html_to_md.mjs"),
         str(src), str(dst)],
        check=True)
    return json.loads(dst.read_text(encoding="utf-8"))


ASSET_RX = re.compile(
    r'(?P<pre>!\[[^\]]*\]\(|img=")(?P<url>(?:https://ads-api\.reddit\.com)?/docs/(?:assets/images|img)/[^)"\s]+)'
)


def localize_assets(text: str, downloaded: dict[str, str]) -> str:
    """Copy referenced Ads API media into the repo instead of hotlinking it."""
    def repl(m: re.Match[str]) -> str:
        url = m.group("url")
        absolute = url if url.startswith("http") else f"https://ads-api.reddit.com{url}"
        if absolute not in downloaded:
            name = re.sub(r"[^A-Za-z0-9._-]", "-", absolute.rsplit("/", 1)[-1])
            dest = REPO / "images" / "ads-api" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                result = subprocess.run(
                    ["curl", "-sSfL", "--max-time", "45", "-o", str(dest), absolute])
                if result.returncode != 0 or dest.stat().st_size == 0:
                    downloaded[absolute] = absolute
                    return m.group(0)
            downloaded[absolute] = f"/images/ads-api/{name}"
        return m.group("pre") + downloaded[absolute]

    return ASSET_RX.sub(repl, text)


def rewrite_links(text: str, path_map: dict[str, str]) -> str:
    """Point source /docs/... links at their preview equivalents."""
    def repl(m: re.Match[str]) -> str:
        label, target = m.group(1), m.group(2)
        frag = ""
        if "#" in target:
            target, frag = target.split("#", 1)
            frag = "#" + frag
        for prefix in (f"{SOURCE}/", "/docs/"):
            if target.startswith(prefix):
                slug = target[len(prefix):].strip("/")
                if slug in path_map:
                    return f"[{label}](/{path_map[slug]}{frag})"
                return f"[{label}]({SOURCE}/{slug}{frag})"
        return m.group(0)

    text = re.sub(r"\[([^\]]*)\]\(([^)\s]+)\)", repl, text)

    # Second pass on the link target alone: some labels contain brackets (for
    # example an inline-code label like `data.events[].click_id`), which the
    # label-aware pattern above cannot span.
    def bare(m: re.Match[str]) -> str:
        target, frag = m.group(1), ""
        if "#" in target:
            target, frag = target.split("#", 1)
            frag = "#" + frag
        for prefix in (f"{SOURCE}/", "/docs/"):
            if target.startswith(prefix):
                slug = target[len(prefix):].strip("/")
                if slug in path_map:
                    return f"](/{path_map[slug]}{frag})"
                return f"]({SOURCE}/{slug}{frag})"
        return m.group(0)

    text = re.sub(r"\]\(((?:https://ads-api\.reddit\.com)?/docs/[^)\s]+)\)", bare, text)

    def attr(m: re.Match[str]) -> str:
        target, frag = m.group(2), ""
        if "#" in target:
            target, frag = target.split("#", 1)
            frag = "#" + frag
        for prefix in (f"{SOURCE}/", "/docs/"):
            if target.startswith(prefix):
                slug = target[len(prefix):].strip("/")
                if slug in path_map:
                    return f'{m.group(1)}"/{path_map[slug]}{frag}"'
                return f'{m.group(1)}"{SOURCE}/{slug}{frag}"'
        return m.group(0)

    return re.sub(r'(href=)"([^"]+)"', attr, text)


def guide_path(slug: str) -> str:
    return "ads-api/" + slug[len("v3/"):]


CARD_SMASH = re.compile(
    r"\[\s*\n+"
    r"(?:\s*!\[[^\]]*\]\((?P<img>[^)\s]+)\)\s*\n+)?"
    r"\s*#{2,4}\s*(?P<title>[^\n]+?)\s*\n+"
    r"(?P<body>(?:(?!\]\()[^\n]*\n?)*?)\s*\]\((?P<href>[^)\s]+)\)",
    re.M,
)


def fix_card_smash(text: str) -> str:
    """Rebuild the source's card grids, which flatten into one giant link.

    Docusaurus card links render as `<a><h2>Title</h2><p>body</p></a>`. Turned
    into markdown that becomes `[\\n## Title\\n\\nbody\\n](/href)`, which MDX
    renders as one clickable blob (mstack's Pattern A / Pattern I). Collect each
    run of them back into a Mintlify `<Columns>` of `<Card>`s.
    """
    cards: list[tuple[str, str, str, str | None]] = []

    def collect(m: re.Match[str]) -> str:
        title = re.sub(r"\s+", " ", m.group("title")).strip()
        body = re.sub(r"\s+", " ", m.group("body")).strip()
        cards.append((title, body, m.group("href"), m.group("img")))
        return f"\x00CARD{len(cards) - 1}\x00"

    text = CARD_SMASH.sub(collect, text)
    if not cards:
        return text

    # Collapse consecutive placeholders into a single Columns block.
    def render(run: list[int]) -> str:
        items = []
        for i in run:
            title, body, href, img = cards[i]
            title = title.replace('"', "'")
            attrs = f'title="{title}" href="{href}"'
            if img:
                attrs += f' img="{img}"'
            inner = f"\n    {body}\n  " if body else "\n  "
            items.append(f"  <Card {attrs}>{inner}</Card>")
        return "<Columns cols={2}>\n" + "\n".join(items) + "\n</Columns>"

    out: list[str] = []
    run: list[int] = []
    for part in re.split(r"(\x00CARD\d+\x00)", text):
        m = re.fullmatch(r"\x00CARD(\d+)\x00", part)
        if m:
            run.append(int(m.group(1)))
            continue
        if part.strip() == "" and run:
            continue
        if run:
            out.append(render(run))
            run = []
        out.append(part)
    if run:
        out.append(render(run))
    return "\n\n".join(p.strip("\n") for p in out if p.strip()) + "\n"


def main() -> None:
    captures = load_captures()
    print(f"captured pages: {len(captures)}")

    OUT.mkdir(parents=True, exist_ok=True)
    SPEC.parent.mkdir(parents=True, exist_ok=True)

    # path_map is needed before the spec is built so operation descriptions can
    # have their source doc links repointed at the preview.
    pre_targets: dict[str, str] = {slug: dest for slug, (dest, _) in PAGES.items()}
    pre_targets.update({slug: dest for slug, (dest, _, _) in HAND_AUTHORED.items()})
    for slug in captures:
        if slug.startswith(GUIDE_PREFIX) and slug != "v3/guides/programs":
            pre_targets[slug] = guide_path(slug)
    for slug in captures:
        if slug.startswith("blog/") and not slug.startswith("blog/tags") and slug != "blog/archive":
            pre_targets[slug] = "ads-api/blog"
    pre_targets["blog"] = "ads-api/blog"

    spec, api_rows = build_spec(captures, pre_targets)
    SPEC.write_text(json.dumps(spec, indent=1) + "\n", encoding="utf-8")
    print(f"openapi: {len(spec['paths'])} paths, "
          f"{sum(len(v) for v in spec['paths'].values())} operations, "
          f"{len(spec['tags'])} tags, {SPEC.stat().st_size // 1024} KB")

    # Which narrative pages to convert, and where they land.
    targets: dict[str, tuple[str, str]] = dict(PAGES)
    for slug, rec in captures.items():
        if slug.startswith(GUIDE_PREFIX) and slug != "v3/guides/programs":
            targets[slug] = (guide_path(slug), rec.get("sidebarLabel") or rec.get("h1", ""))

    blog_slugs = [
        s for s in captures
        if s.startswith("blog/") and not s.startswith("blog/tags") and s != "blog/archive"
    ]

    path_map = {slug: dest for slug, (dest, _) in targets.items()}
    path_map.update({slug: dest for slug, (dest, _, _) in HAND_AUTHORED.items()})
    path_map.update({s: "ads-api/blog" for s in blog_slugs})
    path_map["blog"] = "ads-api/blog"

    html_batch = {slug: article_html(captures[slug]) for slug in list(targets) + blog_slugs}
    markdown = html_to_markdown(html_batch)

    rows: list[dict] = list(api_rows)
    assets: dict[str, str] = {}

    for slug, (dest, sidebar) in sorted(targets.items()):
        rec = captures[slug]
        title = rec.get("h1") or rec["title"].split(" | ")[0]
        body = fix_card_smash(markdown.get(slug, ""))
        body = localize_assets(body, assets)
        body = convert_markdown(rewrite_links(body, path_map))
        # Only the labels in PAGES are used. The crawl could see the active
        # sidebar *category* but not each page's own short label, so guide
        # pages keep `title` as their label rather than getting an invented one.
        fm = [f"title: {yaml_quote(title)}"]
        explicit = PAGES.get(slug, ("", ""))[1]
        if explicit and explicit != title:
            fm.append(f"sidebarTitle: {yaml_quote(explicit)}")
        target = REPO / f"{dest}.mdx"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("---\n" + "\n".join(fm) + "\n---\n\n" + body, encoding="utf-8")
        rows.append({
            "source_url": f"{SOURCE}/{slug}",
            "source_title": title,
            "normalized_path": f"/{dest}",
            "converted_file": f"{dest}.mdx",
            "status": "done",
            "notes": "converted from the rendered article; the source serves no "
                     "raw markdown endpoint",
        })
    print(f"narrative pages written: {len(targets)}")

    for slug, (dest, title, note) in sorted(HAND_AUTHORED.items()):
        rows.append({
            "source_url": f"{SOURCE}/{slug}",
            "source_title": title,
            "normalized_path": f"/{dest}",
            "converted_file": f"{dest}.mdx",
            "status": "done",
            "notes": note,
        })

    # Blog -> one Update timeline, newest first.
    entries = []
    for slug in blog_slugs:
        rec = captures[slug]
        title = rec.get("h1") or rec["title"].split(" | ")[0]
        body = localize_assets(fix_card_smash(markdown.get(slug, "")), assets)
        body = convert_markdown(rewrite_links(body, path_map))
        date = None
        m = re.match(r"blog/(\d{2})-(\d{4})-", slug)
        if m:
            date = f"{m.group(2)}-{m.group(1)}"
        text = re.sub(r"<[^>]+>", " ", rec.get("html", ""))
        dm = re.search(r"([A-Z][a-z]+ \d{1,2},? \d{4})", text)
        label = dm.group(1).replace(",", "") if dm else (date or title)
        entries.append({"slug": slug, "title": title, "label": label, "body": body,
                        "sort": date or "0000-00"})
    entries.sort(key=lambda e: e["sort"], reverse=True)
    seen_labels: set[str] = set()
    parts = []
    for e in entries:
        label = e["label"]
        while label in seen_labels:
            label += " "
        seen_labels.add(label)
        parts.append(
            f'{{/* source: /{e["slug"]} */}}\n'
            f'<Update label="{label}" description={json.dumps(e["title"])}>\n\n'
            f'{e["body"]}\n\n</Update>\n'
        )
        rows.append({
            "source_url": f"{SOURCE}/{e['slug']}",
            "source_title": e["title"],
            "normalized_path": "/ads-api/blog",
            "converted_file": "ads-api/blog.mdx",
            "status": "done",
            "notes": "consolidated into the /ads-api/blog Update timeline",
        })
    (OUT / "blog.mdx").write_text(
        '---\ntitle: "Blog"\nrss: true\n---\n\n' + "\n".join(parts).strip() + "\n",
        encoding="utf-8")
    print(f"blog posts consolidated: {len(entries)}")
    print(f"assets localized: {sum(1 for v in assets.values() if v.startswith('/images'))}")

    # Excluded source URLs, each with a concrete reason.
    covered = {r["source_url"] for r in rows}
    urls = [u.strip() for u in (CRAWL / "urls.txt").read_text().splitlines() if u.strip()]
    for url in urls:
        slug = url[len(SOURCE):].strip("/") or "index"
        if f"{SOURCE}/{slug}" in covered or url in covered:
            continue
        reason = EXCLUSIONS.get(slug)
        if reason is None and slug.startswith("blog/tags/"):
            reason = ("generated tag listing route; the tag is preserved on the "
                      "consolidated Update timeline")
        if reason is None and slug.startswith("v3/api/"):
            reason = ("generated API category index; Mintlify generates the same "
                      "tag grouping from the OpenAPI document")
        if reason is None and slug.startswith("v2"):
            reason = ("v2 of the Ads API, which the source labels Deprecated and "
                      "serves with no server-rendered content")
        rows.append({
            "source_url": url,
            "source_title": "",
            "normalized_path": "",
            "converted_file": "",
            "status": "excluded",
            "notes": reason or "generated route",
        })

    (REPO / "ads-api-parity-manifest.json").write_text(
        json.dumps({
            "source_site": f"{SOURCE}/",
            "source_platform": "Docusaurus 3 + docusaurus-plugin-openapi-docs",
            "generated_by": "scripts/build_ads.py",
            "discovered_pages": len(urls),
            "converted_pages": sum(1 for r in rows if r["status"] == "done"),
            "excluded_pages": sum(1 for r in rows if r["status"] == "excluded"),
            "pages": rows,
        }, indent=2) + "\n",
        encoding="utf-8")
    print(f"manifest rows: {len(rows)} "
          f"(done {sum(1 for r in rows if r['status'] == 'done')}, "
          f"excluded {sum(1 for r in rows if r['status'] == 'excluded')})")


if __name__ == "__main__":
    main()
