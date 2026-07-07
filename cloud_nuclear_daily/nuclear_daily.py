from __future__ import annotations

import html
import json
import os
import smtplib
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from openai import OpenAI


TZ = ZoneInfo("Asia/Shanghai")
GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

TOPICS = {
    "铀资源开发": '(uranium OR "uranium mining" OR "uranium supply" OR 铀矿 OR 铀资源)',
    "核电技术研发": '("nuclear power" OR SMR OR "small modular reactor" OR "advanced reactor" OR 核电 OR 小堆 OR 先进堆)',
    "核聚变技术": '("fusion energy" OR "nuclear fusion" OR tokamak OR stellarator OR 聚变 OR 托卡马克)',
    "核燃料循环": '("nuclear fuel" OR HALEU OR enrichment OR reprocessing OR "spent fuel" OR 核燃料 OR 铀浓缩 OR 后处理 OR 乏燃料)',
}


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def today_cn() -> datetime:
    fixed = os.getenv("REPORT_DATE", "").strip()
    if fixed:
        return datetime.strptime(fixed, "%Y-%m-%d").replace(tzinfo=TZ)
    return datetime.now(TZ)


def window(today: datetime) -> tuple[datetime, datetime]:
    days = 3 if today.weekday() == 0 else 1
    start = (today - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = today.replace(hour=23, minute=59, second=59, microsecond=0)
    return start, end


def gdelt_time(value: datetime) -> str:
    return value.astimezone(ZoneInfo("UTC")).strftime("%Y%m%d%H%M%S")


def fetch_news(start: datetime, end: datetime) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for topic, query in TOPICS.items():
        params = {
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "sort": "HybridRel",
            "maxrecords": "20",
            "startdatetime": gdelt_time(start),
            "enddatetime": gdelt_time(end),
        }
        url = f"{GDELT_ENDPOINT}?{urlencode(params)}"
        try:
            response = requests.get(url, timeout=30)
            if response.status_code == 429:
                time.sleep(12)
                response = requests.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"Warning: skipped {topic} news fetch: {exc}")
            time.sleep(3)
            continue
        for article in response.json().get("articles", []):
            title = str(article.get("title") or "").strip()
            url = str(article.get("url") or "").strip()
            if not title or not url:
                continue
            key = url.split("?")[0].lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "topic": topic,
                    "title": title,
                    "url": url,
                    "source": str(article.get("domain") or article.get("sourcecountry") or "Unknown"),
                    "published": str(article.get("seendate") or article.get("datetime") or ""),
                    "snippet": str(article.get("snippet") or "")[:500],
                }
            )
        time.sleep(3)
    return items[:60]


def build_report(news: list[dict[str, str]], report_date: str, start: datetime, end: datetime) -> dict:
    prompt = f"""
你是核能产业研究员。请基于候选新闻生成中文核能产业日报。
日期：{report_date}
覆盖：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}（北京时间）
主题必须覆盖：铀资源开发、核电技术研发、核聚变技术、核燃料循环。
要求：只使用候选新闻；选择5-8条重点；输出严格JSON。
JSON字段：
executive_summary, sections{{uranium,nuclear_power,fusion,fuel_cycle}},
signals[{{label,text}}], watch_list[], news[{{title,date,region_actor,topic,summary,impact,source_name,source_url}}],
editor_note。
候选新闻：
{json.dumps(news, ensure_ascii=False)[:45000]}
"""
    try:
        client = OpenAI(api_key=env("OPENAI_API_KEY"))
        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip(),
            input=prompt,
            temperature=0.2,
        )
        text = response.output_text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return json.loads(text[text.find("{") : text.rfind("}") + 1])
    except Exception as exc:
        print(f"Warning: OpenAI report generation failed; using fallback summary: {exc}")
        return fallback_report(news, report_date)


def fallback_report(news: list[dict[str, str]], report_date: str) -> dict:
    by_topic = {topic: [] for topic in TOPICS}
    for item in news:
        by_topic.setdefault(item.get("topic", "其他"), []).append(item)

    def topic_text(topic: str) -> str:
        items = by_topic.get(topic) or []
        if not items:
            return "公开新闻源暂未抓取到高相关更新。"
        titles = "；".join(item.get("title", "") for item in items[:2])
        return f"抓取到 {len(items)} 条相关更新，重点包括：{titles}"

    detail_news = []
    for item in news[:8]:
        detail_news.append(
            {
                "title": item.get("title", ""),
                "date": item.get("published", report_date),
                "region_actor": item.get("source", "公开来源"),
                "topic": item.get("topic", ""),
                "summary": item.get("snippet") or item.get("title", ""),
                "impact": "建议持续跟踪该事项对供应链、项目进度、技术路线或燃料循环能力建设的影响。",
                "source_name": item.get("source", "来源"),
                "source_url": item.get("url", ""),
            }
        )

    return {
        "executive_summary": f"{report_date} 自动抓取到 {len(news)} 条核能产业相关新闻。OpenAI 智能摘要暂不可用，本邮件采用备用新闻汇总版，保留来源链接便于复核。",
        "sections": {
            "uranium": topic_text("铀资源开发"),
            "nuclear_power": topic_text("核电技术研发"),
            "fusion": topic_text("核聚变技术"),
            "fuel_cycle": topic_text("核燃料循环"),
        },
        "signals": [
            {"label": "供给信号", "text": topic_text("铀资源开发")},
            {"label": "技术信号", "text": topic_text("核电技术研发")},
            {"label": "前沿信号", "text": topic_text("核聚变技术")},
            {"label": "产业链信号", "text": topic_text("核燃料循环")},
        ],
        "watch_list": [item.get("title", "") for item in news[:5]],
        "news": detail_news,
        "editor_note": "本期为备用汇总模式：自动检索与邮件发送已完成，但 OpenAI API 当前额度不足，未生成深度研判文本。",
    }


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def card(title: str, text: str) -> str:
    return f"""
    <td style="width:25%;padding:10px;border:1px solid #b9d6f3;background:#f8fbff;vertical-align:top">
      <b style="color:#155da8">{esc(title)}</b>
      <p style="margin:8px 0 0;color:#53657d;font-size:13px">{esc(text)}</p>
    </td>
    """


