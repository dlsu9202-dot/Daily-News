#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日新闻自动抓取 + 翻译 + 微信推送
部署在 GitHub Actions，完全免费
"""

import os
import re
import logging
import hashlib
import html
import sys
from datetime import datetime, timedelta
from typing import List, Dict

import yaml
import feedparser
import requests
from deep_translator import GoogleTranslator
from newspaper import Article, ArticleException

# ======== 日志 ========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ======== 全局配置 ========
def load_config(config_path: str = "config.yaml") -> dict:
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    # 从环境变量获取敏感信息
    if os.environ.get("SCT_KEY"):
        cfg['wechat_sct']['send_key'] = os.environ["SCT_KEY"]
    return cfg

def clean_html(raw: str) -> str:
    cleanr = re.compile('<.*?>')
    text = re.sub(cleanr, '', raw)
    return html.unescape(text).strip()

def generate_hash(title: str, link: str) -> str:
    return hashlib.md5(f"{title}{link}".encode('utf-8')).hexdigest()

def is_within_24h(published_parsed) -> bool:
    if not published_parsed:
        return True
    try:
        pub = datetime(*published_parsed[:6])
        return datetime.now() - pub <= timedelta(hours=24)
    except:
        return True

# ======== 翻译 ========
class Translator:
    def __init__(self, target_lang: str = "zh-CN"):
        self.target = target_lang
        self.translator = GoogleTranslator(source='auto', target=target_lang)

    def translate(self, text: str) -> str:
        if not text:
            return text
        try:
            # 限制长度防止超限
            if len(text) > 3000:
                text = text[:3000]
            return self.translator.translate(text)
        except Exception as e:
            logger.warning(f"翻译失败: {e}")
            return text  # 失败返回原文

# ======== 正文提取 ========
def fetch_full_text(url: str, lang: str = 'zh', timeout: int = 8) -> str:
    try:
        article = Article(url, language=lang if lang else 'zh')
        article.download(timeout=timeout)
        article.parse()
        text = article.text.strip()
        if text:
            limit = CONFIG['settings'].get('full_text_max_length', 500)
            return text[:limit] + ('...' if len(text) > limit else '')
        return ''
    except Exception as e:
        logger.warning(f"正文提取失败 {url}: {e}")
        return ''

# ======== 新闻抓取 ========
def fetch_news(config: dict) -> Dict[str, List[Dict]]:
    sources = config.get('sources', [])
    settings = config.get('settings', {})
    max_items = settings.get('max_items_per_source', 5)
    enable_full = settings.get('enable_full_text', True)
    news_by_lang = {"zh": [], "en": []}
    seen = set()

    for src in sources:
        name = src.get("name", "Unknown")
        url = src.get("url")
        lang = src.get("lang", "en")
        if not url:
            continue

        logger.info(f"抓取 {name} ({lang})")
        try:
            feed = feedparser.parse(url)
            entries = feed.entries[:max_items]
            for entry in entries:
                title = entry.get("title", "").strip()
                link = entry.get("link", "")
                summary = entry.get("summary", "")
                published = entry.get("published_parsed")

                if not title:
                    continue
                if not is_within_24h(published):
                    continue

                h = generate_hash(title, link)
                if h in seen:
                    continue
                seen.add(h)

                summary_clean = clean_html(summary) if summary else ""
                if len(summary_clean) > 300:
                    summary_clean = summary_clean[:300] + "..."

                time_str = datetime(*published[:6]).strftime("%m-%d %H:%M") if published else ""

                news_item = {
                    "title": title,
                    "link": link,
                    "summary": summary_clean,
                    "source": name,
                    "time": time_str,
                    "full_text": ""
                }

                # 抓取正文（仅中文和英文）
                if enable_full and lang in ('zh', 'en'):
                    news_item['full_text'] = fetch_full_text(link, lang)

                news_by_lang[lang].append(news_item)

        except Exception as e:
            logger.error(f"抓取源 {name} 失败: {e}")

    return news_by_lang

# ======== 构建 Markdown 消息 ========
def build_markdown(news_zh: List[Dict], news_en: List[Dict],
                   translator: Translator, date_str: str) -> str:
    lines = [f"## 📰 每日新闻简报 {date_str}\n"]

    if news_zh:
        lines.append("### 🇨🇳 国内要闻")
        for item in news_zh:
            body = item['full_text'] or item['summary'] or "暂无详细内容"
            lines.append(f"**{item['title']}**")
            lines.append(f"> {body}")
            lines.append(f"🔗 [阅读原文]({item['link']})  _{item['source']} {item['time']}_\n")

    if news_en:
        lines.append("### 🌍 国际要闻（翻译后）")
        for item in news_en:
            original_title = item['title']
            trans_title = translator.translate(original_title)
            raw_body = item['full_text'] or item['summary'] or ""
            trans_body = translator.translate(raw_body) if raw_body else "暂无详细内容"
            lines.append(f"**{trans_title}**")
            lines.append(f"> {trans_body}")
            lines.append(f"🔗 [阅读原文]({item['link']})  _{item['source']} {item['time']}  (原文: {original_title})_\n")

    if not news_zh and not news_en:
        lines.append("今日暂无重要新闻。")

    return "\n".join(lines)

# ======== 推送微信 ========
def push_wechat(send_key: str, title: str, content: str):
    url = f"https://sctapi.ftqq.com/{send_key}.send"
    try:
        resp = requests.post(url, data={"title": title, "desp": content}, timeout=15)
        if resp.status_code == 200:
            logger.info("微信推送成功")
        else:
            logger.error(f"微信推送失败: {resp.text}")
    except Exception as e:
        logger.error(f"推送异常: {e}")

# ======== 主流程 ========
def main():
    global CONFIG
    CONFIG = load_config()

    translator = Translator(CONFIG['translate']['target_lang'])

    # 抓取
    news_data = fetch_news(CONFIG)
    news_zh = news_data.get('zh', [])
    news_en = news_data.get('en', [])
    logger.info(f"抓取完成：中文 {len(news_zh)} 条，英文 {len(news_en)} 条")

    if not CONFIG['settings'].get('send_if_empty', False) and not news_zh and not news_en:
        logger.info("无新闻，退出")
        return

    date_str = datetime.now().strftime('%Y年%m月%d日')
    md = build_markdown(news_zh, news_en, translator, date_str)

    # 推送
    send_key = CONFIG.get('wechat_sct', {}).get('send_key', '')
    if send_key:
        push_wechat(send_key, f"每日新闻 {date_str}", md)
    else:
        logger.error("未找到 Server酱 SendKey，请检查配置或环境变量 SCT_KEY")

if __name__ == "__main__":
    main()
