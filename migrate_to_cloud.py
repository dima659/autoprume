"""One-time migration of a SAVED local backup, never run automatically."""
import argparse
import os
import sqlite3
from pathlib import Path

import cloud_storage
import database
from shop_logic import Shop, normalize_media

TABLES = ("orders", "products", "leads", "users", "broadcasts", "broadcast_log")


def migrate(db_path, uploads):
    database.validate_config()
    if not os.getenv("DATABASE_URL") or not cloud_storage.enabled():
        raise RuntimeError("Для переноса нужны настройки внешней базы и Neon Object Storage")
    source_path = Path(db_path).resolve()
    if not source_path.is_file():
        raise RuntimeError("Не найден файл резервной копии SQLite")
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        existing = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        rows = {table: [dict(row) for row in source.execute(f'SELECT * FROM "{table}"')]
                for table in TABLES if table in existing}
    finally:
        source.close()
    media = set()
    for product in rows.get("products", []):
        for item in normalize_media(product.get("photo_id", "")):
            if item.startswith(("lp:", "lv:")):
                name = item.split(":", 1)[1]
                if not cloud_storage.NAME_RE.fullmatch(name):
                    raise RuntimeError("В базе есть некорректное имя фото")
                if not (Path(uploads) / name).is_file():
                    raise RuntimeError(
                        f"Нет сохранённого файла {name}. "
                        "Без исходных фото полный перенос невозможен."
                    )
                media.add(name)
    cloud_storage.check_bucket()
    Shop("unused").init_schema()
    target = database.connect("unused")
    try:
        # Prevent accidental merging/overwriting. Stop the bot during migration.
        target.execute("LOCK TABLE " + ", ".join(TABLES) + " IN ACCESS EXCLUSIVE MODE")
        for table in TABLES:
            if target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]:
                raise RuntimeError("Перенос разрешён только в пустую базу. Таблица: " + table)
        for name in sorted(media):
            # Retrying an interrupted migration is safe: same saved file/name.
            cloud_storage.put(name, str(Path(uploads) / name), upsert=True)
        total = 0
        for table in TABLES:
            columns = {row[1] for row in target.execute(f"PRAGMA table_info({table})")}
            for row in rows.get(table, []):
                selected = [key for key in row if key in columns]
                names = ", ".join('"' + key + '"' for key in selected)
                binds = ", ".join("?" for _ in selected)
                target.execute(f'INSERT INTO "{table}" ({names}) VALUES ({binds})',
                               tuple(row[key] for key in selected))
                total += 1
            if table != "users":
                target.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                    f'COALESCE(MAX(id), 1), COUNT(*) > 0) FROM "{table}"'
                )
        target.execute("UPDATE broadcasts SET done = 1 WHERE done = 0")
        target.commit()
        print(f"Перенос завершён: {total} записей, {len(media)} файлов.")
    except BaseException:
        target.rollback()
        raise
    finally:
        target.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Путь к сохранённой копии orders.db")
    parser.add_argument("--uploads", required=True, help="Путь к сохранённой папке uploads")
    args = parser.parse_args()
    migrate(args.db, args.uploads)