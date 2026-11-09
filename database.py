
import sqlite3
from pathlib import Path
from datetime import datetime
import json

DB_PATH = Path("data/items.db")

ACTIVE_STATUSES = (
    "submitting",
    "pending",
    "checking",
    "unknown",
    "ready_to_confirm"
)


# =====================================
# ПОДКЛЮЧЕНИЕ К БАЗЕ
# =====================================

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")

    return conn


def migrate_listing_id_constraints(conn):
    """
    Убирает UNIQUE с market_listing_id,
    сохраняя историю и попытки покупок.
    UNIQUE на custom_id остаётся.
    """

    cursor = conn.cursor()

    tables = {
        "purchase_history": """
            CREATE TABLE purchase_history_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_item_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                price REAL NOT NULL,
                market_listing_id TEXT,
                purchased_at TEXT NOT NULL,
                FOREIGN KEY (tracked_item_id)
                    REFERENCES tracked_items(id)
                    ON DELETE CASCADE
            )
        """,
        "purchase_attempts": """
            CREATE TABLE purchase_attempts_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_item_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                market_listing_id TEXT NOT NULL,
                custom_id TEXT NOT NULL UNIQUE,
                price REAL NOT NULL,
                price_kopecks INTEGER NOT NULL,
                api_item_id TEXT,
                status TEXT NOT NULL DEFAULT 'submitting',
                response_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """
    }

    columns_to_copy = {
        "purchase_history": """
            id, tracked_item_id, item_name, price,
            market_listing_id, purchased_at
        """,
        "purchase_attempts": """
            id, tracked_item_id, item_name, market_listing_id,
            custom_id, price, price_kopecks, api_item_id,
            status, response_json, error, created_at, updated_at
        """
    }

    for table, create_sql in tables.items():
        indexes = cursor.execute(
            f"PRAGMA index_list({table})"
        ).fetchall()

        has_listing_unique = False

        for index in indexes:
            if not index["unique"]:
                continue

            index_name = index["name"].replace('"', '""')

            index_columns = cursor.execute(
                f'PRAGMA index_info("{index_name}")'
            ).fetchall()

            names = [row["name"] for row in index_columns]

            if names == ["market_listing_id"]:
                has_listing_unique = True
                break

        if not has_listing_unique:
            continue

        print(f"Миграция таблицы {table}...")

        cursor.execute(create_sql)

        cursor.execute(f"""
            INSERT INTO {table}_new ({columns_to_copy[table]})
            SELECT {columns_to_copy[table]}
            FROM {table}
        """)

        cursor.execute(f"DROP TABLE {table}")
        cursor.execute(
            f"ALTER TABLE {table}_new RENAME TO {table}"
        )

    conn.commit()


# =====================================
# МИГРАЦИЯ ТАБЛИЦЫ АВТООРДЕРОВ
# =====================================

