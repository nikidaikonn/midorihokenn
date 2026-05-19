#!/usr/bin/env python3
"""
ameblo_scraper.py
=================
アメブロ「epbreading123」の全記事から
  - 読み聞かせた英語絵本のタイトル
  - 手遊びの名前
を抽出し、投稿年月ごとに整理したCSVを出力します。

【使い方】
  pip install requests beautifulsoup4
  python3 ameblo_scraper.py

【出力】
  ameblo_epbreading123.csv  (UTF-8 BOM付き / Googleスプレッドシートで直接開けます)

【オプション】
  --debug   : 各記事の抽出テキストを標準エラーに出力（パターン調整用）
  --limit N : N件だけ取得して動作確認（例: --limit 5）
  --start-page P : Pページ目から取得開始（再開用）
"""

import argparse
import csv
import re
import sys
import time
from collections import defaultdict
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ─── 設定 ───────────────────────────────────────────────────────────────────

BLOG_ID   = "epbreading123"
BASE_URL  = f"https://ameblo.jp/{BLOG_ID}"
DELAY_SEC = 1.5          # リクエスト間の待機秒数（サーバー負荷軽減）
OUTPUT    = "ameblo_epbreading123.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://ameblo.jp/",
}

# ─── 絵本タイトル抽出パターン ────────────────────────────────────────────────
#
# アメブロ記事でよく見られる書き方に対応:
#   「Brown Bear, Brown Bear」
#   『Goodnight Moon』
#   ☆ The Very Hungry Caterpillar
#   絵本：Go Away, Big Green Monster!
#   本日の絵本 / 今日の絵本 → 次行や同行の英語タイトル
#
BOOK_PATTERNS = [
    # 明示的なラベル付き（絵本：Title）
    r'(?:絵本|今日の絵本|本日の絵本|読んだ絵本|英語絵本)[：:\s]*[「『]?([A-Za-z][A-Za-z0-9 \'\-,!?\.&:]+[A-Za-z\?!])',
    # 鍵括弧・二重鍵括弧の中の英語（「Title」『Title』）
    r'[「『]([A-Z][A-Za-z0-9 \'\-,!?\.&:]+)[」』]',
    # "タイトル:" 系
    r'(?:タイトル|Title)[：:\s]+([A-Za-z][A-Za-z0-9 \'\-,!?\.&:]+)',
]

# ─── 手遊び抽出パターン ──────────────────────────────────────────────────────
#
# よく見られる書き方:
#   手遊び：「きらきら星」
#   手遊び → Twinkle Twinkle Little Star
#   ♪ Open Shut Them（手遊び）
#   今日の手遊び：〇〇
#
GAME_PATTERNS = [
    # ラベル後ろのテキスト（日本語・英語どちらも）。日本語助詞や「も」で打ち切り
    r'(?:手遊び|てあそび|finger\s*play|フィンガープレイ)[：:→\s]+[「『]?([^\n「」』。、！？\d　も]{2,40})',
    # 括弧内に「手遊び」
    r'([^\n（(「]{2,30})\s*[（(]\s*(?:手遊び|てあそび)[）)]',
    # ♪ マーク + テキスト（英語部分のみ、日本語が来たら打ち切り）
    r'♪\s*([A-Za-z][A-Za-z\s\'\-,!?\.&:]{2,40})',
]

# ─── ユーティリティ ──────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch(session: requests.Session, url: str, retries: int = 3) -> BeautifulSoup:
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            log(f"  ⚠ リトライ {attempt+1}/{retries-1}: {e}  ({wait}秒後)")
            time.sleep(wait)

# ─── エントリ一覧の取得 ───────────────────────────────────────────────────────

def _parse_entry_list(soup: BeautifulSoup) -> list[dict]:
    """エントリ一覧ページから記事URL・タイトル・日付を抽出する。"""
    entries = []

    # Amebloのエントリ一覧は複数の構造が存在するため多重対応
    candidates = (
        soup.select("article a[href*='/entry-']")
        or soup.select(".skin-archiveList a[href*='/entry-']")
        or soup.select("a[href*='/epbreading123/entry-']")
    )

    seen = set()
    for a in candidates:
        href = a.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)

        full_url = href if href.startswith("http") else "https://ameblo.jp" + href
        title = a.get_text(strip=True)

        # 日付は近傍の time タグから
        parent = a.find_parent(["article", "li", "div"])
        date_text = ""
        if parent:
            t = parent.find("time")
            if t:
                date_text = t.get("datetime", t.get_text(strip=True))

        entries.append({"url": full_url, "title": title, "raw_date": date_text})

    return entries


