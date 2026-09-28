#!/usr/bin/env python3
"""Deterministic Docusaurus -> Mintlify transformer for developers.reddit.com/docs.

Run from the repo root:

    python3 scripts/migrate.py --fetch     # refresh .crawl/mirror from the live site
    python3 scripts/migrate.py             # rebuild MDX + docs.json + parity manifest

Inputs
  .crawl/sitemap.xml              source sitemap (783 URLs)
  .crawl/llms.txt                 source llms.txt (81 doc descriptions)
  .crawl/mirror/**.md             one raw markdown file per source doc page
  .crawl/devvit-docs/             source repo checkout (sidebar + blog + assets)

Outputs
  ./**/*.mdx                      converted pages
  ./docs.json                     navigation + chrome
  ./parity-manifest.json          per-URL parity record
  ./images/**                     referenced assets
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib_mdx import (  # noqa: E402
    convert_markdown,
    strip_imports,
    yaml_quote,
)

REPO = Path(__file__).resolve().parent.parent
CRAWL = REPO / ".crawl"
MIRROR = CRAWL / "mirror"
SRC_REPO = Path("/tmp/devvit-docs")
if (CRAWL / "devvit-docs").exists():
    SRC_REPO = CRAWL / "devvit-docs"

SOURCE_BASE = "https://developers.reddit.com/docs"
VERSION = "0.14"
VERSIONED = SRC_REPO / "versioned_docs" / f"version-{VERSION}"

# Directories the transformer owns. Everything under them is regenerated.
MANAGED_DIRS = [
    "api", "capabilities", "earn-money", "examples", "guides", "introduction",
    "quickstart", "images", "resources",
]
MANAGED_FILES = [
    "changelog.mdx", "devvit_rules.mdx", "quickstart.mdx", "blog.mdx",
    "parity-manifest.json", "llms.txt", "llms-api.txt",
]


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def sitemap_urls() -> list[str]:
    text = (CRAWL / "sitemap.xml").read_text(encoding="utf-8")
    return sorted(set(re.findall(r"<loc>([^<]+)</loc>", text)))


def llms_descriptions() -> dict[str, str]:
    """Source-authored one-line summaries, keyed by doc id.

    The source site generates these with its own llmsTxt plugin. They are not
    rendered on the page, so they never become Mintlify `description`
    frontmatter; they feed the agent-facing root llms.txt instead.
    """
    out: dict[str, str] = {}
    path = CRAWL / "llms.txt"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"- \[([^\]]*)\]\((https://developers\.reddit\.com/docs/[^)]+)\.md\):\s*(.*)", line)
        if m:
            out[m.group(2)[len(SOURCE_BASE) + 1 :]] = m.group(3).strip()
    return out


def doc_ids() -> list[str]:
    ids = []
    for path in MIRROR.rglob("*.md"):
        ids.append(str(path.relative_to(MIRROR))[:-3])
    return sorted(ids)


FOLDER_INDEX_NAMES = {"README", "index"}


def source_url(doc_id: str) -> str:
    """Source URL for a doc id, honouring Docusaurus' folder-index convention."""
    if doc_id == "introduction/introduction":
        return f"{SOURCE_BASE}/"
    parts = doc_id.split("/")
    leaf = parts[-1]
    if len(parts) > 1 and (leaf in FOLDER_INDEX_NAMES or leaf == parts[-2]):
        return f"{SOURCE_BASE}/" + "/".join(parts[:-1])
    return f"{SOURCE_BASE}/{doc_id}"


def mint_path(doc_id: str) -> str:
    """Repo-relative Mintlify slug (no leading slash, no extension).

    Mirrors the source URL exactly, except for the docs root: the source serves
    `introduction/introduction` at `/docs/`, but `/` is the new docs landing
    page, so that one page keeps its file path and gains a redirect.
    """
    if doc_id == "introduction/introduction":
        return "introduction/introduction"
    parts = doc_id.split("/")
    leaf = parts[-1]
    if len(parts) > 1 and (leaf in FOLDER_INDEX_NAMES or leaf == parts[-2]):
        return "/".join(parts[:-1])
    return doc_id


# ---------------------------------------------------------------------------
# Asset handling
# ---------------------------------------------------------------------------

