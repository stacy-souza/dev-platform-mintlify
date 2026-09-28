#!/usr/bin/env python3
"""Rebuild the source changelog page as a native Mintlify <Update> timeline.

The source keeps its whole changelog on one page (`/docs/changelog`), with one
`## Release x.y.z: Title` heading per release and a `**Release Date: ...**` line
underneath. Those release dates are the real publish dates, so no sitemap
`lastmod` guessing is required.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from lib_mdx import convert_markdown, jsx_attr, strip_imports

SOURCE_URL = "https://developers.reddit.com/docs/changelog"


def _convert(text: str, links, assets) -> str:
    import migrate

    return convert_markdown(
        strip_imports(text),
        rewrite_images=lambda t: migrate.rewrite_images(t, "changelog", assets),
        rewrite_links=lambda t: links.rewrite(t, "changelog"),
    ).strip("\n")


def build(repo: Path, mirror: Path, links, assets) -> list[dict]:
    raw = (mirror / "changelog.md").read_text(encoding="utf-8")
    raw = re.sub(r"\A#\s+Changelog\s*\n", "", raw)

    chunks = re.split(r"^##\s+(.+?)\s*$", raw, flags=re.M)
    intro = _convert(chunks[0], links, assets)

    entries: list[tuple[str, str, str, list[str]]] = []
    for i in range(1, len(chunks), 2):
        heading = chunks[i].strip()
        body = chunks[i + 1]
        date_match = re.search(r"\*\*Release Date:\s*([^*]+?)\s*\*\*", body)
        date = date_match.group(1).strip() if date_match else ""
        if date_match:
            body = body[: date_match.start()] + body[date_match.end() :]
        version = re.match(r"Release\s+([\d.]+)", heading)
        tags = [f"v{version.group(1)}"] if version else []
        entries.append((date, heading, _convert(body, links, assets), tags))

    if not all(date for date, *_ in entries):
        raise SystemExit("changelog: every release must carry a Release Date line")

    labels = [date for date, *_ in entries]
    if len(set(labels)) != len(labels):
        raise SystemExit(f"changelog: duplicate <Update label> values: {labels}")

    body_parts = [intro, ""]
    for date, heading, body, tags in entries:
        tag_attr = " tags={" + json.dumps(tags) + "}" if tags else ""
        body_parts.append(
            f'<Update label="{date}" {jsx_attr("description", heading)}{tag_attr}>'
            f"\n\n{body}\n\n</Update>\n"
        )

    (repo / "changelog.mdx").write_text(
        "---\ntitle: \"Changelog\"\nrss: true\n---\n\n"
        + "\n".join(body_parts).strip()
        + "\n",
        encoding="utf-8",
    )

    return [{
        "source_url": SOURCE_URL,
        "source_title": "Changelog",
        "source_sidebar_label": "Changelog",
        "source_h1": "Changelog",
        "source_description": "",
        "normalized_path": "/changelog",
        "nav_section": "",
        "converted_file": "changelog.mdx",
        "status": "done",
        "notes": f"consolidated into a native <Update> timeline with rss: true; "
                 f"{len(entries)} releases, dates taken from the source's own "
                 f"Release Date lines",
    }]