def get_all_entry_links(session: requests.Session, start_page: int = 1) -> list[dict]:
    all_entries = []
    page = start_page

    while True:
        url = (
            f"{BASE_URL}/entrylist.html"
            if page == 1
            else f"{BASE_URL}/entrylist-{page}.html"
        )
        log(f"📄 エントリ一覧 {page}ページ目: {url}")
        try:
            soup = fetch(session, url)
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                log("  → 最終ページに到達")
                break
            raise

        entries = _parse_entry_list(soup)
        if not entries:
            log("  → 記事が見つかりません。最終ページに到達。")
            break

        log(f"  → {len(entries)} 件取得")
        all_entries.extend(entries)

        page += 1
        time.sleep(DELAY_SEC)

    return all_entries

# ─── 記事本文の解析 ──────────────────────────────────────────────────────────

def _parse_date(soup: BeautifulSoup) -> str:
    """記事ページから日付文字列 (YYYY-MM-DD) を返す。"""
    import json as _json

    # 1) JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads(script.string or "")
            for key in ("datePublished", "dateCreated", "dateModified"):
                val = data.get(key, "")
                if val:
                    return val[:10]
        except Exception:
            pass

    # 2) meta タグ
    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("name", "")
        if any(k in prop for k in ("published_time", "date", "created")):
            content = meta.get("content", "")
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", content)
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # 3) time タグ・class 属性
    for sel in ["time[datetime]", ".skin-entryDate time", "time", "[class*='date']"]:
        elem = soup.select_one(sel)
        if not elem:
            continue
        dt = elem.get("datetime", "")
        if dt:
            return dt[:10]
        text = elem.get_text(strip=True)
        m = re.search(r"(\d{4})[年/\-](\d{1,2})[月/\-](\d{1,2})", text)
        if m:
            return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

    return ""


def _clean(text: str) -> str:
    """抽出テキストのクリーニング。"""
    text = text.strip()
    # 先頭の記号（♪ ☆ ★ ● など）を除去
    text = re.sub(r'^[♪☆★●◆◎○・►▶\-\*\s]+', "", text).strip()
    # 末尾の記号・助詞を除去
    text = re.sub(r'[「」『』（）()、。！？!?　\s]+$', "", text).strip()
    # 末尾に日本語が混入している場合、英語部分だけを残す（例: "Foo も楽しみました"→"Foo"）
    m = re.match(r'^([A-Za-z][A-Za-z0-9\s\'\-,!?\.&:]*[A-Za-z\?!])', text)
    if m and len(m.group(1)) >= 3:
        text = m.group(1).strip()
    return text


def _apply_patterns(text: str, patterns: list[str]) -> list[str]:
    results = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.MULTILINE | re.IGNORECASE):
            val = _clean(m.group(1))
            if val and 2 <= len(val) <= 80:
                results.append(val)
    return results


def _join_split_words(text: str) -> str:
    """画像等で分断された英単語を結合する（例: 'Welcome S\nong' → 'Welcome Song'）。

    最後の「単語」が1〜2文字のときだけ結合する（"S" + "ong" は結合、"Dad" + "sat" は結合しない）。
    """
    lines = text.split("\n")
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        last_word = re.search(r"(\S+)$", line)
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
        # 最後の単語が1〜2文字 かつ 次行が小文字で始まる → 語が分断されている
        if (last_word
                and len(last_word.group(1)) <= 2
                and next_line
                and re.match(r"^[a-z]", next_line)):
            result.append(line + next_line)
            i += 2
        else:
            result.append(line)
            i += 1
    return "\n".join(result)


