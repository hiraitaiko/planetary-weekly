#!/usr/bin/env python3
"""週次 惑星科学論文ピック:
OpenAlex取得 -> キーワード絞り込み -> Claudeでスコア/要約(過去の★評価を学習) -> data/*.json 出力。
標準ライブラリのみ。ANTHROPIC_API_KEY が無い場合は LLM をスキップ (--no-llm 相当) して動作確認できる。
LLM(Claude API)がクレジット不足などで失敗した場合は alert.md を書いて異常終了する(ワークフローがIssueで通知)。
"""
import json, os, sys, re, time, datetime as dt, urllib.request, urllib.parse, urllib.error
from pathlib import Path

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text())
DATA = ROOT / "data"
UA = f"planetary-weekly/0.2 (mailto:{CFG['contact_email']})"
BATCH = 16


class LLMError(Exception):
    def __init__(self, kind, code, msg):
        super().__init__(f"{kind} {code}: {msg}")
        self.kind, self.code, self.msg = kind, code, msg


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
    papers = []
    for page in range(1, 11):  # page= 方式ならAPIキー不要(cursor= の2ページ目以降はキーが必要)
        q = urllib.parse.urlencode({
            "filter": flt, "per-page": 200, "page": page, "mailto": CFG["contact_email"],
            "select": "id,doi,title,publication_date,authorships,abstract_inverted_index,cited_by_count,primary_location",
        }, safe=":,|")
        d = None
        for attempt in range(4):  # 一時的な 401/429/5xx は待って再試行
            try:
                d = http_json("https://api.openalex.org/works?" + q); break
            except urllib.error.HTTPError as e:
                if e.code in (401, 429, 500, 502, 503) and attempt < 3:
                    time.sleep(5 * (attempt + 1)); continue
                print(f"[warn] {name} page {page}: HTTP {e.code} (取得済み {len(papers)} 本で打ち切り)", file=sys.stderr)
                return papers
        results = d.get("results", [])
        for w in results:
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
        if len(results) < 200:
            break
    return papers


# 語幹で拾いたいキーワード(後ろは何が付いてもよい)。それ以外は「単語として一致」(複数形の s は許可)
PREFIX_KW = {"astrobiolog", "habitab", "cryovolcan", "cryomagma", "serpentiniz", "biosignature", "prebiotic"}
# 分野外でも「話題の惑星科学論文」として拾うための一般キーワード(config.json に keywords_general があればそちらを優先)
DEFAULT_GENERAL = ["planet", "planetary", "exoplanet", "moon", "lunar", "asteroid", "comet", "meteorite", "meteoroid",
                   "venus", "mercury", "jupiter", "saturn", "uranus", "neptune", "pluto", "solar system", "protoplanetary",
                   "impact crater", "regolith", "chang'e", "osiris-rex", "hayabusa", "juno", "cassini", "dart"]


def _rx(k):
    pat = re.escape(k)
    return re.compile(r"\b" + pat + ("" if k in PREFIX_KW else r"s?\b"))


KW_RE = [(k, _rx(k)) for k in CFG["keywords"]]
GEN_RE = [(k, _rx(k)) for k in CFG.get("keywords_general", DEFAULT_GENERAL)]
SKIP_TITLE = re.compile(r"^(in this issue|this week|editorial|correction|erratum|retraction)", re.I)


def kw_hits(p):
    text = (p["title"] + " " + p["abstract"]).lower()
    return [k for k, rx in KW_RE if rx.search(text)]


def gen_hits(p):
    text = (p["title"] + " " + p["abstract"]).lower()
    return [k for k, rx in GEN_RE if rx.search(text)]


