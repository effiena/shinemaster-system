import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, redirect, jsonify, session, url_for
import sqlite3
from flask_socketio import SocketIO
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os

app = Flask(__name__)
app.secret_key = "supersecretkey"
socketio = SocketIO(app)

COMPANY_INFO = {
    "name": "SHINEMASTER AUTO",
    "address": "No.68 JALAN PUTRA 1, TAMAN TAN SRI YAACOB, 81300 SKUDAI, JOHOR BAHRU",
    "contact": "018-2096907"
}

TZ = ZoneInfo("Asia/Kuala_Lumpur")

@app.context_processor
def inject_company():
    return dict(company=COMPANY_INFO)

# ================= DATABASE =================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "shine.db")


def now_kul():
    return datetime.now(TZ)


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(cursor, table_name, column_name):
    cursor.execute(f"PRAGMA table_info({table_name})")
    cols = cursor.fetchall()
    return any(col[1] == column_name for col in cols)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Orders table - main sales source
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_plate TEXT,
            contact_number TEXT,
            address TEXT,
            service_type TEXT,
            price REAL,
            payment_method TEXT,
            payment_status TEXT,
            loyalty_status TEXT DEFAULT 'Not Eligible',
            created_at TEXT,
            car_type TEXT,
            invoice_no TEXT,
            invoice_date TEXT,
            reported_date TEXT
        )
    """)

    # Loyalty table
    c.execute("""
        CREATE TABLE IF NOT EXISTS loyalty (
            car_plate TEXT PRIMARY KEY,
            paid_count INTEGER
        )
    """)

    # Services table
    c.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL
        )
    """)

    # Legacy sales table kept for compatibility
    c.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice TEXT,
            car_plate TEXT,
            car_type TEXT,
            service_type TEXT,
            payment_method TEXT,
            price REAL,
            date TEXT,
            time TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_plate TEXT,
            service_type TEXT,
            booking_date TEXT,
            booking_time TEXT,
            contact TEXT,
            status TEXT DEFAULT 'Booked',
            created_at TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item TEXT,
            company TEXT,
            phone TEXT,
            address TEXT,
            purchase_date TEXT,
            quantity INTEGER,
            price REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            password TEXT,
            role TEXT
        )
    """)

    # Safe schema upgrades
    if not column_exists(c, "orders", "invoice_date"):
        c.execute("ALTER TABLE orders ADD COLUMN invoice_date TEXT")
    if not column_exists(c, "orders", "reported_date"):
        c.execute("ALTER TABLE orders ADD COLUMN reported_date TEXT")
    if not column_exists(c, "orders", "created_at"):
        c.execute("ALTER TABLE orders ADD COLUMN created_at TEXT")
    if not column_exists(c, "orders", "invoice_no"):
        c.execute("ALTER TABLE orders ADD COLUMN invoice_no TEXT")
    if not column_exists(c, "orders", "car_type"):
        c.execute("ALTER TABLE orders ADD COLUMN car_type TEXT")
    if not column_exists(c, "orders", "loyalty_status"):
        c.execute("ALTER TABLE orders ADD COLUMN loyalty_status TEXT DEFAULT 'Not Eligible'")

    # Inventory upgrades
    if not column_exists(c, "inventory", "serial_number"):
        c.execute("ALTER TABLE inventory ADD COLUMN serial_number TEXT")
    if not column_exists(c, "inventory", "category"):
        c.execute("ALTER TABLE inventory ADD COLUMN category TEXT")
    if not column_exists(c, "inventory", "unit"):
        c.execute("ALTER TABLE inventory ADD COLUMN unit TEXT")
    if not column_exists(c, "inventory", "last_updated"):
        c.execute("ALTER TABLE inventory ADD COLUMN last_updated TEXT")

    c.execute("""
        CREATE TABLE IF NOT EXISTS inventory_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id INTEGER,
            change INTEGER,
            type TEXT,
            reference TEXT,
            date TEXT
        )
    """)

    conn.commit()
    conn.close()


init_db()


def sync_old_orders_data():
    conn = get_db_connection()
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT id, created_at, invoice_date, reported_date
        FROM orders
    """).fetchall()

    for row in rows:
        updates = {}
        created_at = row["created_at"]

        if not created_at:
            dt = now_kul().strftime("%Y-%m-%d %H:%M:%S")
            updates["created_at"] = dt
            created_at = dt

        if created_at and len(created_at) == 19:
            base_date = created_at[:10]
            if not row["invoice_date"]:
                updates["invoice_date"] = base_date
            if not row["reported_date"]:
                updates["reported_date"] = base_date

        if updates:
            set_clause = ", ".join([f"{k}=?" for k in updates.keys()])
            values = list(updates.values()) + [row["id"]]
            cur.execute(f"UPDATE orders SET {set_clause} WHERE id=?", values)

    conn.commit()
    conn.close()


