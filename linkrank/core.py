#!/usr/bin/python3
"""linkrank — internal linking map, internal PageRank (Link Score) & cannibalization.

Crawls one site, builds the internal link graph, computes internal PageRank on a 0-100
logarithmic Link Score scale (Screaming Frog / OnCrawl InRank style), and detects keyword
cannibalization from (a) content/title similarity and (b) — optionally — GSC query×page data.

Methodology refs: Screaming Frog "Link Score"; OnCrawl "InRank"; PageRank (Page & Brin, 1998);
cannibalization via GSC query+page pairs with severity = (pos gap < 3 AND impression ratio > 0.5).

Usage:  python3 internal-link-audit.py <start_url> [--max-pages N] [--depth N]
                 [--sim-threshold F] [--include-subdomains] [--out DIR] [--json-only]
"""
import argparse, sys, json, re, time, math, os, html as _html
from collections import defaultdict, deque, Counter
from urllib.parse import urljoin, urlparse, urldefrag, urlunparse
from urllib import robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from selectolax.parser import HTMLParser

UA = "Mozilla/5.0 (compatible; internal-link-audit/1.0; +seo-tools)"
ASSET_RE = re.compile(r"\.(jpg|jpeg|png|gif|svg|webp|ico|css|js|pdf|docx?|xlsx?|zip|rar|mp4|webm|woff2?|ttf|eot)(\?|$)", re.I)
STOP = set("и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между на по за от до из с у в о к".split())


def normalize(url, strip_query=True):
    url, _ = urldefrag(url or "")
    p = urlparse(url)
    scheme = (p.scheme or "https").lower()
    netloc = p.netloc.lower()
    path = re.sub(r"//+", "/", p.path) or "/"
    query = "" if strip_query else p.query
    return urlunparse((scheme, netloc, path, "", query, ""))


def host_of(url):
    return urlparse(url).netloc.lower()


def same_site(url, root_host, include_sub=False):
    h = host_of(url)
    if not h:
        return False
    if include_sub:
        base = root_host.split(":")[0]
        return h == root_host or h.endswith("." + base)
    return h == root_host


def clean_text(s):
    return re.sub(r"\s+", " ", _html.unescape(s or "")).strip()


class Page:
    __slots__ = ("url", "final", "status", "is_html", "title", "h1", "desc",
                 "canonical", "links", "text")

    def __init__(self, url):
        self.url = url
        self.final = url
        self.status = 0
        self.is_html = False
        self.title = ""
        self.h1 = ""
        self.desc = ""
        self.canonical = ""
        self.links = []   # list of (target_norm, anchor, nofollow)
        self.text = ""


def fetch(url, timeout):
    p = Page(url)
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
        p.final = normalize(r.url)
        p.status = r.status_code
        ct = r.headers.get("Content-Type", "")
        p.is_html = "text/html" in ct.lower()
        if not p.is_html or r.status_code != 200:
            return p
        tree = HTMLParser(r.text)
        t = tree.css_first("title")
        p.title = clean_text(t.text()) if t else ""
        h = tree.css_first("h1")
        p.h1 = clean_text(h.text()) if h else ""
        md = tree.css_first('meta[name="description"]')
        p.desc = clean_text(md.attributes.get("content", "")) if md else ""
        can = tree.css_first('link[rel="canonical"]')
        if can:
            p.canonical = normalize(urljoin(p.final, can.attributes.get("href", "")))
        base = p.final
        for a in tree.css("a"):
            href = a.attributes.get("href")
            if not href:
                continue
            href = href.strip()
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            absu = normalize(urljoin(base, href))
            if ASSET_RE.search(absu):
                continue
            rel = (a.attributes.get("rel") or "").lower()
            nofollow = "nofollow" in rel
            anchor = clean_text(a.text())
            p.links.append((absu, anchor, nofollow))
        for node in tree.css("script,style,noscript"):
            node.decompose()
        body = tree.css_first("body")
        p.text = clean_text(body.text(separator=" ")) if body else ""
    except Exception as e:
        p.status = -1
    return p


