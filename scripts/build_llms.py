#!/usr/bin/env python3
"""Generate a curated root llms.txt plus a nested API index.

Mintlify generates /llms.txt automatically, but its generated entries lean on
page `description` frontmatter — and this migration deliberately has none,
because the source renders no page subtitles and a synthesized `description`
would show up as visible text the source does not have.

A curated root llms.txt solves that without touching any rendered page: it is an
agent-only artifact. Descriptions come from the source's own llms.txt where the
source publishes one, and otherwise from the page's first sentence (the same
derivation the source's llmsTxt plugin uses).

Root stays well under the 50K pass threshold by delegating the 674 generated
TypeDoc pages to /llms-api.txt.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TAGLINE = (
    "Build powerful apps and immersive experiences to enhance the communities "
    "you love. Devvit is Reddit's developer platform for interactive games, "
    "mod tools, and apps that run natively on Reddit."
)

SECTION_ORDER = [
    "Introduction",
    "Build Your App",
    "Launch Your App",
    "Capabilities",
    "Guides",
    "Resources",
]


def first_sentence(mdx: Path) -> str:
    """Derive a one-line summary from a page's first real paragraph."""
    if not mdx.exists():
        return ""
    body = mdx.read_text(encoding="utf-8")
    body = re.sub(r"\A---\n[\s\S]*?\n---\n", "", body)
    for block in re.split(r"\n\s*\n", body):
        line = block.strip()
        if not line or line.startswith(("#", "<", "|", "```", "{", "-", "*", ">", "!", "***")):
            continue
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"[`*_]", "", line)
        line = re.sub(r"&#12[35];", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) < 25:
            continue
        sentence = re.split(r"(?<=[.!?])\s", line)[0]
        return (sentence[:200].rstrip() + ("…" if len(sentence) > 200 else ""))
    return ""


def build(repo: Path) -> None:
    manifest = json.loads((repo / "parity-manifest.json").read_text(encoding="utf-8"))
    rows = [r for r in manifest["pages"] if r["status"] == "done" and r["converted_file"]]

    seen: set[str] = set()
    docs: list[tuple[str, str, str, str]] = []  # section, title, path, description
    api: list[tuple[str, str]] = []

    for row in rows:
        path = row["normalized_path"].split("#")[0]
        if not path or path in seen:
            continue
        # Navigation-only stubs (external sidebar rows) are not content.
        if path.startswith("/resources/"):
            continue
        seen.add(path)
        title = row["source_title"] or row["source_sidebar_label"]
        if not title:
            continue
        if path.startswith("/api/"):
            api.append((title, path))
            continue
        description = row["source_description"] or first_sentence(
            repo / row["converted_file"]
        )
        # The source's own llms.txt keeps markdown escapes in its descriptions.
        description = re.sub(r"\\([-_*`\[\]])", r"\1", description)
        section = (row["nav_section"] or "Resources").split(" > ")[0]
        docs.append((section, title, path, description))

    def section_key(section: str) -> int:
        return SECTION_ORDER.index(section) if section in SECTION_ORDER else len(SECTION_ORDER)

    docs.sort(key=lambda d: (section_key(d[0]), d[2]))

    lines = [
        "# Reddit for Developers",
        "",
        f"> {TAGLINE}",
        "",
        "## Docs",
        "",
        "- [Reddit for Developers](/index.md): Landing page for the Devvit "
        "developer documentation.",
    ]
    current = None
    for section, title, path, description in docs:
        if section != current:
            current = section
            lines += ["", f"## {section}", ""]
        suffix = f": {description}" if description else ""
        lines.append(f"- [{title}]({path}.md){suffix}")

    lines += [
        "",
        "## API playground",
        "",
        "- [Devvit HTTP endpoints](/api-playground/overview.md): Every HTTP route "
        "a Devvit Web app exposes, and which of them the Reddit platform, your "
        "own client, or an external service calls.",
        "- [Call an external endpoint](/api-playground/external-endpoint.md): "
        "POST /external/{path} -- invoke an endpoint declared under "
        "server.externalEndpoints with a callback or managed token.",
        "- [Handle a menu action](/api-playground/menu-action.md): "
        "POST /internal/menu/{action} -- the request and response shapes Reddit "
        "sends when a user picks one of your menu actions.",
        "- [Handle a trigger event](/api-playground/trigger-event.md): "
        "POST /internal/on-{trigger} -- the request and response shapes Reddit "
        "sends when a subscribed event fires.",
        "- [Run a scheduled task](/api-playground/scheduled-task.md): "
        "POST /internal/scheduler/{task} -- the endpoint Reddit calls on your "
        "cron schedule.",
        "- [Handle a form submission](/api-playground/form-submit.md): "
        "POST /internal/form/{form} -- the endpoint Reddit calls when a user "
        "submits one of your forms.",
        "- [Call your app's own endpoint](/api-playground/app-endpoint.md): "
        "POST /api/{path} -- the conventional routes your app's web client calls.",
        "",
        "## API reference",
        "",
        "- [Generated API reference index](/llms-api.txt): Every `@devvit/public-api` "
        "and `@devvit/reddit` class, interface, enumeration, function, variable, "
        "and type alias.",
        "",
        "## Optional",
        "",
        "- [Full documentation corpus](/llms-full.txt)",
        "",
    ]
    (repo / "llms.txt").write_text("\n".join(lines), encoding="utf-8")

    api_lines = [
        "# Reddit for Developers — generated API reference",
        "",
        "> TypeDoc reference for the `@devvit/public-api` and `@devvit/reddit` "
        "packages. Up one level: /llms.txt",
        "",
        "## Docs",
        "",
    ]
    for title, path in sorted(api, key=lambda a: a[1]):
        api_lines.append(f"- [{title}]({path}.md)")
    api_lines.append("")
    (repo / "llms-api.txt").write_text("\n".join(api_lines), encoding="utf-8")

    root_size = (repo / "llms.txt").stat().st_size
    api_size = (repo / "llms-api.txt").stat().st_size
    print(f"llms.txt: {len(docs) + 1} entries, {root_size} bytes")
    print(f"llms-api.txt: {len(api)} entries, {api_size} bytes")
    if root_size > 50_000:
        raise SystemExit("llms.txt exceeds the 50K pass threshold")
    if api_size > 100_000:
        raise SystemExit("llms-api.txt exceeds the 100K fail threshold")


if __name__ == "__main__":
    build(Path(__file__).resolve().parent.parent)
