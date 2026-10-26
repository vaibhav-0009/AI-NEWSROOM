import sqlite3
import shutil
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).resolve().parent
DB = BASE / "news.db"

if not DB.exists():
    raise FileNotFoundError(f"Database nahi mili: {DB}")

# Backup before making any changes
backup = BASE / f"news_backup_{datetime.now():%Y%m%d_%H%M%S}.db"
shutil.copy2(DB, backup)
print(f"Backup created: {backup.name}")

with sqlite3.connect(DB) as conn:
    conn.execute("PRAGMA foreign_keys = ON")

    # Validate required tables and columns
    tables = {
        row[0] for row in conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'table'
        """)
    }

    if not {"news", "articles"}.issubset(tables):
        raise RuntimeError(
            f"Required tables missing. Found: {sorted(tables)}"
        )

    news_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(news)")
    }
    article_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(articles)")
    }

    required_news = {"id"}
    required_articles = {"news_id", "status", "published"}

    if not required_news.issubset(news_cols):
        raise RuntimeError("news table mein id column nahi mila.")

    if not required_articles.issubset(article_cols):
        raise RuntimeError(
            "articles table mein expected columns nahi mile. "
            f"Found: {sorted(article_cols)}"
        )

    # Show counts before cleanup
    before_news = conn.execute(
        "SELECT COUNT(*) FROM news"
    ).fetchone()[0]

    before_articles = conn.execute(
        "SELECT COUNT(*) FROM articles"
    ).fetchone()[0]

    print("Before cleanup — news:", before_news)
    print("Before cleanup — articles:", before_articles)

    # Transaction: changes rollback if a statement fails
    with conn:
        # Delete drafts and articles not marked as published,
        # or having an empty published field.
        conn.execute("""
            DELETE FROM articles
            WHERE LOWER(TRIM(COALESCE(status, ''))) != 'published'
               OR TRIM(COALESCE(published, '')) = ''
        """)

        # Remove news rows with no remaining published article
        conn.execute("""
            DELETE FROM news
            WHERE NOT EXISTS (
                SELECT 1
                FROM articles a
                WHERE a.news_id = news.id
                  AND LOWER(TRIM(COALESCE(a.status, ''))) = 'published'
                  AND TRIM(COALESCE(a.published, '')) != ''
            )
        """)

    remaining_news = conn.execute(
        "SELECT COUNT(*) FROM news"
    ).fetchone()[0]

    remaining_articles = conn.execute(
        "SELECT COUNT(*) FROM articles"
    ).fetchone()[0]

print("Backlog cleanup complete.")
print("Remaining news:", remaining_news)
print("Remaining articles:", remaining_articles)
print("Backup:", backup.name)