def migrate_auto_order_reservations(conn):
    """
    Поддерживает несколько резервов на одну позицию.
    Каждый вариант определяется classid + instanceid.

    Старые записи сохраняются с NULL в classid/instanceid.
    """

    cursor = conn.cursor()

    exists = cursor.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'auto_order_reservations'
    """).fetchone()

    if not exists:
        cursor.execute("""
            CREATE TABLE auto_order_reservations (
                reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_item_id INTEGER NOT NULL,
                classid TEXT,
                instanceid TEXT,
                market_hash_name TEXT NOT NULL,
                phase TEXT,
                count INTEGER NOT NULL DEFAULT 1,
                price REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
    else:
        columns = {
            row["name"]
            for row in cursor.execute(
                "PRAGMA table_info(auto_order_reservations)"
            ).fetchall()
        }

        if (
            "classid" not in columns
            or "instanceid" not in columns
            or "reservation_id" not in columns
        ):
            cursor.execute("""
                ALTER TABLE auto_order_reservations
                RENAME TO auto_order_reservations_old
            """)

            cursor.execute("""
                CREATE TABLE auto_order_reservations (
                    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracked_item_id INTEGER NOT NULL,
                    classid TEXT,
                    instanceid TEXT,
                    market_hash_name TEXT NOT NULL,
                    phase TEXT,
                    count INTEGER NOT NULL DEFAULT 1,
                    price REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)

            old_columns = {
                row["name"]
                for row in cursor.execute(
                    "PRAGMA table_info(auto_order_reservations_old)"
                ).fetchall()
            }

            required = {
                "tracked_item_id",
                "market_hash_name",
                "count",
                "price",
                "updated_at"
            }

            if required.issubset(old_columns):
                phase_expr = (
                    "phase" if "phase" in old_columns else "NULL"
                )

                cursor.execute(f"""
                    INSERT INTO auto_order_reservations (
                        tracked_item_id,
                        classid,
                        instanceid,
                        market_hash_name,
                        phase,
                        count,
                        price,
                        updated_at
                    )
                    SELECT
                        tracked_item_id,
                        NULL,
                        NULL,
                        market_hash_name,
                        {phase_expr},
                        count,
                        price,
                        updated_at
                    FROM auto_order_reservations_old
                """)

    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
        idx_auto_order_variant_unique
        ON auto_order_reservations(classid, instanceid)
        WHERE classid IS NOT NULL
          AND instanceid IS NOT NULL
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS
        idx_auto_order_tracked_item
        ON auto_order_reservations(tracked_item_id)
    """)

    conn.commit()


# =====================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ
# =====================================

def init():
    conn = connect()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracked_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                max_price REAL NOT NULL,
                gem TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 1,
                purchased_quantity INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)

        cursor.execute("PRAGMA table_info(tracked_items)")
        columns = [row["name"] for row in cursor.fetchall()]

        if "quantity" not in columns:
            cursor.execute("""
                ALTER TABLE tracked_items
                ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1
            """)

        if "purchased_quantity" not in columns:
            cursor.execute("""
                ALTER TABLE tracked_items
                ADD COLUMN purchased_quantity INTEGER NOT NULL DEFAULT 0
            """)

        if "is_active" not in columns:
            cursor.execute("""
                ALTER TABLE tracked_items
                ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1
            """)

        if "is_deleted" not in columns:
            cursor.execute("""
                ALTER TABLE tracked_items
                ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0
            """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS purchase_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_item_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                price REAL NOT NULL,
                market_listing_id TEXT,
                purchased_at TEXT NOT NULL,
                FOREIGN KEY (tracked_item_id)
                    REFERENCES tracked_items(id)
                    ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS purchase_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracked_item_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                market_listing_id TEXT NOT NULL,
                custom_id TEXT NOT NULL UNIQUE,
                price REAL NOT NULL,
                price_kopecks INTEGER NOT NULL,
                api_item_id TEXT,
                status TEXT NOT NULL DEFAULT 'submitting',
                response_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        migrate_listing_id_constraints(conn)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processed_order_fills (
                item_id TEXT PRIMARY KEY,
                tracked_item_id INTEGER NOT NULL,
                price REAL NOT NULL DEFAULT 0,
                processed_at TEXT NOT NULL
            )
        """)

        migrate_auto_order_reservations(conn)

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    print("База данных успешно инициализирована!")


# =====================================
# ДОБАВЛЕНИЕ ПРЕДМЕТА
# =====================================

def add_item(name, max_price, gem="", quantity=1):
    name = str(name).strip()
    max_price = float(max_price)
    quantity = int(quantity)
    gem = str(gem or "").strip()

    if not name:
        raise ValueError("Название предмета не может быть пустым")

    if max_price <= 0:
        raise ValueError("Максимальная цена должна быть больше нуля")

    if quantity < 1:
        raise ValueError("Количество должно быть не меньше 1")

    conn = connect()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO tracked_items (
                name, max_price, gem, quantity,
                purchased_quantity, is_active, is_deleted
            )
            VALUES (?, ?, ?, ?, 0, 1, 0)
        """, (name, max_price, gem, quantity))

        item_id = cursor.lastrowid
        conn.commit()
        return item_id

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# ПОЛУЧЕНИЕ ВСЕХ ПРЕДМЕТОВ
# =====================================

def get_all_items():
    conn = connect()

    try:
        rows = conn.execute("""
            SELECT *,
                   quantity - purchased_quantity AS remaining
            FROM tracked_items
            WHERE is_deleted = 0
            ORDER BY id
        """).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


# =====================================
# ПОЛУЧЕНИЕ ОДНОГО ПРЕДМЕТА
# =====================================

def get_item_by_id(item_id):
    conn = connect()

    try:
        row = conn.execute("""
            SELECT *,
                   quantity - purchased_quantity AS remaining
            FROM tracked_items
            WHERE id = ?
              AND is_deleted = 0
        """, (item_id,)).fetchone()

        return dict(row) if row else None

    finally:
        conn.close()