class Assets:
    """Resolves source image references and copies the referenced files in."""

    def __init__(self) -> None:
        self.copied: dict[str, str] = {}
        self.missing: set[str] = set()
        self.transcoded: set[str] = set()

    def resolve(self, ref: str, doc_id: str) -> str | None:
        ref = ref.split("#")[0].split("?")[0]
        if not ref or ref.startswith(("http://", "https://", "data:", "/images/")):
            return None
        candidates: list[Path] = []
        if ref.startswith("@site/"):
            candidates.append(SRC_REPO / ref[len("@site/") :])
        elif ref.startswith("/"):
            candidates.append(SRC_REPO / "static" / ref.lstrip("/"))
            candidates.append(VERSIONED / ref.lstrip("/"))
        else:
            base = (VERSIONED / doc_id).parent
            candidates.append((base / ref).resolve())
        for cand in candidates:
            if cand.is_file():
                return self._copy(cand)
        self.missing.add(f"{doc_id} -> {ref}")
        return None

    def resolve_blog(self, ref: str, post_dir: Path) -> str | None:
        ref = ref.split("#")[0].split("?")[0]
        if not ref or ref.startswith(("http://", "https://", "data:")):
            return None
        if ref.startswith("@site/"):
            cand = SRC_REPO / ref[len("@site/") :]
        elif ref.startswith("/"):
            cand = SRC_REPO / "static" / ref.lstrip("/")
        else:
            cand = (post_dir / ref).resolve()
        if cand.is_file():
            return self._copy(cand)
        self.missing.add(f"blog -> {ref}")
        return None

    def _copy(self, src: Path) -> str:
        key = str(src)
        if key in self.copied:
            return self.copied[key]
        src = src.resolve()
        rel = None
        # Roots are resolved too: relative image references go through
        # Path.resolve(), so an unresolved root never matches and every asset
        # would fall back to the images/misc bucket.
        roots = [
            (VERSIONED.resolve() / "assets", ""),
            (SRC_REPO.resolve() / "docs" / "assets", ""),
            (SRC_REPO.resolve() / "blog" / "assets", "blog/"),
            (SRC_REPO.resolve() / "blog", "blog/"),
            (SRC_REPO.resolve() / "static" / "img", "site/"),
        ]
        # Older doc versions share assets with 0.14; map them onto the same path
        # so a cross-version reference does not duplicate the file.
        roots += [
            (SRC_REPO.resolve() / "versioned_docs" / f"version-{v}" / "assets", "")
            for v in ("0.13", "0.12", "0.11")
        ]
        for root, prefix in roots:
            try:
                rel = prefix + str(src.relative_to(root))
                break
            except ValueError:
                continue
        if rel is None:
            rel = "misc/" + src.name
        rel = re.sub(r"[^A-Za-z0-9._/-]", "-", rel)
        dest = REPO / "images" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Multi-megabyte animated GIFs are the single heaviest thing in this
        # corpus (one is 30 MB). Transcoding to H.264 keeps the motion and the
        # frame content while cutting ~95% of the bytes; the caller renders an
        # autoplaying muted <video> in place of the image.
        if src.suffix.lower() == ".gif" and src.stat().st_size > GIF_MP4_THRESHOLD:
            mp4 = dest.with_suffix(".mp4")
            if not mp4.exists():
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                     "-movflags", "+faststart", "-pix_fmt", "yuv420p",
                     "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                     "-c:v", "libx264", "-crf", "26", "-an", str(mp4)],
                    check=True)
            poster = dest.with_suffix("").with_name(dest.stem + "-poster.jpg")
            if not poster.exists():
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                     "-frames:v", "1", "-q:v", "4", str(poster)],
                    check=True)
            url = "/images/" + rel[: -len(".gif")] + ".mp4"
            self.transcoded.add(url)
            self.copied[key] = url
            return url

        shutil.copy2(src, dest)
        url = "/images/" + rel
        self.copied[key] = url
        return url


# ---------------------------------------------------------------------------
# Page conversion
# ---------------------------------------------------------------------------

FRONTMATTER = re.compile(r"\A---\n([\s\S]*?)\n---\n?")


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    text = text.lstrip("\ufeff")
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if kv:
            meta[kv.group(1)] = kv.group(2).strip().strip("'\"")
    return meta, text[m.end() :]


