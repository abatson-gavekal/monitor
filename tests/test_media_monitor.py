import datetime as dt
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import media_monitor


PEOPLE_HTML = """
<html>
  <body>
    <h1>在高质量发展中促进共同富裕</h1>
    <div class="source">钟才文 《人民日报》（2026年05月21日 第 01 版）</div>
    <div id="ozoom">
      <p>这是人民日报文章第一段。</p>
      <p>这是人民日报文章第二段。</p>
    </div>
  </body>
</html>
"""

ECONOMIC_HTML = """
<html>
  <body>
    <h1>推动经济持续回升向好</h1>
    <div class="laiyuan">金观平</div>
    <div class="TRS_Editor">
      <p>这是经济日报文章第一段。</p>
      <p>这是经济日报文章第二段。</p>
    </div>
  </body>
</html>
"""

LAYOUT_HTML = """
<html>
  <body>
    <a href="../content/202605/21/content_1.html">Article 1</a>
    <a href="../content/202605/21/content_1.html">Duplicate article</a>
    <a href="node_02.html">Next layout</a>
  </body>
</html>
"""


class MediaMonitorTests(unittest.TestCase):
    def test_parse_people_article_extracts_target_byline(self):
        article = media_monitor.parse_article(
            media_monitor.PUBLICATIONS[0],
            dt.date(2026, 5, 21),
            "https://paper.people.com.cn/rmrb/pc/content/202605/21/content_1.html",
            PEOPLE_HTML,
        )

        self.assertEqual(article.title, "在高质量发展中促进共同富裕")
        self.assertEqual(article.byline, "钟才文")
        self.assertIn("这是人民日报文章第一段", article.text)
        self.assertEqual(media_monitor.byline_matches(article, ("钟才文",)), ["钟才文"])

    def test_parse_economic_article_extracts_target_byline(self):
        article = media_monitor.parse_article(
            media_monitor.PUBLICATIONS[1],
            dt.date(2026, 5, 21),
            "http://paper.ce.cn/pc/content/202605/21/content_1.html",
            ECONOMIC_HTML,
        )

        self.assertEqual(article.title, "推动经济持续回升向好")
        self.assertEqual(article.byline, "金观平")
        self.assertIn("这是经济日报文章第二段", article.text)
        self.assertEqual(media_monitor.byline_matches(article, ("金观平",)), ["金观平"])

    def test_collect_article_urls_deduplicates_relative_links(self):
        urls = media_monitor.collect_article_urls(
            media_monitor.PUBLICATIONS[1],
            LAYOUT_HTML,
            "http://paper.ce.cn/pc/layout/202605/21/node_01.html",
        )

        self.assertEqual(
            urls,
            ["http://paper.ce.cn/pc/content/202605/21/content_1.html"],
        )

    def test_match_recording_prevents_duplicate_alerts(self):
        with TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "monitor.sqlite3"
            conn = media_monitor.init_db(db_path)
            article = media_monitor.parse_article(
                media_monitor.PUBLICATIONS[0],
                dt.date(2026, 5, 21),
                "https://paper.people.com.cn/rmrb/pc/content/202605/21/content_1.html",
                PEOPLE_HTML,
            )
            media_monitor.upsert_article(conn, article)

            first = media_monitor.record_match_if_new(conn, article, "钟才文")
            second = media_monitor.record_match_if_new(conn, article, "钟才文")
            conn.commit()

            self.assertTrue(first)
            self.assertFalse(second)
            rows = conn.execute("SELECT COUNT(*) FROM matches").fetchone()
            self.assertEqual(rows, (1,))
            conn.close()


if __name__ == "__main__":
    unittest.main()
