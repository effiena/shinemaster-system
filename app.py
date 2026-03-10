import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, redirect, jsonify, session, url_for
import sqlite3
from flask_socketio import SocketIO, emit
from datetime import datetime
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

@app.context_processor
def inject_company():
    return dict(company=COMPANY_INFO)

# ===== DATABASE CONNECTIONS =====
def get_system_db_connection():
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    return conn

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

        conn = get_db_connection()
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sales = conn.execute("SELECT * FROM sales ORDER BY date DESC, time DESC").fetchall()
    conn.close()
    return sales

# POS page

@app.route("/pos", methods=["GET", "POST"])
def pos():
    if "username" not in session:
        return redirect("/login")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    services = conn.execute("SELECT * FROM services").fetchall()

    if request.method == "POST":
        # Get current date and time
        now = datetime.now(ZoneInfo("Asia/Kuala_Lumpur"))
        date = now.strftime("%Y-%m-%d")
        time = now.strftime("%H:%M:%S")

        invoice = f"INV{now.strftime('%Y%m%d%H%M%S')}"

        car_plate = request.form["car_plate"].replace(" ", "").upper()
        car_type = request.form["car_type"]
        service_type = request.form["service_type"]
        payment_method = request.form["payment_method"]
        price = float(request.form["price"])

        # Create order dict
        order = {
            "car_plate": car_plate,
            "car_type": car_type,
            "service_type": service_type,
            "payment_method": payment_method,
            "price": price
        }

        # Apply loyalty system
        order = process_loyalty(order)

        # Insert into sales table
        conn.execute("""
            INSERT INTO sales
            (invoice, car_plate, car_type, service_type, payment_method, price, date, time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            invoice,
            order["car_plate"],
            order["car_type"],
            order["service_type"],
            order["payment_method"],
            order["price"],
            date,
            time
        ))
        conn.commit()
        conn.close()

        # Add extra info for receipt
        order["invoice_no"] = invoice
        order["date"] = date
        order["time"] = time

        socketio.emit("update_dashboard")

        return render_template("receipt.html", order=order)

    conn.close()
    return render_template("new_order.html", services=services)

# -----------------------------
# Loyalty Logic
# -----------------------------
def process_loyalty(order):
    car_plate = order["car_plate"].replace(" ", "").upper()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Get current paid count
    cur.execute("SELECT paid_count FROM loyalty WHERE car_plate=?", (car_plate,))
    row = cur.fetchone()
    paid_count = (row[0] if row else 0) + 1

    # Free wash on 6th visit
    if paid_count == 6:
        order["price"] = 0.0
        order["loyalty_free"] = True
        paid_count = 0
    else:
        order["loyalty_free"] = False

    order["loyalty_count"] = paid_count
    order["loyalty_eligible"] = paid_count >= 5
    order["loyalty_status"] = "Eligible" if paid_count >= 5 else "Not Eligible"

    # Safe upsert
    cur.execute("""
        INSERT INTO loyalty (car_plate, paid_count)
        VALUES (?, ?)
        ON CONFLICT(car_plate) DO UPDATE SET paid_count=excluded.paid_count
    """, (car_plate, paid_count))

    conn.commit()
    conn.close()
    return order

# -----------------------------
# Create Order Route
# -----------------------------
@app.route("/create_order", methods=["POST"])
def create_order():
    now = datetime.now(ZoneInfo("Asia/Kuala_Lumpur"))
    date = now.strftime("%Y-%m-%d")
    time = now.strftime("%H:%M:%S")

    car_plate = request.form["car_plate"].replace(" ", "").upper()
    car_type = request.form.get("car_type", "-")
    service_type = request.form["service_type"]
    payment_method = request.form["payment_method"]
    price = float(request.form["price"])

    # 1️⃣ Create base order dict
    order = {
        "car_plate": car_plate,
        "car_type": car_type,
        "service_type": service_type,
        "payment_method": payment_method,
        "price": price
    }

    # 2️⃣ Apply loyalty logic safely
    order = process_loyalty(order)

    # 3️⃣ Insert into orders table
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

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

    # 4️⃣ Generate invoice number
    invoice_no = f"INV{now.strftime('%Y%m%d')}{order_id:04d}"
    cur.execute("UPDATE orders SET invoice_no=? WHERE id=?", (invoice_no, order_id))
    conn.commit()
    conn.close()

    # 5️⃣ Add extra info for receipt
    order["invoice_no"] = invoice_no
    order["id"] = order_id
    order["date"] = date
    order["time"] = time

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

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    today = datetime.now().strftime("%Y-%m-%d")

    today_revenue = conn.execute("""
        SELECT IFNULL(SUM(total_amount),0)
        FROM invoices
        WHERE DATE(created_at) = ?
    """, (today,)).fetchone()[0]

    

    cars_today = conn.execute(
        "SELECT COUNT(*) FROM sales WHERE date=?",
        (today,)
    ).fetchone()[0]

    week_revenue = conn.execute("""
        SELECT IFNULL(SUM(total_amount),0)
        FROM invoices
        WHERE strftime('%W', created_at) = strftime('%W','now')
        """).fetchone()[0]

    month_revenue = conn.execute("""
        SELECT IFNULL(SUM(total_amount),0)
        FROM invoices
        WHERE strftime('%m', created_at) = strftime('%m','now')
        """).fetchone()[0]
    
    recent_sales = conn.execute("""
        SELECT invoice, car_plate, service_type, price, time
        FROM sales
        ORDER BY id DESC
        LIMIT 5
    """).fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        today_revenue=today_revenue,
        week_revenue=week_revenue,
        month_revenue=month_revenue
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

    conn = get_db_connection()
    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    time = now.strftime("%H:%M:%S")
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

    conn = get_db_connection()

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
    conn = get_db_connection()
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

    conn = get_db_connection()

    conn.execute("DELETE FROM inventory WHERE id=?", (id,))

    conn.commit()
    conn.close()

    return redirect("/inventory")


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

    plate = request.args.get("plate","")

    conn = get_db_connection()
    services = conn.execute("SELECT * FROM services").fetchall()
    conn.close()


    timeslots = generate_timeslots()
    
    # ✅ Define current_date so the form can set min date
    current_date = datetime.now().strftime("%Y-%m-%d")
    
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

    conn = get_db_connection()

    # check if slot already taken
    existing = conn.execute("""
        SELECT COUNT(*) FROM bookings
        WHERE booking_date=? AND booking_time=?
    """,(date,time)).fetchone()[0]

    if existing >= 3:   # limit 3 cars per slot
        conn.close()
        return "This time slot is full. Please choose another."

    # insert booking
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bookings
        (car_plate, service_type, booking_date, booking_time, contact)
        VALUES (?,?,?,?,?)
    """,(car_plate, service, date, time, contact))
    booking_id = cur.lastrowid  # get auto-increment ID
    conn.commit()
    conn.close()

    # ✅ Prepare booking info dictionary
    booking_data = {
        "car_plate": car_plate,
        "service": service,
        "car_type": car_type,
        "date": date,
        "time": time,
        "contact": contact,
        "booking_id": f"BK{datetime.now().strftime('%Y%m%d')}{booking_id:03d}"
    }

    # Render the booking confirmed page
    return render_template("booking_confirmed.html", booking=booking_data)

