from flask import Flask, render_template, request, redirect, jsonify, session, url_for, send_file
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, date
import calendar
from collections import defaultdict
from zoneinfo import ZoneInfo
import qrcode
from flask_socketio import SocketIO, emit, join_room
from io import BytesIO
import os
import logging

logging.getLogger('engineio').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)

app = Flask(__name__)
app.secret_key = "supersecretkey"
socketio = SocketIO(app, cors_allowed_origins="*")

COMPANY_INFO = {
    "name": "SHINEMASTER AUTO",
    "address": "No.68 JALAN PUTRA 1, TAMAN TAN SRI YAACOB, 81300 SKUDAI, JOHOR BAHRU",
    "contact": "018-2096907"
}

TZ = ZoneInfo("Asia/Kuala_Lumpur")
INVOICE_DB = {}
INVOICE_COUNTER = 1

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
    return any(col[1] == column_name for col in cursor.fetchall())

# ================= DATABASE INIT =================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # ===== ORDERS =====
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

    # ===== LOYALTY =====
    c.execute("""
        CREATE TABLE IF NOT EXISTS loyalty (
            car_plate TEXT PRIMARY KEY,
            paid_count INTEGER
        )
    """)

    # ===== SERVICES =====
    c.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL
        )
    """)

    # ===== SALES (legacy) =====
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

    # ===== BOOKINGS =====
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

    # ===== INVENTORY =====
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

    # ===== USERS =====
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            password TEXT,
            role TEXT
        )
    """)

    # ===== INVENTORY LOG =====
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

    # ===== SAFE COLUMN UPGRADES =====
    existing_columns = [row[1] for row in c.execute("PRAGMA table_info(inventory)").fetchall()]
    if "serial_number" not in existing_columns:
        c.execute("ALTER TABLE inventory ADD COLUMN serial_number TEXT")
    if "category" not in existing_columns:
        c.execute("ALTER TABLE inventory ADD COLUMN category TEXT")
    if "unit" not in existing_columns:
        c.execute("ALTER TABLE inventory ADD COLUMN unit TEXT")
    if "last_updated" not in existing_columns:
        c.execute("ALTER TABLE inventory ADD COLUMN last_updated TEXT")
    if "is_deleted" not in existing_columns:
        c.execute("ALTER TABLE inventory ADD COLUMN is_deleted INTEGER DEFAULT 0")

    # Add retail_orders and retail_order_items tables
    c.execute("""
        CREATE TABLE IF NOT EXISTS retail_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_no TEXT,
            date TEXT,
            payment_method TEXT,
            total REAL,
            paid REAL,
            change REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS retail_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            item_name TEXT,
            quantity INTEGER,
            subtotal REAL,
            FOREIGN KEY (order_id) REFERENCES retail_orders (id)
        )
    """)
    
    # Add receipts table
    c.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_plate TEXT,
            car_type TEXT,
            service_type TEXT,
            price REAL,
            payment_method TEXT,
            receipt_type TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()

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

# ================= HELPERS =================