sync_old_orders_data()

# ================= HELPERS =================

def generate_invoice_no(order_id, dt=None):
    if dt is None:
        dt = now_kul()
    return f"INV{dt.strftime('%Y%m%d')}{order_id:04d}"


def insert_order_record(car_plate, car_type, service_type, payment_method, price,
                        loyalty_status="Not Eligible", contact_number=None,
                        address=None, invoice_date=None, reported_date=None):
    dt = now_kul()
    created_at = dt.strftime("%Y-%m-%d %H:%M:%S")

    if not invoice_date:
        invoice_date = dt.strftime("%Y-%m-%d")
    if not reported_date:
        reported_date = invoice_date

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO orders (
            car_plate, contact_number, address, service_type, price,
            payment_method, payment_status, loyalty_status, created_at,
            car_type, invoice_date, reported_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        car_plate,
        contact_number,
        address,
        service_type,
        price,
        payment_method,
        "Paid",
        loyalty_status,
        created_at,
        car_type,
        invoice_date,
        reported_date
    ))
    conn.commit()

    order_id = cur.lastrowid
    invoice_no = generate_invoice_no(order_id, dt)

    cur.execute("UPDATE orders SET invoice_no=? WHERE id=?", (invoice_no, order_id))
    conn.commit()

    # optional legacy insert for compatibility with old pages
    sale_date = invoice_date
    sale_time = dt.strftime("%H:%M:%S")
    cur.execute("""
        INSERT INTO sales (
            invoice, car_plate, car_type, service_type, payment_method, price, date, time
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        invoice_no, car_plate, car_type, service_type, payment_method, price, sale_date, sale_time
    ))
    conn.commit()
    conn.close()

    return {
        "id": order_id,
        "invoice_no": invoice_no,
        "created_at": created_at,
        "date": invoice_date,
        "time": sale_time,
        "reported_date": reported_date
    }


# ================= HOME =================
@app.route("/home")
def home():
    if "username" not in session:
        return redirect("/login")
    conn = get_db_connection()
    services = conn.execute("SELECT * FROM services ORDER BY name").fetchall()
    conn.close()
    return render_template("new_order.html", services=services)


# ================= LOGIN =================
@app.route("/")
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE username=? AND password=?",
            (username, password)
        ).fetchone()
        conn.close()

        if user:
            session["username"] = username
            session["role"] = user["role"]
            if user["role"] == "admin":
                return redirect("/dashboard")
            return redirect("/pos")

        return "Invalid username or password"

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ================= POS =================
@app.route("/pos", methods=["GET", "POST"])
def pos():
    if "username" not in session:
        return redirect("/login")

    conn = get_db_connection()
    services = conn.execute("SELECT * FROM services ORDER BY name").fetchall()

    if request.method == "POST":
        car_plate = request.form["car_plate"].replace(" ", "").upper()
        car_type = request.form.get("car_type", "-")
        service_type = request.form["service_type"]
        payment_method = request.form["payment_method"]
        price = float(request.form["price"])
        invoice_date = request.form.get("invoice_date") or now_kul().strftime("%Y-%m-%d")
        reported_date = request.form.get("reported_date") or invoice_date

        order = {
            "car_plate": car_plate,
            "car_type": car_type,
            "service_type": service_type,
            "payment_method": payment_method,
            "price": price
        }

        order = process_loyalty(order)

        saved = insert_order_record(
            car_plate=order["car_plate"],
            car_type=order["car_type"],
            service_type=order["service_type"],
            payment_method=order["payment_method"],
            price=order["price"],
            loyalty_status=order["loyalty_status"],
            invoice_date=invoice_date,
            reported_date=reported_date
        )

        order["invoice_no"] = saved["invoice_no"]
        order["date"] = saved["date"]
        order["time"] = saved["time"]
        order["created_at"] = saved["created_at"]
        order["reported_date"] = saved["reported_date"]

        socketio.emit("update_dashboard")
        conn.close()
        return render_template("receipt.html", order=order)

    conn.close()
    return render_template("new_order.html", services=services)


# ================= CREATE ORDER =================
@app.route("/create_order", methods=["POST"])
def create_order():
    if "username" not in session:
        return redirect("/login")

    car_plate = request.form["car_plate"].replace(" ", "").upper()
    car_type = request.form.get("car_type", "-")
    service_type = request.form["service_type"]
    payment_method = request.form["payment_method"]
    price = float(request.form["price"])
    invoice_date = request.form.get("invoice_date") or now_kul().strftime("%Y-%m-%d")
    reported_date = request.form.get("reported_date") or invoice_date

    order = {
        "car_plate": car_plate,
        "car_type": car_type,
        "service_type": service_type,
        "payment_method": payment_method,
        "price": price
    }

    order = process_loyalty(order)

    saved = insert_order_record(
        car_plate=order["car_plate"],
        car_type=order["car_type"],
        service_type=order["service_type"],
        payment_method=order["payment_method"],
        price=order["price"],
        loyalty_status=order["loyalty_status"],
        invoice_date=invoice_date,
        reported_date=reported_date
    )

    order["invoice_no"] = saved["invoice_no"]
    order["id"] = saved["id"]
    order["date"] = saved["date"]
    order["time"] = saved["time"]
    order["created_at"] = saved["created_at"]
    order["reported_date"] = saved["reported_date"]

    socketio.emit("update_dashboard")
    return render_template("receipt.html", order=order)


# ================= LOYALTY =================
def process_loyalty(order):
    car_plate = order["car_plate"]
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT paid_count FROM loyalty WHERE car_plate=?", (car_plate,))
    row = cur.fetchone()
    paid_count = (row["paid_count"] if row else 0) + 1

    if paid_count == 6:
        order["price"] = 0.0
        order["loyalty_free"] = True
        paid_count = 0
    else:
        order["loyalty_free"] = False

    order["loyalty_count"] = paid_count
    order["loyalty_eligible"] = paid_count >= 5
    order["loyalty_status"] = "Eligible" if paid_count >= 5 else "Not Eligible"

    if row:
        cur.execute("UPDATE loyalty SET paid_count=? WHERE car_plate=?", (paid_count, car_plate))
    else:
        cur.execute("INSERT INTO loyalty (car_plate, paid_count) VALUES (?, ?)", (car_plate, paid_count))

    conn.commit()
    conn.close()
    return order


@app.route("/check_loyalty/<car_plate>")
def check_loyalty(car_plate):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT paid_count FROM loyalty WHERE car_plate=?",
        (car_plate.replace(" ", "").upper(),)
    ).fetchone()
    conn.close()
    paid = row["paid_count"] if row else 0
    eligible = paid >= 5
    return {"paid": paid, "eligible": eligible}


# ================= DASHBOARD =================
@app.route("/dashboard")
def dashboard():
    if session.get("role") != "admin":
        return redirect("/pos")

    data = get_revenue_data()
    low_stock = get_low_stock()
    return render_template(
        "dashboard.html",
        today_revenue=data["today_revenue"],
        week_revenue=data["week_revenue"],
        month_revenue=data["month_revenue"],
        cars_today=data["cars_today"],
        recent_sales=data["recent_sales"],
        low_stock=low_stock
    )


@app.route("/dashboard_data")
def dashboard_data():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    return jsonify(get_revenue_data())


def get_revenue_data():
    conn = get_db_connection()
    now = now_kul()
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")

    today_revenue = conn.execute("""
        SELECT IFNULL(SUM(price), 0)
        FROM orders
        WHERE payment_status='Paid' AND DATE(reported_date)=?
    """, (today,)).fetchone()[0]

    week_revenue = conn.execute("""
        SELECT IFNULL(SUM(price), 0)
        FROM orders
        WHERE payment_status='Paid' AND DATE(reported_date) BETWEEN ? AND ?
    """, (week_start, today)).fetchone()[0]

    month_revenue = conn.execute("""
        SELECT IFNULL(SUM(price), 0)
        FROM orders
        WHERE payment_status='Paid' AND strftime('%Y-%m', reported_date)=?
    """, (month_key,)).fetchone()[0]

    cars_today = conn.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE payment_status='Paid' AND DATE(reported_date)=?
    """, (today,)).fetchone()[0]

    recent_sales_raw = conn.execute("""
        SELECT invoice_no, car_plate, service_type, price, created_at, invoice_date, reported_date
        FROM orders
        WHERE payment_status='Paid'
        ORDER BY created_at DESC, id DESC
        LIMIT 10
    """).fetchall()

    recent_sales = []
    for row in recent_sales_raw:
        dt_text = row["created_at"] or ""
        date_part = dt_text[:10] if len(dt_text) >= 10 else "-"
        time_part = dt_text[11:19] if len(dt_text) >= 19 else "-"
        recent_sales.append({
            "invoice": row["invoice_no"] or "-",
            "car_plate": row["car_plate"] or "-",
            "service_type": row["service_type"] or "-",
            "price": row["price"] or 0,
            "date": date_part,
            "time": time_part,
            "invoice_date": row["invoice_date"] or "-",
            "reported_date": row["reported_date"] or "-"
        })

    conn.close()

    return {
        "today_revenue": today_revenue,
        "week_revenue": week_revenue,
        "month_revenue": month_revenue,
        "cars_today": cars_today,
        "recent_sales": recent_sales
    }