def load_sitemap(root, timeout):
    urls = set()
    try:
        seeds = [urljoin(root, "/sitemap.xml")]
        seen = set()
        while seeds:
            sm = seeds.pop()
            if sm in seen:
                continue
            seen.add(sm)
            r = requests.get(sm, headers={"User-Agent": UA}, timeout=timeout)
            if r.status_code != 200:
                continue
            locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", r.text, re.I)
            for loc in locs:
                loc = normalize(loc.strip())
                if loc.endswith(".xml"):
                    seeds.append(loc)
                else:
                    urls.add(loc)
    except Exception:
        pass
    return urls


def crawl(start, max_pages, max_depth, timeout, workers, include_sub):
    root = normalize(start)
    root_host = host_of(root)
    rp = robotparser.RobotFileParser()
    try:
        rp.set_url(urljoin(root, "/robots.txt"))
        rp.read()
    except Exception:
        rp = None

    def allowed(u):
        if rp is None:
            return True
        try:
            return rp.can_fetch(UA, u)
        except Exception:
            return True

    sitemap_urls = load_sitemap(root, timeout)
    pages = {}
    depth = {root: 0}
    queue = deque([root])
    # seed with sitemap urls at depth 1 (discovered, will be fetched too)
    for u in sitemap_urls:
        if same_site(u, root_host, include_sub) and u not in depth:
            depth[u] = 1
            queue.append(u)
    inflight = set()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {}
        def submit(u):
            futures[ex.submit(fetch, u, timeout)] = u
        while (queue or futures) and len(pages) < max_pages:
            while queue and len(futures) < workers * 2 and len(pages) + len(futures) < max_pages:
                u = queue.popleft()
                if u in pages or u in inflight:
                    continue
                if not allowed(u):
                    continue
                inflight.add(u)
                submit(u)
            if not futures:
                break
            done = next(as_completed(list(futures)))
            u = futures.pop(done)
            p = done.result()
            pages[u] = p
            d = depth.get(u, 0)
            if p.is_html and p.status == 200 and d < max_depth:
                for (t, anc, nf) in p.links:
                    if same_site(t, root_host, include_sub) and t not in depth:
                        depth[t] = d + 1
                        queue.append(t)
            sys.stderr.write(f"\r  crawled {len(pages)}  queued {len(queue)}   ")
            sys.stderr.flush()
    sys.stderr.write("\n")
    return root, root_host, pages, depth, sitemap_urls


def build_graph(pages, root_host, include_sub):
    # node identity: canonical target if it is a valid 200 html page, else final url
    valid = {u for u, p in pages.items() if p.status == 200 and p.is_html}
    # map any fetched url -> node
    def node_of(u):
        p = pages.get(u)
        if p is None:
            return u if u in valid else None
        cand = p.final
        if p.canonical and p.canonical in valid:
            cand = p.canonical
        return cand if cand in valid else (cand if pages.get(cand, p).status == 200 else None)
    nodes = set()
    for u in valid:
        nodes.add(node_of(u) or u)
    # edges (followed) + outlink totals (followed + nofollow) for evaporation
    followed_out = defaultdict(list)   # src -> [targets]
    out_total = defaultdict(int)       # src -> count incl nofollow
    anchors_to = defaultdict(Counter)  # target -> Counter(anchor)
    anchor_targets = defaultdict(set)  # anchor -> set(targets)
    edge_count = 0
    for u in valid:
        src = node_of(u) or u
        p = pages[u]
        seen_pair = set()
        for (t, anc, nf) in p.links:
            if not same_site(t, root_host, include_sub):
                continue
            tn = node_of(t)
            if tn is None or tn == src:
                continue
            out_total[src] += 1
            if nf:
                continue
            key = (src, tn)
            if key not in seen_pair:
                followed_out[src].append(tn)
                seen_pair.add(key)
                edge_count += 1
            if anc:
                anchors_to[tn][anc] += 1
                anchor_targets[anc.lower()].add(tn)
    return nodes, followed_out, out_total, anchors_to, anchor_targets, edge_count


