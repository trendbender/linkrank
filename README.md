# linkrank

**Internal PageRank (Link Score) & keyword-cannibalization auditor — free & open-source.**

`linkrank` crawls a website, builds its **internal link graph**, computes **internal PageRank on a
0–100 “Link Score” scale** (the same idea behind Screaming Frog’s *Link Score* and OnCrawl’s *InRank*),
and flags **keyword cannibalization** and **orphan pages** — all locally, with **no paid APIs**.

It answers questions that usually require paid tools:

- Which pages does my internal linking actually **concentrate authority on** — and which are starved?
- Which pages are **orphans** (zero internal inlinks) or buried too deep?
- Where is my site **cannibalizing itself** (one anchor → many targets, duplicate titles, near-duplicate content)?

> Made by [KlientLab](https://klientlab.ru/tools/). MIT licensed.

## Install

```bash
pip install linkrank
```

## Quick start

```bash
linkrank https://example.com
```

Outputs a machine-readable **JSON** and a self-contained **HTML report** into `./reports/`:

```
=== Internal link audit: https://example.com/ ===
pages=253  internal_links=1653  orphans=39  deep(>=4)=39  kw_clusters=11

Top 10 by Link Score:
  100.0  in= 253  https://example.com/
   28.2  in=  37  https://example.com/category-a/
   ...
JSON: reports/internal-link-audit-example-com-2026-07-06.json
HTML: reports/internal-link-audit-example-com-2026-07-06.html
```

## Options

```
linkrank <start_url> [options]
  --max-pages N          cap fully-crawled pages (default 2000)
  --depth N              max crawl depth from the start URL (default 10)
  --timeout S            per-request timeout, seconds (default 12)
  --workers N            concurrent requests (default 10)
  --sim-threshold F      content-similarity cutoff for cannibalization (default 0.80)
  --include-subdomains   treat subdomains as internal
  --out DIR              output directory (default ./reports)
  --json-only            skip the HTML report
```

## What it reports

- **Link Score (0–100)** per page — log-scaled internal PageRank, comparable to Screaming Frog.
- **Inlinks / outlinks**, click-**depth** from the homepage, **orphan** pages (0 internal inlinks).
- **Cannibalization**, three signals:
  1. one **anchor text → multiple target URLs**;
  2. pages sharing the same **primary keyword** (normalized `<title>`/`<h1>`);
  3. **near-duplicate content** (TF-IDF cosine ≥ threshold).

## Methodology

- **Internal PageRank** — power iteration, damping factor `α = 0.85` (Page & Brin, 1998). Each page
  passes its score divided by its number of followed out-links; `rel="nofollow"` links **evaporate**
  (counted in out-degree but transfer no score); dangling nodes are redistributed uniformly. The raw
  PageRank is reported both directly and as a **0–100 logarithmic Link Score** (min–max on `log`),
  matching the way Screaming Frog *Link Score* and OnCrawl *InRank* present internal authority.
- **Graph** — nodes are 200-OK internal HTML URLs; a page’s **canonical** target is used as its node
  identity when that target is itself a valid page; redirects are collapsed onto their final URL.
- **Cannibalization** — anchor-to-many-targets from the crawl, primary-keyword clustering from
  `<title>`/`<h1>`, and TF-IDF cosine similarity of body text.

References: Page, Brin, Motwani & Winograd, *The PageRank Citation Ranking* (1998); Screaming Frog
*Link Score*; OnCrawl *InRank*.

## Requirements

Python ≥ 3.10. Dependencies (installed automatically): `requests`, `selectolax`, `networkx`,
`scikit-learn`.

## Notes & limits

- Respects `robots.txt`; skips non-HTML, `mailto:`/`tel:`/anchors and asset URLs.
- Deterministic output (stable sort) — runs are diffable over time.
- Enforces a hard page cap and per-request timeout — it will not hang on large/slow sites.
- Static-HTML crawler: links injected purely client-side by JavaScript may not be seen. Server-rendered
  `<a href>` links (the vast majority) are captured.

## Contributing

Issues and PRs welcome. Ideas on the roadmap: JS rendering flag, GSC query×page cannibalization signal,
interactive graph export.

## License

MIT © 2026 [KlientLab](https://klientlab.ru/tools/).