# TypeDoc prefixes every page with a package/version backlink and a rule.
# Both forms occur: linked on symbol pages, plain bold on package index pages.
TYPEDOC_HEADER = re.compile(
    r"\A\s*(?:\[\*\*[^\]]*\*\*\]\([^)]*\)|\*\*[^*\n]+\*\*)\s*\n+\*\*\*\s*\n+", re.M
)


def strip_source_chrome(text: str, is_api: bool) -> str:
    """Remove generator/source chrome that should not survive the migration."""
    if is_api:
        text = TYPEDOC_HEADER.sub("", text)
    text = re.sub(r"^(Copy page|Edit on GitHub|Was this helpful\??)\s*$", "", text, flags=re.M)
    text = re.sub(r"<!--\s*truncate\s*-->", "", text)
    text = re.sub(r"<head>[\s\S]*?</head>", "", text)
    text = re.sub(r"<script>[\s\S]*?</script>", "", text)
    return text


def clean_title(text: str) -> str:
    """Strip markdown syntax from a heading so it is safe as frontmatter.

    TypeDoc writes generics as `Class: Listing\\<T\\>`; the backslash escapes are
    markdown, not content, and an invalid escape in a quoted YAML scalar makes
    the whole page fail to parse.
    """
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!<>|~])", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_title(meta: dict[str, str], body: str) -> tuple[str, str]:
    """Return (title, body-without-its-own-duplicate-H1).

    Docusaurus renders frontmatter `title` as the page heading *and* keeps any
    H1 in the body, so a page with both shows two headings. Mintlify renders
    `title` as the only H1, so the body heading is demoted to H2 rather than
    dropped -- dropping it loses a section the source actually displays.
    """
    m = re.search(r"^#\s+(.+?)\s*$", body, flags=re.M)
    h1 = m.group(1).strip() if m else ""
    front = meta.get("title", "").strip()
    if m:
        if front and clean_title(front) != clean_title(h1):
            body = body[: m.start()] + f"## {h1}" + body[m.end() :]
        else:
            body = body[: m.start()] + body[m.end() :]
    return clean_title(front or h1), body


