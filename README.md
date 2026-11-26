# Planetary Weekly
火星・氷衛星・アストロバイオ論文を毎週ピックして日本語要約する静的サイト。

- `config.json` : 対象誌(ISSN)、キーワード、関心分野の説明、上位N件
- `pipeline.py` : OpenAlex取得 → キーワード絞り込み → Claudeでスコア/要約 → `data/YYYY-Www.json`
- `index.html`  : data/ を読んで表示(静的)
- `.github/workflows/weekly.yml` : 毎週月曜7:00 JSTに自動実行

## 使い方
1. GitHubにpush、Settings → Secrets に `ANTHROPIC_API_KEY` を登録
2. Settings → Pages でmainブランチを公開
3. `config.json` の `contact_email` を自分のアドレスに変更
4. ローカル試験: `python3 pipeline.py --no-llm` / `ANTHROPIC_API_KEY=... python3 pipeline.py` / `python3 -m http.server`
