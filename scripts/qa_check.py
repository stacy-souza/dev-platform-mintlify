#!/usr/bin/env python3
"""Mechanical /preview-qa checks that can be verified without a browser.

Covers Gate 1 (config + navigation integrity), Gate 1.5 (port-artifact sweep),
and the deterministic parts of Gates 2-4 (frontmatter parity, icon hierarchy,
single-page wrapper groups, external hrefs inside group pages, collapsibility).
"""

from __future__ import annotations

import glob
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = ("scripts/", "mstack/", ".crawl/", "node_modules/")

failures: list[str] = []
notes: list[str] = []


def fail(gate: str, message: str) -> None:
    failures.append(f"[{gate}] {message}")


def note(gate: str, message: str) -> None:
    notes.append(f"[{gate}] {message}")


def repo_pages() -> list[str]:
    out = []
    for path in glob.glob("**/*.mdx", recursive=True, root_dir=REPO):
        if path.startswith(SKIP_DIRS):
            continue
        out.append(path[:-4])
    return sorted(out)


def nav_pages(node, trail=()):
    """Walk only known navigation containers, never label strings."""
    if isinstance(node, str):
        yield node, trail
        return
    if isinstance(node, list):
        for item in node:
            yield from nav_pages(item, trail)
        return
    if isinstance(node, dict):
        label = node.get("group") or node.get("tab") or node.get("product") or node.get("anchor")
        sub = trail + (label,) if label else trail
        for key in ("groups", "tabs", "products", "versions", "anchors", "dropdowns", "pages", "menu"):
            if key in node:
                yield from nav_pages(node[key], sub)