@app.route("/recent_sales")
def recent_sales():
    if session.get("role") != "admin":
        return redirect("/pos")

    filter_type = request.args.get("filter_type", "created")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")

    conn = get_db_connection()
    query = """
        SELECT id, invoice_no, car_plate, car_type, service_type, payment_method,
               price, invoice_date, reported_date, created_at
        FROM orders
        WHERE payment_status='Paid'
    """
    params = []

    if date_from and date_to:
        if filter_type == "invoice":
            query += " AND DATE(invoice_date) BETWEEN ? AND ?"
            params.extend([date_from, date_to])
        elif filter_type == "reported":
            query += " AND DATE(reported_date) BETWEEN ? AND ?"
            params.extend([date_from, date_to])
        else:
            query += " AND DATE(created_at) BETWEEN ? AND ?"
            params.extend([date_from, date_to])

    query += " ORDER BY created_at DESC, id DESC"

    sales_rows = conn.execute(query, params).fetchall()
    conn.close()

    return render_template(
        "recent_sales.html",
        sales=sales_rows,
        filter_type=filter_type,
        date_from=date_from,
        date_to=date_to
    )


def get_low_stock():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM inventory WHERE quantity <= 5 ORDER BY quantity ASC, item ASC").fetchall()
    conn.close()
    return [dict(x) for x in rows]