# =====================================
# УДАЛЕНИЕ ПРЕДМЕТА
# =====================================

def remove_item(item_id):
    conn = connect()

    try:
        cursor = conn.execute("""
            UPDATE tracked_items
            SET is_deleted = 1,
                is_active = 0
            WHERE id = ?
        """, (item_id,))

        conn.commit()
        return cursor.rowcount == 1

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# РЕДАКТИРОВАНИЕ ПРЕДМЕТА
# =====================================

def update_item(item_id, name, max_price, gem="", quantity=None):
    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id
            FROM tracked_items
            WHERE id = ?
              AND is_deleted = 0
        """, (item_id,))

        if not cursor.fetchone():
            conn.rollback()
            return False

        if quantity is not None:
            quantity = int(quantity)

            if quantity < 1:
                raise ValueError(
                    "Количество должно быть не меньше 1."
                )

            cursor.execute("""
                SELECT
                    purchased_quantity,
                    (
                        SELECT COUNT(*)
                        FROM purchase_attempts
                        WHERE tracked_item_id = tracked_items.id
                          AND status IN (
                              'submitting', 'pending', 'checking',
                              'unknown', 'ready_to_confirm'
                          )
                    ) AS pending_count
                FROM tracked_items
                WHERE id = ?
                  AND is_deleted = 0
            """, (item_id,))

            current = cursor.fetchone()

            reserved_total = (
                int(current["purchased_quantity"])
                + int(current["pending_count"])
            )

            if quantity < reserved_total:
                raise ValueError(
                    "Количество меньше уже купленного "
                    "и зарезервированного."
                )

        if quantity is None:
            cursor.execute("""
                UPDATE tracked_items
                SET name = ?,
                    max_price = ?,
                    gem = ?
                WHERE id = ?
                  AND is_deleted = 0
            """, (
                name.strip(),
                float(max_price),
                (gem or "").strip(),
                item_id
            ))
        else:
            cursor.execute("""
                UPDATE tracked_items
                SET name = ?,
                    max_price = ?,
                    gem = ?,
                    quantity = ?
                WHERE id = ?
                  AND is_deleted = 0
            """, (
                name.strip(),
                float(max_price),
                (gem or "").strip(),
                quantity,
                item_id
            ))

        conn.commit()
        return cursor.rowcount == 1

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# ВКЛЮЧЕНИЕ / ОТКЛЮЧЕНИЕ ПОЗИЦИИ
# =====================================

def set_item_active(item_id, active):
    conn = connect()

    try:
        cursor = conn.execute("""
            UPDATE tracked_items
            SET is_active = ?
            WHERE id = ?
              AND is_deleted = 0
        """, (1 if active else 0, item_id))

        conn.commit()
        return cursor.rowcount == 1

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# РЕГИСТРАЦИЯ УСПЕШНОЙ ПОКУПКИ
# =====================================

def record_purchase(item_id, price, market_listing_id=None):
    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        cursor.execute("""
            SELECT name, quantity, purchased_quantity
            FROM tracked_items
            WHERE id = ?
              AND is_deleted = 0
        """, (item_id,))

        item = cursor.fetchone()

        if not item:
            conn.rollback()
            return False

        cursor.execute("""
            SELECT COUNT(*) AS pending_count
            FROM purchase_attempts
            WHERE tracked_item_id = ?
              AND status IN (
                  'submitting', 'pending', 'checking',
                  'unknown', 'ready_to_confirm'
              )
        """, (item_id,))

        pending_count = int(cursor.fetchone()["pending_count"])

        reserved_total = (
            int(item["purchased_quantity"]) + pending_count
        )

        if reserved_total >= int(item["quantity"]):
            conn.rollback()
            return False

        now = datetime.now().isoformat(timespec="seconds")

        cursor.execute("""
            INSERT INTO purchase_history (
                tracked_item_id, item_name, price,
                market_listing_id, purchased_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            item_id,
            item["name"],
            float(price),
            str(market_listing_id)
            if market_listing_id is not None else None,
            now
        ))

        cursor.execute("""
            UPDATE tracked_items
            SET purchased_quantity = purchased_quantity + 1
            WHERE id = ?
              AND purchased_quantity < quantity
              AND is_deleted = 0
        """, (item_id,))

        if cursor.rowcount != 1:
            conn.rollback()
            return False

        conn.commit()
        return True

    except sqlite3.IntegrityError:
        conn.rollback()
        print("Эта покупка уже зарегистрирована!")
        return False

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# ИСТОРИЯ ПОКУПОК
# =====================================

