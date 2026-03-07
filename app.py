from flask import Flask, render_template, request, redirect, jsonify, session, url_for
import sqlite3
from flask_socketio import SocketIO, emit
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = "supersecretkey"
socketio = SocketIO(app)

# ===== DATABASE CONNECTIONS =====
def get_system_db_connection():
    conn = sqlite3.connect('shine.db')
    conn.row_factory = sqlite3.Row
    return conn

def get_db_connection():
    # Adjust path relative to shine-system folder
    conn = sqlite3.connect('shine.db')
    conn.row_factory = sqlite3.Row
    return conn
# -----------------------------
# Initialize Database
# -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "shine.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Orders table
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            car_type TEXT,
            invoice_no TEXT
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

    conn.commit()
    conn.close()

# run database initialization
init_db()

#home after payment
@app.route("/home")
def home():
    return render_template("new_order.html")

#login route
@app.route("/")
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = sqlite3.connect("shine.db")
        user = conn.execute(
            "SELECT * FROM users WHERE username=? AND password=?",
            (username, password)
        ).fetchone()
        conn.close()

        if user:
            role = user[3]  # assuming 4th column is role

            # <-- SET SESSION HERE
            session["username"] = username
            session["role"] = role

            if role == "admin":
                return redirect("/dashboard")
            elif role == "cashier":
                return redirect("/pos")
        else:
            return "Invalid username or password"

    return render_template("login.html")

def get_latest_sales():
    conn = sqlite3.connect('shinemaster.db')
    conn.row_factory = sqlite3.Row
    sales = conn.execute("SELECT * FROM sales ORDER BY date DESC, time DESC").fetchall()
    conn.close()
    return sales

# POS page
@app.route("/pos", methods=["GET", "POST"])
def pos():
    if "username" not in session:
        return redirect("/login")

    conn = get_db_connection()
    services = conn.execute("SELECT * FROM services").fetchall()

    if request.method == "POST":
        invoice = f"INV{datetime.now().strftime('%Y%m%d%H%M%S')}"
        car_plate = request.form["car_plate"]
        car_type = request.form["car_type"]
        service_type = request.form["service_type"]
        payment_method = request.form["payment_method"]
        price = float(request.form["price"])
        date = datetime.now().strftime("%Y-%m-%d")
        time = datetime.now().strftime("%H:%M:%S")

        conn.execute("""
            INSERT INTO sales (invoice, car_plate, car_type, service_type, payment_method, price, date, time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (invoice, car_plate, car_type, service_type, payment_method, price, date, time))

        conn.commit()
        conn.close()

        socketio.emit("update_dashboard")
        return redirect(f"/receipt/{invoice}")

    return render_template("new_order.html", services=services)
# -----------------------------
# Loyalty Logic
# -----------------------------
def process_loyalty(order):
    car_plate = order["car_plate"].replace(" ", "").upper()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT paid_count FROM loyalty WHERE car_plate=?", (car_plate,))
    row = cur.fetchone()

    if row:
        paid_count = row[0] + 1
    else:
        paid_count = 1

    # Free wash on 6th visit
    if paid_count == 6:
        order["price"] = 0.0
        order["loyalty_free"] = 1
        paid_count = 0
    else:
        order["loyalty_free"] = 0

    order["loyalty_count"] = paid_count
    order["loyalty_status"] = "Eligible" if paid_count >= 5 else "Not Eligible"
    order["loyalty_eligible"] = paid_count >= 5

    if row:
        cur.execute("UPDATE loyalty SET paid_count=? WHERE car_plate=?", (paid_count, car_plate))
    else:
        cur.execute("INSERT INTO loyalty (car_plate, paid_count) VALUES (?, ?)", (car_plate, paid_count))

    conn.commit()
    conn.close()

    return order

@app.route("/create_order", methods=["POST"])
def create_order():
    car_plate = request.form["car_plate"].upper()
    car_type = request.form.get("car_type", "-")
    service_type = request.form["service_type"]
    price = float(request.form["price"])
    payment_method = request.form["payment_method"]

    order = {
        "car_plate": car_plate,
        "car_type": car_type,
        "service_type": service_type,
        "price": price,
        "payment_method": payment_method,
        "loyalty_status": "Not Eligible"
    }

    order = process_loyalty(order)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Insert order
    cur.execute("""
        INSERT INTO orders
        (car_plate, car_type, service_type, price, payment_method, payment_status, loyalty_status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        order["car_plate"],
        order["car_type"],
        order["service_type"],
        order["price"],
        order["payment_method"],
        "Paid",
        order["loyalty_status"]
    ))

    conn.commit()
    order_id = cur.lastrowid

    # Generate invoice
    today = datetime.now().strftime("%Y%m%d")
    invoice_no = f"INV{today}{order_id:04d}"

    cur.execute("UPDATE orders SET invoice_no=? WHERE id=?", (invoice_no, order_id))
    conn.commit()
    conn.close()

    order["invoice_no"] = invoice_no
    order["id"] = order_id

    return render_template("receipt.html", order=order)