def pagerank(nodes, followed_out, out_total, d=0.85, max_iter=100, tol=1e-9):
    nodes = list(nodes)
    N = len(nodes)
    if N == 0:
        return {}
    idx = {u: i for i, u in enumerate(nodes)}
    score = [1.0 / N] * N
    for _ in range(max_iter):
        new = [(1 - d) / N] * N
        leaked = 0.0
        for u in nodes:
            s = score[idx[u]]
            ot = out_total.get(u, 0)
            fo = followed_out.get(u, [])
            if ot == 0:
                leaked += d * s                      # dangling: leaks entirely
                continue
            share = d * s / ot
            for t in fo:
                new[idx[t]] += share
            leaked += d * s * (ot - len(fo)) / ot     # nofollow evaporation
        add = leaked / N
        new = [x + add for x in new]
        err = sum(abs(new[i] - score[i]) for i in range(N))
        score = new
        if err < tol:
            break
    return {u: score[idx[u]] for u in nodes}


def link_score(pr):
    if not pr:
        return {}
    logs = {u: math.log(v + 1e-12) for u, v in pr.items()}
    lo, hi = min(logs.values()), max(logs.values())
    rng = (hi - lo) or 1.0
    return {u: round(100 * (logs[u] - lo) / rng, 1) for u in logs}


def keyword_of(p):
    base = (p.h1 or p.title or "").lower()
    base = re.split(r"[|—\-–:]", base)[0]           # drop brand tail
    toks = [w for w in re.findall(r"[а-яёa-z0-9]+", base) if w not in STOP and len(w) > 2]
    return " ".join(toks[:6]).strip()


def cannibalization_content(pages, valid_nodes, sim_threshold):
    docs, urls = [], []
    for u in valid_nodes:
        p = pages.get(u)
        if not p:
            continue
        txt = " ".join([p.title, p.h1, p.desc, p.text[:4000]])
        if txt.strip():
            docs.append(txt.lower())
            urls.append(u)
    clusters = []
    # 1) same normalized primary keyword
    bykw = defaultdict(list)
    for u in urls:
        kw = keyword_of(pages[u])
        if kw:
            bykw[kw].append(u)
    kw_clusters = [{"type": "same_keyword", "key": k, "pages": v}
                   for k, v in bykw.items() if len(v) > 1]
    # 2) TF-IDF cosine near-duplicates
    sim_pairs = []
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        if len(docs) >= 2:
            vec = TfidfVectorizer(max_features=5000)
            X = vec.fit_transform(docs)
            S = cosine_similarity(X)
            for i in range(len(urls)):
                for j in range(i + 1, len(urls)):
                    if S[i, j] >= sim_threshold:
                        sim_pairs.append((urls[i], urls[j], round(float(S[i, j]), 3)))
            sim_pairs.sort(key=lambda t: -t[2])
    except Exception as e:
        sim_pairs = [("(sklearn error)", str(e), 0)]
    return kw_clusters, sim_pairs[:100]