# ---------- Claude API ----------
def claude(prompt, max_tokens=4000):
    key = os.environ["ANTHROPIC_API_KEY"]
    body = json.dumps({"model": CFG["model"], "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    hdr = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for attempt in range(3):
        try:
            d = http_json("https://api.anthropic.com/v1/messages", body, hdr, timeout=180)
            return "".join(b.get("text", "") for b in d["content"])
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")[:400]
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(20 * (attempt + 1)); continue
            low = msg.lower()
            kind = "billing" if ("credit" in low or "billing" in low) else \
                   "auth" if e.code in (401, 403) else "other"
            raise LLMError(kind, e.code, msg)
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(10); continue
            raise LLMError("network", 0, str(e))


def load_ratings():
    f = DATA / "ratings.json"
    return json.loads(f.read_text()) if f.exists() else {}


def learn(ratings):
    """★評価から (高評価例, 低評価例, キーワード補正) を作る。"""
    items = sorted(ratings.values(), key=lambda r: r.get("t", ""), reverse=True)
    liked = [r for r in items if r.get("stars", 0) >= 4][:10]
    disliked = [r for r in items if 0 < r.get("stars", 0) <= 2][:10]
    acc = {}
    for r in ratings.values():
        s = r.get("stars")
        if s:
            for k in r.get("hits", []):
                acc.setdefault(k, []).append(s - 3)
    kwadj = {k: sum(v) / (len(v) + 2) for k, v in acc.items()}  # 件数が少ないうちは控えめに
    return liked, disliked, kwadj


def kw_bonus(p, kwadj):
    return max(-2.0, min(2.0, 0.5 * sum(kwadj.get(k, 0) for k in p["hits"])))


def pref_text(liked, disliked):
    if not liked and not disliked:
        return ""
    fmt = lambda rs: "\n".join(f"- {r.get('title','')} ({r.get('journal','')})" for r in rs) or "(なし)"
    return f"""
読者が過去に付けた評価(好みの傾向を学習し、relevance に反映してください):
高評価(★4-5):
{fmt(liked)}
低評価(★1-2):
{fmt(disliked)}
"""


def llm_score(cands, prefs):
    res = {}
    for s in range(0, len(cands), BATCH):
        items = [{"i": i, "title": p["title"], "journal": p["journal"], "abstract": p["abstract"][:1500]}
                 for i, p in enumerate(cands) if s <= i < s + BATCH]
        prompt = f"""あなたは惑星科学の研究者向けキュレーターです。読者の関心分野: {CFG['field_description']}
{prefs}
以下の論文それぞれについて JSON 配列のみを返してください(説明文やコードフェンスは不要)。
各要素: {{"i": 番号, "relevance": 0-10, "buzz": 0-10, "summary_ja": "日本語3〜4文の要約(アブストラクトに書かれた内容のみ。誇張や推測をしない)", "why": "選定理由を日本語1文"}}
- relevance: 読者の関心分野(火星・氷衛星・アストロバイオ)・過去の好みとの近さ。惑星科学ではあっても分野から遠い論文は低く、キーワードが偶然一致しただけの無関係な論文(地震、花崗岩の鉱物など)は0〜1にする
- buzz: 分野外にも波及しうる話題性・新規性(惑星科学コミュニティ全体で人気が出そうか)
アブストラクトが空/不十分な論文は summary_ja を空文字にし、relevance は低めにしてください。

論文:
{json.dumps(items, ensure_ascii=False)}"""
        txt = claude(prompt, 8000)
        m = re.search(r"\[.*\]", txt, re.S)
        for r in json.loads(m.group(0)):
            res[r["i"]] = r
    return res


def write_alert(e):
    reasons = {
        "billing": "**Claude APIのクレジットが不足しています。** Anthropic Console の Billing でクレジットを追加してください。",
        "auth": "Claude APIキーが無効です。GitHubの Secrets `ANTHROPIC_API_KEY` を確認してください。",
    }
    head = reasons.get(e.kind, "Claude APIの呼び出しに失敗しました。")
    (ROOT / "alert.md").write_text(
        f"@{os.environ.get('GITHUB_REPOSITORY_OWNER', '')} {head}\n\n"
        f"- 今週のピックは更新されていません。対処後に Actions から `weekly-picks` を再実行してください。\n"
        f"- 詳細: `{e.kind} / HTTP {e.code}`\n")


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
    liked, disliked, kwadj = learn(load_ratings())
    seen_f = DATA / "seen.json"
    prev = set(json.loads(seen_f.read_text())) if seen_f.exists() else set()
    seen, cands = set(prev), []
    for p in allp:
        if not p["title"] or SKIP_TITLE.search(p["title"]) or p["doi"] in seen:
            continue
        seen.add(p["doi"])
        p["hits"] = kw_hits(p)
        p["general"] = [] if p["hits"] else gen_hits(p)
        if p["hits"] or p["general"]:
            cands.append(p)
    cands.sort(key=lambda p: (len(p["hits"]) * 2 + len(p["general"])) * p["jweight"] + kw_bonus(p, kwadj), reverse=True)
    cands = cands[: CFG["candidate_limit"]]
    print(f"candidates: {len(cands)} / {len(allp)}", file=sys.stderr)

    if use_llm and cands:
        try:
            sc = llm_score(cands, pref_text(liked, disliked))
        except LLMError as e:
            write_alert(e)
            print(f"::error::Claude API failed: {e}", file=sys.stderr)
            sys.exit(1)
        for i, p in enumerate(cands):
            r = sc.get(i, {})
            p.update(relevance=r.get("relevance", 0), buzz=r.get("buzz", 0),
                     summary_ja=r.get("summary_ja", ""), why=r.get("why", ""))
    else:
        for p in cands:
            p.update(relevance=min(10, len(p["hits"]) * 2), buzz=0,
                     summary_ja="(LLM未実行: ANTHROPIC_API_KEY を設定すると日本語要約が入ります)", why="キーワード一致: " + ", ".join(p["hits"][:4]))

    for p in cands:
        p["score"] = round(0.6 * p["relevance"] + 0.3 * p["buzz"] + 1.0 * p["jweight"] + kw_bonus(p, kwadj), 2)
    if use_llm:
        min_rel = CFG.get("min_relevance", 3)
        cands = [p for p in cands if p["relevance"] >= min_rel or p["buzz"] >= 7]
    picks = sorted(cands, key=lambda p: p["score"], reverse=True)[: CFG["top_n"]]

    seen_f.write_text(json.dumps(sorted(prev | {p["doi"] for p in picks})))
    iso = today.isocalendar()
    wk = f"{iso[0]}-W{iso[1]:02d}"
    out = {"week": wk, "generated": today.isoformat(), "since": since, "n_scanned": len(allp), "picks": picks}
    (DATA / f"{wk}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    weeks = sorted(p.stem for p in DATA.glob("????-W??.json"))
    (DATA / "index.json").write_text(json.dumps(weeks))
    print(f"wrote data/{wk}.json ({len(picks)} picks)", file=sys.stderr)


if __name__ == "__main__":
    main()