# ================= RECEIPT =================
@app.route("/receipt/<invoice>")
def receipt(invoice):
    if "username" not in session:
        return redirect("/login")

    conn = get_db_connection()

    order = conn.execute("""
        SELECT *
        FROM orders
        WHERE invoice_no=?
        ORDER BY id DESC
        LIMIT 1
    """, (invoice,)).fetchone()

    if order:
        conn.close()
        return render_template("receipt.html", order=order)

    sale = conn.execute("SELECT * FROM sales WHERE invoice=?", (invoice,)).fetchone()
    conn.close()

    if sale:
        return render_template("receipt.html", sale=sale)

    return f"Receipt not found for invoice {invoice}", 404


# ================= PACKAGES =================
@app.route("/packages")
def packages():
    packages = [
        {"name": "Basic Wash", "price": "RM15", "details": ["Exterior hand wash", "Tyre cleaning", "Quick dry"]},
        {"name": "Premium Wash", "price": "RM35", "details": ["Exterior wash", "Interior vacuum", "Dashboard wipe", "Tyre shine"]},
        {"name": "Full Detailing", "price": "RM120", "details": ["Exterior deep wash", "Interior detailing", "Seat cleaning", "Wax protection"]}
    ]
    return render_template("packages.html", packages=packages)

# ================= INVENTORY =================
@app.route("/inventory")
def inventory():
    if "username" not in session:
        return redirect("/login")

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    # Fetch filter parameters
    filter_type = request.args.get("filter_type", "all")
    query = "SELECT * FROM inventory"
    params = []

    if filter_type == "month":
        month = request.args.get("filter_month")
        if month:
            query += " WHERE strftime('%Y-%m', purchase_date)=?"
            params.append(month)
    elif filter_type == "year":
        year = request.args.get("filter_year")
        if year:
            query += " WHERE strftime('%Y', purchase_date)=?"
            params.append(year)
    elif filter_type == "custom":
        start = request.args.get("filter_start")
        end = request.args.get("filter_end")
        if start and end:
            query += " WHERE purchase_date BETWEEN ? AND ?"
            params.extend([start, end])

    query += " ORDER BY purchase_date DESC"

    items = conn.execute(query, params).fetchall()

    # Total spent
    total_spent = sum([row["price"] for row in items])

    conn.close()
    return render_template("inventory.html", items=items, total_spent=total_spent)