def get_purchase_history(item_id=None):
    conn = connect()

    try:
        if item_id is None:
            rows = conn.execute("""
                SELECT *
                FROM purchase_history
                ORDER BY purchased_at DESC
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT *
                FROM purchase_history
                WHERE tracked_item_id = ?
                ORDER BY purchased_at DESC
            """, (item_id,)).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


# =====================================
# СБРОС СЧЁТЧИКА ПОКУПОК
# =====================================

def reset_purchases(item_id):
    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

        cursor.execute(f"""
            SELECT COUNT(*) AS pending_count
            FROM purchase_attempts
            WHERE tracked_item_id = ?
              AND status IN ({placeholders})
        """, (item_id, *ACTIVE_STATUSES))

        pending_count = int(cursor.fetchone()["pending_count"])

        cursor.execute("""
            SELECT COALESCE(SUM(count), 0) AS total_count
            FROM auto_order_reservations
            WHERE tracked_item_id = ?
        """, (item_id,))

        reservation_count = int(
            cursor.fetchone()["total_count"]
        )

        if pending_count > 0:
            raise ValueError(
                "Нельзя сбросить покупки: есть незавершённые "
                "попытки покупки."
            )

        if reservation_count > 0:
            raise ValueError(
                "Сначала отмени активные ордера на площадке."
            )

        cursor.execute("""
            UPDATE tracked_items
            SET purchased_quantity = 0
            WHERE id = ?
              AND is_deleted = 0
        """, (item_id,))

        changed = cursor.rowcount
        conn.commit()
        return changed == 1

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# СОЗДАНИЕ ПОПЫТКИ ПОКУПКИ
# =====================================