def heading_slug(text: str) -> str:
    """Reproduce Mintlify's heading-id slug so redundant anchors can be dropped."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[?!:,;'\"()\[\]{}<>~\\]", "", text.strip().lower())
    return re.sub(r"\s+", "-", text)


def normalize_typedoc_anchors(body: str) -> str:
    """Replace TypeDoc's empty `<a id>` anchor targets.

    TypeDoc emits `<a id="authorname"></a>` above every member heading. Mintlify
    already generates the same id from the heading text, so 3,097 of the 3,188
    anchors in this corpus are redundant -- and every one of them trips
    `mint a11y`'s "anchor without text" rule. Drop the redundant ones and keep
    the genuinely distinct ids (overload suffixes like `hdel-2`) on a `<span>`,
    which holds the fragment target without pretending to be a link.
    """

    def repl(m: re.Match[str]) -> str:
        anchor_id, spacing, hashes, heading = m.groups()
        if heading_slug(heading) == anchor_id:
            return f"{spacing}{hashes} {heading}"
        return f'<span id="{anchor_id}" />{spacing}{hashes} {heading}'

    body = re.sub(
        r'<a id="([^"]+)"></a>(\s*\n+\s*)(#{2,6})\s+(.+)', repl, body
    )
    # Any anchor not followed by a heading still needs to stop being an <a>.
    return re.sub(r'<a id="([^"]+)"></a>', r'<span id="\1" />', body)


TYPEDOC_KINDS = (
    "Class", "Interface", "Type Alias", "Function", "Variable", "Enumeration",
    "Enumeration Member", "Namespace", "Module",
)


def api_title(doc_id: str, body: str) -> tuple[str, str, bool]:
    """Resolve an API page's title the way the source does.

    Every TypeDoc page on the source renders two headings: a Docusaurus title
    equal to the symbol name (which is also the browser tab, e.g.
    "Oembed | Reddit for Developers") and the generator's own kind-prefixed
    heading ("Type Alias: Oembed"). Mirror that: the symbol name becomes
    `title`, and the kind heading stays in the body demoted to an H2.
    """
    symbol = doc_id.rsplit("/", 1)[-1]
    m = re.search(r"^#\s+(.+?)\s*$", body, flags=re.M)
    heading = m.group(1).strip() if m else ""
    deprecated = "~~" in heading
    if m and any(heading.startswith(f"{kind}: ") for kind in TYPEDOC_KINDS):
        body = body[: m.start()] + f"## {heading}\n" + body[m.end() :]
        return symbol.replace("~~", ""), body, deprecated
    # Package / namespace index pages: the source's tab literally reads
    # "README", so use the real heading instead and let the sidebar say
    # "Overview" per the group-landing convention.
    title, body = extract_title({}, body)
    return title, body, deprecated


# Links the source itself gets wrong. `redis.md` points TxClientLike at
# `type-aliases/`, but TypeDoc emits it under `interfaces/`, so the source link
# 404s. Repointing it is the /fix-broken-links remedy for a dead source link.
SOURCE_LINK_FIXES = {
    "api/public-api/type-aliases/TxClientLike": "api/public-api/interfaces/TxClientLike",
}


class LinkRewriter:
    def __init__(self, id_to_path: dict[str, str]) -> None:
        self.id_to_path = id_to_path
        # Alias table so relative links that name the folder-index file, the
        # folder, or the legacy per-entry URL all resolve.
        self.alias: dict[str, str] = {}
        for doc_id, path in id_to_path.items():
            self.alias[doc_id] = path
            self.alias[path] = path
        self.unresolved: set[str] = set()

    def lookup(self, target: str) -> str | None:
        target = target.strip("/")
        target = SOURCE_LINK_FIXES.get(target, target)
        for key in (target, f"{target}/README", f"{target}/{target.split('/')[-1]}"):
            if key in self.alias:
                return "/" + self.alias[key]
        return None

    def rewrite(self, text: str, doc_id: str) -> str:
        doc_dir = os.path.dirname(doc_id)

        def resolve(raw: str) -> str:
            target, _, frag = raw.partition("#")
            frag = f"#{frag}" if frag else ""
            target, _, query = target.partition("?")
            query = f"?{query}" if query else ""
            if not target:
                return raw
            if target.startswith(f"{SOURCE_BASE}/"):
                hit = self.lookup(target[len(SOURCE_BASE) + 1 :])
                return (hit + query + frag) if hit else raw
            if target == SOURCE_BASE or target == f"{SOURCE_BASE}/":
                return "/introduction/introduction" + frag
            if target.startswith(("http://", "https://", "mailto:", "tel:", "#")):
                return raw
            target = re.sub(r"\.mdx?$", "", target)
            if target.startswith("/"):
                hit = self.lookup(target)
                return (hit + query + frag) if hit else target + query + frag
            joined = os.path.normpath(os.path.join(doc_dir, target))
            hit = self.lookup(joined)
            if hit:
                return hit + query + frag
            self.unresolved.add(f"{doc_id} -> {raw}")
            return "/" + joined.lstrip("./") + query + frag

        text = re.sub(
            r"(?<!!)\[([^\]]*)\]\(([^)\s]+)((?:\s+\"[^\"]*\")?)\)",
            lambda m: f"[{m.group(1)}]({resolve(m.group(2))}{m.group(3)})",
            text,
        )
        text = re.sub(
            r"(<a\b[^>]*?href=)([\"'])([^\"']+)\2",
            lambda m: f"{m.group(1)}{m.group(2)}{resolve(m.group(3))}{m.group(2)}",
            text,
        )
        return text


def video_tag(url: str, alt: str) -> str:
    """Render a transcoded GIF as a muted autoplaying video.

    `mint a11y` requires an `alt` attribute on `<video>` alongside the
    `aria-label`, so both carry the original image's alt text.
    """
    poster = url[: -len(".mp4")] + "-poster.jpg"
    label = (alt or "Animation").replace('"', "'")
    return (
        f'<video src="{url}" poster="{poster}" alt="{label}" '
        f'aria-label="{label}" autoPlay={{true}} loop muted '
        f'playsInline={{true}} className="w-full rounded-lg"></video>'
    )


def rewrite_images(text: str, doc_id: str, assets: Assets) -> str:
    def md(m: re.Match[str]) -> str:
        url = assets.resolve(m.group(2), doc_id)
        if url and url in assets.transcoded:
            return video_tag(url, m.group(1))
        return f"![{m.group(1)}]({url or m.group(2)}{m.group(3)})"

    text = re.sub(
        r"!\[([^\]]*)\]\(([^)\s]+)((?:\s+\"[^\"]*\")?)\)", md, text
    )

    def html(m: re.Match[str]) -> str:
        url = assets.resolve(m.group(3), doc_id)
        return f"{m.group(1)}{m.group(2)}{url or m.group(3)}{m.group(2)}"

    text = re.sub(r"(<img\b[^>]*?src=)([\"'])([^\"']+)\2", html, text)
    # Mintlify `<Card img="...">` / `<Frame>` style image props.
    text = re.sub(r"(\bimg=)([\"'])([^\"']+)\2", html, text)
    return text


# Interactive React widgets the source renders inline. Mintlify has no
# equivalent, so each one is approximated and the delta recorded in the manifest.
COMPONENT_FALLBACKS = {
    "ScrollTrapDemo": (
        "<Info>\n  **Interactive demo:** the source page embeds a live feed "
        "simulator with three tabs — an internal scroll trap, a no-scrollbar "
        "trap, and a fixed version. Try it at "
        "[developers.reddit.com/docs/guides/best-practices/scroll-traps]"
        "(https://developers.reddit.com/docs/guides/best-practices/scroll-traps#inline-scroll-trap-demo).\n"
        "</Info>"
    ),
}


def replace_unsupported_components(text: str) -> tuple[str, list[str]]:
    hit: list[str] = []
    for name, replacement in COMPONENT_FALLBACKS.items():
        pattern = re.compile(rf"<{name}\b[^>]*/>|<{name}\b[^>]*>[\s\S]*?</{name}>")
        if pattern.search(text):
            hit.append(name)
            text = pattern.sub(replacement, text)
    return text, hit


def convert_body(raw: str, doc_id: str, assets: Assets, links: LinkRewriter,
                 is_api: bool) -> tuple[str, str, str]:
    meta, body = split_frontmatter(raw)
    body = strip_imports(body)
    body = strip_source_chrome(body, is_api)
    deprecated = False
    if is_api:
        body = normalize_typedoc_anchors(body)
        title, body, deprecated = api_title(doc_id, body)
    else:
        title, body = extract_title(meta, body)
    body, swapped = replace_unsupported_components(body)
    body = convert_markdown(
        body,
        rewrite_images=lambda t: rewrite_images(t, doc_id, assets),
        rewrite_links=lambda t: links.rewrite(t, doc_id),
    )
    note = ""
    if swapped:
        note = ("approximated the source's interactive "
                + ", ".join(swapped) + " widget with a callout linking to it")
    return title, meta.get("sidebar_label", ""), body, note, deprecated


def write_page(path: str, title: str, body: str, sidebar_title: str = "",
               extra: OrderedDict | None = None) -> None:
    dest = REPO / f"{path}.mdx"
    dest.parent.mkdir(parents=True, exist_ok=True)
    fm = [f"title: {yaml_quote(title)}"]
    if sidebar_title and sidebar_title != title:
        fm.append(f"sidebarTitle: {yaml_quote(sidebar_title)}")
    for key, value in (extra or {}).items():
        fm.append(f"{key}: {value}")
    dest.write_text("---\n" + "\n".join(fm) + "\n---\n\n" + body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

# Brand assets pulled straight from the source's own CDN so the preview never
# hotlinks and never approximates a Reddit mark by hand.
BRAND_ASSETS = {
    "brand/logo.svg": f"{SOURCE_BASE}/img/logo.svg",
    # Reddit wordmark lockup, used by the landing page footer exactly as the
    # source landing page footer uses it.
    "brand/reddit-lockup.png": f"{SOURCE_BASE}/img/Reddit_Lockup.png",
    "brand/devvit-icon.png": f"{SOURCE_BASE}/img/devvit_icon.png",
    "brand/devvit-icon.svg": f"{SOURCE_BASE}/img/devvit_icon.svg",
    "brand/flying-snoo.png": "https://www.redditstatic.com/devvit-dev-portal/assets/landing-page/flyingSnoo.png",
    "brand/honk.gif": "https://www.redditstatic.com/devvit-dev-portal/assets/landing-page/gif-honk.gif",
    "brand/sword-and-supper.gif": "https://www.redditstatic.com/devvit-dev-portal/assets/landing-page/gif-swordandsupper.gif",
    "brand/riddonkulous.gif": "https://www.redditstatic.com/devvit-dev-portal/assets/landing-page/gif-riddonkulous.gif",
    "brand/powerful.png": "https://www.redditstatic.com/devvit-dev-portal/assets/landing-page/powerful.png",
    "brand/cross-platform.png": "https://www.redditstatic.com/devvit-dev-portal/assets/landing-page/cross-platform.png",
    "brand/global.png": "https://www.redditstatic.com/devvit-dev-portal/assets/landing-page/global.png",
    "brand/hosting.png": "https://www.redditstatic.com/devvit-dev-portal/assets/landing-page/hosting.png",
    "brand/community.png": "https://www.redditstatic.com/devvit-dev-portal/assets/landing-page/community.png",
    "brand/payments.png": "https://www.redditstatic.com/devvit-dev-portal/assets/landing-page/payments.png",
}

EXPECTED_MIME = {".svg": "image/svg", ".png": "image/png", ".gif": "image/gif"}

# Animated GIFs larger than this are transcoded to H.264 (see Assets._copy).
GIF_MP4_THRESHOLD = 2 * 1024 * 1024


def sync_brand_assets() -> None:
    """Download the source's brand assets, verifying each one is really an image."""
    cache = CRAWL / "brand"
    cache.mkdir(parents=True, exist_ok=True)
    for rel, url in BRAND_ASSETS.items():
        cached = cache / rel.replace("/", "__")
        if not cached.exists() or cached.stat().st_size == 0:
            subprocess.run(
                ["curl", "-sSfL", "--max-time", "60", "-o", str(cached), url],
                check=True)
        head = cached.read_bytes()[:64]
        suffix = Path(rel).suffix
        if suffix == ".svg" and b"<svg" not in head and b"<?xml" not in head:
            raise SystemExit(f"{url} did not return SVG")
        if suffix == ".png" and not head.startswith(b"\x89PNG"):
            raise SystemExit(f"{url} did not return PNG")
        if suffix == ".gif" and not head.startswith(b"GIF8"):
            raise SystemExit(f"{url} did not return GIF")
        dest = REPO / "images" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, dest)