def insert_order_record(
    car_plate, car_type, service_type, payment_method,
    price, paid_amount=0, loyalty_status="Not Eligible",
    contact_number=None, address=None, invoice_date=None, reported_date=None
):
    dt = now_kul()
    created_at = dt.strftime("%Y-%m-%d %H:%M:%S")

    # ensure numeric
    price = float(price)
    paid_amount = float(paid_amount)
    balance = price - paid_amount
    payment_status = "Paid" if balance <= 0 else "Partial"

    if not invoice_date:
        invoice_date = dt.strftime("%Y-%m-%d")
    if not reported_date:
        reported_date = invoice_date

    conn = get_db_connection()
    cur = conn.cursor()

    # INSERT into orders including paid_amount, balance, payment_status
    cur.execute("""
        INSERT INTO orders (
            car_plate, contact_number, address, service_type, price, paid_amount,
            balance, payment_method, payment_status, loyalty_status, created_at, car_type,
            invoice_date, reported_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        car_plate, contact_number, address, service_type, price, paid_amount,
        balance, payment_method, payment_status, loyalty_status, created_at, car_type,
        invoice_date, reported_date
    ))
    conn.commit()
    order_id = cur.lastrowid
    invoice_no = generate_invoice_no(order_id, dt)

    cur.execute("UPDATE orders SET invoice_no=? WHERE id=?", (invoice_no, order_id))
    conn.commit()

    # optional legacy insert for old pages
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
        "reported_date": reported_date,
        "paid_amount": paid_amount,
        "balance": balance,
        "payment_status": payment_status
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
            "SELECT * FROM users WHERE username=? AND password=?", (username, password)
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

@app.route("/voucher")
def voucher():
    invoice = request.args.get("invoice", "-")
    car_plate = request.args.get("car_plate", "-")
    service = request.args.get("service", "-")
    amount = request.args.get("amount", "0")
    date = request.args.get("date", "")
    time = request.args.get("time", "")

    return render_template(
        "voucher.html",
        invoice=invoice,
        car_plate=car_plate,
        service=service,
        amount=amount,
        date=date,
        time=time
    )

@app.route("/voucher/<car_plate>")
def voucher_by_car(car_plate):

    conn = get_db_connection()

    order = conn.execute("""
        SELECT * FROM orders
        WHERE car_plate = ?
        ORDER BY id DESC
        LIMIT 1
    """, (car_plate,)).fetchone()

    conn.close()

    if not order:
        return "Order not found"

    return render_template(
        "voucher.html",
        invoice=order["invoice_no"],
        car_plate=order["car_plate"],
        service=order["service_type"],
        amount=8,   # manual discount RM8
        date=order["created_at"][:10],
        time=order["created_at"][11:19]
    )

@app.route("/counter")
def counter():
    return render_template("counter.html")
# ========= LAUNCH PAGE =============
@app.route('/launch_page')
def launch_page():
    return render_template('launch_page.html')

@app.route('/members')
def members():
    return render_template('members.html')

@app.route('/coating')
def coating():
    return render_template('coating.html')

@app.route('/dispo')
def dispo():
    return render_template('dispo.html')
# ==== QR-CODE ======
@app.route('/qr_booking')
def qr_booking():
    booking_url = "[shinemaster-system-production.up.railway.app](https://shinemaster-system-production.up.railway.app/booking)" # Replace with your actual deployed URL if different
    qr = qrcode.QRCode(
        version=1,
        box_size=10,
        border=4
    )
    qr.add_data(booking_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png")

@app.route('/web')
def web():
    return render_template("web.html")

# ================= POS =================
# ================= POS RETAIL =================
@app.route("/pos_retail", methods=["GET","POST"])
def pos_retail():
    if request.method == "GET":
        return render_template("pos_retail.html", order=None)

    data = request.get_json()
    cart = data.get("cart", [])
    payment_method = data.get("payment_method", "cash")
    paid = float(data.get("paid", 0))
    total = sum(float(item["subtotal"]) for item in cart)
    change = max(0, paid - total)

    # Save order to DB
    conn = get_db_connection()
    cur = conn.cursor()
    invoice_no = int(datetime.now().timestamp()) # Using timestamp for a simple retail invoice
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        "INSERT INTO retail_orders (invoice_no, date, payment_method, total, paid, change) VALUES (?,?,?,?,?,?)",
        (invoice_no, now_str, payment_method, total, paid, change)
    )
    order_id = cur.lastrowid

    for item in cart:
        cur.execute(
            "INSERT INTO retail_order_items (order_id, item_name, quantity, subtotal) VALUES (?,?,?,?)",
            (order_id, item["item"], item["qty"], item["subtotal"])
        )
    conn.commit()
    conn.close()

    # Generate QR code if E-Wallet
    qr_url = None
    if payment_method == "ewallet":
        qr_data = f"Pay RM {total} via E-Wallet"
        qr_url = f"[api.qrserver.com](https://api.qrserver.com/v1/create-qr-code/?size=150x150&data={urllib.parse.quote(qr_data)})"

    order_data = {
        "invoice_no": invoice_no,
        "date": now_str,
        "items": cart,
        "total": total,
        "paid": paid,
        "change": change,
        "payment_method": payment_method,
        "qr_url": qr_url
    }
    return render_template("receipt_retail.html", order=order_data)

#===========post_test========
def save_receipt_to_db(car_plate, car_type, service_type, price, payment_method, receipt_type):
    conn = get_db_connection() # Use the standardized get_db_connection
    cursor = conn.cursor()

    # Make sure table exists (already done in init_db, but good to ensure if run standalone)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            car_plate TEXT,
            car_type TEXT,
            service_type TEXT,
            price REAL,
            payment_method TEXT,
            receipt_type TEXT,
            created_at TEXT
        )
    """)

    # Insert receipt
    cursor.execute("""
        INSERT INTO receipts (car_plate, car_type, service_type, price, payment_method, receipt_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (car_plate, car_type, service_type, price, payment_method, receipt_type, datetime.now().isoformat()))
    conn.commit()
    receipt_id = cursor.lastrowid
    conn.close()
    return receipt_id
#####===================#########

####=====POS ROUTES======#####

# ===== POS ROUTES =====
@app.route('/pos', methods=['GET', 'POST'])
def pos():
    if request.method == 'POST':

        # ===== Gather form data =====
        car_plate = request.form['car_plate']
        car_type = request.form['car_type']
        service_type = request.form['service_type']
        price = float(request.form['price'])
        payment_method = request.form['payment_method']
        receipt_type = request.form.get("receipt_type", "ORIGINAL")
        discount = float(request.form.get('discount', 0))

        # ONLY CASH uses input amount
        paid_amount = float(request.form.get("paid_amount", price))

        # ===== Time setup =====
        now = datetime.now(TZ)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        # ===== Loyalty logic =====
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT paid_count FROM loyalty WHERE car_plate=?", (car_plate,))
        loyalty_row = cur.fetchone()
        count = loyalty_row["paid_count"] if loyalty_row else 0

        loyalty_free = False
        loyalty_eligible = False

        if loyalty_row and loyalty_row["paid_count"] == 5:
            loyalty_free = True
        elif count >= 4:
            loyalty_eligible = True

        if not loyalty_free:
            new_count = count + 1

            if loyalty_row:
                cur.execute("UPDATE loyalty SET paid_count=? WHERE car_plate=?", (new_count, car_plate))
            else:
                cur.execute("INSERT INTO loyalty (car_plate, paid_count) VALUES (?, ?)", (car_plate, new_count))

            if new_count == 5:
                loyalty_eligible = True

            elif new_count > 5:
                cur.execute("UPDATE loyalty SET paid_count=0 WHERE car_plate=?", (car_plate,))
                new_count = 0

            count = new_count

        else:
            count = 6
            cur.execute("UPDATE loyalty SET paid_count=0 WHERE car_plate=?", (car_plate,))

        conn.commit()

        # ===== Pricing logic =====
        effective_price = 0.0 if loyalty_free else price
        net_total = effective_price - discount

        # ===== PAYMENT LOGIC FIX =====
        # CASH = user input
        # E-Wallet / QR / Card = always full net_total (no balance issues)
        if payment_method == "Cash":
            final_paid = paid_amount
            balance = net_total - final_paid
        else:
            final_paid = net_total
            balance = 0.0

        payment_status = "PAID" if balance <= 0 else "PARTIAL"

        # ===== DAILY INVOICE SYSTEM (FIXED) =====
        cur.execute("""
            SELECT invoice_no
            FROM orders
            WHERE invoice_no LIKE ?
            ORDER BY id DESC
            LIMIT 1
        """, (f"INV-{date_str.replace('-','')}-%",))

        last = cur.fetchone()

        if last and last[0]:
            last_seq = int(last[0].split("-")[-1])
            seq = last_seq + 1
        else:
            seq = 1

        invoice_no = f"INV-{date_str.replace('-','')}-{seq}"




        # ===== Insert order =====
        cur.execute("""
            INSERT INTO orders
            (car_plate, car_type, service_type, price, discount, paid_amount, balance,
             payment_method, payment_status, loyalty_status,
             created_at, invoice_date, reported_date, date, time, invoice_no)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            car_plate,
            car_type,
            service_type,
            effective_price,
            discount,
            final_paid,
            balance,
            payment_method,
            payment_status,
            "Free Wash" if loyalty_free else ("Eligible" if loyalty_eligible else "Not Eligible"),
            now.strftime("%Y-%m-%d %H:%M:%S"),
            date_str,
            date_str,
            date_str,
            time_str,
            invoice_no
        ))

        conn.commit()
        order_id = cur.lastrowid
        conn.close()

        # ===== Order object for receipt =====
        order = {
            "id": order_id,
            "invoice_no": invoice_no,
            "car_plate": car_plate,
            "car_type": car_type,
            "service_type": service_type,
            "price": effective_price,
            "discount": discount,
            "paid_amount": final_paid,
            "balance": balance,
            "payment_method": payment_method,
            "date": date_str,
            "time": time_str,
            "loyalty_count": count,
            "loyalty_free": loyalty_free,
            "loyalty_eligible": loyalty_eligible,
            "payment_status": payment_status
        }

        return render_template("receipt.html", order=order, receipt_type=receipt_type)

    # ===== GET POS PAGE =====
    conn = get_db_connection()
    services = conn.execute("SELECT * FROM services ORDER BY name").fetchall()
    conn.close()

    return render_template("pos.html", services=services)


###===============PROMO==============================
@app.route('/promo_booking')
@app.route('/promo')
def promo():
    return render_template('promo_booking.html')

@app.route('/book_promo', methods=['GET', 'POST'])
def book_promo():
    if request.method == 'POST':
        return "Promo booking submitted"

    timeslots = [
        "10:00 AM",
        "11:30 AM",
        "1:00 PM",
        "2:30 PM",
        "4:00 PM",
        "5:30 PM"
    ]

    disabled_slots = []  # later you can load from DB

    return render_template(
        'book_promo.html',
        timeslots=timeslots,
        disabled_slots=disabled_slots,
        current_date=date.today().strftime("%Y-%m-%d")
    )
# ================= CREATE ORDER (This route seems like a newer version of POS, consolidate if possible) =================
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
    
    # Get receipt_type from form, default to ORIGINAL
    receipt_type = request.form.get("receipt_type", "ORIGINAL") 

    order = {
        "car_plate": car_plate,
        "car_type": car_type,
        "service_type": service_type,
        "payment_method": payment_method,
        "price": price,
        "loyalty_status": "Not Eligible" # Default, will be updated by process_loyalty
    }

    # Process loyalty *before* inserting the order to get the final price and loyalty status
    processed_order = process_loyalty(order)
    
    saved = insert_order_record(
        car_plate=processed_order["car_plate"],
        car_type=processed_order["car_type"],
        service_type=processed_order["service_type"],
        payment_method=processed_order["payment_method"],
        price=processed_order["price"], # Use the potentially modified price from loyalty processing
        loyalty_status=processed_order["loyalty_status"], # Use the loyalty status from loyalty processing
        contact_number=request.form.get("contact_number"), # Added from form data
        address=request.form.get("address"), # Added from form data
        paid_amount=float(request.form.get("paid_amount", 0)),
        invoice_date=invoice_date,
        reported_date=reported_date
    )

    # Update the order dictionary with generated details and loyalty status
    processed_order["invoice_no"] = saved["invoice_no"]
    processed_order["id"] = saved["id"]
    processed_order["date"] = saved["date"]
    processed_order["time"] = saved["time"]
    processed_order["created_at"] = saved["created_at"]
    processed_order["reported_date"] = saved["reported_date"]
    
    # Store loyalty_count and loyalty_eligible for display
    processed_order["loyalty_count"] = processed_order.get("loyalty_count", 0)
    processed_order["loyalty_eligible"] = processed_order.get("loyalty_eligible", False)
    # The loyalty processing already sets loyalty_free and loyalty_status correctly

    return render_template("receipt.html", order=processed_order, receipt_type=receipt_type)