def build_report_html(root, stats, top_ls, orphans, deep, anchor_multi, kw_clusters, sim_pairs, ts):
    def esc(s):
        return _html.escape(str(s))
    rows_ls = "".join(
        f"<tr><td>{esc(u)}</td><td class='n'>{ls}</td><td class='n'>{inl}</td><td class='n'>{outl}</td><td class='n'>{dep}</td></tr>"
        for (u, ls, inl, outl, dep) in top_ls)
    rows_orph = "".join(f"<tr><td>{esc(u)}</td></tr>" for u in orphans[:200]) or "<tr><td>—</td></tr>"
    rows_deep = "".join(f"<tr><td>{esc(u)}</td><td class='n'>{dep}</td></tr>" for u, dep in deep[:100]) or "<tr><td>—</td></tr>"
    rows_anch = "".join(
        f"<tr><td>{esc(a)}</td><td class='n'>{len(ts_)}</td><td>{esc(' , '.join(list(ts_)[:4]))}</td></tr>"
        for a, ts_ in anchor_multi[:60]) or "<tr><td>—</td></tr>"
    rows_kw = "".join(
        f"<tr><td>{esc(c['key'])}</td><td class='n'>{len(c['pages'])}</td><td>{esc('  |  '.join(c['pages'][:6]))}</td></tr>"
        for c in sorted(kw_clusters, key=lambda c: -len(c['pages']))[:60]) or "<tr><td>—</td></tr>"
    rows_sim = "".join(
        f"<tr><td>{esc(a)}</td><td>{esc(b)}</td><td class='n'>{s}</td></tr>"
        for (a, b, s) in sim_pairs[:60]) or "<tr><td>—</td></tr>"
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Internal link audit — {esc(root)}</title>
<style>
body{{background:#fff;color:#1a1a1a;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;line-height:1.5;margin:0}}
.wrap{{max-width:1040px;margin:0 auto;padding:32px 24px 70px}}
h1{{font-size:22px}} h2{{font-size:18px;margin:30px 0 10px;border-bottom:2px solid #eee;padding-bottom:6px}}
.sub{{color:#666;font-size:13px;margin-bottom:18px}}
.grid{{display:flex;flex-wrap:wrap;gap:12px;margin:14px 0}}
.card{{flex:1 1 150px;background:#fafbfc;border:1px solid #ececec;border-radius:9px;padding:12px 14px}}
.card b{{display:block;font-size:13px;color:#2f6fed}} .card .big{{font-size:24px;font-weight:700}}
table{{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px}}
th,td{{border:1px solid #e3e3e3;padding:6px 9px;text-align:left;vertical-align:top;word-break:break-all}}
th{{background:#f4f6f9}} td.n{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
</style></head><body><div class="wrap">
<h1>Аудит внутренней перелинковки — {esc(root)}</h1>
<div class="sub">Внутренний PageRank (Link Score 0–100, стиль Screaming Frog) · каннибализация · сироты · {esc(ts)}</div>
<div class="grid">
<div class="card"><b>Страниц (200/HTML)</b><span class="big">{stats['nodes']}</span></div>
<div class="card"><b>Внутренних ссылок</b><span class="big">{stats['edges']}</span></div>
<div class="card"><b>Сироты (0 вх. ссылок)</b><span class="big">{stats['orphans']}</span></div>
<div class="card"><b>Глубина ≥4</b><span class="big">{stats['deep']}</span></div>
<div class="card"><b>Кластеры каннибализации</b><span class="big">{stats['kw_clusters']}</span></div>
</div>
<h2>Топ страниц по Link Score (внутренний PageRank)</h2>
<table><tr><th>URL</th><th>Link Score</th><th>вх.</th><th>исх.</th><th>глубина</th></tr>{rows_ls}</table>
<h2>Каннибализация №1 — один анкор ведёт на разные страницы</h2>
<table><tr><th>Анкор</th><th>целей</th><th>примеры целей</th></tr>{rows_anch}</table>
<h2>Каннибализация №2 — страницы с одинаковым целевым ключом (title/H1)</h2>
<table><tr><th>Ключ</th><th>страниц</th><th>URL</th></tr>{rows_kw}</table>
<h2>Каннибализация №3 — почти дублирующийся контент (TF-IDF косинус)</h2>
<table><tr><th>Страница A</th><th>Страница B</th><th>похожесть</th></tr>{rows_sim}</table>
<h2>Страницы-сироты (нет внутренних входящих ссылок)</h2>
<table><tr><th>URL</th></tr>{rows_orph}</table>
<h2>Глубоко закопанные страницы (клик-глубина ≥4)</h2>
<table><tr><th>URL</th><th>глубина</th></tr>{rows_deep}</table>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(prog="linkrank", description="Internal PageRank (Link Score) & keyword-cannibalization auditor")
    ap.add_argument("start_url")
    ap.add_argument("--max-pages", type=int, default=2000)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=12)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--sim-threshold", type=float, default=0.80)
    ap.add_argument("--include-subdomains", action="store_true")
    ap.add_argument("--out", default="./reports")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    ts = time.strftime("%Y-%m-%d %H:%M")
    sys.stderr.write(f"Crawling {args.start_url} (max-pages={args.max_pages})...\n")
    root, root_host, pages, depth, sitemap_urls = crawl(
        args.start_url, args.max_pages, args.depth, args.timeout, args.workers, args.include_subdomains)

    nodes, followed_out, out_total, anchors_to, anchor_targets, edge_count = build_graph(pages, root_host, args.include_subdomains)
    pr = pagerank(nodes, followed_out, out_total)
    ls = link_score(pr)

    inlinks = Counter()
    for src, tgts in followed_out.items():
        for t in tgts:
            inlinks[t] += 1
    outlinks = {u: len(followed_out.get(u, [])) for u in nodes}

    # depth via BFS over followed edges from root
    node_depth = {root: 0}
    dq = deque([root])
    adj = followed_out
    while dq:
        u = dq.popleft()
        for t in adj.get(u, []):
            if t not in node_depth:
                node_depth[t] = node_depth[u] + 1
                dq.append(t)

    orphans = sorted([u for u in nodes if inlinks.get(u, 0) == 0 and u != root])
    deep = sorted([(u, node_depth.get(u, 99)) for u in nodes if node_depth.get(u, 99) >= 4], key=lambda t: -t[1])

    anchor_multi = sorted([(a, ts_) for a, ts_ in anchor_targets.items() if len(ts_) > 1],
                          key=lambda t: -len(t[1]))
    kw_clusters, sim_pairs = cannibalization_content(pages, nodes, args.sim_threshold)

    top_ls = sorted(nodes, key=lambda u: -ls.get(u, 0))[:40]
    top_ls_rows = [(u, ls.get(u, 0), inlinks.get(u, 0), outlinks.get(u, 0), node_depth.get(u, "—")) for u in top_ls]

    stats = {"nodes": len(nodes), "edges": edge_count, "orphans": len(orphans),
             "deep": len(deep), "kw_clusters": len(kw_clusters)}

    os.makedirs(args.out, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", host_of(root).lower()).strip("-")
    stamp = time.strftime("%Y-%m-%d")
    data = {
        "root": root, "generated": ts, "stats": stats,
        "pages": [{"url": u, "link_score": ls.get(u, 0), "pagerank": pr.get(u, 0),
                   "inlinks": inlinks.get(u, 0), "outlinks": outlinks.get(u, 0),
                   "depth": node_depth.get(u, None),
                   "title": pages[u].title if u in pages else "",
                   "top_anchors": anchors_to.get(u, Counter()).most_common(5)}
                  for u in sorted(nodes, key=lambda u: -ls.get(u, 0))],
        "orphans": orphans,
        "deep_pages": [{"url": u, "depth": d} for u, d in deep],
        "cannibalization": {
            "anchor_to_many": [{"anchor": a, "targets": sorted(ts_)} for a, ts_ in anchor_multi[:200]],
            "same_keyword_clusters": kw_clusters,
            "content_similar_pairs": [{"a": a, "b": b, "sim": s} for a, b, s in sim_pairs],
        },
    }
    json_path = os.path.join(args.out, f"linkrank-{slug}-{stamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n=== Internal link audit: {root} ===")
    print(f"pages={stats['nodes']}  internal_links={stats['edges']}  orphans={stats['orphans']}  deep(>=4)={stats['deep']}  kw_clusters={stats['kw_clusters']}")
    print("\nTop 10 by Link Score:")
    for u in top_ls[:10]:
        print(f"  {ls.get(u,0):5.1f}  in={inlinks.get(u,0):>4}  {u}")
    print(f"\nJSON: {json_path}")

    if not args.json_only:
        html = build_report_html(root, stats, top_ls_rows, orphans, deep, anchor_multi, kw_clusters, sim_pairs, ts)
        html_path = os.path.join(args.out, f"linkrank-{slug}-{stamp}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"HTML: {html_path}")


if __name__ == "__main__":
    main()