@app.route("/add_inventory", methods=["POST"])
def add_inventory():
    if session.get("role") != "admin":
        return "Admin only"

    item = request.form["item"]
    company = request.form["company"]
    phone = request.form["phone"]
    address = request.form["address"]
    purchase_date = request.form["purchase_date"]
    quantity = request.form["quantity"]
    price = request.form["price"]
    category = request.form.get("category", "")
    unit = request.form.get("unit", "")
    serial_number = request.form.get("serial_number", "")
    last_updated = now_kul().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db_connection()
    conn.execute("""
        INSERT INTO inventory
        (item, company, phone, address, purchase_date, quantity, price, category, unit, serial_number, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        item, company, phone, address, purchase_date, quantity, price,
        category, unit, serial_number, last_updated
    ))
    conn.commit()
    conn.close()

    return redirect("/inventory")

@app.route("/edit_inventory/<int:id>", methods=["GET", "POST"])
def edit_inventory(id):
    if session.get("role") != "admin":
        return "Admin only"

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    # Fetch the item
    item = conn.execute("SELECT * FROM inventory WHERE id=?", (id,)).fetchone()
    if not item:
        conn.close()
        return "Item not found", 404

    if request.method == "POST":
        try:
            item_name = request.form.get("item", "").strip()
            company = request.form.get("company", "").strip()
            phone = request.form.get("phone", "").strip()
            address = request.form.get("address", "").strip()
            purchase_date = request.form.get("purchase_date", "").strip()

            quantity = request.form.get("quantity", "0").strip()
            quantity = int(quantity) if quantity else 0

            price = request.form.get("price", "0").strip()
            price = float(price) if price else 0.0

            serial_number = request.form.get("serial_number", "").strip()

            # Update database
            conn.execute("""
                UPDATE inventory
                SET item=?, company=?, phone=?, address=?, purchase_date=?,
                    quantity=?, price=?, serial_number=?, last_updated=CURRENT_TIMESTAMP
                WHERE id=?
            """, (item_name, company, phone, address, purchase_date,
                  quantity, price, serial_number, id))
            conn.commit()
        except Exception as e:
            conn.close()
            return f"Error updating item: {e}", 500

        conn.close()
        return redirect("/inventory")

    conn.close()
    return render_template("edit_inventory.html", item=item)

@app.route("/delete_inventory/<int:id>")
def delete_inventory(id):
    if session.get("role") != "admin":
        return "Admin only"

    conn = get_db_connection()
    conn.execute("DELETE FROM inventory WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return redirect("/inventory")


# ================= BOOKING =================
def generate_timeslots():
    slots = []
    start = 9
    end = 22
    for hour in range(start, end):
        slots.append(f"{hour:02d}:00")
        slots.append(f"{hour:02d}:30")
    return slots


@app.route("/booking")
def booking():
    plate = request.args.get("plate", "")

    conn = get_db_connection()
    services = conn.execute("SELECT * FROM services ORDER BY name").fetchall()
    conn.close()

    timeslots = generate_timeslots()
    current_date = now_kul().strftime("%Y-%m-%d")

    return render_template(
        "booking.html",
        services=services,
        timeslots=timeslots,
        plate=plate,
        current_date=current_date
    )


@app.route("/create_booking", methods=["POST"])
def create_booking():
    car_plate = request.form["car_plate"].upper()
    service = request.form["service_type"]
    date = request.form["booking_date"]
    time = request.form["booking_time"]
    contact = request.form["contact"]
    car_type = request.form.get("car_type", "-")
    created_at = now_kul().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db_connection()

    existing = conn.execute("""
        SELECT COUNT(*) FROM bookings
        WHERE booking_date=? AND booking_time=?
    """, (date, time)).fetchone()[0]

    if existing >= 3:
        conn.close()
        return "This time slot is full. Please choose another."

    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bookings
        (car_plate, service_type, booking_date, booking_time, contact, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (car_plate, service, date, time, contact, created_at))
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()

    booking_data = {
        "car_plate": car_plate,
        "service": service,
        "car_type": car_type,
        "date": date,
        "time": time,
        "contact": contact,
        "booking_id": f"BK{now_kul().strftime('%Y%m%d')}{booking_id:03d}"
    }

    return render_template("booking_confirmed.html", booking=booking_data)


@app.route("/booking_admin")
def booking_admin():
    if session.get("role") != "admin":
        return redirect("/pos")

    conn = get_db_connection()
    bookings = conn.execute("""
        SELECT * FROM bookings
        ORDER BY booking_date ASC, booking_time ASC, id ASC
    """).fetchall()
    conn.close()

    return render_template("booking_admin.html", bookings=bookings)


# ================= STAFF =================
@app.route("/staff")
def staff():
    if session.get("role") != "admin":
        return redirect("/pos")

    conn = get_db_connection()
    staff = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
    conn.close()

    return render_template("staff.html", staff=staff)


@app.route("/add_staff", methods=["POST"])
def add_staff():
    if session.get("role") != "admin":
        return "Admin only"

    username = request.form["username"]
    password = request.form["password"]
    role = request.form["role"]

    conn = get_db_connection()
    conn.execute("""
        INSERT INTO users (username, password, role)
        VALUES (?, ?, ?)
    """, (username, password, role))
    conn.commit()
    conn.close()

    return redirect("/staff")


# ================= FINANCE =================
@app.route("/finance")
def finance():
    if session.get("role") != "admin":
        return "Admin only"

    conn = get_db_connection()
    today = now_kul().strftime("%Y-%m-%d")

    daily_orders = conn.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE payment_status='Paid' AND reported_date=?
    """, (today,)).fetchone()[0]

    daily_revenue = conn.execute("""
        SELECT IFNULL(SUM(price), 0)
        FROM orders
        WHERE payment_status='Paid' AND reported_date=?
    """, (today,)).fetchone()[0]

    payment_methods = ["Cash", "Card", "QR", "E-Wallet"]
    by_method = []

    for method in payment_methods:
        total = conn.execute("""
            SELECT IFNULL(SUM(price), 0)
            FROM orders
            WHERE payment_status='Paid' AND reported_date=? AND payment_method=?
        """, (today, method)).fetchone()[0]
        by_method.append((method, total))

    total_revenue = sum([x[1] for x in by_method])
    conn.close()

    report = {
        "report_date": today,
        "daily_orders": daily_orders,
        "daily_revenue": daily_revenue,
        "by_method": by_method,
        "total_revenue": total_revenue
    }

    return render_template("finance.html", report=report)


# ================= RUN =================
if __name__ == "__main__":
    socketio.run(app, debug=True)
