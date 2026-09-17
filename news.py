"""해외 주요 언론 8곳 RSS → Claude로 같은 사건 묶기 → World TOP10 저장 + Discord 전송"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

KST = ZoneInfo("Asia/Seoul")
DATA_DIR = Path("data")
NEXT_DAY_UNTIL = 7  # 이 시각(KST) 이전 실행은 전날 22시 몫의 재시도로 취급

GN = "https://news.google.com/rss/search?q=site:{}+when:1d&hl=en-US&gl=US&ceid=US:en"
FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/news/rss.xml"),
    ("Reuters", GN.format("reuters.com")),
    ("AP", GN.format("apnews.com")),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("NBC News", "https://feeds.nbcnews.com/nbcnews/public/news"),
    ("The Guardian", "https://www.theguardian.com/international/rss"),
    ("DW", "https://rss.dw.com/rdf/rss-en-all"),
    ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
]
PER_FEED = 40                 # 언론사당 최대 헤드라인 수
MIN_FEEDS = 4                 # 이보다 적게 수집되면 실패 처리
MODEL = "claude-haiku-4-5-20251001"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal daily news digest)"}


def target_date(now):
    """실행이 지연돼 자정을 넘겼어도, 원래 몫이었던 날짜를 돌려준다."""
    if now.hour < NEXT_DAY_UNTIL:
        return (now - timedelta(days=1)).date()
    return now.date()


# ---------- 1. RSS 수집 ----------
def fetch_feed(name, url):
    for attempt in range(1, 4):
        try:
            res = requests.get(url, headers=HEADERS, timeout=30)
            res.raise_for_status()
            feed = feedparser.parse(res.content)
            if feed.entries:
                return feed.entries
            raise ValueError("항목 없음")
        except Exception as e:
            print(f"{name} 시도 {attempt} 실패: {e}")
            time.sleep(5)
    return []


def collect():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=26)
    items, ok = [], []
    for name, url in FEEDS:
        entries = fetch_feed(name, url)
        count = 0
        for e in entries:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not title or not link:
                continue
            published = e.get("published_parsed") or e.get("updated_parsed")
            if published and datetime(*published[:6], tzinfo=timezone.utc) < cutoff:
                continue
            # Google News 제목 끝의 " - Reuters" 같은 출처 표기 제거
            title = re.sub(r"\s+-\s+(Reuters|AP News|The Associated Press)$", "", title)
            items.append({"id": len(items), "source": name, "title": title, "url": link})
            count += 1
            if count == PER_FEED:
                break
        print(f"{name}: {count}건")
        if count:
            ok.append(name)
    return items, ok


# ---------- 2. Claude로 같은 사건 묶기 ----------
PROMPT = """Below are headlines from the past day from major English-language news outlets, one per line as [id] (outlet) title.

Your job is to find which news stories were covered by the MOST outlets.

Group headlines into stories. A story is one news development. Put these in the SAME story:
- different outlets reporting the same event with different wording or angles
  (e.g. "Fed raises rates" / "Powell signals more hikes ahead" / "Mortgage rates jump after Fed decision")