# ================= LOYALTY =================
def process_loyalty(order):
    car_plate = order["car_plate"]
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT paid_count FROM loyalty WHERE car_plate=?", (car_plate,))
    row = cur.fetchone()
    current_paid_count = row["paid_count"] if row else 0

    order["loyalty_free"] = False
    order["loyalty_eligible"] = False

    # Check for free wash eligibility for THIS transaction
    if current_paid_count == 5: # This means the 6th wash is free
        order["price"] = 0.0
        order["loyalty_free"] = True
        order["loyalty_status"] = "Free Wash"
        # Reset loyalty count to 0 after providing free wash
        cur.execute("UPDATE loyalty SET paid_count=? WHERE car_plate=?", (0, car_plate))
        order["loyalty_count"] = 0 # For receipt display
    else:
        # Increment paid_count for a regular paid wash
        new_paid_count = current_paid_count + 1
        if row:
            cur.execute("UPDATE loyalty SET paid_count=? WHERE car_plate=?", (new_paid_count, car_plate))
        else:
            cur.execute("INSERT INTO loyalty (car_plate, paid_count) VALUES (?, ?)", (car_plate, new_paid_count))
        
        order["loyalty_count"] = new_paid_count # For receipt display
        
        if new_paid_count >= 5: # Mark as eligible for next free wash
            order["loyalty_eligible"] = True
            order["loyalty_status"] = "Eligible for Free Wash Next"
        else:
            order["loyalty_status"] = "Not Eligible"

    conn.commit()
    conn.close()
    return order


@app.route("/check_loyalty/<car_plate>")
def check_loyalty(car_plate):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT paid_count FROM loyalty WHERE car_plate=?", (car_plate.replace(" ", "").upper(),)
    ).fetchone()
    conn.close()
    paid = row["paid_count"] if row else 0
    eligible = paid >= 5
    return jsonify({"paid": paid, "eligible": eligible})

# ================= DASHBOARD =================

SERVICE_NAMES = {
    "wash_basic": "CAR WASH - BASIC",
    "wash_special": "CAR WASH - SPECIAL",
    "wash_maintain": "CAR WASH - MAINTENANCE",
    "coat1": "COATING - 1 YEAR",
    "coat2": "COATING - 2 YEAR",
    "coat3": "COATING - 3 YEAR",
    "disp2": "DISPOSABLE - 2 YEAR",
    "disp3": "DISPOSABLE - 3 YEAR",
    "DISPO_GRAPHENE_PROMO": "DISPO GRAPHENE COATING PROMO",
    "wax": "WAXING",
    "int_detail": "INTERIOR DETAILING",
    "int_coat": "INTERIOR COATING"
}

def safe_invoice(row):
    inv = row["invoice_no"] if row["invoice_no"] else ""
    if not inv or inv.strip() == "":
        return f"INV-{row['id']:05d}"
    return inv

@app.route("/dashboard_data")
def dashboard_data():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403

    conn = get_db_connection()
    now = now_kul()

    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")

    # ================= HELPER =================
    def calculate_revenue(rows):
        total = 0
        for r in rows:
            price = r["price"] or 0
            discount = r["discount"] or 0
            paid = r["paid_amount"] or 0

            net = price - discount

            # Prevent negative / overcount
            if net < 0:
                net = 0

            total += min(paid, net)

        return round(total, 2)

    # ================= FETCH DATA =================
    today_rows = conn.execute("""
        SELECT price, discount, paid_amount
        FROM orders
        WHERE DATE(reported_date)=?
    """, (today,)).fetchall()

    week_rows = conn.execute("""
        SELECT price, discount, paid_amount
        FROM orders
        WHERE DATE(reported_date) BETWEEN ? AND ?
    """, (week_start, today)).fetchall()

    month_rows = conn.execute("""
        SELECT price, discount, paid_amount
        FROM orders
        WHERE strftime('%Y-%m', reported_date)=?
    """, (month_key,)).fetchall()

    # ================= CALCULATE =================
    today_revenue = calculate_revenue(today_rows)
    week_revenue = calculate_revenue(week_rows)
    month_revenue = calculate_revenue(month_rows)

    # ================= CARS TODAY =================
    cars_today = conn.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE paid_amount > 0
        AND DATE(reported_date)=?
    """, (today,)).fetchone()[0]

    # ================= RECENT SALES =================
    recent_sales_raw = conn.execute("""
        SELECT
            id,
            COALESCE(invoice_no, '') AS invoice_no,
            COALESCE(car_plate, '-') AS car_plate,
            COALESCE(car_type, '-') AS car_type,
            COALESCE(service_type, '-') AS service_type,
            COALESCE(payment_method, '-') AS payment_method,
            COALESCE(price, 0) AS price,
            COALESCE(discount, 0) AS discount,
            COALESCE(paid_amount, 0) AS paid_amount,
            created_at,
            COALESCE(payment_status, '-') AS payment_status
        FROM orders
        ORDER BY created_at DESC, id DESC
        LIMIT 10
    """).fetchall()

    recent_sales = []

    for r in recent_sales_raw:
        price = r["price"]
        discount = r["discount"]
        paid = r["paid_amount"]

        net = price - discount
        if net < 0:
            net = 0

        invoice = r["invoice_no"] if r["invoice_no"] else f"INV-{r['id']:05d}"
        created = r["created_at"] or ""

        recent_sales.append({
            "id": r["id"],
            "invoice": invoice,
            "car_plate": r["car_plate"],
            "car_type": r["car_type"],
            "service_type": SERVICE_NAMES.get(r["service_type"], r["service_type"]),
            "payment_method": r["payment_method"],
            "price": round(price, 2),
            "net": round(net, 2),
            "paid": round(paid, 2),
            "date": created[:10] if len(created) >= 10 else "-",
            "time": created[11:19] if len(created) >= 19 else "-",
            "status": r["payment_status"]
        })

    conn.close()

    return jsonify({
        "today_revenue": today_revenue,
        "week_revenue": week_revenue,
        "month_revenue": month_revenue,
        "cars_today": cars_today,
        "recent_sales": recent_sales
    })

@app.route("/dashboard")
def dashboard():
    if session.get("role") != "admin":
        return redirect("/pos")

    data = get_revenue_data()
    low_stock = get_low_stock()

    conn = get_db_connection()

    bookings_raw = conn.execute("""
        SELECT car_plate, service_type, booking_date, booking_time, type
        FROM bookings
        WHERE LOWER(status)='confirmed'
        ORDER BY id DESC
        LIMIT 10
    """).fetchall()

    recent_sales_raw = conn.execute("""
        SELECT
            id,
            invoice_no,
            car_plate,
            car_type,
            service_type,
            payment_method,
            price,
            discount,
            paid_amount,
            created_at,
            payment_status
        FROM orders
        ORDER BY created_at DESC, id DESC
        LIMIT 10
    """).fetchall()

    conn.close()

    new_bookings = []
    for b in bookings_raw:
        new_bookings.append({
            "car_plate": b["car_plate"] or "-",
            "service": SERVICE_NAMES.get(b["service_type"], b["service_type"]),
            "date": b["booking_date"] or "-",
            "time": b["booking_time"] or "-",
            "type": b["type"] or "normal"
        })

    recent_sales = []

    for r in recent_sales_raw:

        price = r["price"] or 0
        discount = r["discount"] or 0
        paid = r["paid_amount"] or 0

        net = price - discount


        recent_sales.append({
            "id": r["id"],
            "invoice": safe_invoice(r),
            "car_plate": r["car_plate"] or "-",
            "car_type": r["car_type"] or "-",
            "service_type": SERVICE_NAMES.get(r["service_type"], r["service_type"]),
            "payment_method": r["payment_method"] or "-",
            # 🔥 IMPORTANT FIELDS
            "price": price,
            "discount": discount,
            "net": net,
            "paid": paid,


            "date": r["created_at"][:10] if r["created_at"] else "-",
            "time": r["created_at"][11:19] if r["created_at"] else "-",
            "status": r["payment_status"] or "-"
        })

    return render_template(
        "dashboard.html",
        today_revenue=data["today_revenue"],
        week_revenue=data["week_revenue"],
        month_revenue=data["month_revenue"],
        cars_today=data["cars_today"],
        recent_sales=recent_sales,
        new_bookings=new_bookings,
        low_stock=low_stock
    )

# ================= REVENUE + RECENT SALES =================

def get_revenue_data():
    conn = get_db_connection()
    now = now_kul()

    today = now.strftime("%Y-%m-%d")

    # ✅ FIX 1: Proper week (Monday → Today)
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")

    month_key = now.strftime("%Y-%m")

    # ================= HELPER =================
    def calculate_revenue(rows):
        total = 0
        for r in rows:
            price = r["price"] or 0
            discount = r["discount"] or 0
            paid = r["paid_amount"] or 0

            net = price - discount

            # ✅ prevent negative values
            if net < 0:
                net = 0

            total += min(paid, net)

        return round(total, 2)

    # ================= GET RAW DATA =================
    today_rows = conn.execute("""
        SELECT price, discount, paid_amount
        FROM orders
        WHERE DATE(reported_date)=?
    """, (today,)).fetchall()

    week_rows = conn.execute("""
        SELECT price, discount, paid_amount
        FROM orders
        WHERE DATE(reported_date) BETWEEN ? AND ?
    """, (week_start, today)).fetchall()

    month_rows = conn.execute("""
        SELECT price, discount, paid_amount
        FROM orders
        WHERE strftime('%Y-%m', reported_date)=?
    """, (month_key,)).fetchall()

    # ================= CALCULATE =================
    today_revenue = calculate_revenue(today_rows)
    week_revenue = calculate_revenue(week_rows)
    month_revenue = calculate_revenue(month_rows)

    # ================= CARS TODAY =================
    cars_today = conn.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE paid_amount > 0
        AND DATE(reported_date)=?
    """, (today,)).fetchone()[0]

    # ================= RECENT SALES =================
    recent_sales_raw = conn.execute("""
        SELECT
            id,
            COALESCE(invoice_no, '') AS invoice_no,
            COALESCE(car_plate, '-') AS car_plate,
            COALESCE(car_type, '-') AS car_type,
            COALESCE(service_type, '-') AS service_type,
            COALESCE(payment_method, '-') AS payment_method,
            COALESCE(price, 0) AS price,
            COALESCE(discount, 0) AS discount,
            COALESCE(paid_amount, 0) AS paid_amount,
            created_at,
            COALESCE(payment_status, '-') AS payment_status
        FROM orders
        ORDER BY created_at DESC, id DESC
        LIMIT 10
    """).fetchall()

    recent_sales = []

    for r in recent_sales_raw:
        price = r["price"]
        discount = r["discount"]
        paid = r["paid_amount"]

        net = price - discount

        # ✅ prevent negative
        if net < 0:
            net = 0

        invoice = r["invoice_no"] if r["invoice_no"] else f"INV-{r['id']:05d}"
        created = r["created_at"] or ""

        recent_sales.append({
            "id": r["id"],
            "invoice": invoice,
            "car_plate": r["car_plate"],
            "car_type": r["car_type"],
            "service_type": SERVICE_NAMES.get(r["service_type"], r["service_type"]),
            "payment_method": r["payment_method"],
            "price": round(price, 2),
            "net": round(net, 2),
            "paid": round(paid, 2),
            "date": created[:10] if len(created) >= 10 else "-",
            "time": created[11:19] if len(created) >= 19 else "-",
            "status": r["payment_status"]
        })

    conn.close()

    return {
        "today_revenue": today_revenue,
        "week_revenue": week_revenue,
        "month_revenue": month_revenue,
        "cars_today": cars_today,
        "recent_sales": recent_sales
    }


