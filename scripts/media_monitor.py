from __future__ import annotations

import argparse
import datetime as dt
import email.message
import hashlib
import logging
import os
import re
import smtplib
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


LOGGER = logging.getLogger("media_monitor")
USER_AGENT = (
    "Mozilla/5.0 (compatible; CodexMediaMonitor/0.1; "
    "+https://github.com/abatson-gavekal/monitor)"
)


@dataclass(frozen=True)
class Publication:
    key: str
    name: str
    layout_url_template: str
    content_url_pattern: re.Pattern[str]
    layout_url_pattern: re.Pattern[str]
    target_bylines: tuple[str, ...]


@dataclass(frozen=True)
class Article:
    source_key: str
    source_name: str
    publication_date: str
    title: str
    byline: str
    url: str
    text: str


PUBLICATIONS = (
    Publication(
        key="people_daily",
        name="人民日报",
        layout_url_template=(
            "https://paper.people.com.cn/rmrb/pc/layout/{yyyymm}/{dd}/node_01.html"
        ),
        content_url_pattern=re.compile(
            r"/rmrb/pc/content/\d{6}/\d{2}/content_\d+\.html$"
        ),
        layout_url_pattern=re.compile(r"/rmrb/pc/layout/\d{6}/\d{2}/node_\d+\.html$"),
        target_bylines=("钟才文",),
    ),
    Publication(
        key="economic_daily",
        name="经济日报",
        layout_url_template="http://paper.ce.cn/pc/layout/{yyyymm}/{dd}/node_01.html",
        content_url_pattern=re.compile(r"/pc/content/\d{6}/\d{2}/content_\d+\.html$"),
        layout_url_pattern=re.compile(r"/pc/layout/\d{6}/\d{2}/node_\d+\.html$"),
        target_bylines=("金观平",),
    ),
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor People's Daily and Economic Daily e-paper bylines."
    )
    parser.add_argument(
        "--date",
        help="Publication date in YYYY-MM-DD format. Defaults to today's Asia/Shanghai date.",
    )
    parser.add_argument(
        "--db",
        default="data/media_monitor.sqlite3",
        help="SQLite database path.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=25.0,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print email content and do not send or mark matches emailed.",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Store articles and matches, but do not send alerts.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser.parse_args(argv)


def default_china_date() -> dt.date:
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
    except Exception:
        return dt.date.today()


def publication_date_from_arg(value: str | None) -> dt.date:
    if value:
        return dt.date.fromisoformat(value)
    return default_china_date()


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL,
            source_name TEXT NOT NULL,
            publication_date TEXT NOT NULL,
            title TEXT NOT NULL,
            byline TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL,
            source_name TEXT NOT NULL,
            publication_date TEXT NOT NULL,
            title TEXT NOT NULL,
            byline TEXT NOT NULL,
            url TEXT NOT NULL,
            target_byline TEXT NOT NULL,
            text TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            emailed_at TEXT,
            UNIQUE(url, target_byline)
        )
        """
    )
    conn.commit()
    return conn


def fetch_html(session: requests.Session, url: str, timeout: float) -> str:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding
    return response.text


def date_parts(publication_date: dt.date) -> dict[str, str]:
    return {
        "yyyymm": publication_date.strftime("%Y%m"),
        "dd": publication_date.strftime("%d"),
    }


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def visible_lines(soup: BeautifulSoup) -> list[str]:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = []
    for raw_line in soup.get_text("\n").splitlines():
        line = normalize_space(raw_line)
        if line:
            lines.append(line)
    return lines


def unique_in_order(values: Iterable[str]) -> list[str]:
    seen = set()
    unique = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def collect_layout_urls(
    publication: Publication,
    publication_date: dt.date,
    session: requests.Session,
    timeout: float,
) -> list[str]:
    start_url = publication.layout_url_template.format(**date_parts(publication_date))
    to_visit = [start_url]
    visited: set[str] = set()
    layout_urls: list[str] = []

    while to_visit:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)
        layout_urls.append(url)
        LOGGER.info("Fetching layout: %s", url)
        html = fetch_html(session, url, timeout)
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            candidate = urljoin(url, link["href"])
            if publication.layout_url_pattern.search(candidate) and candidate not in visited:
                to_visit.append(candidate)
    return unique_in_order(layout_urls)


def collect_article_urls(publication: Publication, layout_html: str, layout_url: str) -> list[str]:
    soup = BeautifulSoup(layout_html, "html.parser")
    urls = []
    for link in soup.find_all("a", href=True):
        for candidate in content_url_candidates(layout_url, link["href"]):
            if publication.content_url_pattern.search(candidate):
                urls.append(candidate)
                break
    return unique_in_order(urls)


def content_url_candidates(layout_url: str, href: str) -> list[str]:
    candidates = [urljoin(layout_url, href)]
    if "/content/" in href and "/pc/" in layout_url:
        pc_root = layout_url.split("/pc/", maxsplit=1)[0] + "/pc/"
        normalized_href = re.sub(r"^(\.\./)+", "", href.lstrip("/"))
        candidates.append(urljoin(pc_root, normalized_href))
    return unique_in_order(candidates)


def title_from_soup(soup: BeautifulSoup, lines: list[str]) -> str:
    for selector in ("h1", ".title", "#title"):
        tag = soup.select_one(selector)
        if tag:
            title = normalize_space(tag.get_text(" "))
            if title:
                return title

    meta = soup.find("meta", attrs={"property": "og:title"}) or soup.find(
        "meta", attrs={"name": "ArticleTitle"}
    )
    if meta and meta.get("content"):
        return normalize_space(meta["content"])

    for line in lines:
        if len(line) >= 4 and not line.startswith(("第", "版", "人民网", "经济日报")):
            return line
    return ""


def article_text_from_soup(soup: BeautifulSoup, lines: list[str]) -> str:
    selectors = (
        "#ozoom",
        "#zoom",
        ".articleContent",
        ".article-content",
        ".content",
        ".TRS_Editor",
        ".text",
        ".main",
    )
    for selector in selectors:
        tag = soup.select_one(selector)
        if not tag:
            continue
        paragraphs = [
            normalize_space(p.get_text(" "))
            for p in tag.find_all(["p", "div"])
            if normalize_space(p.get_text(" "))
        ]
        text = "\n\n".join(unique_in_order(paragraphs))
        if len(text) >= 20:
            return text

    paragraph_lines = [
        normalize_space(p.get_text(" "))
        for p in soup.find_all("p")
        if normalize_space(p.get_text(" "))
    ]
    if paragraph_lines:
        return "\n\n".join(unique_in_order(paragraph_lines))

    return "\n".join(lines)


def byline_from_lines(title: str, lines: list[str], target_bylines: tuple[str, ...]) -> str:
    title_indexes = [index for index, line in enumerate(lines) if line == title]
    candidate_windows = []
    if title_indexes:
        for index in title_indexes:
            candidate_windows.extend(lines[index + 1 : index + 8])
    candidate_windows.extend(lines[:20])

    for target in target_bylines:
        for line in candidate_windows:
            if target in line:
                return clean_byline(line)

    for line in candidate_windows:
        cleaned = clean_byline(line)
        if is_plausible_byline(cleaned):
            return cleaned
    return ""


def clean_byline(line: str) -> str:
    line = re.split(r"《(?:人民|经济)日报", line, maxsplit=1)[0]
    line = re.split(r"来源[:：]|责任编辑[:：]|发布时间[:：]", line, maxsplit=1)[0]
    line = re.sub(r"^(本报讯|本报记者|记者)\s*", "", line)
    return normalize_space(line.strip("：: "))


def is_plausible_byline(line: str) -> bool:
    if not line or len(line) > 40:
        return False
    if re.search(r"\d{4}年|\d{2}月|\d{2}日|第\d+版|版面|目录|返回|下一篇|上一篇", line):
        return False
    if line.startswith(("——", "--", "（", "(", "http")):
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", line))


def parse_article(
    publication: Publication,
    publication_date: dt.date,
    url: str,
    html: str,
) -> Article:
    soup = BeautifulSoup(html, "html.parser")
    lines = visible_lines(soup)
    title = title_from_soup(soup, lines)
    text = article_text_from_soup(soup, lines)
    byline = byline_from_lines(title, lines, publication.target_bylines)
    return Article(
        source_key=publication.key,
        source_name=publication.name,
        publication_date=publication_date.isoformat(),
        title=title,
        byline=byline,
        url=url,
        text=text,
    )


def upsert_article(conn: sqlite3.Connection, article: Article) -> None:
    now = utc_now()
    content_hash = hashlib.sha256(article.text.encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT INTO articles (
            source_key, source_name, publication_date, title, byline, url,
            text, content_hash, fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title=excluded.title,
            byline=excluded.byline,
            text=excluded.text,
            content_hash=excluded.content_hash,
            fetched_at=excluded.fetched_at
        """,
        (
            article.source_key,
            article.source_name,
            article.publication_date,
            article.title,
            article.byline,
            article.url,
            article.text,
            content_hash,
            now,
        ),
    )