def extract_books_and_games(soup: BeautifulSoup, debug: bool = False) -> tuple[list[str], list[str]]:
    """記事本文から絵本タイトルと手遊び名を抽出する。"""
    body = (
        soup.select_one(".skin-entryBody")
        or soup.select_one("[class*='entryBody']")
        or soup.select_one("article")
        or soup.body
    )
    raw_text = body.get_text("\n", strip=True) if body else ""
    # 分断単語を結合してから処理
    text = _join_split_words(raw_text)

    if debug:
        log("─── 本文テキスト（先頭600字） ───")
        log(text[:600])
        log("─────────────────────────────────")

    books = _apply_patterns(text, BOOK_PATTERNS)
    games = _apply_patterns(text, GAME_PATTERNS)

    lines = text.split("\n")
    for i, line in enumerate(lines):
        line_s = line.strip()

        # ── 絵本: 「本日の〇冊目」「今日の〇冊目」の次行 ──
        if re.search(r"(?:本日|今日)の\d+冊目", line_s):
            if i + 1 < len(lines):
                title = lines[i + 1].strip()
                if title and re.search(r"[A-Za-z]", title):
                    books.append(_clean(title))

        # ── 手遊び: 「手遊び歌」「今月の手遊び」などの次行 ──
        if re.search(r"手遊び|てあそび|finger.?play", line_s, re.IGNORECASE):
            # 同一行の「：→」以降
            m = re.search(r"[：:→]\s*(.+)", line_s)
            if m:
                val = _clean(m.group(1))
                if val:
                    games.append(val)
            # 次行（英語または短い日本語タイトルのみ）
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                jp_ratio = sum(1 for c in next_line if "　" <= c <= "鿿") / max(len(next_line), 1)
                if next_line and len(next_line) < 40 and jp_ratio < 0.6:
                    val = _clean(next_line)
                    if val:
                        games.append(val)

    # 単語1つだけ（"sat" など）は絵本タイトルとして除外
    books = [b for b in books if len(b.split()) >= 2 or (len(b) >= 4 and b[0].isupper())]
    # 重複除去（順序保持）
    books = list(dict.fromkeys(b for b in books if b))
    games = list(dict.fromkeys(g for g in games if g))

    return books, games


def scrape_entry(session: requests.Session, url: str, raw_date: str, debug: bool) -> dict:
    """単一記事ページをスクレイプして結果を返す。"""
    soup = fetch(session, url)

    date = _parse_date(soup) or raw_date

    title_elem = (
        soup.select_one("h1")
        or soup.select_one(".skin-entryTitle")
        or soup.select_one("[class*='entryTitle']")
    )
    title = title_elem.get_text(strip=True) if title_elem else ""

    books, games = extract_books_and_games(soup, debug=debug)

    return {
        "url":   url,
        "date":  date,
        "title": title,
        "books": books,
        "games": games,
    }

# ─── CSV 出力 ────────────────────────────────────────────────────────────────

def write_csv(results: list[dict], path: str) -> None:
    monthly: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        ym = r["date"][:7] if len(r.get("date", "")) >= 7 else "不明"
        monthly[ym].append(r)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["年月", "投稿日", "記事タイトル", "英語絵本タイトル", "手遊び名", "記事URL"])

        for ym in sorted(monthly.keys()):
            for r in sorted(monthly[ym], key=lambda x: x.get("date", "")):
                if not r["books"] and not r["games"]:
                    continue  # 絵本・手遊びどちらも空＝お知らせ記事は除外
                books_str = " / ".join(r["books"]) if r["books"] else ""
                games_str = " / ".join(r["games"]) if r["games"] else ""
                w.writerow([ym, r["date"], r["title"], books_str, games_str, r["url"]])

    log(f"\n✅ CSV 保存完了: {path}")

# ─── メイン ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="アメブロ epbreading123 スクレイパー")
    parser.add_argument("--debug",      action="store_true", help="各記事の本文テキストを表示")
    parser.add_argument("--limit",      type=int, default=0, help="取得記事数の上限（0=全件）")
    parser.add_argument("--start-page", type=int, default=1, help="一覧の開始ページ番号")
    args = parser.parse_args()

    session = get_session()

    # ステップ1: 全エントリのURLを収集
    log("=" * 60)
    log(f"🔍 ブログ: {BASE_URL}")
    log("=" * 60)
    entries = get_all_entry_links(session, start_page=args.start_page)
    log(f"\n📋 合計 {len(entries)} 件の記事を検出")

    if args.limit:
        entries = entries[: args.limit]
        log(f"   ※ --limit {args.limit} により先頭 {args.limit} 件のみ処理")

    # ステップ2: 各記事をスクレイプ
    results = []
    errors  = 0
    for i, entry in enumerate(entries, 1):
        log(f"\n[{i}/{len(entries)}] {entry['url']}")
        try:
            data = scrape_entry(session, entry["url"], entry.get("raw_date", ""), args.debug)
            log(f"  📅 {data['date']} | 絵本: {data['books']} | 手遊び: {data['games']}")
            results.append(data)
        except Exception as e:
            log(f"  ❌ エラー: {e}")
            errors += 1
        time.sleep(DELAY_SEC)

    # ステップ3: CSV出力
    log(f"\n処理完了 — 成功: {len(results)} 件 / エラー: {errors} 件")
    write_csv(results, OUTPUT)
    log(f"📁 ファイル: {OUTPUT}")
    log("   → Googleスプレッドシートで「ファイル > インポート」から読み込めます")


if __name__ == "__main__":
    main()