def get_low_stock():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM inventory WHERE quantity <= 5 AND is_deleted = 0 ORDER BY quantity ASC, item ASC").fetchall()
    conn.close()
    return [dict(x) for x in rows]

@app.route("/recent_sales")
def recent_sales():
    if session.get("role") != "admin":
        return redirect("/pos")

    filter_type = request.args.get("filter_type", "created")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")

    conn = get_db_connection()

    query = """
        SELECT
            id,
            invoice_no,
            car_plate,
            car_type,
            service_type,
            payment_method,
            price,
            invoice_date,
            reported_date,
            created_at,
            loyalty_status,
            payment_status
        FROM orders
        WHERE payment_status='Paid'
    """

    params = []

    if date_from and date_to:
        if filter_type == "invoice":
            query += " AND DATE(invoice_date) BETWEEN ? AND ?"
        elif filter_type == "reported":
            query += " AND DATE(reported_date) BETWEEN ? AND ?"
        else:
            query += " AND DATE(created_at) BETWEEN ? AND ?"
        params.extend([date_from, date_to])

    query += " ORDER BY created_at DESC, id DESC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    sales = []

    for r in rows:
        sales.append({
            "id": r["id"],
            "invoice": safe_invoice(r),
            "car_plate": r["car_plate"] or "-",
            "car_type": r["car_type"] or "-",
            "service_type": SERVICE_NAMES.get(r["service_type"], r["service_type"]),
            "payment_method": r["payment_method"] or "-",
            "price": r["price"] or 0,
            "net": price,
            "invoice_date": r["invoice_date"] or "-",
            "reported_date": r["reported_date"] or "-",
            "date": r["created_at"][:10] if r["created_at"] else "-",
            "time": r["created_at"][11:19] if r["created_at"] else "-",
            "status": r["payment_status"] or "-"
        })

    return render_template(
        "recent_sales.html",
        sales=sales,
        filter_type=filter_type,
        date_from=date_from,
        date_to=date_to
    )
@app.route("/sales_report_page")
def sales_report_page():
    return render_template("sales_report.html")

# ================= Admin add/update recommendation =================
@app.route("/update_recommendation/<int:id>", methods=["POST"])
def update_recommendation(id):
    if "username" not in session:
        return redirect("/login")

    payment_transfer = request.form["payment_transfer"]

    conn = get_db_connection()
    conn.execute("""
        UPDATE recommendations
        SET payment_transfer=?
        WHERE id=?
    """, (payment_transfer, id))
    conn.commit()
    conn.close()

    return redirect("/recommendation_report")

@app.route("/delete_recommendation/<int:id>")
def delete_recommendation(id):
    if "username" not in session:
        return redirect("/login")

    conn = get_db_connection()
    conn.execute("DELETE FROM recommendations WHERE id=?", (id,))
    conn.commit()
    conn.close()

    return redirect("/recommendation_report")


# ================= RECEIPT =================
def generate_invoice_no(conn, now=None):
    if now is None:
        now = datetime.now()

    date_str = now.strftime("%Y%m%d")
    cur = conn.cursor()

    cur.execute("""
        SELECT invoice_no
        FROM orders
        WHERE invoice_no LIKE ?
        ORDER BY id DESC
        LIMIT 1
    """, (f"INV-{date_str}-%",))

    last = cur.fetchone()

    if last and last["invoice_no"]:
        last_seq = int(last["invoice_no"].split("-")[-1])
        seq = last_seq + 1
    else:
        seq = 1

    return f"INV-{date_str}-{seq}"


