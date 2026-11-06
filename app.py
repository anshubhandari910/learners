
from flask import Flask, render_template, request, jsonify, render_template_string
import os
import hmac
import logging
import re
import uuid
import secrets
from decimal import Decimal
from urllib.parse import quote

import psycopg
import resend
import requests

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

CASHFREE_API_VERSION = "2023-08-01"


def get_database_url():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return database_url


def get_cashfree_config():
    client_id = os.environ.get("CASHFREE_CLIENT_ID")
    client_secret = os.environ.get("CASHFREE_CLIENT_SECRET")
    environment = os.environ.get("CASHFREE_ENV", "sandbox").lower()

    if not client_id or not client_secret:
        raise RuntimeError("Cashfree API credentials are not configured")

    if environment == "production":
        base_url = "https://api.cashfree.com/pg"
    else:
        base_url = "https://sandbox.cashfree.com/pg"

    return client_id, client_secret, base_url


def cashfree_headers():
    client_id, client_secret, _ = get_cashfree_config()
    return {
        "Content-Type": "application/json",
        "x-api-version": CASHFREE_API_VERSION,
        "x-client-id": client_id,
        "x-client-secret": client_secret
    }


def get_books():
    with psycopg.connect(
        get_database_url(),
        connect_timeout=10
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, description, price
                FROM books
                WHERE is_active = TRUE
                ORDER BY id DESC
            """)
            rows = cur.fetchall()

    return [
        {
            "id": row[0],
            "title": row[1],
            "description": row[2] or "",
            "price": float(row[3])
        }
        for row in rows
    ]


def get_order_from_database(order_id):
    with psycopg.connect(
        get_database_url(),
        connect_timeout=10
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT order_id, book_id, customer_name,
                       customer_email, customer_phone, amount,
                       payment_status
                FROM orders
                WHERE order_id = %s
            """, (order_id,))
            return cur.fetchone()


def verify_cashfree_order(order_id):
    _, _, base_url = get_cashfree_config()

    response = requests.get(
        f"{base_url}/orders/{quote(order_id, safe='')}",
        headers=cashfree_headers(),
        timeout=20
    )

    if response.status_code != 200:
        app.logger.error(
            "Cashfree order verification failed. HTTP %s",
            response.status_code
        )
        raise RuntimeError("Unable to verify payment with Cashfree")

    return response.json()