def byline_matches(article: Article, targets: tuple[str, ...]) -> list[str]:
    haystack = article.byline or "\n".join(article.text.splitlines()[:8])
    return [target for target in targets if target in haystack]


def record_match_if_new(
    conn: sqlite3.Connection, article: Article, target_byline: str
) -> bool:
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO matches (
            source_key, source_name, publication_date, title, byline, url,
            target_byline, text, first_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article.source_key,
            article.source_name,
            article.publication_date,
            article.title,
            article.byline,
            article.url,
            target_byline,
            article.text,
            utc_now(),
        ),
    )
    return conn.total_changes > before


def pending_matches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT *
        FROM matches
        WHERE emailed_at IS NULL
        ORDER BY publication_date, source_name, title
        """
    ).fetchall()
    conn.row_factory = None
    return rows


def mark_matches_emailed(conn: sqlite3.Connection, match_ids: Iterable[int]) -> None:
    ids = list(match_ids)
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE matches SET emailed_at = ? WHERE id IN ({placeholders})",
        (utc_now(), *ids),
    )


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def build_email_body(matches: list[sqlite3.Row]) -> str:
    parts = ["New monitored byline matches:\n"]
    for row in matches:
        parts.append(
            "\n".join(
                (
                    f"{row['source_name']} | {row['publication_date']}",
                    f"Target byline: {row['target_byline']}",
                    f"Title: {row['title']}",
                    f"Byline: {row['byline'] or '(not extracted)'}",
                    f"Source: {row['url']}",
                    "",
                    row["text"].strip(),
                    "\n" + "=" * 72,
                )
            )
        )
    return "\n\n".join(parts)


def send_email(subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST")
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM") or username
    recipients = [
        item.strip()
        for item in os.environ.get("ALERT_EMAIL_TO", "").split(",")
        if item.strip()
    ]
    port = int(os.environ.get("SMTP_PORT", "587"))
    use_tls = os.environ.get("SMTP_TLS", "true").lower() not in {"0", "false", "no"}

    missing = [
        name
        for name, value in (
            ("SMTP_HOST", host),
            ("SMTP_FROM or SMTP_USERNAME", sender),
            ("ALERT_EMAIL_TO", recipients),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing email configuration: {', '.join(missing)}")

    message = email.message.EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if use_tls:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)


def run_monitor(args: argparse.Namespace) -> int:
    publication_date = publication_date_from_arg(args.date)
    db_path = Path(args.db)
    conn = init_db(db_path)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    article_count = 0
    new_match_count = 0
    try:
        for publication in PUBLICATIONS:
            layout_urls = collect_layout_urls(
                publication, publication_date, session, args.timeout
            )
            article_urls: list[str] = []
            for layout_url in layout_urls:
                html = fetch_html(session, layout_url, args.timeout)
                article_urls.extend(collect_article_urls(publication, html, layout_url))

            for article_url in unique_in_order(article_urls):
                LOGGER.info("Fetching article: %s", article_url)
                html = fetch_html(session, article_url, args.timeout)
                article = parse_article(publication, publication_date, article_url, html)
                if not article.title:
                    LOGGER.warning("Could not extract title from %s", article_url)
                upsert_article(conn, article)
                article_count += 1
                for target in byline_matches(article, publication.target_bylines):
                    if record_match_if_new(conn, article, target):
                        new_match_count += 1
        conn.commit()

        pending = pending_matches(conn)
        if pending:
            subject = f"Media monitor: {len(pending)} new byline match(es)"
            body = build_email_body(pending)
            if args.dry_run:
                print(body)
            elif args.no_email:
                LOGGER.info("%d pending match(es); email disabled.", len(pending))
            else:
                send_email(subject, body)
                mark_matches_emailed(conn, [row["id"] for row in pending])
                conn.commit()

        LOGGER.info(
            "Stored %d article(s), found %d new match(es), %d pending email alert(s).",
            article_count,
            new_match_count,
            len(pending),
        )
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return run_monitor(args)


if __name__ == "__main__":
    raise SystemExit(main())
