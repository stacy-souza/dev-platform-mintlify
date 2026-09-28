/* Batch HTML -> markdown for the Reddit Ads API docs.
 *
 * The Ads API site is Docusaurus but, unlike developers.reddit.com/docs, it
 * serves no raw `.md` endpoint, so narrative pages are converted from the
 * rendered article DOM. Docusaurus' own widgets (admonitions, tab groups,
 * code blocks with titles, collapsible details) are mapped to their Mintlify
 * equivalents here rather than left as raw HTML.
 *
 * Usage: node ads_html_to_md.mjs <input.json> <output.json>
 *   input:  { slug: articleHtml }
 *   output: { slug: markdown }
 */
import fs from "node:fs";
import TurndownService from "/workspace/.crawl/node_modules/turndown/lib/turndown.cjs.js";
import { gfm } from "/workspace/.crawl/node_modules/turndown-plugin-gfm/lib/turndown-plugin-gfm.cjs.js";

const [, , inPath, outPath] = process.argv;
const input = JSON.parse(fs.readFileSync(inPath, "utf8"));

const ADMONITION = {
  note: "Note",
  tip: "Tip",
  info: "Info",
  warning: "Warning",
  caution: "Warning",
  danger: "Warning",
};

const td = new TurndownService({
  headingStyle: "atx",
  codeBlockStyle: "fenced",
  bulletListMarker: "-",
  emDelimiter: "_",
});

// Tables are the main reason the GFM plugin is needed: the What's New page and
// most guides document fields and enums as tables, which core Turndown flattens
// into one run of text.
td.use(gfm);

// Chrome that should never reach the converted page.
td.remove(["script", "style", "nav", "header", "button"]);

td.addRule("drop-chrome", {
  filter: (node) => {
    const c = node.getAttribute && (node.getAttribute("class") || "");
    return (
      /breadcrumbs|theme-doc-toc|tocCollapsible|pagination-nav|theme-doc-footer|theme-doc-version-badge|hash-link|theme-last-updated|anchorWithStickyNavbar|feedback/i.test(
        c
      ) ||
      // Controls belonging to the source's interactive request builders. The
      // template they wrap is kept; the Clear/Reset chrome is not portable.
      /^(inlineToolbar|toolbarActions|toolbarLinkButton|warningBadge|languageTag|copyButton)/i.test(c) ||
      /^(Was this helpful|Edit this page|Fill all fields|Clear|Reset)$/i.test(
        (node.textContent || "").trim()
      )
    );
  },
  replacement: () => "",
});

td.addRule("admonition", {
  filter: (node) =>
    node.nodeName === "DIV" && /theme-admonition/.test(node.getAttribute("class") || ""),
  replacement: (content, node) => {
    const cls = node.getAttribute("class") || "";
    const kind =
      Object.keys(ADMONITION).find((k) => new RegExp(`admonition-${k}\\b`, "i").test(cls)) ||
      Object.keys(ADMONITION).find((k) => new RegExp(`\\b${k}\\b`, "i").test(cls)) ||
      "note";
    const tag = ADMONITION[kind];
    // The first line is Docusaurus' own heading chip; drop it when it is just
    // the admonition name so the callout does not repeat itself.
    const lines = content.trim().split("\n");
    if (lines.length && new RegExp(`^\\**${kind}\\**$`, "i").test(lines[0].trim())) lines.shift();
    return `\n\n<${tag}>\n\n${lines.join("\n").trim()}\n\n</${tag}>\n\n`;
  },
});

td.addRule("tabs", {
  filter: (node) =>
    node.nodeName === "DIV" && /tabs-container/.test(node.getAttribute("class") || ""),
  replacement: (content, node) => {
    const labels = [...node.querySelectorAll("li[role='tab'], .tabs__item")].map((li) =>
      (li.textContent || "").trim()
    );
    const panels = [...node.querySelectorAll("div[role='tabpanel'], .margin-top--md > div")];
    if (!labels.length) return `\n\n${content}\n\n`;
    const parts = labels.map((label, i) => {
      const panel = panels[i];
      const body = panel ? td.turndown(panel.innerHTML).trim() : "";
      return `<Tab title="${label.replace(/"/g, "'")}">\n\n${body}\n\n</Tab>`;
    });
    return `\n\n<Tabs>\n\n${parts.join("\n\n")}\n\n</Tabs>\n\n`;
  },
});

td.addRule("details", {
  filter: (node) => node.nodeName === "DETAILS",
  replacement: (content, node) => {
    const summary = node.querySelector("summary");
    const title = summary ? (summary.textContent || "").trim().replace(/"/g, "'") : "Details";
    if (summary) summary.remove();
    const body = td.turndown(node.innerHTML).trim();
    return `\n\n<Accordion title="${title}">\n\n${body}\n\n</Accordion>\n\n`;
  },
});

td.addRule("codeblock", {
  filter: (node) =>
    node.nodeName === "DIV" && /theme-code-block|codeBlockContainer/.test(node.getAttribute("class") || ""),
  replacement: (content, node) => {
    const titleEl = node.querySelector(".codeBlockTitle, [class*=codeBlockTitle]");
    const title = titleEl ? (titleEl.textContent || "").trim() : "";
    const pre = node.querySelector("pre");
    const code = pre ? pre.textContent.replace(/\n$/, "") : (node.textContent || "").trim();
    const cls = (node.getAttribute("class") || "") + " " + ((pre && pre.getAttribute("class")) || "");
    const lang = (cls.match(/language-([a-z0-9+#-]+)/i) || [, ""])[1];
    const head = [lang || "", title ? `title="${title.replace(/"/g, "'")}"` : ""]
      .filter(Boolean)
      .join(" ");
    return `\n\n\`\`\`${head}\n${code}\n\`\`\`\n\n`;
  },
});

td.addRule("plain-pre", {
  filter: (node) => node.nodeName === "PRE",
  replacement: (content, node) => {
    const cls = node.getAttribute("class") || "";
    const lang = (cls.match(/language-([a-z0-9+#-]+)/i) || [, ""])[1];
    return `\n\n\`\`\`${lang}\n${node.textContent.replace(/\n$/, "")}\n\`\`\`\n\n`;
  },
});

const out = {};
for (const [slug, html] of Object.entries(input)) {
  try {
    out[slug] = td
      .turndown(html)
      .replace(/\u200b/g, "")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  } catch (err) {
    out[slug] = "";
    process.stderr.write(`convert failed ${slug}: ${String(err).slice(0, 120)}\n`);
  }
}
fs.writeFileSync(outPath, JSON.stringify(out));
console.log(`converted ${Object.keys(out).length} pages`);
