import hashlib
import hmac
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

ROOT = Path(__file__).resolve().parent
DB = ROOT / "autoprime.db"


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL, price INTEGER NOT NULL, category TEXT NOT NULL,
          description TEXT NOT NULL, accent TEXT NOT NULL DEFAULT '#c8ff32',
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL, username TEXT, product_id INTEGER NOT NULL,
          product_name TEXT NOT NULL, price INTEGER NOT NULL,
          phone TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'Новый',
          created_at INTEGER NOT NULL
        );
        """)
        count = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if not count:
            con.executemany(
                "INSERT INTO products(name,price,category,description,accent) VALUES(?,?,?,?,?)",
                [
                    ("AP Sport Carbon", 34990, "Спорт", "Карбоновый руль, анатомический хват и перфорация.", "#c8ff32"),
                    ("AP Classic Black", 24990, "Классика", "Строгая форма, натуральная кожа и красная строчка.", "#ff4d5e"),
                    ("AP Track Alcantara", 41990, "Трек", "Алькантара, метка нулевого положения и усиленный каркас.", "#64b5ff"),
                    ("AP Street Lime", 29990, "Street", "Яркий акцент AutoPrime и комфортный ежедневный хват.", "#c8ff32"),
                ],
            )


def check_init_data(raw: str, token: str):
    if not raw:
        return None
    try:
        pairs = parse_qsl(raw, strict_parsing=True)
        if len({k for k, _ in pairs}) != len(pairs):
            return None
        data = dict(pairs)
        received = data.pop("hash")
        auth_date = int(data.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + 30 or now - auth_date > 86400:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        user = json.loads(data.get("user", "{}"))
        uid = user.get("id")
        if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
            return None
        return user
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def rows(sql, params=()):
    with db() as con:
        return [dict(x) for x in con.execute(sql, params).fetchall()]


def create_app(token: str, admin_id: int, manager: str):
    app = web.Application(client_max_size=1024 * 1024)

    @web.middleware
    async def auth(request, handler):
        if request.path.startswith("/api/"):
            user = check_init_data(request.headers.get("X-Telegram-Init-Data", ""), token)
            if not user:
                return web.json_response({"error": "Откройте магазин из Telegram"}, status=401)
            request["tg_user"] = user
            request["is_admin"] = user["id"] == admin_id
        return await handler(request)

    app.middlewares.append(auth)

    async def index(_):
        return web.FileResponse(ROOT / "index.html")

    async def health(_):
        return web.Response(text="ok")

    async def me(request):
        u = request["tg_user"]
        return web.json_response({
            "id": u["id"], "name": u.get("first_name", "Покупатель"),
            "username": u.get("username", ""), "is_admin": request["is_admin"],
            "manager": manager,
        })

    async def products(request):
        show_all = request["is_admin"]
        sql = "SELECT * FROM products" + ("" if show_all else " WHERE active=1") + " ORDER BY id"
        return web.json_response(rows(sql))

    async def orders(request):
        u = request["tg_user"]
        if request["is_admin"]:
            data = rows("SELECT * FROM orders ORDER BY id DESC")
        else:
            data = rows("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (u["id"],))
        return web.json_response(data)

    async def make_order(request):
        u = request["tg_user"]
        try:
            body = await request.json()
            pid = int(body["product_id"])
            phone = str(body["phone"]).strip()[:40]
            comment = str(body.get("comment", "")).strip()[:500]
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return web.json_response({"error": "Проверьте данные"}, status=400)
        if len(phone) < 5:
            return web.json_response({"error": "Укажите телефон"}, status=400)
        with db() as con:
            product = con.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if not product:
                return web.json_response({"error": "Товар не найден"}, status=404)
            cur = con.execute(
                """INSERT INTO orders(user_id,username,product_id,product_name,price,phone,comment,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (u["id"], u.get("username", ""), pid, product["name"], product["price"], phone, comment, int(time.time())),
            )
            oid = cur.lastrowid
        return web.json_response({"ok": True, "order_id": oid})

    async def set_status(request):
        if not request["is_admin"]:
            return web.json_response({"error": "Нет доступа"}, status=403)
        try:
            oid = int(request.match_info["id"])
            status = (await request.json())["status"]
        except (ValueError, KeyError, json.JSONDecodeError):
            return web.json_response({"error": "Проверьте данные"}, status=400)
        allowed = {"Новый", "Подтверждён", "В работе", "Готов", "Отменён"}
        if status not in allowed:
            return web.json_response({"error": "Неверный статус"}, status=400)
        with db() as con:
            con.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
        return web.json_response({"ok": True})

    async def add_product(request):
        if not request["is_admin"]:
            return web.json_response({"error": "Нет доступа"}, status=403)
        try:
            b = await request.json()
            name = str(b["name"]).strip()[:80]
            price = int(b["price"])
            category = str(b.get("category", "Рули")).strip()[:40]
            description = str(b.get("description", "")).strip()[:300]
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return web.json_response({"error": "Проверьте данные"}, status=400)
        if not name or price <= 0:
            return web.json_response({"error": "Проверьте название и цену"}, status=400)
        with db() as con:
            con.execute(
                "INSERT INTO products(name,price,category,description) VALUES(?,?,?,?)",
                (name, price, category, description),
            )
        return web.json_response({"ok": True})

    async def toggle_product(request):
        if not request["is_admin"]:
            return web.json_response({"error": "Нет доступа"}, status=403)
        pid = int(request.match_info["id"])
        with db() as con:
            con.execute("UPDATE products SET active=1-active WHERE id=?", (pid,))
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/me", me)
    app.router.add_get("/api/products", products)
    app.router.add_get("/api/orders", orders)
    app.router.add_post("/api/orders", make_order)
    app.router.add_patch("/api/admin/orders/{id}", set_status)
    app.router.add_post("/api/admin/products", add_product)
    app.router.add_patch("/api/admin/products/{id}/toggle", toggle_product)
    return app