@app.route("/receipt/<invoice>")
def receipt(invoice):
    if "username" not in session:
        return redirect("/login")

    conn = get_db_connection()

    row = conn.execute("""
        SELECT * FROM orders 
        WHERE invoice_no=? 
        ORDER BY id DESC 
        LIMIT 1
    """, (invoice,)).fetchone()

    conn.close()

    if not row:
        return f"Receipt not found for invoice {invoice}", 404

    order = dict(row)

    # ================= SAFE DEFAULTS =================
    order.setdefault("paid_amount", 0.0)
    order.setdefault("price", 0.0)
    order.setdefault("discount", 0.0)
    order.setdefault("payment_status", "Paid")

    # ================= FORCE CONSISTENT DATE/TIME =================
    # ALWAYS use created_at (source of truth)
    if order.get("created_at"):
        dt = order["created_at"]
        order["date"] = dt[:10]
        order["time"] = dt[11:16]

    # ================= FIX BALANCE PROPERLY =================
    net = order["price"] - order.get("discount", 0)
    order["balance"] = order["paid_amount"] - net

    return render_template(
        "receipt.html",
        order=order,
        is_copy=request.args.get("copy", "false").lower() == "true",
        is_reprint=request.args.get("reprint", "false").lower() == "true"
    )

# ================= PACKAGES =================
@app.route("/packages")
def packages():
    packages = [
        {"name": "Basic Wash", "price": "RM15", "details": ["Exterior hand wash", "Tyre cleaning", "Quick dry"]},
        {"name": "Premium Wash", "price": "RM35", "details": ["Exterior wash", "Interior vacuum", "Dashboard wipe", "Tyre shine"]},
        {"name": "Full Detailing", "price": "RM120", "details": ["Exterior deep wash", "Interior detailing", "Seat cleaning", "Wax protection"]}
    ]
    return render_template("packages.html", packages=packages)

# Individual package pages
@app.route("/package_basic")
def package_basic():
    return render_template("package_basic.html")

@app.route("/package_supreme")
def package_supreme():
    return render_template("package_supreme.html")

@app.route("/package_polishing")
def package_polishing():
    return render_template("package_polishing.html")

@app.route("/special_package")
def package_special():
    return render_template("special_package.html")



# =========================
# INVOICE HOME (CREATE PAGE)
# =========================
@app.route("/invoice_new")
def invoice_new():
    return render_template("invoice_new.html")


# =========================
# CREATE INVOICE (FROM UI)
# =========================
@app.route("/invoice/create", methods=["POST"])
def create_invoice():
    global INVOICE_COUNTER

    data = request.json

    invoice_no = generate_invoice_no(INVOICE_COUNTER)
    INVOICE_COUNTER += 1

    INVOICE_DB[invoice_no] = {
        "invoice_no": invoice_no,
        "customer_name": data.get("customer_name"),
        "customer_phone": data.get("customer_phone"),
        "customer_company": data.get("customer_company"),
        "car_plate": data.get("car_plate"),
        "car_type": data.get("car_type"),
        "items": data.get("items", []),
        "total": float(data.get("total", 0)),
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    return jsonify({"invoice_no": invoice_no})


# =========================
# VIEW INVOICE (PRINT PAGE)
# =========================
@app.route("/invoice/<invoice_no>")
def invoice_page(invoice_no):

    data = INVOICE_DB.get(invoice_no)

    if not data:
        return "Invoice not found", 404

    return render_template("invoice_new.html", **data)


# ================= INVENTORY =================
@app.route("/inventory")
def inventory():
    if "username" not in session:
        return redirect("/login")

    conn = get_db_connection()

    filter_type = request.args.get("filter_type", "all")
    filter_month = request.args.get("filter_month", "")
    filter_year = request.args.get("filter_year", "")
    filter_start = request.args.get("filter_start", "")
    filter_end = request.args.get("filter_end", "")

    query = "SELECT * FROM inventory WHERE is_deleted=0"
    params = []

    if filter_type == "month":
        query += " AND strftime('%Y-%m', purchase_date)=?"
        params.append(filter_month)

    elif filter_type == "year":
        query += " AND strftime('%Y', purchase_date)=?"
        params.append(filter_year)

    elif filter_type == "custom":
        query += " AND purchase_date BETWEEN ? AND ?"
        params += [filter_start, filter_end]

    query += " ORDER BY id DESC"

    items = conn.execute(query, params).fetchall()

    total = conn.execute("""
        SELECT COALESCE(SUM(total_amount),0)
        FROM inventory WHERE is_deleted=0
    """).fetchone()[0]

    conn.close()

    return render_template(
        "inventory.html",
        items=items,
        total_spent=f"{total:.2f}"
    )

@app.route('/save_all_bookings', methods=['POST'])
def save_all_bookings():
    data = request.get_json()

    conn = sqlite3.connect('shine.db')
    cur = conn.cursor()

    for b in data:
        cur.execute("""
            INSERT INTO bookings 
            (car_plate, contact_number, service_type, price, date, time)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            b['car_plate'],
            b['contact_number'],
            b['service_type'],
            b['price'],
            b['date'],
            b['time']
        ))

    conn.commit()
    conn.close()

    return "All bookings saved successfully!"



@app.route("/inventory/save", methods=["POST"])
def save_item():
    data = request.json
    conn = get_db_connection()

    try:
        conn.execute("""
            UPDATE inventory SET
                item=?, company=?, phone=?, address=?, purchase_date=?,
                quantity=?, price=?, total_amount=?,
                serial_number=?, category=?, unit=?, last_updated=?
            WHERE id=?
        """, (
            data["item"],
            data.get("company", ""),
            data.get("phone", ""),
            data.get("address", ""),
            data.get("purchase_date", ""),
            int(data.get("quantity", 0)),
            float(data.get("price", 0)),
            float(data.get("quantity", 0)) * float(data.get("price", 0)),
            data.get("serial_number", ""),
            data.get("category", ""),
            data.get("unit", ""),
            now_kul().strftime("%Y-%m-%d %H:%M:%S"),
            data["id"]
        ))

        conn.commit()
        return jsonify({"success": True})

    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)})

    finally:
        conn.close()

@app.route("/inventory/delete", methods=["POST"])
def delete_item():
    data = request.json
    conn = get_db_connection()

    conn.execute("""
        UPDATE inventory
        SET is_deleted=1, last_updated=?
        WHERE id=?
    """, (now_kul().strftime("%Y-%m-%d %H:%M:%S"), data["id"]))

    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/add_inventory", methods=["POST"])
def add_inventory():
    if session.get("role") != "admin":
        return "Admin only"

    item = request.form["item"]
    company = request.form.get("company", "")
    phone = request.form.get("phone", "")
    address = request.form.get("address", "")
    purchase_date = request.form.get("purchase_date", "")
    quantity = int(request.form.get("quantity", 0))
    price = float(request.form.get("price", 0))
    total_amount = quantity * price
    serial_number = request.form.get("serial_number", "")
    category = request.form.get("category", "")
    unit = request.form.get("unit", "")
    last_updated = now_kul().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    # =====================================================
    # 🔒 SAFE: GET NEXT REF FROM EXISTING DATA ONLY
    # =====================================================
    conn.execute("BEGIN IMMEDIATE")

    row = conn.execute("""
        SELECT COALESCE(
            MAX(CAST(SUBSTR(reference_no, 5) AS INTEGER)),
            0
        )
        FROM inventory
    """).fetchone()

    new_number = row[0] + 1
    reference_no = f"REF-{new_number:04d}"

    # =====================================================
    # 🔍 CHECK EXISTING ITEM (SOFT DELETE RESTORE)
    # =====================================================
    existing = conn.execute(
        "SELECT id, is_deleted FROM inventory WHERE item=? COLLATE NOCASE",
        (item,)
    ).fetchone()

    # =====================================================
    # ♻️ RESTORE ITEM (NO NEW REF)
    # =====================================================
    if existing and existing["is_deleted"] == 1:
        conn.execute("""
            UPDATE inventory SET 
                company=?, phone=?, address=?, purchase_date=?, quantity=?, price=?, 
                serial_number=?, category=?, unit=?, is_deleted=0, last_updated=? 
            WHERE id=?
        """, (
            company, phone, address, purchase_date, quantity, price,
            serial_number, category, unit, last_updated, existing["id"]
        ))

    # =====================================================
    # ➕ INSERT NEW ITEM (WITH SAFE REF)
    # =====================================================
    else:
        conn.execute("""
            INSERT INTO inventory (
                item, company, phone, address, purchase_date,
                quantity, price, total_amount, serial_number, category, unit,

                reference_no, last_updated, is_deleted
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            item, company, phone, address, purchase_date,
            quantity, price, total_amount, serial_number, category, unit,
            reference_no, last_updated
        ))
    print("DB PATH:", DB_PATH)
    conn.commit()
    conn.close()

    return redirect("/inventory")

@app.route("/recommendation_form")
def recommendation_form():
    return render_template("recommendation_form.html")


@app.route("/add_recommendation", methods=["GET", "POST"])
def add_recommendation():
    if "username" not in session:
        return redirect("/login")
    invoice_no = request.form.get("invoice_no", "")
    
    date = request.form["date"]
    car_plate = request.form["car_plate"]
    package_name = request.form["package_name"]
    sales_person = request.form["sales_person"]
    payment_amount = float(request.form["payment_amount"])
    payment_transfer = request.form["payment_transfer"]

    commission = payment_amount * 0.10

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO recommendations (
            invoice_no, date, car_plate, package_name,
            sales_person, payment_amount, commission, payment_transfer
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        invoice_no, date, car_plate, package_name,
        sales_person, payment_amount, commission, payment_transfer
    ))

    conn.commit()
    conn.close()

    return redirect("/recommendation_report")

