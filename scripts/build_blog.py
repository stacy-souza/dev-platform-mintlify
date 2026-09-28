#!/usr/bin/env python3
"""Consolidate the source blog into one native Mintlify <Update> timeline.

The source runs a Docusaurus blog at /docs/blog with ten posts plus generated
index, archive, and tag routes. mstack's changelog rules apply to blog-style
update feeds too: one page of <Update> blocks newest-first with `rss: true`, and
`docs.json` redirects covering every per-entry URL.

Publish dates come from each post's directory name (the Docusaurus convention),
never from sitemap `lastmod`. Tags come from each post's own frontmatter, which
is also what generated the source's 14 tag routes.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import migrate
from lib_mdx import convert_markdown, jsx_attr, strip_imports

SOURCE_BASE = "https://developers.reddit.com/docs/blog"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"\A---\n([\s\S]*?)\n---\n?", text)
    if not m:
        return {}, text
    meta: dict = {}
    body_yaml = m.group(1)
    tags = re.search(r"^tags:\s*\[([^\]]*)\]", body_yaml, re.M)
    if tags:
        meta["tags"] = [t.strip().strip("'\"") for t in tags.group(1).split(",") if t.strip()]
    for key in ("slug", "title", "image"):
        hit = re.search(rf"^{key}:\s*(.+)$", body_yaml, re.M)
        if hit:
            meta[key] = hit.group(1).strip().strip("'\"")
    return meta, text[m.end() :]


def build(repo: Path, src_repo: Path, links, assets) -> list[dict]:
    posts = []
    for post_dir in sorted((src_repo / "blog").iterdir()):
        if not post_dir.is_dir() or post_dir.name == "assets":
            continue
        entry = next(
            (post_dir / name for name in ("index.md", "index.mdx")
             if (post_dir / name).exists()), None)
        if entry is None:
            continue
        published = date.fromisoformat(post_dir.name[:10])
        meta, body = _parse_frontmatter(entry.read_text(encoding="utf-8"))
        # Without an explicit `slug`, Docusaurus serves a post at its
        # date-prefixed path, so that is the URL that needs preserving.
        slug = meta.get("slug") or (
            f"{published:%Y/%m/%d}/{post_dir.name[11:]}"
        )

        h1 = re.search(r"^#\s+(.+?)\s*$", body, flags=re.M)
        title = meta.get("title") or (h1.group(1).strip() if h1 else slug)
        if h1:
            body = body[: h1.start()] + body[h1.end() :]

        body = strip_imports(body)

        # Posts authored as .mdx import their media as ES modules and render it
        # through `src={Identifier}`. Map each identifier back to its asset.
        imports = dict(
            re.findall(
                r"^import\s+(\w+)\s+from\s+['\"]([^'\"]+)['\"];?\s*$",
                entry.read_text(encoding="utf-8"),
                re.M,
            )
        )

        def fix_md_image(m: re.Match[str]) -> str:
            url = assets.resolve_blog(m.group(2), post_dir)
            if url and url in assets.transcoded:
                return migrate.video_tag(url, m.group(1))
            return f"![{m.group(1)}]({url or m.group(2)})"

        def fix_html_image(m: re.Match[str]) -> str:
            url = assets.resolve_blog(m.group(3), post_dir)
            return f"{m.group(1)}{m.group(2)}{url or m.group(3)}{m.group(2)}"

        def fix_jsx_image(m: re.Match[str]) -> str:
            ref = imports.get(m.group(1))
            url = assets.resolve_blog(ref, post_dir) if ref else None
            if url and url in assets.transcoded:
                return f'src="{url}"'
            return f'src="{url}"' if url else m.group(0)

        def images(text: str) -> str:
            text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", fix_md_image, text)
            text = re.sub(r"(<img\b[^>]*?src=)([\"'])([^\"']+)\2", fix_html_image, text)
            return re.sub(r"src=\{(\w+)\}", fix_jsx_image, text)

        body = convert_markdown(
            body,
            rewrite_images=images,
            rewrite_links=lambda t: links.rewrite(t, "blog"),
        ).strip("\n")

        posts.append({
            "date": published,
            "slug": slug,
            "title": title,
            "tags": meta.get("tags", []),
            "body": body,
        })

    posts.sort(key=lambda p: p["date"], reverse=True)

    labels = [p["date"].strftime("%B %-d, %Y") for p in posts]
    if len(set(labels)) != len(labels):
        raise SystemExit(f"blog: duplicate <Update label> values: {labels}")

    parts = []
    for post, label in zip(posts, labels):
        tag_attr = ""
        if post["tags"]:
            tag_attr = " tags={" + json.dumps(post["tags"]) + "}"
        parts.append(
            f'{{/* source: /blog/{post["slug"]} */}}\n'
            f'<Update label="{label}" {jsx_attr("description", post["title"])}'
            f"{tag_attr}>\n\n"
            f'{post["body"]}\n\n</Update>\n'
        )

    (repo / "blog.mdx").write_text(
        "---\n"
        'title: "Blog"\n'
        "rss: true\n"
        "---\n\n" + "\n".join(parts).strip() + "\n",
        encoding="utf-8",
    )

    # Mintlify turns each <Update label> into a heading anchor, so every source
    # post keeps a stable deep link on the consolidated timeline.
    anchors = {
        post["slug"]: re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        for post, label in zip(posts, labels)
    }

    rows = []
    for post in posts:
        anchor = anchors[post["slug"]]
        rows.append({
            "source_url": f"{SOURCE_BASE}/{post['slug']}",
            "source_title": post["title"],
            "source_sidebar_label": "",
            "source_h1": post["title"],
            "source_description": "",
            "normalized_path": f"/blog#{anchor}",
            "nav_section": "Resources",
            "converted_file": "blog.mdx",
            "status": "done",
            "notes": "consolidated into the /blog <Update> timeline; the "
                     "per-entry source URL redirects to its anchor",
        })
    return rows, anchors