def clean_outputs() -> None:
    for name in MANAGED_DIRS:
        target = REPO / name
        if not target.exists():
            continue
        if name == "images":
            # The Ads API preview (scripts/build_ads.py) owns images/ads-api,
            # and this transformer must not delete another builder's assets.
            for child in target.iterdir():
                if child.name == "ads-api":
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            continue
        shutil.rmtree(target)
    for name in MANAGED_FILES:
        target = REPO / name
        if target.exists():
            target.unlink()


def fetch_sources() -> None:
    CRAWL.mkdir(exist_ok=True)
    subprocess.run(
        ["curl", "-sS", "--max-time", "90", "-o", str(CRAWL / "sitemap.xml"),
         f"{SOURCE_BASE}/sitemap.xml"], check=True)
    subprocess.run(
        ["curl", "-sS", "--max-time", "90", "-o", str(CRAWL / "llms.txt"),
         f"{SOURCE_BASE}/llms.txt"], check=True)
    if not SRC_REPO.exists():
        subprocess.run(
            ["git", "clone", "--depth", "1",
             "https://github.com/reddit/devvit-docs.git", str(SRC_REPO)], check=True)
    ids = []
    for path in VERSIONED.rglob("*.md*"):
        ids.append(re.sub(r"\.mdx?$", "", str(path.relative_to(VERSIONED))))
    MIRROR.mkdir(parents=True, exist_ok=True)
    listing = CRAWL / "doc-ids.txt"
    listing.write_text("\n".join(sorted(ids)) + "\n", encoding="utf-8")
    subprocess.run(
        f'cd {CRAWL} && cat doc-ids.txt | xargs -P 24 -I{{}} bash -c '
        f'\'f="mirror/{{}}.md"; mkdir -p "$(dirname "$f")"; '
        f'curl -sS --max-time 40 -o "$f" "{SOURCE_BASE}/{{}}.md"\'',
        shell=True, check=True)
    print(f"mirrored {len(ids)} pages")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="refresh the mirror first")
    args = parser.parse_args()
    if args.fetch:
        fetch_sources()

    import build_nav  # noqa: PLC0415

    clean_outputs()
    sync_brand_assets()

    ids = doc_ids()
    id_to_path = {doc_id: mint_path(doc_id) for doc_id in ids}
    links = LinkRewriter(id_to_path)
    assets = Assets()
    descriptions = llms_descriptions()
    sidebar_labels = build_nav.sidebar_labels(SRC_REPO, VERSION)
    overrides_dir = Path(__file__).resolve().parent / "overrides"

    manifest: list[dict] = []
    titles: dict[str, str] = {}

    for doc_id in ids:
        is_api = doc_id.startswith("api/")
        raw = (MIRROR / f"{doc_id}.md").read_text(encoding="utf-8")
        override = overrides_dir / f"{doc_id}.mdx"
        deprecated = False
        if override.exists():
            meta, body = split_frontmatter(override.read_text(encoding="utf-8"))
            title = meta["title"]
            body = links.rewrite(rewrite_images(body, doc_id, assets), doc_id)
            note = "hand-converted: source page uses a bespoke React component"
            sidebar_label = meta.get("sidebarTitle", "")
        else:
            title, sidebar_label, body, note, deprecated = convert_body(
                raw, doc_id, assets, links, is_api)
            if is_api and not note:
                note = "typedoc package header stripped"
        sidebar_label = sidebar_labels.get(doc_id, sidebar_label)
        if is_api and doc_id.endswith("/README"):
            sidebar_label = "Overview"
        titles[doc_id] = title

        extra: OrderedDict = OrderedDict()
        if deprecated:
            extra["deprecated"] = "true"
        if doc_id in build_nav.HIDDEN_PAGES:
            extra["hidden"] = "true"
            note = f"hidden: true — {build_nav.HIDDEN_PAGES[doc_id]}"
        for url, (link_doc_id, _label) in build_nav.LINK_PAGES.items():
            if doc_id == link_doc_id:
                extra["url"] = yaml_quote(url)
                note = ("source page is a meta-refresh redirect; reproduced with "
                        "url: frontmatter so it also serves the source sidebar row")
        if doc_id in build_nav.DUPLICATE_REDIRECTS:
            canonical = build_nav.DUPLICATE_REDIRECTS[doc_id]
            extra["hidden"] = "true"
            note = (f"stale duplicate of /{canonical} on the source; the URL "
                    f"redirects to the canonical page")

        write_page(id_to_path[doc_id], title, body, sidebar_label, extra)
        manifest.append({
            "source_url": source_url(doc_id),
            "source_title": title,
            "source_sidebar_label": sidebar_label or title,
            "source_h1": title,
            "source_description": descriptions.get(doc_id, ""),
            "normalized_path": "/" + id_to_path[doc_id],
            "nav_section": "",
            "converted_file": f"{id_to_path[doc_id]}.mdx",
            "status": "done",
            "notes": note,
        })

    print(f"converted {len(ids)} pages")
    if assets.missing:
        print(f"MISSING ASSETS ({len(assets.missing)}):")
        for item in sorted(assets.missing)[:40]:
            print("  ", item)
    if links.unresolved:
        print(f"UNRESOLVED LINKS ({len(links.unresolved)}):")
        for item in sorted(links.unresolved)[:40]:
            print("  ", item)

    import build_changelog  # noqa: PLC0415
    import build_blog  # noqa: PLC0415

    manifest += build_changelog.build(REPO, MIRROR, links, assets)
    blog_rows, blog_anchors = build_blog.build(REPO, SRC_REPO, links, assets)
    manifest += blog_rows
    manifest += build_nav.build_stubs(REPO)

    nav, redirects, sections = build_nav.build(
        REPO, SRC_REPO, VERSION, id_to_path, titles, blog_anchors)
    for row in manifest:
        row["nav_section"] = sections.get(row["normalized_path"], row["nav_section"])

    manifest += build_nav.excluded_rows(sitemap_urls(), manifest)
    build_nav.write_docs_json(REPO, nav, redirects)

    (REPO / "parity-manifest.json").write_text(
        json.dumps({
            "source_site": "https://developers.reddit.com/docs/",
            "source_platform": f"Docusaurus 3 (published version {VERSION})",
            "generated_by": "scripts/migrate.py",
            "discovered_pages": len(manifest),
            "converted_pages": sum(1 for r in manifest if r["status"] == "done"),
            "excluded_pages": sum(1 for r in manifest if r["status"] == "excluded"),
            "blocked_pages": sum(1 for r in manifest if r["status"] == "blocked"),
            "pages": manifest,
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"manifest rows: {len(manifest)}")

    import build_llms  # noqa: PLC0415

    build_llms.build(REPO)


if __name__ == "__main__":
    main()