@app.route("/recommendation_report")
def recommendation_report():
    if "username" not in session:
        return redirect("/login")

    conn = get_db_connection()
    role = session.get("role")
    sales_person = session.get("sales_person")

    # =========================
    # ADMIN VIEW (ALL DATA)
    # =========================
    if role == "admin":
        rows = conn.execute("""
            SELECT * FROM recommendations
            ORDER BY id DESC
        """).fetchall()

    # =========================
    # SALES PERSON VIEW (OWN DATA ONLY)
    # =========================
    else:
        rows = conn.execute("""
            SELECT * FROM recommendations
            WHERE sales_person = ?
            ORDER BY id DESC
        """, (sales_person,)).fetchall()

    conn.close()

    # totals based on filtered data
    total_commission = sum(r["commission"] for r in rows)
    total_sales = sum(r["payment_amount"] for r in rows)

    return render_template(
        "recommendation_report.html",
        reports=rows,
        total_commission=total_commission,
        total_sales=total_sales
    )

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
            category = request.form.get("category", "").strip() # Added category
            unit = request.form.get("unit", "").strip() # Added unit
            last_updated = now_kul().strftime("%Y-%m-%d %H:%M:%S")

            # Update database
            conn.execute(
                """
                UPDATE inventory SET 
                    item=?, company=?, phone=?, address=?, purchase_date=?, 
                    quantity=?, price=?, serial_number=?, category=?, unit=?, last_updated=? 
                WHERE id=?
                """,
                (item_name, company, phone, address, purchase_date, quantity, price, 
                 serial_number, category, unit, last_updated, id)
            )
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
    # Perform a soft delete instead of permanent delete
    conn.execute("UPDATE inventory SET is_deleted=1, last_updated=? WHERE id=?", (now_kul().strftime("%Y-%m-%d %H:%M:%S"), id))
    conn.commit()
    conn.close()
    return redirect("/inventory")

@app.route("/inventory_deleted")
def inventory_deleted():
    if session.get("role") != "admin":
        return redirect("/pos")
    conn = get_db_connection()
    items = conn.execute(
        "SELECT * FROM inventory WHERE is_deleted = 1 ORDER BY last_updated DESC"
    ).fetchall()
    conn.close()
    return render_template("inventory_deleted.html", items=items)

@app.route("/restore_inventory/<int:id>")
def restore_inventory(id):
    conn = get_db_connection()
    conn.execute("UPDATE inventory SET is_deleted = 0, last_updated=? WHERE id=?", (now_kul().strftime("%Y-%m-%d %H:%M:%S"), id))
    conn.commit()
    conn.close()
    return redirect("/inventory_deleted")

# ================= BOOKING =================
def generate_timeslots():
    slots = []
    start = 9
    end = 22
    for hour in range(start, end):
        slots.append(f"{hour:02d}:00")
        slots.append(f"{hour:02d}:30")
    return slots

def get_disabled_slots(timeslots, booked_times):
    disabled = set()
    for slot in timeslots:
        slot_dt = datetime.strptime(slot, "%H:%M").time()
        for booked in booked_times:
            booked_dt = datetime.strptime(booked, "%H:%M").time()
            diff_minutes = abs((slot_dt.hour*60 + slot_dt.minute) - (booked_dt.hour*60 + booked_dt.minute))
            if diff_minutes < 180:
                disabled.add(slot)
    return disabled

@app.route("/booking")
def booking():
    plate = request.args.get("plate", "")
    current_date_str = request.args.get("date", now_kul().strftime("%Y-%m-%d"))

    current_date = datetime.strptime(current_date_str, "%Y-%m-%d").date()
    today_date = now_kul().date()

    conn = get_db_connection()
    services = conn.execute("SELECT * FROM services ORDER BY name").fetchall()
    bookings = conn.execute(
        "SELECT booking_time FROM bookings WHERE booking_date=? AND LOWER(status)='confirmed'",
        (current_date_str,)
    ).fetchall()
    conn.close()

    booked_times = [row["booking_time"] for row in bookings]
    timeslots = generate_timeslots()

    # Filter out past slots for today
    if current_date == today_date:
        now_time = now_kul().time()
        timeslots = [ts for ts in timeslots if datetime.strptime(ts, "%H:%M").time() > now_time]

    disabled_slots = get_disabled_slots(timeslots, booked_times)

    return render_template(
        "booking.html",
        services=services,
        timeslots=timeslots,
        plate=plate,
        current_date=current_date_str,
        abs=abs,
        datetime=datetime,
        booked_times=booked_times,
        disabled_slots=disabled_slots,
        today_date_str=today_date.strftime("%Y-%m-%d")
    )

@app.route("/create_booking", methods=["POST"])
def create_booking():

    print("FORM DATA:", request.form)
    try:
        car_plate = request.form.get("car_plate", "").upper()
        service = request.form.get("service_type", "")
        date_str = request.form.get("booking_date", "")
        time_str = request.form.get("booking_time", "")
        contact = request.form.get("contact", "")
        car_type = request.form.get("car_type", "-")