- reactions, analysis, and live updates about that same event
Keep them SEPARATE only if they are genuinely different events that merely share a broad topic
(e.g. two unrelated shootings, or two different AI companies' announcements).

Check every headline against the stories you have formed before starting a new story.

Return the 15 stories covered by the most distinct outlets, most-covered first. For each:
- "headline": a concise, neutral English headline (max 15 words)
- "summary": one neutral English sentence explaining what happened
- "ids": the ids of EVERY headline belonging to that story

Return ONLY valid JSON, no other text:
{"stories": [{"headline": "...", "summary": "...", "ids": [1, 2]}]}

Headlines:
"""


def cluster(items):
    lines = "\n".join(f"[{it['id']}] ({it['source']}) {it['title']}" for it in items)
    body = {
        "model": MODEL,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": PROMPT + lines}],
    }
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last_error = None
    for attempt in range(1, 4):
        try:
            res = requests.post("https://api.anthropic.com/v1/messages",
                                headers=headers, json=body, timeout=120)
            res.raise_for_status()
            data = res.json()
            text = "".join(b.get("text", "") for b in data["content"] if b["type"] == "text")
            usage = data.get("usage", {})
            print(f"Claude 토큰 사용: 입력 {usage.get('input_tokens')} / 출력 {usage.get('output_tokens')}")
            return json.loads(text[text.index("{"): text.rindex("}") + 1])["stories"]
        except Exception as e:
            last_error = e
            print(f"Claude 시도 {attempt} 실패: {e}")
            time.sleep(10)
    raise RuntimeError(f"Claude 요청 실패: {last_error}")


def build_top10(items, stories):
    by_id = {it["id"]: it for it in items}
    result = []
    for s in stories:
        members = [by_id[i] for i in s.get("ids", []) if isinstance(i, int) and i in by_id]
        if not members:
            continue
        sources, seen = [], set()
        for m in members:
            if m["source"] in seen:
                continue
            seen.add(m["source"])
            sources.append({"name": m["source"], "title": m["title"], "url": m["url"]})
        result.append({
            "headline": s.get("headline") or members[0]["title"],
            "summary": s.get("summary", ""),
            "source_count": len(sources),
            "sources": sources,
        })
    # 매체 수는 모델 판단 대신 실제로 다시 세서 정렬
    result.sort(key=lambda x: x["source_count"], reverse=True)
    result = result[:10]
    for i, r in enumerate(result, 1):
        r["rank"] = i
    return result


# ---------- 3. 저장 ----------
def save(date_str, now, feeds_ok, top10):
    DATA_DIR.mkdir(exist_ok=True)
    payload = {
        "date": date_str,
        "crawled_at": now.isoformat(timespec="seconds"),
        "feeds": feeds_ok,
        "items": top10,
    }
    (DATA_DIR / f"{date_str}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    index_path = DATA_DIR / "index.json"
    dates = json.loads(index_path.read_text()) if index_path.exists() else []
    index_path.write_text(json.dumps(sorted(set(dates) | {date_str}), indent=2))


# ---------- 4. Discord ----------
def best_link(story):
    direct = [s["url"] for s in story["sources"] if "news.google.com" not in s["url"]]
    return direct[0] if direct else story["sources"][0]["url"]


def send_discord(webhook, now, top10):
    lines, length = [], 0
    for s in top10:
        line = f"**{s['rank']}.** [{s['headline']}]({best_link(s)}) `{s['source_count']}개 매체`"
        if length + len(line) + 1 > 4000:  # Discord 한도 보호
            line = f"**{s['rank']}.** {s['headline']} `{s['source_count']}개 매체`"
        lines.append(line)
        length += len(line) + 1
    weekday = "월화수목금토일"[now.weekday()]
    embed = {
        "title": f"🌍 World TOP10 · {now.month}/{now.day}({weekday})",
        "description": "\n".join(lines),
        "footer": {"text": f"{now.strftime('%m/%d %H:%M')} 수집 · 영문 주요 언론 {len(FEEDS)}곳"},
        "color": 0x2F6BFF,
    }
    requests.post(webhook, json={"embeds": [embed]}, timeout=20).raise_for_status()


def main():
    now = datetime.now(KST)
    date_str = target_date(now).isoformat()

    if (DATA_DIR / f"{date_str}.json").exists():
        print(f"[{date_str}] 이미 저장됨, 종료")
        return

    items, feeds_ok = collect()
    if len(feeds_ok) < MIN_FEEDS:
        sys.exit(f"수집된 언론사가 {len(feeds_ok)}곳뿐이에요: {feeds_ok}")

    top10 = build_top10(items, cluster(items))
    if len(top10) < 5:
        sys.exit(f"사건을 {len(top10)}개만 묶었어요. 결과를 확인하세요.")

    save(date_str, now, feeds_ok, top10)
    print(f"[{date_str}] 저장 완료")
    for s in top10:
        print(f"{s['rank']:>2}. ({s['source_count']}) {s['headline']}")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if webhook:
        send_discord(webhook, now, top10)
        print("Discord 전송 완료")


if __name__ == "__main__":
    main()