@app.route("/check_loyalty/<car_plate>")
def check_loyalty(car_plate):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT paid_count FROM loyalty WHERE car_plate=?", (car_plate.upper(),))
    row = cur.fetchone()
    conn.close()

    paid = row[0] if row else 0
    eligible = paid >= 5

    return {"paid": paid, "eligible": eligible}

# Dashboard
@app.route("/dashboard")
def dashboard():

    if session.get("role") != "admin":
        return redirect("/pos")

    conn = sqlite3.connect("shine.db")

    today = datetime.now().strftime("%Y-%m-%d")

    today_sales = conn.execute(
    "SELECT SUM(price) FROM sales WHERE date=?",
    (today,)).fetchone()[0]

    cars_today = conn.execute(
    "SELECT COUNT(*) FROM sales WHERE date=?",
    (today,)).fetchone()[0]

    conn.close()

    return render_template(
        "dashboard.html",
        today_sales=today_sales or 0,
        cars_today=cars_today
    )

@app.route("/dashboard_data")
def dashboard_data():

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    today = datetime.now().strftime("%Y-%m-%d")

    today_revenue = conn.execute(
        "SELECT SUM(price) FROM sales WHERE date=?",(today,)
    ).fetchone()[0] or 0

    cars_today = conn.execute(
        "SELECT COUNT(*) FROM sales WHERE date=?",(today,)
    ).fetchone()[0]

    recent_sales = conn.execute("""
        SELECT invoice, car_plate, service_type, price, time
        FROM sales
        ORDER BY id DESC
        LIMIT 5
    """).fetchall()

    conn.close()

    return jsonify({
        "today_revenue": today_revenue,
        "cars_today": cars_today,
        "recent_sales":[dict(x) for x in recent_sales]
    })

@app.route("/receipt/<invoice>")
def receipt(invoice):

    conn = sqlite3.connect("shine.db")

    sale = conn.execute(
    "SELECT * FROM sales WHERE invoice=?",
    (invoice,)).fetchone()

    conn.close()

    return render_template("receipt.html",sale=sale)


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

    conn = sqlite3.connect("shine.db")

    conn.execute("""
    INSERT INTO inventory
    (item,company,phone,address,purchase_date,quantity,price)
    VALUES (?,?,?,?,?,?,?)
    """,(item,company,phone,address,purchase_date,quantity,price))

    conn.commit()
    conn.close()

    return redirect("/inventory")

@app.route('/inventory')
def inventory():
    if "username" not in session:
        return redirect("/")  # login required

    # Fetch all inventory items
    conn = sqlite3.connect('shine.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM inventory")
    items = cursor.fetchall()
    conn.close()

    # Render template with items
    return render_template("inventory.html", items=items)

@app.route("/delete_inventory/<int:id>")
def delete_inventory(id):

    if session.get("role") != "admin":
        return "Admin only"

    conn = sqlite3.connect("shine.db")

    conn.execute("DELETE FROM inventory WHERE id=?", (id,))

    conn.commit()
    conn.close()

    return redirect("/inventory")


@app.route("/staff")
def staff():

    if session.get("role") != "admin":
        return redirect("/pos")

    conn = sqlite3.connect("shine.db")

    staff = conn.execute("SELECT * FROM staff").fetchall()

    conn.close()

    return render_template("staff.html",staff=staff)

@app.route("/add_staff", methods=["POST"])
def add_staff():

    if session.get("role") != "admin":
        return "Admin only"

    username = request.form["username"]
    password = request.form["password"]
    role = request.form["role"]

    conn = sqlite3.connect("shine.db")

    conn.execute("""
    INSERT INTO users (username,password,role)
    VALUES (?,?,?)
    """,(username,password,role))

    conn.commit()
    conn.close()

    return redirect("/staff")

@app.route("/finance")
def finance():

    if session.get("role") != "admin":
        return "Admin only"

    conn = sqlite3.connect("shine.db")

    today = conn.execute(
    "SELECT SUM(price) FROM sales WHERE date=date('now')"
    ).fetchone()[0]

    week = conn.execute(
    "SELECT SUM(price) FROM sales WHERE date >= date('now','-7 day')"
    ).fetchone()[0]

    month = conn.execute(
    "SELECT SUM(price) FROM sales WHERE date >= date('now','-30 day')"
    ).fetchone()[0]

    conn.close()

    return render_template("finance.html",
    today=today or 0,
    week=week or 0,
    month=month or 0)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

if __name__ == "__main__":
    socketio.run(app, debug=True)
