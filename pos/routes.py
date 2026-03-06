from flask import Blueprint, render_template, request, redirect, session
import sqlite3
from datetime import datetime

pos_bp = Blueprint('pos', __name__, template_folder='templates', static_folder='static')

def get_pos_db_connection():
    conn = sqlite3.connect('shinemaster/shinemaster.db')  # link to POS DB
    conn.row_factory = sqlite3.Row
    return conn

@pos_bp.route("/pos", methods=["GET", "POST"])
def pos():
    if "username" not in session:
        return redirect("/login")
    
    if request.method == "POST":
        invoice = f"INV{datetime.now().strftime('%Y%m%d%H%M%S')}"
        car_plate = request.form["car_plate"]
        car_type = request.form["car_type"]
        service_type = request.form["service_type"]
        payment_method = request.form["payment_method"]
        price = float(request.form["price"])
        date = datetime.now().strftime("%Y-%m-%d")
        time = datetime.now().strftime("%H:%M:%S")

        conn = get_pos_db_connection()
        conn.execute("""
            INSERT INTO sales (invoice, car_plate, car_type, service_type, payment_method, price, date, time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (invoice, car_plate, car_type, service_type, payment_method, price, date, time))
        conn.commit()
        conn.close()

        return redirect(f"/receipt/{invoice}")
    
    return render_template("new_order.html")