def create_purchase_attempt(
    item_id,
    item_name,
    market_listing_id,
    custom_id,
    price,
    price_kopecks,
    total_budget=None
):
    """Резервирует покупку с проверкой лимита и бюджета."""

    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                name, max_price, quantity,
                purchased_quantity, is_active, is_deleted
            FROM tracked_items
            WHERE id = ?
        """, (item_id,))

        item = cursor.fetchone()

        if not item:
            conn.rollback()
            return None

        if not bool(item["is_active"]) or bool(item["is_deleted"]):
            conn.rollback()
            return None

        price = float(price)
        price_kopecks = int(price_kopecks)

        if price <= 0 or price > float(item["max_price"]):
            conn.rollback()
            return None

        if price_kopecks <= 0:
            conn.rollback()
            return None

        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

        cursor.execute(f"""
            SELECT COUNT(*) AS pending_count
            FROM purchase_attempts
            WHERE tracked_item_id = ?
              AND status IN ({placeholders})
        """, (item_id, *ACTIVE_STATUSES))

        pending_count = int(cursor.fetchone()["pending_count"])

        reserved = int(item["purchased_quantity"]) + pending_count

        if reserved >= int(item["quantity"]):
            conn.rollback()
            return None

        if total_budget is not None and float(total_budget) > 0:
            cursor.execute("""
                SELECT COALESCE(SUM(price), 0)
                FROM purchase_history
            """)
            purchased = float(cursor.fetchone()[0])

            cursor.execute(f"""
                SELECT COALESCE(SUM(price), 0)
                FROM purchase_attempts
                WHERE status IN ({placeholders})
            """, ACTIVE_STATUSES)
            active_reserved = float(cursor.fetchone()[0])

            cursor.execute("""
                SELECT COALESCE(SUM(count * price), 0)
                FROM auto_order_reservations
                WHERE count > 0
            """)
            order_reserved = float(cursor.fetchone()[0])

            budget_used = (
                purchased + active_reserved + order_reserved
            )

            if budget_used + price > float(total_budget):
                conn.rollback()
                return None

        now = datetime.now().isoformat(timespec="seconds")

        cursor.execute("""
            INSERT INTO purchase_attempts (
                tracked_item_id, item_name, market_listing_id,
                custom_id, price, price_kopecks, status,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'submitting', ?, ?)
        """, (
            item_id,
            item_name,
            str(market_listing_id),
            str(custom_id),
            price,
            price_kopecks,
            now,
            now
        ))

        attempt_id = cursor.lastrowid

        conn.commit()
        return attempt_id

    except sqlite3.IntegrityError:
        conn.rollback()
        return None

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# ОБНОВЛЕНИЕ ПОПЫТКИ ПОКУПКИ
# =====================================

def update_purchase_attempt(
    attempt_id,
    status,
    response=None,
    error=None,
    api_item_id=None
):
    allowed_statuses = {
        "submitting", "pending", "checking", "unknown",
        "ready_to_confirm", "failed", "cancelled",
        "duplicate", "quota_conflict"
    }

    if status not in allowed_statuses:
        raise ValueError(f"Недопустимый статус покупки: {status}")

    conn = connect()

    try:
        response_json = (
            json.dumps(response, ensure_ascii=False, default=str)
            if response is not None else None
        )

        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

        cursor = conn.execute(f"""
            UPDATE purchase_attempts
            SET status = ?,
                response_json = COALESCE(?, response_json),
                error = ?,
                api_item_id = COALESCE(?, api_item_id),
                updated_at = ?
            WHERE id = ?
              AND status IN ({placeholders})
        """, (
            status,
            response_json,
            error,
            str(api_item_id) if api_item_id is not None else None,
            datetime.now().isoformat(timespec="seconds"),
            attempt_id,
            *ACTIVE_STATUSES
        ))

        conn.commit()
        return cursor.rowcount == 1

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def cancel_purchase_attempt(
    attempt_id,
    reason="Покупка вручную отменена пользователем"
):
    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")

        cursor = conn.execute("""
            UPDATE purchase_attempts
            SET status = 'cancelled',
                error = ?,
                updated_at = ?
            WHERE id = ?
              AND status IN ('pending', 'checking', 'unknown')
        """, (
            str(reason),
            datetime.now().isoformat(timespec="seconds"),
            int(attempt_id)
        ))

        conn.commit()
        return cursor.rowcount == 1

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()
# =====================================
# ПОЛУЧЕНИЕ ПОПЫТКИ ПО CUSTOM_ID
# =====================================

def get_purchase_attempt(custom_id):
    conn = connect()

    try:
        row = conn.execute("""
            SELECT *
            FROM purchase_attempts
            WHERE custom_id = ?
        """, (str(custom_id),)).fetchone()

        return dict(row) if row else None

    finally:
        conn.close()


# =====================================
# ПОЛУЧЕНИЕ НЕЗАВЕРШЁННЫХ ПОКУПОК
# =====================================

def get_pending_purchase_attempts():
    conn = connect()

    try:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

        rows = conn.execute(f"""
            SELECT *
            FROM purchase_attempts
            WHERE status IN ({placeholders})
            ORDER BY created_at ASC
        """, ACTIVE_STATUSES).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


# =====================================
# ПОДТВЕРЖДЕНИЕ ПОКУПКИ
# =====================================

def confirm_purchase_attempt(custom_id):
    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM purchase_attempts
            WHERE custom_id = ?
        """, (str(custom_id),))

        attempt = cursor.fetchone()

        if not attempt:
            conn.rollback()
            return False

        if attempt["status"] in ("confirmed", "quota_conflict"):
            conn.commit()
            return True

        if attempt["status"] != "ready_to_confirm":
            conn.rollback()
            return False

        cursor.execute("""
            SELECT quantity, purchased_quantity
            FROM tracked_items
            WHERE id = ?
        """, (attempt["tracked_item_id"],))

        tracked_item = cursor.fetchone()

        if not tracked_item:
            conn.rollback()
            return False

        quota_conflict = (
            int(tracked_item["purchased_quantity"])
            >= int(tracked_item["quantity"])
        )

        now = datetime.now().isoformat(timespec="seconds")

        cursor.execute("""
            INSERT INTO purchase_history (
                tracked_item_id, item_name, price,
                market_listing_id, purchased_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            attempt["tracked_item_id"],
            attempt["item_name"],
            attempt["price"],
            attempt["market_listing_id"],
            now
        ))

        cursor.execute("""
            UPDATE tracked_items
            SET purchased_quantity = purchased_quantity + 1
            WHERE id = ?
        """, (attempt["tracked_item_id"],))

        if cursor.rowcount != 1:
            raise RuntimeError(
                "Не удалось обновить количество покупок."
            )

        if quota_conflict:
            new_status = "quota_conflict"
            error = (
                "Покупка состоялась, но лимит количества "
                "был исчерпан или изменён."
            )
        else:
            new_status = "confirmed"
            error = None

        cursor.execute("""
            UPDATE purchase_attempts
            SET status = ?,
                error = ?,
                updated_at = ?
            WHERE custom_id = ?
              AND status = 'ready_to_confirm'
        """, (new_status, error, now, str(custom_id)))

        if cursor.rowcount != 1:
            raise RuntimeError(
                "Не удалось изменить статус попытки покупки."
            )

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# МАССОВОЕ РЕДАКТИРОВАНИЕ
# =====================================

def bulk_update_items(item_ids, changes):
    allowed_fields = {
        "name", "max_price", "quantity", "gem"
    }

    ids = list(set(int(x) for x in item_ids))

    if not ids or not changes:
        return False

    if not set(changes).issubset(allowed_fields):
        raise ValueError("Недопустимое поле для редактирования")

    values = dict(changes)

    if "name" in values:
        values["name"] = str(values["name"]).strip()
        if not values["name"]:
            raise ValueError("Название не может быть пустым")

    if "max_price" in values:
        values["max_price"] = float(values["max_price"])
        if values["max_price"] <= 0:
            raise ValueError("Цена должна быть больше нуля")

    if "quantity" in values:
        values["quantity"] = int(values["quantity"])
        if values["quantity"] < 1:
            raise ValueError("Количество должно быть не меньше 1")

    if "gem" in values:
        values["gem"] = str(values["gem"] or "").strip()

    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        placeholders = ",".join("?" for _ in ids)

        if "quantity" in values:
            cursor.execute(f"""
                SELECT
                    ti.id,
                    ti.purchased_quantity,
                    (
                        SELECT COUNT(*)
                        FROM purchase_attempts pa
                        WHERE pa.tracked_item_id = ti.id
                          AND pa.status IN (
                              'submitting', 'pending', 'checking',
                              'unknown', 'ready_to_confirm'
                          )
                    ) AS pending_count
                FROM tracked_items ti
                WHERE ti.id IN ({placeholders})
                  AND ti.is_deleted = 0
            """, ids)

            for item in cursor.fetchall():
                reserved_total = (
                    int(item["purchased_quantity"])
                    + int(item["pending_count"])
                )

                if values["quantity"] < reserved_total:
                    raise ValueError(
                        f"Для ID {item['id']} количество меньше "
                        "уже купленного и зарезервированного."
                    )

        fields = list(values)
        assignments = ", ".join(
            f"{field} = ?" for field in fields
        )

        params = [values[field] for field in fields] + ids

        cursor.execute(f"""
            UPDATE tracked_items
            SET {assignments}
            WHERE id IN ({placeholders})
              AND is_deleted = 0
        """, params)

        changed = cursor.rowcount

        conn.commit()
        return changed

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# МАССОВОЕ УДАЛЕНИЕ
# =====================================

def bulk_delete_items(item_ids):
    """Архивирует позиции, не удаляя историю."""

    ids = list(set(int(x) for x in item_ids))

    if not ids:
        return 0

    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in ids)

        cursor = conn.execute(f"""
            UPDATE tracked_items
            SET is_deleted = 1,
                is_active = 0
            WHERE id IN ({placeholders})
              AND is_deleted = 0
        """, ids)

        changed = cursor.rowcount
        conn.commit()
        return changed

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def delete_all_items():
    """Архивирует все позиции."""

    conn = connect()

    try:
        cursor = conn.execute("""
            UPDATE tracked_items
            SET is_deleted = 1,
                is_active = 0
            WHERE is_deleted = 0
        """)

        changed = cursor.rowcount
        conn.commit()
        return changed

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# ОБЩИЙ РАСХОД БЮДЖЕТА
# =====================================

def get_budget_usage():
    """
    Покупки + активные попытки прямой покупки
    + резервы активных автоордеров.
    """

    conn = connect()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT COALESCE(SUM(price), 0)
            FROM purchase_history
        """)
        purchased = float(cursor.fetchone()[0] or 0)

        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

        cursor.execute(f"""
            SELECT COALESCE(SUM(price), 0)
            FROM purchase_attempts
            WHERE status IN ({placeholders})
        """, ACTIVE_STATUSES)
        attempts = float(cursor.fetchone()[0] or 0)

        cursor.execute("""
            SELECT COALESCE(SUM(count * price), 0)
            FROM auto_order_reservations
            WHERE count > 0
        """)
        orders = float(cursor.fetchone()[0] or 0)

        return purchased + attempts + orders

    finally:
        conn.close()


