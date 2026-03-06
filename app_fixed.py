from flask import Flask, render_template, request, redirect, session, url_for
import sqlite3
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = "supersecretkey"

# ===== DATABASE CONNECTIONS =====
def get_system_db_connection():
    conn = sqlite3.connect('shine.db')
    conn.row_factory = sqlite3.Row
    return conn

def get_db_connection():
    # Adjust path relative to shine-system folder
    conn = sqlite3.connect('../shinemaster/shinemaster.db')
    conn.row_factory = sqlite3.Row
    return conn

#login route
@app.route("/", methods=["GET","POST"])
def login():

    if request.method == "POST":

        username = request.form["username"]
        password = request.form["password"]

        conn = sqlite3.connect("shine.db")
        user = conn.execute(
        "SELECT * FROM users WHERE username=? AND password=?",
        (username,password)).fetchone()

        conn.close()

        if user:
            session["username"] = user[1]
            session["role"] = user[3]

            return redirect("/dashboard")

    return render_template("login.html")

def get_latest_sales():
    conn = sqlite3.connect('../shinemaster/shinemaster.db')
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

    # If you have a services table in shinemaster
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
    conn = sqlite3.connect('../shinemaster/shinemaster.db')
    conn.row_factory = sqlite3.Row
    services = conn.execute("SELECT * FROM services").fetchall()
        conn.execute("""
            INSERT INTO sales (invoice, car_plate, car_type, service_type, payment_method, price, date, time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (invoice, car_plate, car_type, service_type, payment_method, price, date, time))
        conn.commit()
        conn.close()
        return redirect(f"/receipt/{invoice}")

    conn.close()
    return render_template("new_order.html", services=services)

# Create order

@app.route("/create_order", methods=["POST"])
def create_order():

    car_plate = request.form.get("car_plate")
    car_type = request.form.get("car_type")
    service_type = request.form.get("service_type")
    payment_method = request.form.get("payment_method")
    price = request.form.get("price")

    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    time = now.strftime("%H:%M:%S")

    invoice = "INV" + now.strftime("%Y%m%d%H%M%S")

    conn = sqlite3.connect("shine.db")

    conn.execute("""
        INSERT INTO sales
        (invoice, car_plate, car_type, service_type, payment_method, price, date, time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (invoice, car_plate, car_type, service_type, payment_method, price, date, time))

    conn.commit()
    conn.close()

    return redirect("/pos")
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


if __name__ == "__main__":
    app.run(debug=True)