@app.route("/booking_admin")
def booking_admin():

    if session.get("role") != "admin":
        return redirect("/pos")

    conn = get_db_connection()
    bookings = conn.execute("SELECT * FROM bookings ORDER BY booking_date, booking_time").fetchall()
    conn.close()

    return render_template("booking_admin.html", bookings=bookings)


@app.route("/staff")
def staff():

    if session.get("role") != "admin":
        return redirect("/pos")

    conn = get_db_connection()

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

    conn = get_db_connection()

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

    conn = sqlite3.connect('shine.db')
    conn.row_factory = sqlite3.Row

    today = datetime.now().strftime("%Y-%m-%d")

    # Daily summary
    daily_orders = conn.execute(
        "SELECT COUNT(*) FROM sales WHERE date=?", (today,)
    ).fetchone()[0]

    daily_revenue = conn.execute(
        "SELECT SUM(price) FROM sales WHERE date=?", (today,)
    ).fetchone()[0] or 0.0


    by_method = conn.execute(
        """
        SELECT payment_method, SUM(price) as total
        FROM sales
        WHERE date=?
        GROUP BY payment_method
        """, (today,)
    ).fetchall()

    # Payment method breakdown
    payment_methods = ["Cash", "Card", "QR", "E-Wallet"]  # list all expected
    by_method = []

    for method in payment_methods:
        total = conn.execute(
            "SELECT SUM(price) FROM sales WHERE date=? AND payment_method=?",
            (today, method)
        ).fetchone()[0] or 0.0
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


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

if __name__ == "__main__":
    socketio.run(app, debug=True)