def render_html(report: dict, report_date: str, start: datetime, end: datetime) -> str:
    sections = report.get("sections") or {}
    signals = report.get("signals") or []
    watch = report.get("watch_list") or []
    news = report.get("news") or []

    signal_rows = "".join(
        f"<li><b>{esc(item.get('label'))}：</b>{esc(item.get('text'))}</li>"
        for item in signals[:4]
        if isinstance(item, dict)
    )
    watch_rows = "".join(f"<li>{esc(item)}</li>" for item in watch[:5])
    news_rows = ""
    for item in news:
        if not isinstance(item, dict):
            continue
        url = esc(item.get("source_url"))
        link = f'<a href="{url}" style="color:#155da8">{esc(item.get("source_name") or "来源")}</a>' if url else esc(item.get("source_name"))
        news_rows += f"""
        <div style="border:1px solid #d9e8f7;border-radius:8px;padding:14px;margin:12px 0;background:#fff">
          <h3 style="margin:0 0 8px;color:#0b2f5b;font-size:16px">{esc(item.get("title"))}</h3>
          <p style="margin:0 0 8px;color:#667085;font-size:12px">日期：{esc(item.get("date"))} ｜ 主体：{esc(item.get("region_actor"))} ｜ 板块：{esc(item.get("topic"))}</p>
          <p style="margin:0 0 8px">{esc(item.get("summary"))}</p>
          <p style="margin:0;padding:8px;background:#f4f9ff"><b>产业影响：</b>{esc(item.get("impact"))}</p>
          <p style="margin:8px 0 0;color:#667085;font-size:12px"><b>来源：</b>{link}</p>
        </div>
        """

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"></head>
<body style="margin:0;background:#eef5fb;color:#172033;font-family:Microsoft YaHei,Arial,sans-serif;line-height:1.55">
<div style="max-width:794px;margin:0 auto;padding:24px">
  <section style="background:#fff;border:1px solid #d6e4f2">
    <div style="background:#0b2f5b;color:#fff;padding:30px 36px;border-bottom:5px solid #17a2c6">
      <div style="font-size:13px;letter-spacing:.08em">NUCLEAR INDUSTRY DAILY BRIEF</div>
      <h1 style="margin:10px 0 0;font-size:32px">核能产业日报</h1>
      <p>铀资源开发｜核电技术研发｜核聚变技术｜核燃料循环产业</p>
    </div>
    <div style="padding:24px 36px">
      <p style="color:#667085">日期：{esc(report_date)} ｜ 覆盖：{start:%m-%d} 至 {end:%m-%d} ｜ 重点新闻：{len(news)} 条</p>
      <h2 style="color:#0b2f5b;border-left:6px solid #155da8;padding-left:10px">一、核心摘要</h2>
      <div style="background:#e8f2ff;border-left:5px solid #155da8;padding:14px;border-radius:8px">{esc(report.get("executive_summary"))}</div>
      <h2 style="color:#0b2f5b;border-left:6px solid #155da8;padding-left:10px">二、四大板块概览</h2>
      <table style="width:100%;border-collapse:separate;border-spacing:8px"><tr>
        {card("铀资源开发", sections.get("uranium"))}
        {card("核电技术研发", sections.get("nuclear_power"))}
        {card("核聚变技术", sections.get("fusion"))}
        {card("核燃料循环", sections.get("fuel_cycle"))}
      </tr></table>
      <h2 style="color:#0b2f5b;border-left:6px solid #155da8;padding-left:10px">三、值得关注的信号</h2>
      <ul>{signal_rows}</ul>
      <h2 style="color:#0b2f5b;border-left:6px solid #155da8;padding-left:10px">四、今日重点跟踪清单</h2>
      <ol>{watch_rows}</ol>
    </div>
  </section>
  <section style="background:#fff;border:1px solid #d6e4f2;margin-top:24px;padding:24px 36px">
    <h2 style="color:#0b2f5b;border-left:6px solid #155da8;padding-left:10px">新闻明细与产业影响</h2>
    {news_rows}
    <h2 style="color:#0b2f5b;border-left:6px solid #155da8;padding-left:10px">编辑说明</h2>
    <div style="background:#e8f2ff;border-left:5px solid #155da8;padding:14px;border-radius:8px">{esc(report.get("editor_note"))}</div>
  </section>
</div>
</body></html>"""


def send_email(subject: str, html_body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = env("GMAIL_ADDRESS")
    message["To"] = env("RECIPIENT_EMAIL")
    message.set_content("核能产业日报。请使用支持 HTML 的邮件客户端查看蓝白模板版。")
    message.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(env("GMAIL_ADDRESS"), env("GMAIL_APP_PASSWORD"))
        smtp.send_message(message)


def main() -> None:
    today = today_cn()
    report_date = today.strftime("%Y-%m-%d")
    start, end = window(today)
    news = fetch_news(start, end)
    if not news:
        raise RuntimeError("No news collected; aborting to avoid an empty report.")
    report = build_report(news, report_date, start, end)
    send_email(f"核能产业日报 - {report_date}", render_html(report, report_date, start, end))
    print(f"Sent nuclear daily report for {report_date}.")


if __name__ == "__main__":
    main()