def mark_order_paid(order_id):
    # Only call this after Cashfree's server confirms PAID.
    token = secrets.token_urlsafe(32)

    with psycopg.connect(
        get_database_url(),
        connect_timeout=10
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE orders
                SET payment_status = 'PAID',
                    paid_at = COALESCE(paid_at, NOW()),
                    download_token = COALESCE(download_token, %s)
                WHERE order_id = %s
                  AND payment_status <> 'PAID'
                RETURNING order_id
            """, (token, order_id))

            updated = cur.fetchone()

            if updated:
                return token

            cur.execute("""
                SELECT download_token
                FROM orders
                WHERE order_id = %s
                  AND payment_status = 'PAID'
            """, (order_id,))
            row = cur.fetchone()

            return row[0] if row else None


@app.route("/")
def home():
    try:
        books = get_books()
        return render_template("index.html", books=books)
    except Exception:
        app.logger.exception("Unable to load books")
        return render_template(
            "index.html",
            books=[],
            db_error=True
        ), 503


@app.route("/health")
def health():
    return {
        "status": "ok",
        "app": "Learners Page"
    }


@app.route("/db-test")
def db_test():
    try:
        books = get_books()
        return {
            "status": "success",
            "app": "Learners Page",
            "books": books
        }
    except Exception:
        app.logger.exception("Database test failed")
        return {
            "status": "error",
            "message": "Database test failed. Check Render logs."
        }, 500


@app.route("/create-order", methods=["POST"])
def create_order():
    data = request.get_json(silent=True) or {}

    book_id = data.get("book_id")
    customer_name = str(data.get("name", "")).strip()
    customer_email = str(data.get("email", "")).strip().lower()
    customer_phone = re.sub(r"\D", "", str(data.get("phone", "")))

    if not str(book_id).isdigit():
        return jsonify({"error": "Please select a valid eBook."}), 400

    if not customer_name or len(customer_name) > 100:
        return jsonify({"error": "Please enter a valid name."}), 400

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", customer_email):
        return jsonify({"error": "Please enter a valid email address."}), 400

    if not re.fullmatch(r"[6-9]\d{9}", customer_phone):
        return jsonify({"error": "Enter a valid 10-digit Indian mobile number."}), 400

    try:
        # Always read the price from our database, never from the browser.
        with psycopg.connect(
            get_database_url(),
            connect_timeout=10
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, title, price
                    FROM books
                    WHERE id = %s AND is_active = TRUE
                """, (int(book_id),))
                book = cur.fetchone()

        if not book:
            return jsonify({"error": "This eBook is unavailable."}), 404

        actual_book_id, book_title, book_price = book
        amount = Decimal(str(book_price)).quantize(Decimal("0.01"))

        order_id = "LP_" + uuid.uuid4().hex[:24]
        site_url = os.environ.get("SITE_URL", "").rstrip("/")

        if not site_url.startswith("https://"):
            return jsonify({
                "error": "Website URL is not configured. Please contact support."
            }), 503

        return_url = (
            f"{site_url}/payment-return?order_id={{order_id}}"
        )

        # Save a pending order before calling Cashfree.
        with psycopg.connect(
            get_database_url(),
            connect_timeout=10
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO orders (
                        order_id, book_id, customer_name,
                        customer_email, customer_phone, amount,
                        payment_status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
                """, (
                    order_id,
                    actual_book_id,
                    customer_name,
                    customer_email,
                    customer_phone,
                    amount
                ))

        _, _, base_url = get_cashfree_config()

        payload = {
            "order_id": order_id,
            "order_amount": float(amount),
            "order_currency": "INR",
            "customer_details": {
                "customer_id": "customer_" + uuid.uuid4().hex[:16],
                "customer_name": customer_name,
                "customer_email": customer_email,
                "customer_phone": customer_phone
            },
            "order_meta": {
                "return_url": return_url
            },
            "order_note": book_title[:200]
        }

        response = requests.post(
            f"{base_url}/orders",
            json=payload,
            headers=cashfree_headers(),
            timeout=20
        )

        if response.status_code not in (200, 201):
            app.logger.error(
                "Cashfree create order failed. HTTP %s",
                response.status_code
            )
            with psycopg.connect(get_database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE orders
                        SET payment_status = 'FAILED'
                        WHERE order_id = %s
                    """, (order_id,))
            return jsonify({
                "error": "Could not start payment. Please try again."
            }), 502

        cashfree_data = response.json()
        payment_session_id = cashfree_data.get("payment_session_id")

        if not payment_session_id:
            raise RuntimeError("Cashfree did not return a payment session")

        with psycopg.connect(get_database_url()) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE orders
                    SET payment_session_id = %s
                    WHERE order_id = %s
                """, (payment_session_id, order_id))

        return jsonify({
            "status": "success",
            "order_id": order_id,
            "payment_session_id": payment_session_id,
            "environment": os.environ.get("CASHFREE_ENV", "sandbox")
        })

    except Exception:
        app.logger.exception("Create order failed")
        return jsonify({
            "error": "Unable to create payment order. Please check server logs."
        }), 500


@app.route("/payment-return")
def payment_return():
    order_id = request.args.get("order_id", "").strip()

    if not order_id or len(order_id) > 50:
        return "Invalid order reference.", 400

    try:
        order = get_order_from_database(order_id)

        if not order:
            return "Order not found.", 404

        cashfree_order = verify_cashfree_order(order_id)

        expected_amount = Decimal(str(order[5])).quantize(Decimal("0.01"))
        received_amount = Decimal(
            str(cashfree_order.get("order_amount", "0"))
        ).quantize(Decimal("0.01"))

        if (
            cashfree_order.get("order_status") == "PAID"
            and cashfree_order.get("order_currency") == "INR"
            and received_amount == expected_amount
        ):
            mark_order_paid(order_id)

            return render_template_string("""
                <!doctype html>
                <html lang="en">
                <head>
                  <meta name="viewport" content="width=device-width, initial-scale=1">
                  <title>Payment Successful | Learners Page</title>
                  <style>
                    body {font-family: Arial,sans-serif; background:#f4f7f3;
                    display:grid; place-items:center; min-height:90vh; margin:0;}
                    main {background:white; padding:35px; border-radius:18px;
                    max-width:420px; text-align:center; box-shadow:0 8px 30px #0001;}
                    h1 {color:#176b4d;} p {color:#526258; line-height:1.6;}
                    a {display:inline-block; background:#176b4d; color:white;
                    padding:12px 22px; border-radius:9px; text-decoration:none;}
                  </style>
                </head>
                <body>
                  <main>
                    <h1>Payment Successful!</h1>
                    <p>Your payment has been verified by Cashfree.</p>
                    <p>Order ID: {{ order_id }}</p>
                    <p>Your eBook delivery is not activated yet. Please keep your order ID.</p>
                    <a href="/">Back to Learners Page</a>
                  </main>
                </body>
                </html>
            """, order_id=order_id)

        return render_template_string("""
            <!doctype html>
            <html lang="en">
            <head><meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Payment Pending | Learners Page</title></head>
            <body style="font-family:Arial,sans-serif;text-align:center;padding:50px;">
              <h2>Payment not confirmed yet</h2>
              <p>Your order status is: {{ status }}</p>
              <p>If money was deducted, please contact support with your order ID.</p>
              <p>Order ID: {{ order_id }}</p>
              <a href="/">Back to Learners Page</a>
            </body>
            </html>
        """, status=cashfree_order.get("order_status", "UNKNOWN"),
             order_id=order_id)

    except Exception:
        app.logger.exception("Payment return verification failed")
        return (
            "We could not verify the payment right now. "
            "Please contact support with your order ID.",
            502
        )


@app.route("/email-test")
def email_test():
    supplied_token = request.args.get("token", "")
    expected_token = os.environ.get("EMAIL_TEST_TOKEN", "")
    api_key = os.environ.get("RESEND_API_KEY", "")
    recipient = os.environ.get("EMAIL_TEST_TO", "")

    if not expected_token or not hmac.compare_digest(
        supplied_token, expected_token
    ):
        return {
            "status": "error",
            "message": "Unauthorized"
        }, 401

    if not api_key or not recipient:
        return {
            "status": "error",
            "message": "Email environment variables are missing."
        }, 503

    try:
        resend.api_key = api_key

        result = resend.Emails.send({
            "from": "Learners Page <onboarding@resend.dev>",
            "to": [recipient],
            "subject": "Learners Page email test",
            "text": (
                "Hello! Your Learners Page website has "
                "successfully connected to Resend."
            )
        })

        email_id = (
            result.get("id")
            if isinstance(result, dict)
            else getattr(result, "id", None)
        )

        return {
            "status": "success",
            "message": "Resend accepted the test email.",
            "email_id": email_id
        }

    except Exception:
        app.logger.exception("Resend email test failed")
        return {
            "status": "error",
            "message": (
                "Email sending failed. Check Resend dashboard "
                "and Render logs."
            )
        }, 502


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )
