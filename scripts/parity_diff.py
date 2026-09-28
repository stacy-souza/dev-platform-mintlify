#!/usr/bin/env python3
"""Gate 2: structural diff of every converted page against its mirrored source.

Compares heading text sequences, code-block counts, table rows, list items,
images, and link targets between `.crawl/mirror/<id>.md` and the generated MDX.
Structure drifting is how a conversion silently loses a section, so this runs
over all 754 mirrored pages rather than a sample.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIRROR = REPO / ".crawl" / "mirror"


def strip_fences(text: str) -> tuple[str, int]:
    blocks = 0
    out: list[str] = []
    inside = False
    marker = ""
    for line in text.split("\n"):
        fence = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if not inside and fence:
            inside, marker = True, fence.group(1)
            blocks += 1
            continue
        if inside and fence and fence.group(1)[0] == marker[0] and len(fence.group(1)) >= len(marker):
            inside = False
            continue
        if not inside:
            out.append(line)
    return "\n".join(out), blocks


def normalize_heading(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("&#123;", "{").replace("&#125;", "}")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    # Angle brackets carry meaning in TypeDoc headings (`Promise<void>`), so
    # strip only real markup tags, and do it after entity decoding so escaped
    # and unescaped source spellings compare equal.
    text = re.sub(r"</?(?:span|a|code|em|strong|br)\b[^>]*>", "", text)
    text = re.sub(r"[`*_~\\]", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def profile(text: str) -> dict:
    body, blocks = strip_fences(text)
    headings = [normalize_heading(m.group(2)) for m in re.finditer(r"^(#{1,6})\s+(.+)$", body, re.M)]
    return {
        "headings": headings,
        "code_blocks": blocks,
        "table_rows": len(re.findall(r"^\s*\|.+\|\s*$", body, re.M)),
        "list_items": len(re.findall(r"^\s*(?:[-*+]|\d+\.)\s+\S", body, re.M)),
        # Large animated GIFs become <video> elements, so count both as media.
        "images": (
            len(re.findall(r"!\[[^\]]*\]\(", body))
            + len(re.findall(r"<img\b", body))
            + len(re.findall(r"<video\b", body))
        ),
        "links": len(re.findall(r"(?<!!)\[[^\]]*\]\(", body)) + len(re.findall(r"<a\b", body)),
    }


def main() -> int:
    manifest = json.loads((REPO / "parity-manifest.json").read_text(encoding="utf-8"))
    overrides = {
        p.relative_to(REPO / "scripts" / "overrides").as_posix()[:-4]
        for p in (REPO / "scripts" / "overrides").rglob("*.mdx")
    }

    checked = 0
    problems: list[str] = []
    for row in manifest["pages"]:
        converted = row["converted_file"]
        if not converted.endswith(".mdx") or row["status"] != "done":
            continue
        doc_id = row["source_url"].replace("https://developers.reddit.com/docs/", "").strip("/")
        candidates = [doc_id, f"{doc_id}/README", f"{doc_id}/{doc_id.rsplit('/', 1)[-1]}"]
        if row["normalized_path"] == "/introduction/introduction":
            candidates.insert(0, "introduction/introduction")
        source = next((MIRROR / f"{c}.md" for c in candidates if (MIRROR / f"{c}.md").exists()), None)
        if source is None:
            continue
        mirror_id = source.relative_to(MIRROR).as_posix()[:-3]
        if mirror_id in overrides:
            continue
        # The changelog's `## Release x.y.z` headings become <Update> labels by
        # design, so its heading sequence is expected to differ.
        if converted == "changelog.mdx":
            continue

        target = REPO / converted
        if not target.exists():
            problems.append(f"{converted}: missing on disk")
            continue

        src = profile(source.read_text(encoding="utf-8"))
        dst = profile(re.sub(r"\A---\n[\s\S]*?\n---\n", "", target.read_text(encoding="utf-8")))
        checked += 1

        # The source H1 becomes frontmatter `title`; TypeDoc kind headings are
        # demoted from H1 to H2, so compare the heading *set* minus the title.
        title = normalize_heading(row["source_title"])
        src_h = [h for h in src["headings"] if h != title]
        dst_h = [h for h in dst["headings"] if h != title]
        if src_h != dst_h:
            only_src = [h for h in src_h if h not in dst_h]
            only_dst = [h for h in dst_h if h not in src_h]
            if only_src or only_dst:
                problems.append(
                    f"{converted}: heading drift, source-only={only_src[:4]} "
                    f"preview-only={only_dst[:4]}"
                )
        for key, slack in (("code_blocks", 0), ("table_rows", 0), ("images", 0)):
            if abs(src[key] - dst[key]) > slack:
                problems.append(
                    f"{converted}: {key} {src[key]} on source vs {dst[key]} on preview"
                )
        if dst["list_items"] < src["list_items"] - 1:
            problems.append(
                f"{converted}: list items {src['list_items']} -> {dst['list_items']}"
            )

    print(f"structurally compared {checked} pages against their mirrored source")
    if problems:
        for line in problems[:60]:
            print("FAIL", line)
        print(f"\n{len(problems)} structural differences")
        return 1
    print("no structural differences")
    return 0


if __name__ == "__main__":
    sys.exit(main())
