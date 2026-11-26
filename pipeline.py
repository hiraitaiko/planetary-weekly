#!/usr/bin/env python3
"""週次 惑星科学論文ピック: OpenAlex取得 -> キーワード絞り込み -> Claudeでスコア/要約 -> data/*.json 出力。
標準ライブラリのみ。ANTHROPIC_API_KEY が無い場合は LLM をスキップ (--no-llm 相当) して動作確認できる。
"""
import json, os, sys, re, datetime as dt, urllib.request, urllib.parse
from pathlib import Path

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text())
DATA = ROOT / "data"
UA = f"planetary-weekly/0.1 (mailto:{CFG['contact_email']})"


def http_json(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def abstract_from_inverted(inv):
    if not inv:
        return ""
    words = {}
    for w, poss in inv.items():
        for p in poss:
            words[p] = w
    return " ".join(words[i] for i in sorted(words))


def fetch_journal(name, info, since):
    issns = "|".join(info["issn"])
    flt = f"primary_location.source.issn:{issns},from_publication_date:{since},type:article"
    papers, cursor = [], "*"
    while cursor and len(papers) < 1500:
        q = urllib.parse.urlencode({
            "filter": flt, "per-page": 200, "cursor": cursor, "mailto": CFG["contact_email"],
            "select": "id,doi,title,publication_date,authorships,abstract_inverted_index,cited_by_count,primary_location",
        }, safe=":,|")
        d = http_json("https://api.openalex.org/works?" + q)
        for w in d.get("results", []):
            papers.append({
                "journal": name, "jweight": info["weight"],
                "title": (w.get("title") or "").strip(),
                "doi": w.get("doi"),
                "url": w.get("doi") or (w.get("primary_location") or {}).get("landing_page_url"),
                "date": w.get("publication_date"),
                "authors": [a["author"]["display_name"] for a in (w.get("authorships") or [])[:6]],
                "n_authors": len(w.get("authorships") or []),
                "abstract": abstract_from_inverted(w.get("abstract_inverted_index")),
                "cited_by": w.get("cited_by_count", 0),
            })
        cursor = d.get("meta", {}).get("next_cursor")
        if not d.get("results"):
            break
    return papers


KW_RE = [(k, re.compile(r"\b" + re.escape(k))) for k in CFG["keywords"]]
SKIP_TITLE = re.compile(r"^(in this issue|this week|editorial|correction|erratum|retraction)", re.I)


def kw_hits(p):
    text = (p["title"] + " " + p["abstract"]).lower()
    return [k for k, rx in KW_RE if rx.search(text)]


def claude(prompt, max_tokens=4000):
    key = os.environ["ANTHROPIC_API_KEY"]
    body = json.dumps({"model": CFG["model"], "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    d = http_json("https://api.anthropic.com/v1/messages", body,
                  {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, timeout=180)
    return "".join(b.get("text", "") for b in d["content"])


def llm_score(cands):
    items = [{"i": i, "title": p["title"], "journal": p["journal"], "abstract": p["abstract"][:1500]}
             for i, p in enumerate(cands)]
    prompt = f"""あなたは惑星科学の研究者向けキュレーターです。読者の関心分野: {CFG['field_description']}

以下の論文それぞれについて JSON 配列のみを返してください(説明文やコードフェンスは不要)。
各要素: {{"i": 番号, "relevance": 0-10, "buzz": 0-10, "summary_ja": "日本語3〜4文の要約(アブストラクトに書かれた内容のみ。誇張や推測をしない)", "why": "選定理由を日本語1文"}}
- relevance: 読者の関心分野との近さ
- buzz: 分野外にも波及しうる話題性・新規性(世間的に人気が出そうか)
アブストラクトが空/不十分な論文は summary_ja を空文字にし、relevance は低めにしてください。

論文:
{json.dumps(items, ensure_ascii=False)}"""
    txt = claude(prompt, 8000)
    m = re.search(r"\[.*\]", txt, re.S)
    return {r["i"]: r for r in json.loads(m.group(0))}


def main():
    use_llm = bool(os.environ.get("ANTHROPIC_API_KEY")) and "--no-llm" not in sys.argv
    today = dt.date.today()
    since = (today - dt.timedelta(days=CFG["days_back"])).isoformat()
    allp = []
    for name, info in CFG["journals"].items():
        j_since = (today - dt.timedelta(days=info.get("days_back", CFG["days_back"]))).isoformat()
        try:
            got = fetch_journal(name, info, j_since)
        except Exception as e:  # 1誌の失敗で全体を止めない
            print(f"[warn] {name}: {e}", file=sys.stderr); continue
        print(f"{name}: {len(got)} papers", file=sys.stderr)
        allp += got

    DATA.mkdir(exist_ok=True)
    seen_f = DATA / "seen.json"
    prev = set(json.loads(seen_f.read_text())) if seen_f.exists() else set()
    seen, cands = set(prev), []
    for p in allp:
        if not p["title"] or SKIP_TITLE.search(p["title"]) or p["doi"] in seen:
            continue
        seen.add(p["doi"])
        p["hits"] = kw_hits(p)
        if p["hits"]:
            cands.append(p)
    cands.sort(key=lambda p: (len(p["hits"]) * p["jweight"]), reverse=True)
    cands = cands[: CFG["candidate_limit"]]
    print(f"candidates: {len(cands)} / {len(allp)}", file=sys.stderr)

    if use_llm and cands:
        sc = llm_score(cands)
        for i, p in enumerate(cands):
            r = sc.get(i, {})
            p.update(relevance=r.get("relevance", 0), buzz=r.get("buzz", 0),
                     summary_ja=r.get("summary_ja", ""), why=r.get("why", ""))
    else:
        for p in cands:
            p.update(relevance=min(10, len(p["hits"]) * 2), buzz=0,
                     summary_ja="(LLM未実行: ANTHROPIC_API_KEY を設定すると日本語要約が入ります)", why="キーワード一致: " + ", ".join(p["hits"][:4]))

    for p in cands:
        p["score"] = round(0.6 * p["relevance"] + 0.3 * p["buzz"] + 1.0 * p["jweight"], 2)
    picks = sorted(cands, key=lambda p: p["score"], reverse=True)[: CFG["top_n"]]

    seen_f.write_text(json.dumps(sorted(prev | {p["doi"] for p in picks})))
    iso = today.isocalendar()
    wk = f"{iso[0]}-W{iso[1]:02d}"
    DATA.mkdir(exist_ok=True)
    out = {"week": wk, "generated": today.isoformat(), "since": since, "n_scanned": len(allp), "picks": picks}
    (DATA / f"{wk}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    weeks = sorted(p.stem for p in DATA.glob("????-W??.json"))
    (DATA / "index.json").write_text(json.dumps(weeks))
    print(f"wrote data/{wk}.json ({len(picks)} picks)", file=sys.stderr)


if __name__ == "__main__":
    main()