def frontmatter(page: str) -> dict[str, str]:
    text = (REPO / f"{page}.mdx").read_text(encoding="utf-8")
    m = re.match(r"\A---\n([\s\S]*?)\n---", text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if kv:
            out[kv.group(1)] = kv.group(2).strip().strip("\"'")
    return out


def gate1(config: dict) -> None:
    pages = repo_pages()
    nav = [p for p, _ in nav_pages(config["navigation"])]
    dupes = sorted({p for p in nav if nav.count(p) > 1})
    if dupes:
        fail("1", f"pages listed more than once in navigation: {dupes[:10]}")

    dangling = sorted(set(nav) - set(pages))
    if dangling:
        fail("1", f"navigation entries with no file: {dangling[:10]}")

    # The two landing pages are reached from the product switcher and each
    # other's header rather than from a sidebar, the way both source sites do.
    landing = {"index", "ads-api"}
    hidden = {p for p in pages if frontmatter(p).get("hidden") == "true"}
    orphans = sorted(set(pages) - set(nav) - hidden - landing)
    if orphans:
        fail("1", f"orphan pages absent from navigation: {orphans[:10]}")

    note("1", f"{len(pages)} pages on disk, {len(nav)} in navigation, "
              f"{len(hidden)} deliberately hidden, {len(landing)} landing pages "
              "served outside nav")

    if list(REPO.glob("*.js")):
        fail("1", "root-level .js file present; Mintlify v4 never executes it")

    for required in ("theme", "name", "colors", "navigation", "contextual"):
        if required not in config:
            fail("1", f"docs.json is missing {required}")
    if config["theme"] != "luma":
        fail("1", f"theme must be luma, found {config['theme']}")
    if config.get("seo", {}).get("metatags", {}).get("robots") != "noindex":
        fail("1", "seo.metatags.robots must be noindex on a preview")


def gate15() -> None:
    patterns = {
        "A broken-card link smash": (
            r"\[\s*[\U0001F000-\U0001FFFF\u2600-\u27bf][^\]]*?####[^\]]*?\]\([^)]+\)", re.DOTALL),
        "B inline emoji card": (
            r"\[[\U0001F000-\U0001FFFF\u2600-\u27bf]+[A-Z][^\]]+\]\([^)]+\)", 0),
        "C broken steps": (r"\[\s*\d+\s*\n\s*\n\s*###[^\]]+\]\([^)]+\)", re.DOTALL),
        "D orphan emoji heading": (
            r"^[\U0001F000-\U0001FFFF\u2600-\u27bf]\s*\n\s*\n####\s", re.M),
        "E prism-code fence": (r"```prism-code", 0),
        "F admonition in frontmatter": (r"^description:\s*\"?:::", re.M),
        "H orphan horizontal rules": (r"^---\s*\n\s*\n---", re.M),
        "J source chrome leakage": (
            r"^(Copy page|Edit on GitHub|Was this helpful)", re.M),
        "leftover Docusaurus syntax": (r"^\s*:::|<TabItem|@site/|<details", re.M),
    }
    for page in repo_pages():
        text = (REPO / f"{page}.mdx").read_text(encoding="utf-8")
        for name, (rx, flags) in patterns.items():
            if re.search(rx, text, flags):
                fail("1.5", f"{name}: {page}.mdx")


def gate2() -> None:
    """Frontmatter parity: nothing fabricated, nothing that renders unbacked."""
    manifest = json.loads((REPO / "parity-manifest.json").read_text(encoding="utf-8"))
    by_path = {r["normalized_path"].split("#")[0]: r for r in manifest["pages"]}
    with_description = []
    for page in repo_pages():
        meta = frontmatter(page)
        # A `mode: "custom"` page renders nothing but its own JSX, so its
        # description is metadata only and never reaches the page as a subtitle.
        if "description" in meta and meta.get("mode") != "custom":
            with_description.append(page)
        if not meta.get("title") and "url" not in meta:
            fail("2", f"{page}.mdx has no title")
        if meta.get("sidebarTitle") == meta.get("title"):
            fail("2", f"{page}.mdx has a sidebarTitle identical to its title")
        if "icon" in meta:
            fail("2", f"{page}.mdx sets an icon; the source navigation is icon-free")
    if with_description:
        fail("2", "description frontmatter renders as a subtitle the source does "
                  f"not have: {with_description[:10]}")
    note("2", f"{len(by_path)} manifest paths; {len(with_description)} pages "
              "carry synthesized descriptions")

    statuses = {r["status"] for r in manifest["pages"]}
    if statuses - {"done", "excluded", "blocked"}:
        fail("2", f"manifest has unexpected statuses: {statuses}")
    for row in manifest["pages"]:
        if row["status"] != "done" and not row["notes"]:
            fail("2", f"{row['source_url']} is {row['status']} with no reason")


def gate3(config: dict) -> None:
    nav = config["navigation"]

    def walk(node, depth=0, parent_is_group=False):
        if isinstance(node, list):
            for item in node:
                walk(item, depth, parent_is_group)
            return
        if not isinstance(node, dict):
            return
        if "group" in node:
            if depth > 0 and "icon" in node:
                fail("3", f"nested group '{node['group']}' carries an icon")
            pages = node.get("pages", [])
            strings = [p for p in pages if isinstance(p, str)]
            if len(pages) == 1 and strings:
                label = frontmatter(strings[0]).get("sidebarTitle") or frontmatter(strings[0]).get("title")
                if label == node["group"]:
                    fail("3", f"single-page wrapper group duplicates its page label: {node['group']}")
            for entry in pages:
                if isinstance(entry, dict) and "href" in entry and "group" not in entry:
                    fail("3", f"external href inside group pages: {node['group']}")
            for entry in pages:
                walk(entry, depth + 1, True)
            return
        for key in ("groups", "tabs", "products", "versions", "anchors", "pages"):
            if key in node:
                walk(node[key], depth, parent_is_group)

    walk(nav)

    # Devvit's source docs have no tab row; the Ads API's source does, running
    # Guides / API / What's New / Blog across its navbar on every page.
    if "tabs" in nav:
        fail("3", "navigation uses tabs at the top level; neither source does")
    for product in nav.get("products", []):
        has_tabs = "tabs" in product
        if has_tabs != (product["product"] == "Ads API"):
            fail("3", f"{product['product']} tab row does not match its source")
    # `global` sits on each product rather than on `navigation`, so the version
    # dropdown lists that product's own versions.
    globals_ = [nav.get("global", {})] + [p.get("global", {}) for p in nav.get("products", [])]
    if any(g.get("anchors") for g in globals_):
        fail("3", "global.anchors present; the source has no sidebar anchor rail")
    if not any(g.get("versions") for g in globals_):
        fail("3", "no global.versions; the source docs navbar has a version dropdown")
    if config.get("navbar", {}).get("links"):
        fail("3", "navbar.links present; the source docs navbar has no right-side links")
    if config.get("navbar", {}).get("primary"):
        fail("3", "navbar.primary present; the source docs navbar has no CTA")
    footer = config.get("footer", {})
    if footer.get("socials"):
        fail("3", "footer.socials present; the source docs footer has none")
    labels = [i["label"] for col in footer.get("links", []) for i in col["items"]]
    expected = ["Blog", "The Reddit Developer Fund", "r/Devvit", "r/GamesOnReddit",
                "Join our Discord"]
    if labels != expected:
        fail("3", f"footer links do not mirror the source: {labels}")
    if "index" in [p for p, _ in nav_pages(nav)]:
        fail("3", "site root is listed inside a sidebar group")
    if "ads-api" in [p for p, _ in nav_pages(nav)]:
        fail("3", "the Ads API landing page is listed inside a sidebar group")
    note("3", "footer mirrors the source's five links; navbar has no links, CTA, "
              "socials, or anchor rail, and only the Ads API carries a tab row, "
              "matching each source's docs chrome")


def gate4(config: dict) -> None:
    """Collapsibility: every nested group must be collapsed like the source."""
    def walk(node, depth=0):
        if isinstance(node, list):
            for item in node:
                walk(item, depth)
            return
        if not isinstance(node, dict):
            return
        if "group" in node:
            if depth > 0 and node.get("expanded") is not False:
                fail("4", f"nested group '{node['group']}' is not expanded:false")
            for entry in node.get("pages", []):
                walk(entry, depth + 1)
            return
        for key in ("groups", "tabs", "products", "versions", "anchors", "pages"):
            if key in node:
                walk(node[key], depth)

    walk(config["navigation"])
    nested = sum(
        1 for _ in re.finditer(r'"expanded": false', json.dumps(config, indent=2))
    )
    note("4", f"{nested} nested groups start collapsed, matching the source's "
              "chevron behaviour; Mintlify auto-expands the active section")


def main() -> int:
    config = json.loads((REPO / "docs.json").read_text(encoding="utf-8"))
    gate1(config)
    gate15()
    gate2()
    gate3(config)
    gate4(config)

    for line in notes:
        print("note", line)
    if failures:
        print()
        for line in failures:
            print("FAIL", line)
        print(f"\n{len(failures)} failures")
        return 1
    print("\nall mechanical gates pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