# Promo check
        promo = request.args.get("promo")  # from URL ?promo=1

        if promo == "1":
            if service not in ["disp1", "disp2", "disp3"]:
                return "❌ Invalid promo booking", 400



        if not all([car_plate, service, date_str, time_str, contact]):
            return "Missing form data", 400

        created_at = now_kul().strftime("%Y-%m-%d %H:%M:%S")

        conn = get_db_connection()

        # Day limit
        count_day = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE booking_date=? AND LOWER(status)='confirmed'",
            (date_str,)
        ).fetchone()[0]

        if count_day >= 3:
            conn.close()
            return "<script>alert('Date full');window.location='/booking';</script>"

        # Time gap check
        existing_bookings = conn.execute(
            "SELECT booking_time FROM bookings WHERE booking_date=? AND LOWER(status)='confirmed'",
            (date_str,)
        ).fetchall()

        from datetime import datetime
        selected_slot_dt = datetime.strptime(time_str, "%H:%M").time()

        for row in existing_bookings:
            if not row["booking_time"]:
                continue
            try:
                booked_slot_dt = datetime.strptime(row["booking_time"], "%H:%M").time()
            except:
                continue

            diff_minutes = abs(
                (selected_slot_dt.hour*60 + selected_slot_dt.minute) -
                (booked_slot_dt.hour*60 + booked_slot_dt.minute)
            )

            if diff_minutes < 180:
                conn.close()
                return "<script>alert('Time too close');window.location='/booking';</script>"

        # Insert
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO bookings 
            (car_plate, service_type, booking_date, booking_time, contact, created_at, status, car_type, type)
            VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)
            """,
            (
                car_plate,
                service,
                date_str,
                time_str,
                contact,
                created_at,
                car_type,
                "normal"
            )
        )

        booking_id = cur.lastrowid
        conn.commit()
        conn.close()

        session["latest_booking"] = {
            "car_plate": car_plate,
            "service": service,
            "car_type": car_type,
            "date": date_str,
            "time": time_str,
            "contact": contact,
            "booking_id": f"BK{now_kul().strftime('%Y%m%d')}{booking_id:03d}"
        }

        return redirect(url_for("booking_confirmed"))

    except Exception as e:
        print("BOOKING ERROR:", e)
        return str(e), 500


@app.route("/create_promo_booking", methods=["POST"])
def create_promo_booking():
    try:
        car_plate = request.form.get("car_plate", "").upper()
        car_type = request.form.get("car_type")
        date_str = request.form.get("booking_date")
        time_str = request.form.get("booking_time")
        contact = request.form.get("contact")

        service = "DISPO Graphene Coating"

        if not all([car_plate, car_type, date_str, time_str, contact]):
            return "Missing form data", 400

        price_map = {
            "Sedan": 488,
            "SUV_MPV": 588,
            "LARGE-MPV_4X4": 688
        }

        original_price = price_map.get(car_type, 0)
        discount = 200
        final_price = max(original_price - discount, 0)

        created_at = now_kul().strftime("%Y-%m-%d %H:%M:%S")

        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO promo_bookings
            (car_plate, car_type, service_type, booking_date, booking_time, contact,
             original_price, discount, final_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            car_plate, car_type, service,
            date_str, time_str, contact,
            original_price, discount, final_price, created_at
        ))

        conn.commit()
        conn.close()

        return redirect(url_for("promo_result"))

    except Exception as e:
        print("PROMO ERROR:", e)
        return str(e), 500

@app.route("/booking_confirmed")
def booking_confirmed():
    booking = session.pop("latest_booking", None)  # Get and remove from session
    if not booking:
        return redirect("/booking")  # fallback if no booking in session
    return render_template("booking_confirmed.html", booking=booking)

@app.route("/latest_bookings")
def latest_bookings():
    conn = get_db_connection()

    rows = conn.execute("""
        SELECT DISTINCT car_plate, service_type, booking_date, booking_time, type
        FROM bookings
        WHERE status='confirmed'
        ORDER BY booking_date DESC, booking_time DESC
        LIMIT 10
    """).fetchall()

    conn.close()

    bookings = []

    seen = set()

    for r in rows:
        key = (r["car_plate"], r["booking_date"], r["booking_time"])

        if key in seen:
            continue
        seen.add(key)

        bookings.append({
            "car_plate": r["car_plate"],
            "service": r["service_type"],
            "date": r["booking_date"],
            "time": r["booking_time"],
            "type": r["type"] or "normal"
        })

    return {"new_bookings": bookings}

@app.route("/booking_admin")
def booking_admin():
    if session.get("role") != "admin":
        return redirect("/pos")
    
    today = now_kul() # Use TZ-aware datetime for today
    year = request.args.get("year", default=today.year, type=int)
    month = request.args.get("month", default=today.month, type=int)

    conn = get_db_connection()
    bookings = conn.execute(
        """
        SELECT * FROM bookings 
        WHERE strftime('%Y', booking_date) = ? AND strftime('%m', booking_date) = ? 
        AND LOWER(status) = 'confirmed' 
        ORDER BY booking_date ASC, booking_time ASC, id ASC
        """,
        (str(year), f"{month:02d}")
    ).fetchall()
    conn.close()

    grouped_bookings = defaultdict(list)
    for b in bookings:
        grouped_bookings[b["booking_date"]].append(b)

    cal = calendar.Calendar(firstweekday=0) # Monday is the first day of the week
    calendar_data = cal.monthdayscalendar(year, month)
    month_name = calendar.month_name[month]

    prev_month = month - 1
    prev_year = year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    next_month = month + 1
    next_year = year
    if next_month == 13:
        next_month = 1
        next_year += 1

    total_confirmed = len(bookings)
    full_days = sum(1 for day_bookings in grouped_bookings.values() if len(day_bookings) >= 3)
    
    return render_template(
        "booking_admin.html",
        calendar_data=calendar_data,
        grouped_bookings=grouped_bookings,
        month=month,
        year=year,
        month_name=month_name,
        prev_month=prev_month,
        prev_year=prev_year,
        next_month=next_month,
        next_year=next_year,
        total_confirmed=total_confirmed,
        full_days=full_days,
        today_date=today.date() # Pass today's date for highlighting
    )



#====NEW BOOKING PAGE ===
@app.route("/new_booking", methods=["GET", "POST"])
def new_booking():
    if request.method == "POST":

        car_plate = request.form.get("car_plate")
        car_type = request.form.get("car_type")
        service = request.form.get("service_type")
        date = request.form.get("booking_date")
        service_mode = request.form.get("service_mode")
        time = "09:00"

        if not all([car_plate, car_type, service, date, service_mode]):
            return "Missing form data", 400

        conn = get_db_connection()

        conn.execute("""
            INSERT INTO bookings 
            (car_plate, car_type, service_type, booking_date, booking_time, service_mode, status, type)
            VALUES (?, ?, ?, ?, ?, ?, 'confirmed', 'promo')
        """, (car_plate, car_type, service, date, time, service_mode))

        conn.commit()
        conn.close()

        return render_template(
            "booking_success.html",
            car_plate=car_plate,
            car_type=car_type,
            date=date
        )

    return render_template("new_booking.html")

@app.route("/get_slots")
def get_slots():
    date = request.args.get("date")

    conn = get_db_connection()

    rows = conn.execute("""
        SELECT booking_time 
        FROM bookings 
        WHERE booking_date=? AND LOWER(status)='confirmed'
    """, (date,)).fetchall()

    conn.close()

    booked_slots = [r["booking_time"] for r in rows if r["booking_time"]]

    return {"booked": booked_slots}
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
    try:
        conn.execute("""
            INSERT INTO users (username, password, role) VALUES (?, ?, ?)
        """, (username, password, role))
        conn.commit()
    except sqlite3.IntegrityError:
        # Handle case where username might already exist (if username was UNIQUE)
        conn.close()
        return "<script>alert('Error: Username already exists.');window.location='/staff';</script>"
    except Exception as e:
        conn.close()
        return f"Error adding staff: {e}", 500
    finally:
        conn.close()
    return redirect("/staff")

# ================= FINANCE =================
@app.route("/finance")
def finance():
    if session.get("role") != "admin":
        return "Admin only"

    conn = get_db_connection()
    today = now_kul().strftime("%Y-%m-%d")

    daily_orders = conn.execute(
        """SELECT COUNT(*) FROM orders WHERE payment_status='Paid' AND reported_date=? """,
        (today,)
    ).fetchone()[0]

    daily_revenue = conn.execute(
        """SELECT IFNULL(SUM(price), 0) FROM orders WHERE payment_status='Paid' AND reported_date=? """,
        (today,)
    ).fetchone()[0]

    payment_methods = ["Cash", "Card", "QR", "E-Wallet"]
    by_method = []
    for method in payment_methods:
        total = conn.execute(
            """SELECT IFNULL(SUM(price), 0) FROM orders WHERE payment_status='Paid' AND reported_date=? AND payment_method=? """,
            (today, method)
        ).fetchone()[0]
        by_method.append((method, total))
    
    total_revenue_from_methods = sum([x[1] for x in by_method])
    # Ensure total_revenue also includes anything not covered by payment_methods for robustness
    # This might not be strictly needed if all payments use one of the types, but good practice.
    total_revenue = conn.execute(
        """SELECT IFNULL(SUM(price), 0) FROM orders WHERE payment_status='Paid' AND reported_date=? """,
        (today,)
    ).fetchone()[0]

    conn.close()

    report = {
        "report_date": today,
        "daily_orders": daily_orders,
        "daily_revenue": daily_revenue,
        "by_method": by_method,
        "total_revenue": total_revenue # Use the sum from database, not just payment_methods list
    }
    return render_template("finance.html", report=report)





#=============
#AGENT DASHBOARD
#=============
@app.route("/agent_dashboard")
def agent_dashboard():
    if "username" not in session:
        return redirect("/login")

    if session["role"] != "sales":
        return "Access denied"

    conn = get_db_connection()

    rows = conn.execute("""
        SELECT 
            id,
            invoice_no,
            date,
            car_plate,
            package_name,
            sales_person,
            payment_amount,
            commission,
            payment_transfer
        FROM recommendations
        WHERE sales_person = ?
        ORDER BY id DESC
    """, (session["full_name"],)).fetchall()

    # totals
    total_commission = sum(r["commission"] for r in rows)
    total_sales = sum(r["payment_amount"] for r in rows)

    conn.close()

    return render_template(
        "agent_dashboard.html",
        rows=rows,
        total_commission=total_commission,
        total_sales=total_sales
    )

@app.route("/recommendations")
def recommendations():
    if "username" not in session:
        return redirect("/login")

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM recommendations ORDER BY id DESC")
    rows = cur.fetchall()

    conn.close()

    return render_template("recommendations.html", rows=rows)


@app.route("/agent_report/<sales_person>")
def agent_report(sales_person):
    if "username" not in session:
        return redirect("/login")

    conn = get_db_connection()

    rows = conn.execute("""
        SELECT * FROM recommendations
        WHERE sales_person = ?
        ORDER BY id DESC
    """, (sales_person,)).fetchall()

    conn.close()

    # ===== totals =====
    total_commission = sum(r["commission"] for r in rows)
    total_paid = sum(r["commission"] for r in rows if r["payment_transfer"] == "Paid")
    total_pending = sum(r["commission"] for r in rows if r["payment_transfer"] == "Pending")

    return render_template(
        "agent_report.html",
        reports=rows,
        sales_person=sales_person,
        total_commission=total_commission,
        total_paid=total_paid,
        total_pending=total_pending
    )

@app.route("/agent_select")
def agent_select():
    conn = get_db_connection()

    agents = conn.execute("""
        SELECT DISTINCT sales_person FROM recommendations
    """).fetchall()

    conn.close()

    return render_template("agent_select.html", agents=agents)

@app.route("/sales_report")
def sales_report():
    if session.get("role") != "admin":
        return {"error": "unauthorized"}, 403

    from_date = request.args.get("from")
    to_date = request.args.get("to")

    conn = get_db_connection()

    query = """
        SELECT
            id,
            invoice_no,
            car_plate,
            service_type,
            price,
            discount,
            paid_amount,
            created_at
        FROM orders
        WHERE 1=1
    """

    params = []

    if from_date and to_date:
        query += " AND DATE(created_at) BETWEEN ? AND ?"
        params.extend([from_date, to_date])

    query += " ORDER BY id DESC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    sales = []

    for r in rows:
        price = r["price"] or 0
        discount = r["discount"] or 0
        paid = r["paid_amount"] or 0

        net = price - discount
        if net < 0:
            net = 0

        sales.append({
            "invoice": r["invoice_no"] or f"INV-{r['id']:05d}",
            "car_plate": r["car_plate"] or "-",
            "service_type": r["service_type"],
            "price": round(price, 2),
            "net": round(net, 2),
            "paid": round(paid, 2),
            "date": r["created_at"][:10] if r["created_at"] else "-"
        })

    return jsonify(sales)

@app.route("/car_list_100")
def car_list_100():
    return render_template("car_list_100.html")


@app.route("/print_car_list_100")
def print_car_list_100():
    import sqlite3

    conn = sqlite3.connect("shine.db")
    cur = conn.cursor()

    cur.execute("SELECT * FROM dispo_promo ORDER BY id DESC")
    rows = cur.fetchall()

    conn.close()

    return render_template("print_car_list_100.html", rows=rows)

@app.route("/save_car_list_100", methods=["POST"])
def save_car_list_100():
    import sqlite3

    data = request.json

    conn = sqlite3.connect("shine.db")
    cur = conn.cursor()

    for row in data["rows"]:
        if row["car_plate"]:  # skip empty rows
            cur.execute("""
                INSERT INTO dispo_promo
                (car_plate, invoice_id, refer_by, date_booking, paid_amount, task)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                row["car_plate"],
                row["invoice_id"],
                row["refer_by"],
                row["date_booking"],
                row["paid_amount"],
                row["task"]
            ))

    conn.commit()
    conn.close()

    return {"status":"success"}


