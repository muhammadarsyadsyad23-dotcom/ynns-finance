#!/usr/bin/env python3
"""YNNS FINANCE — Backend (Phase 1)."""
import requests
import json
import re
import os, sqlite3, jwt, hashlib, secrets, hmac
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

DB = "data/ynns.db"
SECRET = os.environ.get("YNNS_SECRET") or secrets.token_hex(32)
os.makedirs("data", exist_ok=True)

# ============ PASSWORD HASHING ============
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 200_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iter_str, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256": return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iter_str))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False

# ============ DB INIT ============
def init_db():
    conn = sqlite3.connect(DB); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        type TEXT DEFAULT 'CASH',
        opening_balance REAL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT NOT NULL,
        icon TEXT,
        type TEXT NOT NULL,
        is_default INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        category_id INTEGER,
        type TEXT NOT NULL,
        amount REAL NOT NULL CHECK(amount > 0),
        description TEXT,
        transaction_date TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (account_id) REFERENCES accounts(id),
        FOREIGN KEY (category_id) REFERENCES categories(id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        source_account_id INTEGER NOT NULL,
        destination_account_id INTEGER NOT NULL,
        amount REAL NOT NULL CHECK(amount > 0),
        description TEXT,
        transfer_date TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (source_account_id) REFERENCES accounts(id),
        FOREIGN KEY (destination_account_id) REFERENCES accounts(id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tr_user ON transfers(user_id, transfer_date)")

    # Phase 3: Savings Goals
    c.execute("""CREATE TABLE IF NOT EXISTS savings_goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        icon TEXT DEFAULT '🎯',
        color TEXT DEFAULT '#22D3EE',
        target_amount REAL NOT NULL CHECK(target_amount > 0),
        current_amount REAL DEFAULT 0,
        target_date TEXT,
        status TEXT DEFAULT 'ACTIVE',
        category TEXT DEFAULT 'GENERAL',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS savings_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        goal_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        amount REAL NOT NULL CHECK(amount > 0),
        description TEXT,
        transaction_date TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (goal_id) REFERENCES savings_goals(id),
        FOREIGN KEY (account_id) REFERENCES accounts(id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sg_user ON savings_goals(user_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_st_goal ON savings_transactions(goal_id, transaction_date)")

    # Phase 3: Budgets
    c.execute("""CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        category_id INTEGER NOT NULL,
        limit_amount REAL NOT NULL CHECK(limit_amount > 0),
        period TEXT DEFAULT 'MONTHLY',
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (category_id) REFERENCES categories(id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_bg_user ON budgets(user_id, period)")

    # Settings untuk AI (Groq API key)
    c.execute("""CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        groq_api_key TEXT,
        ai_model TEXT DEFAULT 'llama-3.3-70b-versatile',
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id, transaction_date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tx_cat ON transactions(category_id)")
    defaults = [
        ("Bensin","⛽","EXPENSE"),("Rokok","🚬","EXPENSE"),("Belanja Bulanan","🛒","EXPENSE"),
        ("Makanan","🍜","EXPENSE"),("Listrik","💡","EXPENSE"),("Air","💧","EXPENSE"),
        ("Pulsa","📱","EXPENSE"),("Internet","🌐","EXPENSE"),("Kesehatan","🏥","EXPENSE"),
        ("Pendidikan","🎓","EXPENSE"),("Transportasi","🚗","EXPENSE"),("Rumah","🏠","EXPENSE"),
        ("Keluarga","👨‍👩‍👧","EXPENSE"),("Usaha","💼","EXPENSE"),("Kreator","🎥","EXPENSE"),
        ("Lainnya","📦","EXPENSE"),("Gaji","💰","INCOME"),("Bonus","🎁","INCOME"),
        ("Penjualan","📈","INCOME"),("Lainnya","📦","INCOME"),
    ]
    for n,i,t in defaults:
        c.execute("SELECT id FROM categories WHERE user_id IS NULL AND name=? AND type=?",(n,t))
        if not c.fetchone():
            c.execute("INSERT INTO categories (user_id,name,icon,type,is_default) VALUES (NULL,?,?,?,1)",(n,i,t))
    conn.commit(); conn.close()

def db():
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row; return conn

# ============ AUTH ============
def make_token(uid):
    return jwt.encode({"uid":uid,"exp":datetime.utcnow()+timedelta(days=30)}, SECRET, algorithm="HS256")

def auth_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        h = request.headers.get("Authorization","")
        if not h.startswith("Bearer "): return jsonify({"error":"no token"}), 401
        try:
            payload = jwt.decode(h[7:], SECRET, algorithms=["HS256"])
            request.uid = payload["uid"]
        except Exception:
            return jsonify({"error":"invalid token"}), 401
        return f(*a, **kw)
    return wrapper

# ============ BALANCE ENGINE ============
def calc_balance(uid, account_id=None):
    conn = db(); c = conn.cursor()
    tr_in = 0; tr_out = 0; sg_out = 0; sg_in = 0
    if account_id:
        c.execute("SELECT opening_balance FROM accounts WHERE id=? AND user_id=?",(account_id,uid))
        row = c.fetchone(); opening = row["opening_balance"] if row else 0
        c.execute("SELECT type, COALESCE(SUM(amount),0) AS total FROM transactions WHERE user_id=? AND account_id=? GROUP BY type",(uid,account_id))
        sums = {r["type"]: r["total"] for r in c.fetchall()}
        c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM transfers WHERE user_id=? AND destination_account_id=?",(uid,account_id))
        tr_in = c.fetchone()["t"]
        c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM transfers WHERE user_id=? AND source_account_id=?",(uid,account_id))
        tr_out = c.fetchone()["t"]
        try:
            c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM savings_transactions WHERE user_id=? AND account_id=? AND type='DEPOSIT'",(uid,account_id))
            sg_out = c.fetchone()["t"]
            c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM savings_transactions WHERE user_id=? AND account_id=? AND type='WITHDRAW'",(uid,account_id))
            sg_in = c.fetchone()["t"]
        except Exception:
            sg_out = 0; sg_in = 0
    else:
        c.execute("SELECT COALESCE(SUM(opening_balance),0) AS o FROM accounts WHERE user_id=?",(uid,))
        opening = c.fetchone()["o"]
        c.execute("SELECT type, COALESCE(SUM(amount),0) AS total FROM transactions WHERE user_id=? GROUP BY type",(uid,))
        sums = {r["type"]: r["total"] for r in c.fetchall()}
    conn.close()
    income = sums.get("INCOME",0); expense = sums.get("EXPENSE",0)
    return {"opening":opening,"income":income,"expense":expense,
            "transfer_in":tr_in,"transfer_out":tr_out,
            "savings_out":sg_out,"savings_in":sg_in,
            "balance":opening + income - expense + tr_in - tr_out - sg_out + sg_in}

# ============ ROUTES ============
@app.route("/")
def index(): return send_from_directory("static","index.html")

@app.route("/api/register", methods=["POST"])
def register():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    if not name or not email or len(pw) < 6:
        return jsonify({"error":"nama, email, password (min 6) wajib"}), 400
    conn = db(); c = conn.cursor()
    try:
        h = hash_password(pw)
        c.execute("INSERT INTO users (name,email,password_hash) VALUES (?,?,?)",(name,email,h))
        uid = c.lastrowid
        c.execute("INSERT INTO accounts (user_id,name,type,opening_balance) VALUES (?,?,?,?)",(uid,"Dompet Utama","CASH",0))
        conn.commit()
        return jsonify({"ok":True,"token":make_token(uid),"user":{"id":uid,"name":name,"email":email}})
    except sqlite3.IntegrityError:
        return jsonify({"error":"email sudah terdaftar"}), 400
    finally: conn.close()

@app.route("/api/login", methods=["POST"])
def login():
    d = request.get_json(force=True)
    email = (d.get("email") or "").strip().lower()
    pw = d.get("password") or ""
    conn = db(); c = conn.cursor()
    c.execute("SELECT id,name,email,password_hash FROM users WHERE email=?",(email,))
    u = c.fetchone(); conn.close()
    if not u or not verify_password(pw, u["password_hash"]):
        return jsonify({"error":"email / password salah"}), 401
    return jsonify({"ok":True,"token":make_token(u["id"]),
                    "user":{"id":u["id"],"name":u["name"],"email":u["email"]}})

@app.route("/api/me")
@auth_required
def me():
    conn = db(); c = conn.cursor()
    c.execute("SELECT id,name,email FROM users WHERE id=?",(request.uid,))
    u = c.fetchone(); conn.close()
    return jsonify(dict(u))

@app.route("/api/accounts")
@auth_required
def accounts():
    conn = db(); c = conn.cursor()
    c.execute("SELECT id,name,type,opening_balance FROM accounts WHERE user_id=?",(request.uid,))
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    for r in rows: r["balance"] = calc_balance(request.uid, r["id"])["balance"]
    return jsonify(rows)

@app.route("/api/categories")
@auth_required
def categories():
    conn = db(); c = conn.cursor()
    c.execute("SELECT id,name,icon,type FROM categories WHERE user_id IS NULL OR user_id=? ORDER BY type,id",(request.uid,))
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return jsonify(rows)

@app.route("/api/dashboard")
@auth_required
def dashboard():
    bal = calc_balance(request.uid)
    month_start = datetime.utcnow().strftime("%Y-%m-01")
    conn = db(); c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE user_id=? AND type='INCOME' AND transaction_date>=?",(request.uid,month_start))
    mi = c.fetchone()["t"]
    c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE user_id=? AND type='EXPENSE' AND transaction_date>=?",(request.uid,month_start))
    me = c.fetchone()["t"]
    c.execute("SELECT COUNT(*) AS n FROM transactions WHERE user_id=?",(request.uid,))
    tc = c.fetchone()["n"]; conn.close()
    return jsonify({"balance":bal["balance"],"opening":bal["opening"],
                    "total_income":bal["income"],"total_expense":bal["expense"],
                    "month_income":mi,"month_expense":me,"net_cashflow":mi-me,
                    "tx_count":tc,"today":datetime.utcnow().strftime("%Y-%m-%d")})

@app.route("/api/transactions", methods=["GET"])
@auth_required
def list_tx():
    limit = min(int(request.args.get("limit", 100)), 500)
    tx_type = request.args.get("type")
    cat = request.args.get("category_id")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    q = request.args.get("q")
    sql = """SELECT t.id,t.type,t.amount,t.description,t.transaction_date,t.created_at,t.category_id,t.account_id,
             c.name AS category_name,c.icon AS category_icon,a.name AS account_name
             FROM transactions t
             LEFT JOIN categories c ON c.id=t.category_id
             LEFT JOIN accounts a ON a.id=t.account_id
             WHERE t.user_id=?"""
    params = [request.uid]
    if tx_type in ("INCOME","EXPENSE"):
        sql += " AND t.type=?"; params.append(tx_type)
    if cat:
        sql += " AND t.category_id=?"; params.append(cat)
    if date_from:
        sql += " AND t.transaction_date>=?"; params.append(date_from)
    if date_to:
        sql += " AND t.transaction_date<=?"; params.append(date_to)
    if q:
        sql += " AND t.description LIKE ?"; params.append(f"%{q}%")
    sql += " ORDER BY t.transaction_date DESC, t.id DESC LIMIT ?"
    params.append(limit)
    conn = db(); c = conn.cursor()
    c.execute(sql, params)
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return jsonify(rows)

@app.route("/api/transactions", methods=["POST"])
@auth_required
def create_tx():
    d = request.get_json(force=True)
    try: amount = float(d.get("amount") or 0)
    except: return jsonify({"error":"amount tidak valid"}), 400
    if amount <= 0: return jsonify({"error":"amount harus > 0"}), 400
    tx_type = d.get("type")
    if tx_type not in ("INCOME","EXPENSE"): return jsonify({"error":"type harus INCOME atau EXPENSE"}), 400
    account_id = int(d.get("account_id") or 0)
    category_id = d.get("category_id")
    description = (d.get("description") or "").strip()
    tx_date = d.get("transaction_date") or datetime.utcnow().strftime("%Y-%m-%d")
    conn = db(); c = conn.cursor()
    c.execute("SELECT id FROM accounts WHERE id=? AND user_id=?",(account_id,request.uid))
    if not c.fetchone(): conn.close(); return jsonify({"error":"akun tidak valid"}), 400
    if category_id:
        c.execute("SELECT id FROM categories WHERE id=? AND (user_id IS NULL OR user_id=?)",(category_id,request.uid))
        if not c.fetchone(): conn.close(); return jsonify({"error":"kategori tidak valid"}), 400
    if tx_type == "EXPENSE":
        bal = calc_balance(request.uid, account_id)["balance"]
        if bal < amount:
            conn.close(); return jsonify({"error":f"saldo tidak cukup. Saldo: {bal}"}), 400
    try:
        c.execute("BEGIN")
        c.execute("""INSERT INTO transactions (user_id,account_id,category_id,type,amount,description,transaction_date)
                     VALUES (?,?,?,?,?,?,?)""",(request.uid,account_id,category_id,tx_type,amount,description,tx_date))
        tx_id = c.lastrowid
        conn.commit()
        return jsonify({"ok":True,"id":tx_id,"balance_after":calc_balance(request.uid, account_id)["balance"]})
    except Exception as e:
        conn.rollback(); return jsonify({"error":str(e)}), 500
    finally: conn.close()

@app.route("/api/transactions/<int:tx_id>", methods=["DELETE"])
@auth_required
def delete_tx(tx_id):
    conn = db(); c = conn.cursor()
    c.execute("SELECT id FROM transactions WHERE id=? AND user_id=?",(tx_id,request.uid))
    if not c.fetchone(): conn.close(); return jsonify({"error":"transaksi tidak ditemukan"}), 404
    try:
        c.execute("BEGIN")
        c.execute("DELETE FROM transactions WHERE id=? AND user_id=?",(tx_id,request.uid))
        conn.commit(); return jsonify({"ok":True})
    except Exception as e:
        conn.rollback(); return jsonify({"error":str(e)}), 500
    finally: conn.close()

@app.route("/api/summary/category")
@auth_required
def summary_category():
    period = request.args.get("period","month")
    if period == "week": start = (datetime.utcnow()-timedelta(days=7)).strftime("%Y-%m-%d")
    elif period == "year": start = datetime.utcnow().strftime("%Y-01-01")
    else: start = datetime.utcnow().strftime("%Y-%m-01")
    conn = db(); c = conn.cursor()
    c.execute("""SELECT c.name, c.icon, COALESCE(SUM(t.amount),0) AS total
                 FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
                 WHERE t.user_id=? AND t.type='EXPENSE' AND t.transaction_date>=?
                 GROUP BY c.id ORDER BY total DESC""",(request.uid,start))
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return jsonify(rows)


# ============ PHASE 2: TRANSFERS ============
@app.route("/api/transfers", methods=["GET"])
@auth_required
def list_transfers():
    conn = db(); c = conn.cursor()
    c.execute("""SELECT t.id,t.amount,t.description,t.transfer_date,
                 s.name AS source_name, d.name AS destination_name
                 FROM transfers t
                 LEFT JOIN accounts s ON s.id=t.source_account_id
                 LEFT JOIN accounts d ON d.id=t.destination_account_id
                 WHERE t.user_id=? ORDER BY t.transfer_date DESC, t.id DESC LIMIT 100""",(request.uid,))
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return jsonify(rows)

@app.route("/api/transfers", methods=["POST"])
@auth_required
def create_transfer():
    d = request.get_json(force=True)
    try: amount = float(d.get("amount") or 0)
    except: return jsonify({"error":"amount tidak valid"}), 400
    if amount <= 0: return jsonify({"error":"amount harus > 0"}), 400
    src = int(d.get("source_account_id") or 0)
    dst = int(d.get("destination_account_id") or 0)
    if src == dst: return jsonify({"error":"akun sumber & tujuan tidak boleh sama"}), 400
    desc = (d.get("description") or "").strip()
    tdate = d.get("transfer_date") or datetime.utcnow().strftime("%Y-%m-%d")
    conn = db(); c = conn.cursor()
    c.execute("SELECT id FROM accounts WHERE id=? AND user_id=?",(src,request.uid))
    if not c.fetchone(): conn.close(); return jsonify({"error":"akun sumber tidak valid"}), 400
    c.execute("SELECT id FROM accounts WHERE id=? AND user_id=?",(dst,request.uid))
    if not c.fetchone(): conn.close(); return jsonify({"error":"akun tujuan tidak valid"}), 400
    bal = calc_balance(request.uid, src)["balance"]
    if bal < amount:
        conn.close(); return jsonify({"error":f"saldo sumber tidak cukup. Saldo: {bal}"}), 400
    try:
        c.execute("BEGIN")
        c.execute("""INSERT INTO transfers (user_id,source_account_id,destination_account_id,amount,description,transfer_date)
                     VALUES (?,?,?,?,?,?)""",(request.uid,src,dst,amount,desc,tdate))
        tid = c.lastrowid; conn.commit()
        return jsonify({"ok":True,"id":tid})
    except Exception as e:
        conn.rollback(); return jsonify({"error":str(e)}), 500
    finally: conn.close()

@app.route("/api/transfers/<int:tid>", methods=["DELETE"])
@auth_required
def delete_transfer(tid):
    conn = db(); c = conn.cursor()
    c.execute("SELECT id FROM transfers WHERE id=? AND user_id=?",(tid,request.uid))
    if not c.fetchone(): conn.close(); return jsonify({"error":"tidak ditemukan"}), 404
    c.execute("DELETE FROM transfers WHERE id=? AND user_id=?",(tid,request.uid))
    conn.commit(); conn.close()
    return jsonify({"ok":True})

# ============ PHASE 2: EDIT TRANSACTION ============
@app.route("/api/transactions/<int:tx_id>", methods=["PUT"])
@auth_required
def update_tx(tx_id):
    d = request.get_json(force=True)
    conn = db(); c = conn.cursor()
    c.execute("SELECT * FROM transactions WHERE id=? AND user_id=?",(tx_id,request.uid))
    old = c.fetchone()
    if not old: conn.close(); return jsonify({"error":"tidak ditemukan"}), 404
    try: amount = float(d.get("amount", old["amount"]))
    except: conn.close(); return jsonify({"error":"amount invalid"}), 400
    if amount <= 0: conn.close(); return jsonify({"error":"amount > 0"}), 400
    tx_type = d.get("type", old["type"])
    if tx_type not in ("INCOME","EXPENSE"): conn.close(); return jsonify({"error":"type invalid"}), 400
    account_id = int(d.get("account_id", old["account_id"]))
    category_id = d.get("category_id", old["category_id"])
    description = d.get("description", old["description"])
    tx_date = d.get("transaction_date", old["transaction_date"])
    c.execute("SELECT id FROM accounts WHERE id=? AND user_id=?",(account_id,request.uid))
    if not c.fetchone(): conn.close(); return jsonify({"error":"akun invalid"}), 400
    if tx_type == "EXPENSE":
        bal = calc_balance(request.uid, account_id)["balance"]
        if old["account_id"] == account_id and old["type"] == "EXPENSE":
            bal += old["amount"]
        elif old["account_id"] == account_id and old["type"] == "INCOME":
            bal -= old["amount"]
        if bal < amount:
            conn.close(); return jsonify({"error":f"saldo tidak cukup. Available: {bal}"}), 400
    c.execute("""UPDATE transactions SET account_id=?,category_id=?,type=?,amount=?,description=?,transaction_date=?
                 WHERE id=? AND user_id=?""",(account_id,category_id,tx_type,amount,description,tx_date,tx_id,request.uid))
    conn.commit(); conn.close()
    return jsonify({"ok":True})

# ============ PHASE 2: MULTIPLE ACCOUNTS ============
@app.route("/api/accounts", methods=["POST"])
@auth_required
def add_account():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name: return jsonify({"error":"nama akun wajib"}), 400
    atype = d.get("type","CASH")
    if atype not in ("CASH","BANK","E_WALLET","OTHER"): atype = "CASH"
    try: opening = float(d.get("opening_balance") or 0)
    except: opening = 0
    conn = db(); c = conn.cursor()
    c.execute("INSERT INTO accounts (user_id,name,type,opening_balance) VALUES (?,?,?,?)",(request.uid,name,atype,opening))
    aid = c.lastrowid; conn.commit(); conn.close()
    return jsonify({"ok":True,"id":aid})

@app.route("/api/accounts/<int:aid>", methods=["DELETE"])
@auth_required
def del_account(aid):
    conn = db(); c = conn.cursor()
    # Cek akun milik user
    c.execute("SELECT id,name FROM accounts WHERE id=? AND user_id=?",(aid,request.uid))
    acc = c.fetchone()
    if not acc:
        conn.close()
        return jsonify({"error":"Akun tidak ditemukan"}), 404
    # Cek transaksi income/expense — kalau ada, blokir (biar audit trail aman)
    c.execute("SELECT COUNT(*) AS n FROM transactions WHERE user_id=? AND account_id=?",(request.uid,aid))
    tx_count = c.fetchone()["n"]
    if tx_count > 0:
        conn.close()
        return jsonify({"error":f"Akun ini punya {tx_count} transaksi. Hapus transaksinya dulu."}), 400
    # Cek saldo
    bal = calc_balance(request.uid, aid)["balance"]
    if abs(bal) > 0.01:
        conn.close()
        return jsonify({"error":f"Akun masih punya saldo Rp{int(bal)}. Transfer keluar dulu."}), 400
    # Cek savings transactions
    c.execute("SELECT COUNT(*) AS n FROM savings_transactions WHERE user_id=? AND account_id=?",(request.uid,aid))
    st_count = c.fetchone()["n"]
    if st_count > 0:
        conn.close()
        return jsonify({"error":f"Akun ini terlibat di {st_count} transaksi tabungan. Gak bisa dihapus."}), 400
    # Semua aman — hapus transfers terkait, hapus akun
    try:
        c.execute("BEGIN")
        c.execute("DELETE FROM transfers WHERE user_id=? AND (source_account_id=? OR destination_account_id=?)",(request.uid,aid,aid))
        c.execute("DELETE FROM accounts WHERE id=? AND user_id=?",(aid,request.uid))
        conn.commit()
        return jsonify({"ok":True,"deleted":acc["name"]})
    except Exception as e:
        conn.rollback()
        return jsonify({"error":str(e)}), 500
    finally:
        conn.close()

@app.route("/api/wealth")
@auth_required
def wealth():
    conn = db(); c = conn.cursor()
    c.execute("SELECT id,name,type,opening_balance FROM accounts WHERE user_id=?",(request.uid,))
    accs = [dict(r) for r in c.fetchall()]; conn.close()
    total = 0
    for a in accs:
        a["balance"] = calc_balance(request.uid, a["id"])["balance"]
        total += a["balance"]
    return jsonify({"total_wealth":total,"accounts":accs})



# ============ PHASE 3: SAVINGS ============
@app.route("/api/savings", methods=["GET"])
@auth_required
def list_savings():
    conn = db(); c = conn.cursor()
    c.execute("""SELECT id,name,description,icon,color,target_amount,current_amount,target_date,status,category,created_at
                 FROM savings_goals WHERE user_id=? ORDER BY status ASC, id DESC""",(request.uid,))
    rows = []
    for r in c.fetchall():
        d = dict(r)
        d["progress"] = round((d["current_amount"] / d["target_amount"] * 100), 1) if d["target_amount"] else 0
        rows.append(d)
    conn.close()
    return jsonify(rows)

@app.route("/api/savings", methods=["POST"])
@auth_required
def create_saving():
    d = request.get_json(force=True)
    name = (d.get("name") or "").strip()
    if not name: return jsonify({"error":"nama target wajib"}), 400
    try: target = float(d.get("target_amount") or 0)
    except: return jsonify({"error":"target invalid"}), 400
    if target <= 0: return jsonify({"error":"target harus > 0"}), 400
    icon = d.get("icon") or "🎯"
    color = d.get("color") or "#22D3EE"
    desc = (d.get("description") or "").strip()
    tdate = d.get("target_date") or None
    cat = d.get("category") or "GENERAL"
    conn = db(); c = conn.cursor()
    c.execute("""INSERT INTO savings_goals (user_id,name,description,icon,color,target_amount,target_date,category)
                 VALUES (?,?,?,?,?,?,?,?)""",(request.uid,name,desc,icon,color,target,tdate,cat))
    sid = c.lastrowid; conn.commit(); conn.close()
    return jsonify({"ok":True,"id":sid})

@app.route("/api/savings/<int:sid>", methods=["DELETE"])
@auth_required
def delete_saving(sid):
    conn = db(); c = conn.cursor()
    c.execute("SELECT current_amount FROM savings_goals WHERE id=? AND user_id=?",(sid,request.uid))
    r = c.fetchone()
    if not r: conn.close(); return jsonify({"error":"tidak ditemukan"}), 404
    if r["current_amount"] > 0:
        conn.close()
        return jsonify({"error":f"target masih punya saldo Rp{r['current_amount']}. Tarik dulu sebelum hapus."}), 400
    c.execute("DELETE FROM savings_transactions WHERE goal_id=? AND user_id=?",(sid,request.uid))
    c.execute("DELETE FROM savings_goals WHERE id=? AND user_id=?",(sid,request.uid))
    conn.commit(); conn.close()
    return jsonify({"ok":True})

@app.route("/api/savings/<int:sid>/deposit", methods=["POST"])
@auth_required
def deposit_saving(sid):
    d = request.get_json(force=True)
    try: amount = float(d.get("amount") or 0)
    except: return jsonify({"error":"amount invalid"}), 400
    if amount <= 0: return jsonify({"error":"amount > 0"}), 400
    account_id = int(d.get("account_id") or 0)
    desc = (d.get("description") or "").strip()
    tdate = d.get("transaction_date") or datetime.utcnow().strftime("%Y-%m-%d")
    conn = db(); c = conn.cursor()
    c.execute("SELECT id,current_amount,target_amount FROM savings_goals WHERE id=? AND user_id=?",(sid,request.uid))
    goal = c.fetchone()
    if not goal: conn.close(); return jsonify({"error":"target tidak ditemukan"}), 404
    c.execute("SELECT id FROM accounts WHERE id=? AND user_id=?",(account_id,request.uid))
    if not c.fetchone(): conn.close(); return jsonify({"error":"akun invalid"}), 400
    bal = calc_balance(request.uid, account_id)["balance"]
    if bal < amount:
        conn.close(); return jsonify({"error":f"saldo tidak cukup. Saldo: {bal}"}), 400
    try:
        c.execute("BEGIN")
        c.execute("""INSERT INTO savings_transactions (user_id,goal_id,account_id,type,amount,description,transaction_date)
                     VALUES (?,?,?,?,?,?,?)""",(request.uid,sid,account_id,"DEPOSIT",amount,desc,tdate))
        c.execute("UPDATE savings_goals SET current_amount=current_amount+?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(amount,sid))
        new_amt = goal["current_amount"] + amount
        if new_amt >= goal["target_amount"]:
            c.execute("UPDATE savings_goals SET status='COMPLETED' WHERE id=?",(sid,))
        conn.commit()
        return jsonify({"ok":True,"current_amount":new_amt})
    except Exception as e:
        conn.rollback(); return jsonify({"error":str(e)}), 500
    finally: conn.close()

@app.route("/api/savings/<int:sid>/withdraw", methods=["POST"])
@auth_required
def withdraw_saving(sid):
    d = request.get_json(force=True)
    try: amount = float(d.get("amount") or 0)
    except: return jsonify({"error":"amount invalid"}), 400
    if amount <= 0: return jsonify({"error":"amount > 0"}), 400
    account_id = int(d.get("account_id") or 0)
    desc = (d.get("description") or "").strip()
    tdate = d.get("transaction_date") or datetime.utcnow().strftime("%Y-%m-%d")
    conn = db(); c = conn.cursor()
    c.execute("SELECT id,current_amount FROM savings_goals WHERE id=? AND user_id=?",(sid,request.uid))
    goal = c.fetchone()
    if not goal: conn.close(); return jsonify({"error":"target tidak ditemukan"}), 404
    if goal["current_amount"] < amount:
        conn.close(); return jsonify({"error":f"saldo target tidak cukup. Tersedia: {goal['current_amount']}"}), 400
    c.execute("SELECT id FROM accounts WHERE id=? AND user_id=?",(account_id,request.uid))
    if not c.fetchone(): conn.close(); return jsonify({"error":"akun invalid"}), 400
    try:
        c.execute("BEGIN")
        c.execute("""INSERT INTO savings_transactions (user_id,goal_id,account_id,type,amount,description,transaction_date)
                     VALUES (?,?,?,?,?,?,?)""",(request.uid,sid,account_id,"WITHDRAW",amount,desc,tdate))
        c.execute("UPDATE savings_goals SET current_amount=current_amount-?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(amount,sid))
        new_amt = goal["current_amount"] - amount
        if new_amt < goal["target_amount"] if False else False:
            pass
        c.execute("UPDATE savings_goals SET status='ACTIVE' WHERE id=? AND current_amount < target_amount",(sid,))
        conn.commit()
        return jsonify({"ok":True,"current_amount":new_amt})
    except Exception as e:
        conn.rollback(); return jsonify({"error":str(e)}), 500
    finally: conn.close()

@app.route("/api/savings/<int:sid>/history")
@auth_required
def savings_history(sid):
    conn = db(); c = conn.cursor()
    c.execute("""SELECT st.id,st.type,st.amount,st.description,st.transaction_date,a.name AS account_name
                 FROM savings_transactions st LEFT JOIN accounts a ON a.id=st.account_id
                 WHERE st.user_id=? AND st.goal_id=? ORDER BY st.transaction_date DESC, st.id DESC LIMIT 50""",
              (request.uid,sid))
    rows = [dict(r) for r in c.fetchall()]; conn.close()
    return jsonify(rows)

# ============ PHASE 3: BUDGETS ============
@app.route("/api/budgets", methods=["GET"])
@auth_required
def list_budgets():
    month_start = datetime.utcnow().strftime("%Y-%m-01")
    conn = db(); c = conn.cursor()
    c.execute("""SELECT b.id,b.category_id,b.limit_amount,b.period,b.start_date,b.end_date,
                 c.name AS category_name, c.icon AS category_icon
                 FROM budgets b LEFT JOIN categories c ON c.id=b.category_id
                 WHERE b.user_id=? AND b.period='MONTHLY' ORDER BY b.id DESC""",(request.uid,))
    rows = []
    for r in c.fetchall():
        d = dict(r)
        c.execute("""SELECT COALESCE(SUM(amount),0) AS t FROM transactions
                     WHERE user_id=? AND category_id=? AND type='EXPENSE' AND transaction_date>=?""",
                  (request.uid,d["category_id"],month_start))
        spent = c.fetchone()["t"]
        d["spent"] = spent
        d["remaining"] = d["limit_amount"] - spent
        d["pct"] = round(spent / d["limit_amount"] * 100, 1) if d["limit_amount"] else 0
        if d["pct"] >= 100: d["status"] = "OVER"
        elif d["pct"] >= 80: d["status"] = "WARN"
        else: d["status"] = "OK"
        rows.append(d)
    conn.close()
    return jsonify(rows)

@app.route("/api/budgets", methods=["POST"])
@auth_required
def create_budget():
    d = request.get_json(force=True)
    try: limit = float(d.get("limit_amount") or 0)
    except: return jsonify({"error":"limit invalid"}), 400
    if limit <= 0: return jsonify({"error":"limit > 0"}), 400
    cat = d.get("category_id")
    if not cat: return jsonify({"error":"kategori wajib"}), 400
    conn = db(); c = conn.cursor()
    c.execute("SELECT id FROM categories WHERE id=? AND (user_id IS NULL OR user_id=?)",(cat,request.uid))
    if not c.fetchone(): conn.close(); return jsonify({"error":"kategori invalid"}), 400
    # Cek duplikat bulan ini
    month_start = datetime.utcnow().strftime("%Y-%m-01")
    c.execute("SELECT id FROM budgets WHERE user_id=? AND category_id=? AND period='MONTHLY' AND start_date=?",(request.uid,cat,month_start))
    if c.fetchone():
        conn.close(); return jsonify({"error":"budget kategori ini sudah ada bulan ini"}), 400
    month_end = datetime.utcnow().strftime("%Y-%m-") + "31"
    c.execute("""INSERT INTO budgets (user_id,category_id,limit_amount,period,start_date,end_date)
                 VALUES (?,?,?,?,?,?)""",(request.uid,cat,limit,"MONTHLY",month_start,month_end))
    bid = c.lastrowid; conn.commit(); conn.close()
    return jsonify({"ok":True,"id":bid})

@app.route("/api/budgets/<int:bid>", methods=["DELETE"])
@auth_required
def delete_budget(bid):
    conn = db(); c = conn.cursor()
    c.execute("DELETE FROM budgets WHERE id=? AND user_id=?",(bid,request.uid))
    conn.commit(); conn.close()
    return jsonify({"ok":True})

# ============ PHASE 3: REPORTS ============
@app.route("/api/reports/summary")
@auth_required
def reports_summary():
    period = request.args.get("period","month")
    if period == "week": start = (datetime.utcnow()-timedelta(days=7)).strftime("%Y-%m-%d")
    elif period == "year": start = datetime.utcnow().strftime("%Y-01-01")
    else: start = datetime.utcnow().strftime("%Y-%m-01")
    conn = db(); c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE user_id=? AND type='INCOME' AND transaction_date>=?",(request.uid,start))
    income = c.fetchone()["t"]
    c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE user_id=? AND type='EXPENSE' AND transaction_date>=?",(request.uid,start))
    expense = c.fetchone()["t"]
    c.execute("""SELECT c.name,c.icon,COALESCE(SUM(t.amount),0) AS total FROM transactions t
                 LEFT JOIN categories c ON c.id=t.category_id
                 WHERE t.user_id=? AND t.type='EXPENSE' AND t.transaction_date>=?
                 GROUP BY c.id ORDER BY total DESC""",(request.uid,start))
    categories = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({"period":period,"start":start,"income":income,"expense":expense,
                    "net":income-expense,"categories":categories})

@app.route("/api/reports/daily")
@auth_required
def reports_daily():
    """Return 30 hari terakhir: income, expense per hari."""
    conn = db(); c = conn.cursor()
    days = []
    for i in range(29, -1, -1):
        d = (datetime.utcnow()-timedelta(days=i)).strftime("%Y-%m-%d")
        c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE user_id=? AND type='INCOME' AND transaction_date=?",(request.uid,d))
        inc = c.fetchone()["t"]
        c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE user_id=? AND type='EXPENSE' AND transaction_date=?",(request.uid,d))
        exp = c.fetchone()["t"]
        days.append({"date":d,"income":inc,"expense":exp})
    conn.close()
    return jsonify(days)

# ============ PHASE 3: WEALTH (includes savings) ============
@app.route("/api/wealth_full")
@auth_required
def wealth_full():
    conn = db(); c = conn.cursor()
    c.execute("SELECT id,name,type,opening_balance FROM accounts WHERE user_id=?",(request.uid,))
    accs = [dict(r) for r in c.fetchall()]
    cash_total = 0
    for a in accs:
        a["balance"] = calc_balance(request.uid, a["id"])["balance"]
        cash_total += a["balance"]
    c.execute("SELECT COALESCE(SUM(current_amount),0) AS t FROM savings_goals WHERE user_id=? AND status!='ARCHIVED'",(request.uid,))
    savings_total = c.fetchone()["t"]
    conn.close()
    return jsonify({"cash":cash_total,"savings":savings_total,"total_wealth":cash_total+savings_total,"accounts":accs})


# ============ YNNS AI — NATURAL LANGUAGE PARSER ============
import re as _re

def _parse_amount(text):
    """Parse angka: 50rb, 50 ribu, 50k, 50000, 1jt, 1.5jt, 50.000"""
    t = text.lower().replace(".", "").replace(",", ".")
    patterns = [
        (r"(\d+(?:\.\d+)?)\s*(?:jt|juta)", 1_000_000),
        (r"(\d+(?:\.\d+)?)\s*(?:rb|ribu|k\b)", 1_000),
        (r"(\d{4,})", 1),
    ]
    for pat, mult in patterns:
        m = _re.search(pat, t)
        if m:
            try:
                return int(float(m.group(1)) * mult)
            except: pass
    return None

def _match_category(text, uid):
    """Cocokkan kategori dari text."""
    conn = db(); c = conn.cursor()
    c.execute("SELECT id, name, type FROM categories WHERE user_id IS NULL OR user_id=?", (uid,))
    cats = [dict(r) for r in c.fetchall()]
    conn.close()
    t = text.lower()
    # Keyword map
    kw = {
        "bensin": "Bensin", "bbm": "Bensin", "pertalite": "Bensin", "pertamax": "Bensin",
        "rokok": "Rokok", "kretek": "Rokok",
        "belanja": "Belanja Bulanan", "belanja bulanan": "Belanja Bulanan", "groceries": "Belanja Bulanan",
        "makan": "Makanan", "makanan": "Makanan", "sarapan": "Makanan", "lunch": "Makanan", "dinner": "Makanan",
        "listrik": "Listrik", "token listrik": "Listrik", "pln": "Listrik",
        "air": "Air", "pdam": "Air",
        "pulsa": "Pulsa", "kuota": "Pulsa",
        "internet": "Internet", "wifi": "Internet",
        "obat": "Kesehatan", "dokter": "Kesehatan",
        "sekolah": "Pendidikan", "kuliah": "Pendidikan", "buku": "Pendidikan",
        "transport": "Transportasi", "ojek": "Transportasi", "grab": "Transportasi", "gojek": "Transportasi",
        "sewa": "Rumah", "kontrakan": "Rumah",
        "keluarga": "Keluarga",
        "gaji": "Gaji", "salary": "Gaji",
        "bonus": "Bonus", "thr": "Bonus",
        "jualan": "Penjualan", "penjualan": "Penjualan", "omzet": "Penjualan",
    }
    for k, v in kw.items():
        if k in t:
            for cat in cats:
                if cat["name"] == v:
                    return cat
    # Fuzzy: cari nama kategori yang ada di text
    for cat in cats:
        if cat["name"].lower() in t:
            return cat
    return None

def _get_default_account(uid):
    conn = db(); c = conn.cursor()
    c.execute("SELECT id, name FROM accounts WHERE user_id=? ORDER BY id LIMIT 1", (uid,))
    r = c.fetchone(); conn.close()
    return dict(r) if r else None

def _find_account(text, uid):
    """Cari akun berdasarkan nama di text."""
    conn = db(); c = conn.cursor()
    c.execute("SELECT id, name FROM accounts WHERE user_id=?", (uid,))
    accs = [dict(r) for r in c.fetchall()]
    conn.close()
    t = text.lower()
    for a in accs:
        if a["name"].lower() in t:
            return a
    return None

def _find_saving_goal(text, uid):
    conn = db(); c = conn.cursor()
    c.execute("SELECT id, name FROM savings_goals WHERE user_id=? AND status!='ARCHIVED'", (uid,))
    goals = [dict(r) for r in c.fetchall()]
    conn.close()
    t = text.lower()
    for g in goals:
        if g["name"].lower() in t:
            return g
    # Fuzzy: cari kata kunci
    for g in goals:
        words = g["name"].lower().split()
        if any(w in t for w in words if len(w) > 3):
            return g
    return None

def ai_parse(text, uid):
    """Parse bahasa Indonesia → structured action."""
    t = text.lower().strip()
    raw = text.strip()
    out = {"intent": "UNKNOWN", "params": {}, "summary": "", "reply": "", "requires_confirmation": False}

    # ---- QUERY: saldo ----
    if any(k in t for k in ["saldo", "berapa uang", "berapa duit", "punya uang", "total kekayaan"]):
        conn = db(); c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(current_amount),0) AS s FROM savings_goals WHERE user_id=?", (uid,))
        sav = c.fetchone()["s"]; conn.close()
        bal = calc_balance(uid)
        out["intent"] = "QUERY_BALANCE"
        out["summary"] = f"Saldo cash Rp {int(bal['balance']):,} · Tabungan Rp {int(sav):,} · Total Rp {int(bal['balance']+sav):,}".replace(",", ".")
        out["reply"] = out["summary"]
        return out

    # ---- QUERY: pengeluaran ----
    if any(k in t for k in ["pengeluaran", "keluar berapa", "habis berapa", "spending"]):
        month_start = datetime.utcnow().strftime("%Y-%m-01")
        conn = db(); c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE user_id=? AND type='EXPENSE' AND transaction_date>=?", (uid, month_start))
        exp = c.fetchone()["t"]
        c.execute("""SELECT c.name, SUM(t.amount) AS total FROM transactions t
                     LEFT JOIN categories c ON c.id=t.category_id
                     WHERE t.user_id=? AND t.type='EXPENSE' AND t.transaction_date>=?
                     GROUP BY c.id ORDER BY total DESC LIMIT 3""", (uid, month_start))
        tops = c.fetchall(); conn.close()
        top_str = ", ".join([f"{r['name']} Rp {int(r['total']):,}".replace(",", ".") for r in tops]) if tops else "-"
        out["intent"] = "QUERY_EXPENSE"
        out["summary"] = f"Pengeluaran bulan ini Rp {int(exp):,}. Top: {top_str}".replace(",", ".")
        out["reply"] = out["summary"]
        return out

    # ---- QUERY: pemasukan ----
    if any(k in t for k in ["pemasukan", "income", "masuk berapa", "pendapatan"]):
        month_start = datetime.utcnow().strftime("%Y-%m-01")
        conn = db(); c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(amount),0) AS t FROM transactions WHERE user_id=? AND type='INCOME' AND transaction_date>=?", (uid, month_start))
        inc = c.fetchone()["t"]; conn.close()
        out["intent"] = "QUERY_INCOME"
        out["summary"] = f"Pemasukan bulan ini Rp {int(inc):,}".replace(",", ".")
        out["reply"] = out["summary"]
        return out

    # ---- TRANSFER ----
    if "transfer" in t or "pindah" in t or ("dari" in t and "ke" in t and _parse_amount(t)):
        amt = _parse_amount(t)
        src = _find_account(t.split(" ke ")[0] if " ke " in t else t, uid)
        dst = _find_account(t.split(" ke ")[-1] if " ke " in t else "", uid)
        if not amt:
            out["reply"] = "Berapa yang mau ditransfer?"
            return out
        if not src or not dst or src["id"] == dst["id"]:
            out["reply"] = "Sebutkan akun sumber dan tujuan. Contoh: 'transfer 50rb dari Dompet ke BCA'"
            return out
        out["intent"] = "CREATE_TRANSFER"
        out["params"] = {"source_account_id": src["id"], "destination_account_id": dst["id"], "amount": amt, "description": "Via AI", "transfer_date": datetime.utcnow().strftime("%Y-%m-%d")}
        out["summary"] = f"Transfer Rp {amt:,} dari {src['name']} ke {dst['name']}".replace(",", ".")
        out["reply"] = f"Transfer Rp {amt:,} dari {src['name']} ke {dst['name']}?".replace(",", ".")
        out["requires_confirmation"] = True
        return out

    # ---- DEPOSIT SAVINGS ----
    if any(k in t for k in ["tabung", "nabung", "simpan ke tabungan", "masukin ke"]):
        amt = _parse_amount(t)
        goal = _find_saving_goal(t, uid)
        acc = _find_account(t, uid) or _get_default_account(uid)
        if not amt:
            out["reply"] = "Berapa yang mau ditabung?"
            return out
        if not goal:
            out["reply"] = "Target tabungan mana? Yang ada: " + ", ".join([g["name"] for g in db_goals(uid)])
            return out
        out["intent"] = "SAVING_DEPOSIT"
        out["params"] = {"goal_id": goal["id"], "amount": amt, "account_id": acc["id"], "description": "Via AI", "transaction_date": datetime.utcnow().strftime("%Y-%m-%d")}
        out["summary"] = f"Tabung Rp {amt:,} ke '{goal['name']}' dari {acc['name']}".replace(",", ".")
        out["reply"] = f"Oke, tabung Rp {amt:,} ke '{goal['name']}'?".replace(",", ".")
        out["requires_confirmation"] = True
        return out

    # ---- DELETE LAST ----
    if any(k in t for k in ["hapus", "batalkan", "cancel"]) and ("terakhir" in t or "tadi" in t or "barusan" in t):
        conn = db(); c = conn.cursor()
        c.execute("SELECT id, type, amount, description, transaction_date FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
        r = c.fetchone(); conn.close()
        if not r:
            out["reply"] = "Gak ada transaksi untuk dihapus."
            return out
        out["intent"] = "DELETE_LAST_TRANSACTION"
        out["params"] = {"tx_id": r["id"]}
        out["summary"] = f"Hapus transaksi terakhir: {r['type']} Rp {int(r['amount']):,} ({r['transaction_date']})".replace(",", ".")
        out["reply"] = out["summary"] + "?"
        out["requires_confirmation"] = True
        return out

    # ---- CREATE INCOME/EXPENSE ----
    amt = _parse_amount(t)
    is_income = any(k in t for k in ["pemasukan", "pendapatan", "terima", "dapat", "gaji", "masuk", "income", "bonus", "jualan", "penjualan", "omzet"])
    is_expense = any(k in t for k in ["pengeluaran", "keluar", "beli", "bayar", "habis", "belanja", "buat", "isi", "topup", "top up"])

    if amt and (is_income or is_expense):
        # Kalau ada "keluar" & "masuk" dua-duanya, prioritas yang lebih spesifik
        if is_income and not is_expense:
            tx_type = "INCOME"
        elif is_expense and not is_income:
            tx_type = "EXPENSE"
        elif "beli" in t or "bayar" in t or "belanja" in t:
            tx_type = "EXPENSE"
        else:
            tx_type = "INCOME" if "terima" in t or "dapat" in t or "gaji" in t else "EXPENSE"

        cat = _match_category(t, uid)
        acc = _find_account(t, uid) or _get_default_account(uid)
        if not acc:
            out["reply"] = "Belum ada akun. Bikin akun dulu di menu Kelola Akun."
            return out
        out["intent"] = "CREATE_INCOME" if tx_type == "INCOME" else "CREATE_EXPENSE"
        out["params"] = {"type": tx_type, "amount": amt,
                         "category_id": cat["id"] if cat else None,
                         "category_name": cat["name"] if cat else "Lainnya",
                         "account_id": acc["id"],
                         "account_name": acc["name"],
                         "description": raw, "transaction_date": datetime.utcnow().strftime("%Y-%m-%d")}
        label = "Pemasukan" if tx_type == "INCOME" else "Pengeluaran"
        cname = cat["name"] if cat else "Lainnya"
        out["summary"] = f"{label} {cname} Rp {amt:,} dari {acc['name']}".replace(",", ".")
        out["reply"] = f"Oke, catat {label} {cname} Rp {amt:,}?".replace(",", ".")
        out["requires_confirmation"] = True
        return out

    if amt and not (is_income or is_expense):
        out["reply"] = f"Rp {amt:,} ini untuk apa? Pemasukan atau pengeluaran?".replace(",", ".")
        return out

    # ---- UNKNOWN ----
    out["reply"] = "Aku ngerti: 'bensin 50rb', 'gaji 5jt', 'tabung 100rb buat kamera', 'transfer 50rb dari Dompet ke BCA', 'berapa saldo', 'pengeluaran bulan ini', 'hapus yang terakhir'."
    return out

def db_goals(uid):
    conn = db(); c = conn.cursor()
    c.execute("SELECT id, name FROM savings_goals WHERE user_id=?", (uid,))
    r = [dict(x) for x in c.fetchall()]; conn.close()
    return r

@app.route("/api/ai/parse", methods=["POST"])
@auth_required
def ai_parse_endpoint():
    d = request.get_json(force=True)
    text = (d or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "text kosong"}), 400
    result = ai_parse(text, request.uid)
    return jsonify(result)



# ============ YNNS AI — LLM CHAT (Groq) + SETTINGS ============
def _get_setting(uid, key, default=None):
    conn = db(); c = conn.cursor()
    try:
        c.execute(f"SELECT {key} FROM user_settings WHERE user_id=?", (uid,))
        r = c.fetchone()
        return r[key] if r and r[key] else default
    except Exception:
        return default
    finally:
        conn.close()

def _save_setting(uid, key, value):
    conn = db(); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (uid,))
    c.execute(f"UPDATE user_settings SET {key}=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (value, uid))
    conn.commit(); conn.close()

def _build_context(uid):
    bal = calc_balance(uid)
    conn = db(); c = conn.cursor()
    c.execute("SELECT id, name, type FROM accounts WHERE user_id=?", (uid,))
    accs = [dict(r) for r in c.fetchall()]
    acc_lines = []
    for a in accs:
        b = calc_balance(uid, a["id"])["balance"]
        acc_lines.append(f"  - id={a['id']} | {a['name']} ({a['type']}) = Rp {int(b):,}".replace(",", "."))
    c.execute("SELECT id, name, target_amount, current_amount FROM savings_goals WHERE user_id=? AND status!='ARCHIVED'", (uid,))
    goals = [dict(r) for r in c.fetchall()]
    goal_lines = [f"  - id={g['id']} | {g['name']} = Rp {int(g['current_amount']):,} / Rp {int(g['target_amount']):,}".replace(",", ".") for g in goals]
    c.execute("SELECT id, name, icon, type FROM categories WHERE user_id IS NULL OR user_id=? ORDER BY type, id", (uid,))
    cats = [dict(r) for r in c.fetchall()]
    cat_lines = [f"  - id={c2['id']} | {c2['name']} ({c2['type']})" for c2 in cats]
    c.execute("SELECT t.id, t.type, t.amount, t.description, t.transaction_date, c.name AS cat FROM transactions t LEFT JOIN categories c ON c.id=t.category_id WHERE t.user_id=? ORDER BY t.id DESC LIMIT 5", (uid,))
    recent = [dict(r) for r in c.fetchall()]
    c.execute("SELECT COALESCE(SUM(current_amount),0) AS s FROM savings_goals WHERE user_id=?", (uid,))
    sav = c.fetchone()["s"]
    conn.close()
    from datetime import datetime as _dt
    today = _dt.utcnow().strftime("%Y-%m-%d")
    ctx = f"""KONTEKS KEUANGAN USER (data REAL dari database, JANGAN ngarang):
Hari ini: {today}

- Total saldo cash: Rp {int(bal['balance']):,}
- Total tabungan: Rp {int(sav):,}
- Total kekayaan: Rp {int(bal['balance']+sav):,}
- Total pemasukan: Rp {int(bal['income']):,}
- Total pengeluaran: Rp {int(bal['expense']):,}

Akun (id | nama | saldo):
{chr(10).join(acc_lines) if acc_lines else "  (belum ada akun)"}

Target tabungan (id | nama | progress):
{chr(10).join(goal_lines) if goal_lines else "  (belum ada)"}

Kategori (id | nama | tipe):
{chr(10).join(cat_lines) if cat_lines else "  (belum ada)"}

5 transaksi terakhir:
{chr(10).join([f"  - id={t['id']} | {t['type']} Rp {int(t['amount']):,} | {t['cat'] or '-'} | {t['transaction_date']}" for t in recent]) if recent else "  (belum ada)"}
""".replace(",", ".")
    return ctx, {"accounts": accs, "goals": goals, "categories": cats, "balance": bal}

def _call_groq(api_key, model, messages):
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 800,
                  "response_format": {"type": "json_object"}},
            timeout=25
        )
        if r.status_code != 200:
            return {"error": f"Groq HTTP {r.status_code}: {r.text[:300]}"}
        data = r.json()
        return {"text": data["choices"][0]["message"]["content"]}
    except Exception as e:
        return {"error": str(e)}

_SYSTEM_PROMPT = """Kamu YNNS AI, asisten keuangan di aplikasi YNNS FINANCE. Ramah, hangat, ngobrol Indonesia santai.

SANGAT PENTING - BACA DENGAN TELITI:
Kalau user minta CATAT/CATET/SIMPAN/TAMBAH/INPUT/HAPUS/PINDAH/TABUNG sesuatu, kamu WAJIB return "action" object. JANGAN cuma balas "oke saya catat" tanpa action.

Kalau user cuma ngobrol/tanya, "action" HARUS null.

CONTOH 1 - Catat pengeluaran:
User: "bensin 50rb"
Output: {"reply": "Oke, catat bensin Rp 50.000?", "action": {"intent": "CREATE_EXPENSE", "params": {"type": "EXPENSE", "amount": 50000, "category_id": <id_bensin_dari_konteks>, "account_id": <id_dompet_utama_dari_konteks>, "description": "Bensin", "transaction_date": "YYYY-MM-DD_hari_ini"}, "requires_confirmation": true}}

CONTOH 2 - Catat pemasukan:
User: "gaji 5jt masuk"
Output: {"reply": "Oke, catat pemasukan gaji Rp 5.000.000?", "action": {"intent": "CREATE_INCOME", "params": {"type": "INCOME", "amount": 5000000, "category_id": <id_gaji>, "account_id": <id_dompet>, "description": "Gaji", "transaction_date": "YYYY-MM-DD"}, "requires_confirmation": true}}

CONTOH 3 - Transfer:
User: "transfer 50rb dari Dompet ke DANA"
Output: {"reply": "Oke, transfer Rp 50.000 dari Dompet ke DANA?", "action": {"intent": "CREATE_TRANSFER", "params": {"source_account_id": <id_dompet>, "destination_account_id": <id_dana>, "amount": 50000, "description": "Via AI", "transfer_date": "YYYY-MM-DD"}, "requires_confirmation": true}}

CONTOH 4 - Nabung:
User: "tabung 100rb buat Studio Kreator"
Output: {"reply": "Oke, tabung Rp 100.000 ke Studio Kreator?", "action": {"intent": "SAVING_DEPOSIT", "params": {"goal_id": <id_studio>, "amount": 100000, "account_id": <id_dompet>, "description": "Via AI", "transaction_date": "YYYY-MM-DD"}, "requires_confirmation": true}}

CONTOH 5 - Cuma ngobrol (action null):
User: "halo apa kabar"
Output: {"reply": "Hai! Kabar baik. Ada yang bisa aku bantu hari ini?", "action": null}

CONTOH 6 - Tanya saldo (action null, jawab dari konteks):
User: "berapa saldo saya"
Output: {"reply": "Saldo cash kamu Rp X, tabungan Rp Y, total Rp Z.", "action": null}

CONTOH 7 - Ambigu (action null, tanya balik):
User: "keluar 500"
Output: {"reply": "Rp 500.000 ini untuk apa? Pemasukan atau pengeluaran? Dan kategori apa?", "action": null}

ATURAN:
1. WAJIB pakai angka REAL dari KONTEKS keuangan user. Jangan ngarang.
2. category_id HARUS dari daftar KATEGORI di konteks. Kalau gak match, pakai kategori "Lainnya" yang sesuai tipe.
3. account_id HARUS dari daftar AKUN di konteks. Default: akun pertama.
4. transaction_date format YYYY-MM-DD, default hari ini dari konteks.
5. Output HARUS JSON valid. Tidak ada teks di luar JSON.
6. JANGAN balas "saya akan catat" tanpa action object.
"""

@app.route("/api/ai/settings", methods=["GET", "POST"])
@auth_required
def ai_settings():
    if request.method == "GET":
        key = _get_setting(request.uid, "groq_api_key", "")
        model = _get_setting(request.uid, "ai_model", "llama-3.3-70b-versatile")
        masked = (key[:8] + "..." + key[-4:]) if key and len(key) > 12 else ("(kosong)" if not key else key)
        return jsonify({"has_key": bool(key), "key_masked": masked, "model": model})
    d = request.get_json(force=True)
    if "groq_api_key" in d:
        _save_setting(request.uid, "groq_api_key", (d.get("groq_api_key") or "").strip())
    if "ai_model" in d:
        _save_setting(request.uid, "ai_model", (d.get("ai_model") or "llama-3.3-70b-versatile").strip())
    return jsonify({"ok": True})

@app.route("/api/ai/chat", methods=["POST"])
@auth_required
def ai_chat():
    d = request.get_json(force=True)
    text = (d or {}).get("text", "").strip()
    history = (d or {}).get("history", [])
    if not text:
        return jsonify({"error": "text kosong"}), 400

    api_key = _get_setting(request.uid, "groq_api_key")
    model = _get_setting(request.uid, "ai_model", "llama-3.3-70b-versatile")

    if not api_key:
        fb = ai_parse(text, request.uid)
        fb["engine"] = "rule"
        fb["note"] = "Belum ada API key Groq. Set di menu AI settings."
        return jsonify(fb)

    ctx, _ = _build_context(request.uid)
    msgs = [{"role": "system", "content": _SYSTEM_PROMPT + "\n\n" + ctx}]
    for h in history[-6:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            msgs.append({"role": h["role"], "content": str(h["content"])[:500]})
    msgs.append({"role": "user", "content": text})

    result = _call_groq(api_key, model, msgs)
    if "error" in result:
        fb = ai_parse(text, request.uid)
        fb["engine"] = "rule-fallback"
        fb["error_hint"] = result["error"]
        return jsonify(fb)

    raw_text = result["text"].strip()

    parsed = None
    try:
        decoder = json.JSONDecoder()
        clean = raw_text
        if clean.startswith("```"):
            clean = re.sub(r'^```(?:json)?\s*', '', clean)
            clean = re.sub(r'\s*```\s*$', '', clean)
        parsed, idx = decoder.raw_decode(clean.lstrip())
    except Exception as e:
        parsed = None

    if parsed is None:
        clean = raw_text
        if "```" in clean:
            m = re.search(r'```(?:json)?\s*(.*?)```', clean, re.DOTALL)
            if m:
                clean = m.group(1).strip()
        start = clean.find("{")
        if start >= 0:
            depth = 0
            in_str = False
            escape = False
            for i in range(start, len(clean)):
                ch = clean[i]
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = clean[start:i+1]
                        try:
                            parsed = json.loads(candidate)
                        except Exception as e:
                            pass
                        break

    if parsed is None:
        m_reply = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_text)
        if m_reply:
            reply_text = m_reply.group(1).replace('\\"', '"').replace('\\n', ' ')
            return jsonify({"engine": "llm", "intent": "CHAT",
                            "reply": reply_text, "summary": reply_text,
                            "requires_confirmation": False})
        return jsonify({"engine": "llm", "intent": "CHAT",
                        "reply": raw_text, "summary": raw_text,
                        "requires_confirmation": False})

    reply = parsed.get("reply") or parsed.get("summary") or "..."
    action = parsed.get("action")

    if isinstance(reply, str) and reply.strip().startswith("{"):
        try:
            inner = json.loads(reply)
            if isinstance(inner, dict):
                reply = inner.get("reply") or inner.get("summary") or reply
                if not action:
                    action = inner.get("action")
        except Exception:
            pass

    if action and isinstance(action, dict) and action.get("intent"):
        return jsonify({"engine": "llm", "intent": action.get("intent"),
                        "params": action.get("params", {}),
                        "reply": reply, "summary": reply,
                        "requires_confirmation": action.get("requires_confirmation", True)})
    return jsonify({"engine": "llm", "intent": "CHAT", "reply": reply,
                    "summary": reply, "requires_confirmation": False})



@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory("static", filename)


@app.route("/manifest.json")
def manifest():
    from flask import Response
    try:
        with open("static/manifest.json") as f:
            content = f.read()
        return Response(content, mimetype="application/manifest+json")
    except Exception:
        return Response("{}", mimetype="application/manifest+json")

if __name__ == "__main__":
    init_db()
    print("[+] YNNS FINANCE jalan di http://127.0.0.1:5000")
    import os as _os
    _port = int(_os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=_port, debug=False)