def get_budget_details():
    conn = connect()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT COALESCE(SUM(price), 0)
            FROM purchase_history
        """)
        purchased = float(cursor.fetchone()[0] or 0)

        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

        cursor.execute(f"""
            SELECT COALESCE(SUM(price), 0)
            FROM purchase_attempts
            WHERE status IN ({placeholders})
        """, ACTIVE_STATUSES)
        attempts = float(cursor.fetchone()[0] or 0)

        cursor.execute("""
            SELECT COALESCE(SUM(count * price), 0)
            FROM auto_order_reservations
            WHERE count > 0
        """)
        orders = float(cursor.fetchone()[0] or 0)

        return {
            "purchased": purchased,
            "attempts": attempts,
            "orders": orders,
            "total": purchased + attempts + orders
        }

    finally:
        conn.close()


# =====================================
# ПОЛНЫЙ СБРОС ДАННЫХ ЗАКУПКИ
# =====================================

def reset_purchase_data():
    """
    Запрещает сброс при активных ордерах
    или незавершённых попытках покупки.
    """

    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        cursor.execute("""
            SELECT COALESCE(SUM(count), 0)
            FROM auto_order_reservations
            WHERE count > 0
        """)

        active_orders = int(cursor.fetchone()[0] or 0)

        if active_orders > 0:
            raise ValueError(
                "Сначала отмени активные автоордера на площадке."
            )

        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

        cursor.execute(f"""
            SELECT COUNT(*)
            FROM purchase_attempts
            WHERE status IN ({placeholders})
        """, ACTIVE_STATUSES)

        pending = int(cursor.fetchone()[0])

        if pending > 0:
            raise ValueError(
                "Есть незавершённые попытки покупки. "
                "Дождись их завершения."
            )

        cursor.execute("DELETE FROM purchase_history")
        cursor.execute("DELETE FROM purchase_attempts")
        cursor.execute("DELETE FROM processed_order_fills")

        cursor.execute("""
            UPDATE tracked_items
            SET purchased_quantity = 0
        """)

        conn.commit()
        print("Внутренние данные прошлой закупки очищены.")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =====================================
# КОЛИЧЕСТВО НЕЗАВЕРШЁННЫХ ПОКУПОК
# =====================================

def get_pending_count(item_id):
    conn = connect()

    try:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

        cursor = conn.execute(f"""
            SELECT COUNT(*)
            FROM purchase_attempts
            WHERE tracked_item_id = ?
              AND status IN ({placeholders})
        """, (item_id, *ACTIVE_STATUSES))

        return int(cursor.fetchone()[0])

    finally:
        conn.close()


# =====================================
# АВТООРДЕРА: СОХРАНЕНИЕ РЕЗЕРВОВ
# =====================================

def save_auto_order_reservation(
    tracked_item_id,
    market_hash_name,
    phase,
    count,
    price,
    classid=None,
    instanceid=None
):
    """
    Один резерв на каждый classid + instanceid.

    price — цена одной единицы в рублях.
    count — количество в ордере.
    """

    tracked_item_id = int(tracked_item_id)
    count = int(count)
    price = float(price)

    if classid is None or instanceid is None:
        raise ValueError(
            "Для автоордера обязательны classid и instanceid."
        )

    classid = str(classid)
    instanceid = str(instanceid)

    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        existing = cursor.execute("""
            SELECT reservation_id
            FROM auto_order_reservations
            WHERE classid = ?
              AND instanceid = ?
        """, (classid, instanceid)).fetchone()

        if count <= 0:
            cursor.execute("""
                DELETE FROM auto_order_reservations
                WHERE classid = ?
                  AND instanceid = ?
            """, (classid, instanceid))

        elif existing:
            cursor.execute("""
                UPDATE auto_order_reservations
                SET tracked_item_id = ?,
                    market_hash_name = ?,
                    phase = ?,
                    count = ?,
                    price = ?,
                    updated_at = ?
                WHERE reservation_id = ?
            """, (
                tracked_item_id,
                str(market_hash_name),
                phase,
                count,
                max(0.0, price),
                datetime.now().isoformat(timespec="seconds"),
                existing["reservation_id"]
            ))

        else:
            cursor.execute("""
                INSERT INTO auto_order_reservations (
                    tracked_item_id,
                    classid,
                    instanceid,
                    market_hash_name,
                    phase,
                    count,
                    price,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tracked_item_id,
                classid,
                instanceid,
                str(market_hash_name),
                phase,
                count,
                max(0.0, price),
                datetime.now().isoformat(timespec="seconds")
            ))

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def remove_auto_order_reservation(
    tracked_item_id=None,
    classid=None,
    instanceid=None
):
    """
    Удаляет резерв конкретного варианта
    или все резервы отслеживаемой позиции.
    """

    conn = connect()

    try:
        if classid is not None and instanceid is not None:
            conn.execute("""
                DELETE FROM auto_order_reservations
                WHERE classid = ?
                  AND instanceid = ?
            """, (str(classid), str(instanceid)))

        elif tracked_item_id is not None:
            conn.execute("""
                DELETE FROM auto_order_reservations
                WHERE tracked_item_id = ?
            """, (int(tracked_item_id),))

        else:
            raise ValueError(
                "Укажи tracked_item_id либо classid и instanceid."
            )

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def get_auto_order_reservations():
    conn = connect()

    try:
        rows = conn.execute("""
            SELECT *
            FROM auto_order_reservations
            ORDER BY tracked_item_id, classid, instanceid
        """).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()