# PAYSLIP PAGE
@app.route("/payslip")
def payslip():
    return render_template("payslip.html")


# PRINT PAYSLIP
@app.route("/print_payslip", methods=["POST"])
def print_payslip():

    employee_name = request.form.get("employee_name")
    payment_date = request.form.get("payment_date")
    basic_salary = request.form.get("basic_salary")
    overtime = request.form.get("overtime")
    payment_amount = request.form.get("payment_amount")
    nett_salary = request.form.get("nett_salary")
    payment_method = request.form.get("payment_method")
    account_details = request.form.get("account_details")

    return render_template(
        "payslip.html",
        employee_name=employee_name,
        payment_date=payment_date,
        basic_salary=basic_salary,
        overtime=overtime,
        payment_amount=payment_amount,
        nett_salary=nett_salary,
        payment_method=payment_method,
        account_details=account_details,
        print_mode=True
    )

@app.route("/form_promo")
def form_promo():
    return render_template("form_promo.html")

# =====================
# SOCKET EVENTS
# =====================

@socketio.on('join')
def on_join(data):
    user_id = data['user_id']
    join_room(str(user_id))   # user room
    join_room("admin")       # admin room

# USER SEND MESSAGE
@socketio.on('send_message')
def handle_message(data):
    user_id = data['user_id']
    message = data['message']

    db = get_db()
    db.execute(
        "INSERT INTO chat (user_id, sender, message) VALUES (?, ?, ?)",
        (user_id, "user", message)
    )
    db.commit()

    # send to admin dashboard
    emit('receive_message', {
        "user_id": user_id,
        "sender": "user",
        "message": message
    }, room="admin")

# ADMIN REPLY
@socketio.on('admin_reply')
def admin_reply(data):
    user_id = data['user_id']
    message = data['message']

    db = get_db()
    db.execute(
        "INSERT INTO chat (user_id, sender, message) VALUES (?, ?, ?)",
        (user_id, "admin", message)
    )
    db.commit()

    # send back to that user only
    emit('receive_message', {
        "sender": "admin",
        "message": message
    }, room=str(user_id))

# ================= RUN =================
if __name__ == "__main__":
    # Only for local dev
    init_db()
    sync_old_orders_data()
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
else:
    # When using Gunicorn/WSGI, run init once per process
    init_db()
    sync_old_orders_data()