# =====================================
# АВТООРДЕРА: УЧЁТ ИСПОЛНЕНИЙ
# =====================================

def register_order_fill(item_id, tracked_item_id, price):
    """
    Регистрирует исполнение автоордера ровно один раз.

    Реальную покупку учитываем даже при достижении лимита
    или архивировании позиции.
    """

    item_id = str(item_id).strip()
    tracked_item_id = int(tracked_item_id)
    price = float(price)

    if not item_id:
        raise ValueError("Пустой item_id исполнения ордера")

    if price < 0:
        raise ValueError("Цена исполнения не может быть отрицательной")

    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id, name
            FROM tracked_items
            WHERE id = ?
        """, (tracked_item_id,))

        item = cursor.fetchone()

        if not item:
            conn.rollback()
            return False

        now = datetime.now().isoformat(timespec="seconds")

        cursor.execute("""
            INSERT OR IGNORE INTO processed_order_fills (
                item_id,
                tracked_item_id,
                price,
                processed_at
            )
            VALUES (?, ?, ?, ?)
        """, (item_id, tracked_item_id, price, now))

        if cursor.rowcount != 1:
            conn.rollback()
            return False

        cursor.execute("""
            INSERT INTO purchase_history (
                tracked_item_id,
                item_name,
                price,
                market_listing_id,
                purchased_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            tracked_item_id,
            item["name"],
            price,
            "order:" + item_id,
            now
        ))

        cursor.execute("""
            UPDATE tracked_items
            SET purchased_quantity = purchased_quantity + 1
            WHERE id = ?
        """, (tracked_item_id,))

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def get_processed_order_fill_ids():
    conn = connect()

    try:
        rows = conn.execute("""
            SELECT item_id
            FROM processed_order_fills
        """).fetchall()

        return {str(row["item_id"]) for row in rows}

    finally:
        conn.close()
# =====================================
# РУЧНАЯ ОТМЕНА ЗАВИСШЕЙ ПОПЫТКИ ПОКУПКИ
# =====================================

def cancel_purchase_attempt(attempt_id, reason="Покупка вручную отменена пользователем"):
    """
    Отменяет незавершённую попытку покупки.

    ВАЖНО:
    - Не обращается к API маркета.
    - Не отменяет реальные ордера.
    - Не добавляет покупку в историю.
    - Не увеличивает purchased_quantity.
    - Освобождает резерв количества и бюджета.
    - Не позволяет отменить уже подтверждённую покупку.
    """

    attempt_id = int(attempt_id)

    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)

        cursor.execute(f"""
            SELECT id, status
            FROM purchase_attempts
            WHERE id = ?
              AND status IN ({placeholders})
        """, (attempt_id, *ACTIVE_STATUSES))

        attempt = cursor.fetchone()

        if not attempt:
            conn.rollback()
            return False

        cursor.execute("""
            UPDATE purchase_attempts
            SET status = 'cancelled',
                error = ?,
                updated_at = ?
            WHERE id = ?
              AND status = ?
        """, (
            str(reason),
            datetime.now().isoformat(timespec="seconds"),
            attempt_id,
            attempt["status"]
        ))

        if cursor.rowcount != 1:
            conn.rollback()
            return False

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

# =====================================
# ИНИЦИАЛИЗАЦИЯ ПРИ ИМПОРТЕ
# =====================================

init()
