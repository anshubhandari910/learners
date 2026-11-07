from flask import Flask, render_template, request, jsonify, render_template_string, redirect, url_for
import os
import hmac
import hashlib
import html
import base64
import logging
import re
import uuid
import secrets
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode

import psycopg
import resend
import requests

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

CASHFREE_API_VERSION = "2023-08-01"
STORAGE_BUCKET = os.environ.get("SUPABASE_BUCKET", "file_uploads")
DOWNLOAD_LINK_TTL_SECONDS = 600  # Supabase file URL is valid for 10 minutes.


# -----------------------------------------
# CONFIGURATION
# -----------------------------------------

def get_database_url():
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")
    return value


def get_site_url():
    value = os.environ.get("SITE_URL", "").rstrip("/")
    if not value.startswith("https://"):
        raise RuntimeError("SITE_URL must be configured with https://")
    return value


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


def get_supabase_config():
    project_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not project_url or not service_key:
        raise RuntimeError("Supabase server credentials are not configured")

    if not project_url.startswith("https://"):
        raise RuntimeError("SUPABASE_URL must use HTTPS")

    return project_url, service_key


# -----------------------------------------
# DATABASE
# -----------------------------------------

def get_books():
    with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, description, price
                FROM public.books
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
    with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT order_id, book_id, customer_name,
                       customer_email, customer_phone, amount,
                       payment_status
                FROM public.orders
                WHERE order_id = %s
            """, (order_id,))
            return cur.fetchone()


def mark_order_paid(order_id):
    """Mark an order paid once and return its persistent download token."""
    new_token = secrets.token_urlsafe(32)

    with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.orders
                SET payment_status = 'PAID',
                    paid_at = COALESCE(paid_at, NOW()),
                    download_token = COALESCE(download_token, %s)
                WHERE order_id = %s
                  AND payment_status <> 'PAID'
                RETURNING download_token
            """, (new_token, order_id))
            row = cur.fetchone()

            if row:
                return row[0]

            cur.execute("""
                SELECT download_token
                FROM public.orders
                WHERE order_id = %s AND payment_status = 'PAID'
            """, (order_id,))
            row = cur.fetchone()
            return row[0] if row else None


def get_paid_order_for_delivery(order_id):
    with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.order_id, o.customer_name, o.customer_email,
                       o.payment_status, o.download_token,
                       b.title, b.file_path, o.email_sent_at
                FROM public.orders o
                JOIN public.books b ON b.id = o.book_id
                WHERE o.order_id = %s
            """, (order_id,))
            return cur.fetchone()


# -----------------------------------------
# CASHFREE PAYMENT VERIFICATION
# -----------------------------------------

def verify_cashfree_order(order_id):
    _, _, base_url = get_cashfree_config()
    response = requests.get(
        f"{base_url}/orders/{quote(order_id, safe='')}",
        headers=cashfree_headers(),
        timeout=20
    )

    if response.status_code != 200:
        app.logger.error(
            "Cashfree verification error HTTP %s: %s",
            response.status_code, response.text[:1000]
        )
        raise RuntimeError("Unable to verify payment with Cashfree")

    return response.json()


def verify_paid_order_against_database(order_id):
    """Trust Cashfree's server response, then compare it with our DB order."""
    db_order = get_order_from_database(order_id)
    if not db_order:
        return None

    cf_order = verify_cashfree_order(order_id)

    try:
        expected_amount = Decimal(str(db_order[5])).quantize(Decimal("0.01"))
        received_amount = Decimal(
            str(cf_order.get("order_amount", "0"))
        ).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        app.logger.warning("Invalid amount in Cashfree response for %s", order_id)
        return None

    if (
        cf_order.get("order_status") != "PAID"
        or cf_order.get("order_currency") != "INR"
        or received_amount != expected_amount
    ):
        return None

    return mark_order_paid(order_id)


# -----------------------------------------
# CUSTOMER EMAIL DELIVERY
# -----------------------------------------

def send_customer_delivery_email(order_id):
    """
    Send only when EMAIL_FROM is configured to a verified Resend domain.
    Without EMAIL_FROM, the secure website download page remains available.
    """
    api_key = os.environ.get("RESEND_API_KEY", "")
    sender = os.environ.get("EMAIL_FROM", "").strip()

    if not api_key or not sender:
        app.logger.info(
            "Customer email not sent for %s: configure RESEND_API_KEY and "
            "EMAIL_FROM after verifying a sending domain.",
            order_id
        )
        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE public.orders
                    SET email_error = %s
                    WHERE order_id = %s AND email_sent_at IS NULL
                """, ("EMAIL_FROM or RESEND_API_KEY is not configured", order_id))
        return False

    order = get_paid_order_for_delivery(order_id)
    if not order or order[3] != "PAID" or not order[4]:
        return False

    _, customer_name, customer_email, _, token, book_title, _, email_sent_at = order
    if email_sent_at:
        return True

    download_page = f"{get_site_url()}/download/{quote(token, safe='')}"
    safe_customer_name = html.escape(str(customer_name or ""))
    safe_book_title = html.escape(str(book_title or ""))

    try:
        resend.api_key = api_key
        resend.Emails.send({
            "from": sender,
            "to": [customer_email],
            "subject": f"Your eBook is ready: {book_title}",
            "html": f"""
                <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;line-height:1.6">
                  <h2>Thank you for your purchase, {safe_customer_name}!</h2>
                  <p>Your payment has been verified successfully.</p>
                  <p><strong>{safe_book_title}</strong> is ready to download.</p>
                  <p><a href="{download_page}" style="display:inline-block;background:#176b4d;color:#fff;padding:13px 22px;border-radius:8px;text-decoration:none">Download your eBook</a></p>
                  <p>If the button does not work, open this link:</p>
                  <p>{download_page}</p>
                  <p>Thank you for choosing Learners Page.</p>
                </div>
            """
        })

        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE public.orders
                    SET email_sent_at = NOW(), email_error = NULL
                    WHERE order_id = %s AND email_sent_at IS NULL
                """, (order_id,))
        return True

    except Exception as exc:
        app.logger.exception("Customer email failed for order %s", order_id)
        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE public.orders
                    SET email_error = %s
                    WHERE order_id = %s AND email_sent_at IS NULL
                """, (str(exc)[:1000], order_id))
        return False


def activate_paid_order(order_id):
    token = verify_paid_order_against_database(order_id)
    if token:
        send_customer_delivery_email(order_id)
    return token


# -----------------------------------------
# SUPABASE PRIVATE STORAGE DOWNLOAD
# -----------------------------------------

def create_supabase_signed_download(file_path):
    if not file_path:
        raise RuntimeError("This eBook does not have a Storage file path")

    project_url, service_key = get_supabase_config()
    encoded_path = quote(str(file_path).lstrip("/"), safe="/")
    endpoint = (
        f"{project_url}/storage/v1/object/sign/"
        f"{quote(STORAGE_BUCKET, safe='')}/{encoded_path}"
    )

    response = requests.post(
        endpoint,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json"
        },
        json={"expiresIn": DOWNLOAD_LINK_TTL_SECONDS},
        timeout=20
    )

    if response.status_code not in (200, 201):
        app.logger.error(
            "Supabase signed URL error HTTP %s: %s",
            response.status_code, response.text[:1000]
        )
        raise RuntimeError("Could not create a secure download link")

    data = response.json()
    signed_path = data.get("signedURL") or data.get("signedUrl")
    if not signed_path:
        raise RuntimeError("Supabase did not return a signed URL")

    if signed_path.startswith("https://"):
        signed_url = signed_path
    else:
        signed_url = f"{project_url}/storage/v1{signed_path}"

    # Ask Storage to return the file as an attachment.
    separator = "&" if "?" in signed_url else "?"
    signed_url += separator + urlencode({"download": os.path.basename(file_path)})
    return signed_url


# -----------------------------------------
# HOME PAGE AND BASIC TESTS
# -----------------------------------------

@app.route("/")
def home():
    try:
        return render_template("index.html", books=get_books())
    except Exception:
        app.logger.exception("Unable to load books")
        return render_template("index.html", books=[], db_error=True), 503


@app.route("/health")
def health():
    return {"status": "ok", "app": "Learners Page"}


@app.route("/db-test")
def db_test():
    try:
        return {
            "status": "success",
            "app": "Learners Page",
            "books": get_books()
        }
    except Exception:
        app.logger.exception("Database test failed")
        return {
            "status": "error",
            "message": "Database test failed. Check Render logs."
        }, 500


@app.route("/storage-test")
def storage_test():
    # Does not reveal credentials or create a public URL.
    try:
        get_supabase_config()
        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*)
                    FROM public.books
                    WHERE is_active = TRUE
                      AND file_path IS NOT NULL
                      AND file_path <> ''
                """)
                count = cur.fetchone()[0]
        return {
            "status": "success",
            "supabase_configured": True,
            "active_books_with_file_paths": count,
            "bucket": STORAGE_BUCKET
        }
    except Exception:
        app.logger.exception("Storage configuration test failed")
        return {
            "status": "error",
            "message": "Storage test failed. Check Render logs."
        }, 500


# -----------------------------------------
# CREATE CASHFREE PAYMENT ORDER
# -----------------------------------------

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

    order_id = "LP_" + uuid.uuid4().hex[:24]

    try:
        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, title, price
                    FROM public.books
                    WHERE id = %s AND is_active = TRUE
                """, (int(book_id),))
                book = cur.fetchone()

        if not book:
            return jsonify({"error": "This eBook is unavailable."}), 404

        actual_book_id, book_title, book_price = book
        amount = Decimal(str(book_price)).quantize(Decimal("0.01"))
        if amount <= 0:
            return jsonify({"error": "Invalid book price."}), 400

        site_url = get_site_url()
        return_url = f"{site_url}/payment-return?order_id={{order_id}}"
        notify_url = f"{site_url}/cashfree-webhook"

        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO public.orders (
                        order_id, book_id, customer_name, customer_email,
                        customer_phone, amount, payment_status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
                """, (
                    order_id, actual_book_id, customer_name,
                    customer_email, customer_phone, amount
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
                "return_url": return_url,
                "notify_url": notify_url
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
                "Cashfree create order failed HTTP %s: %s",
                response.status_code, response.text[:1000]
            )
            with psycopg.connect(get_database_url()) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE public.orders
                        SET payment_status = 'FAILED'
                        WHERE order_id = %s AND payment_status = 'PENDING'
                    """, (order_id,))
            return jsonify({
                "error": "Could not start payment. Please try again."
            }), 502

        cashfree_data = response.json()
        payment_session_id = cashfree_data.get("payment_session_id")
        if not payment_session_id:
            raise RuntimeError("Cashfree did not return a payment session.")

        with psycopg.connect(get_database_url()) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE public.orders
                    SET payment_session_id = %s
                    WHERE order_id = %s
                """, (payment_session_id, order_id))

        return jsonify({
            "status": "success",
            "order_id": order_id,
            "payment_session_id": payment_session_id,
            "environment": os.environ.get("CASHFREE_ENV", "sandbox").lower()
        })

    except requests.RequestException:
        app.logger.exception("Network error while connecting to Cashfree")
        return jsonify({
            "error": "Payment service connection failed. Please try again."
        }), 502
    except Exception:
        app.logger.exception("Create order failed")
        return jsonify({
            "error": "Unable to create payment order. Check server logs."
        }), 500


# -----------------------------------------
# CASHFREE WEBHOOK
# -----------------------------------------

def verify_cashfree_webhook_signature(raw_body):
    signature = request.headers.get("x-webhook-signature", "")
    timestamp = request.headers.get("x-webhook-timestamp", "")
    client_secret = os.environ.get("CASHFREE_CLIENT_SECRET", "")

    if not signature or not timestamp or not client_secret:
        return False

    signed_payload = timestamp.encode("utf-8") + raw_body
    expected = base64.b64encode(
        hmac.new(
            client_secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256
        ).digest()
    ).decode("utf-8")

    return hmac.compare_digest(expected, signature)


@app.route("/cashfree-webhook", methods=["POST"])
def cashfree_webhook():
    raw_body = request.get_data(cache=True)

    if not verify_cashfree_webhook_signature(raw_body):
        app.logger.warning("Rejected Cashfree webhook: invalid signature")
        return {"status": "error", "message": "Invalid signature"}, 401

    try:
        event = request.get_json(silent=True) or {}
        event_type = str(event.get("type", ""))

        # Only process successful payment events. Other events are acknowledged.
        if event_type != "PAYMENT_SUCCESS_WEBHOOK":
            return {"status": "ignored", "event": event_type}, 200

        data = event.get("data") or {}
        order_data = data.get("order") or {}
        order_id = str(order_data.get("order_id", "")).strip()

        if not order_id or len(order_id) > 50:
            return {"status": "error", "message": "Missing order ID"}, 400

        token = activate_paid_order(order_id)
        if not token:
            # Do not mark an order paid from webhook payload alone.
            # Cashfree's order-fetch API must independently confirm it.
            app.logger.warning(
                "Webhook received but server-side order verification did not confirm %s",
                order_id
            )
            return {"status": "pending_verification"}, 200

        return {"status": "success"}, 200

    except Exception:
        app.logger.exception("Cashfree webhook processing failed")
        return {"status": "error"}, 500


# -----------------------------------------
# PAYMENT RETURN AND SERVER VERIFICATION
# -----------------------------------------

@app.route("/payment-return")
def payment_return():
    order_id = request.args.get("order_id", "").strip()
    if not order_id or len(order_id) > 50:
        return "Invalid order reference.", 400

    try:
        order = get_order_from_database(order_id)
        if not order:
            return "Order not found.", 404

        token = activate_paid_order(order_id)

        if token:
            download_page = url_for("download_page", token=token)
            return render_template_string("""
                <!doctype html>
                <html lang="en">
                <head>
                  <meta name="viewport" content="width=device-width, initial-scale=1">
                  <title>Payment Successful | Learners Page</title>
                  <style>
                    body{font-family:Arial,sans-serif;background:#f4f7f3;display:grid;place-items:center;min-height:90vh;margin:0;padding:18px}
                    main{background:white;padding:35px;border-radius:18px;max-width:440px;width:100%;box-sizing:border-box;text-align:center;box-shadow:0 8px 30px #0001}
                    h1{color:#176b4d}p{color:#526258;line-height:1.6}
                    a{display:inline-block;background:#176b4d;color:white;padding:13px 22px;border-radius:9px;text-decoration:none;margin-top:10px}
                    .muted{font-size:13px}
                  </style>
                </head>
                <body><main>
                  <h1>Payment Successful!</h1>
                  <p>Your payment has been verified by Cashfree.</p>
                  <p>Order ID: {{ order_id }}</p>
                  <a href="{{ download_page }}">Download your eBook</a>
                  <p class="muted">Your secure download link is available on this page.</p>
                  <p class="muted">If you do not receive an email, use the download button above.</p>
                  <p><a href="/" style="background:#e9efeb;color:#176b4d">Back to Learners Page</a></p>
                </main></body></html>
            """, order_id=order_id, download_page=download_page)

        return render_template_string("""
            <!doctype html><html lang="en"><head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Payment Pending | Learners Page</title></head>
            <body style="font-family:Arial,sans-serif;text-align:center;padding:50px">
              <h2>Payment not confirmed yet</h2>
              <p>Order status: {{ status }}</p>
              <p>If money was deducted, please contact support with your order ID.</p>
              <p>Order ID: {{ order_id }}</p>
              <a href="/">Back to Learners Page</a>
            </body></html>
        """, status="PENDING / NOT VERIFIED", order_id=order_id)

    except Exception:
        app.logger.exception("Payment return verification failed")
        return (
            "We could not verify the payment right now. "
            "Please contact support with your order ID.",
            502
        )


# -----------------------------------------
# SECURE CUSTOMER DOWNLOAD PAGE
# -----------------------------------------

@app.route("/download/<token>")
def download_page(token):
    if not token or len(token) > 200:
        return "Invalid download link.", 400

    try:
        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT o.payment_status, o.download_token,
                           b.title, b.file_path
                    FROM public.orders o
                    JOIN public.books b ON b.id = o.book_id
                    WHERE o.download_token = %s
                    LIMIT 1
                """, (token,))
                row = cur.fetchone()

        if not row or row[0] != "PAID" or not row[1]:
            return "This download link is invalid or payment is not confirmed.", 403

        _, _, book_title, file_path = row
        if not file_path:
            return "The eBook file is not configured. Please contact support.", 503

        return render_template_string("""
            <!doctype html>
            <html lang="en"><head>
              <meta name="viewport" content="width=device-width, initial-scale=1">
              <title>Your eBook | Learners Page</title>
              <style>
                body{font-family:Arial,sans-serif;background:#f4f7f3;display:grid;place-items:center;min-height:90vh;margin:0;padding:18px}
                main{background:white;padding:35px;border-radius:18px;max-width:440px;width:100%;box-sizing:border-box;text-align:center;box-shadow:0 8px 30px #0001}
                h1{color:#176b4d}p{color:#526258;line-height:1.6}
                button{border:0;background:#176b4d;color:white;padding:14px 24px;border-radius:9px;font-size:16px;cursor:pointer}
                a{color:#176b4d}
              </style>
            </head><body><main>
              <h1>Your eBook is ready!</h1>
              <p><strong>{{ book_title }}</strong></p>
              <p>Click below to download your purchased eBook.</p>
              <form method="post" action="{{ url_for('start_download', token=token) }}">
                <button type="submit">Download eBook (PDF)</button>
              </form>
              <p style="font-size:13px">For security, the file download link expires after 10 minutes. You can revisit this page to generate a fresh link.</p>
              <p><a href="/">Learners Page home</a></p>
            </main></body></html>
        """, book_title=book_title, token=token)

    except Exception:
        app.logger.exception("Download page failed")
        return "Unable to load your download page. Please try again later.", 500


@app.route("/download/<token>/file", methods=["GET", "POST"])
def start_download(token):
    if not token or len(token) > 200:
        return "Invalid download link.", 400

    try:
        with psycopg.connect(get_database_url(), connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT o.payment_status, o.download_token, b.file_path
                    FROM public.orders o
                    JOIN public.books b ON b.id = o.book_id
                    WHERE o.download_token = %s
                    LIMIT 1
                """, (token,))
                row = cur.fetchone()

        if not row or row[0] != "PAID" or not row[1]:
            return "This download link is invalid or payment is not confirmed.", 403

        signed_url = create_supabase_signed_download(row[2])
        return redirect(signed_url, code=302)

    except Exception:
        app.logger.exception("Secure file download failed")
        return "Could not prepare the download. Please try again later.", 502


# -----------------------------------------
# EMAIL TEST (OWNER ONLY)
# -----------------------------------------

@app.route("/email-test")
def email_test():
    supplied_token = request.args.get("token", "")
    expected_token = os.environ.get("EMAIL_TEST_TOKEN", "")
    api_key = os.environ.get("RESEND_API_KEY", "")
    recipient = os.environ.get("EMAIL_TEST_TO", "")

    if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
        return {"status": "error", "message": "Unauthorized"}, 401

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
            result.get("id") if isinstance(result, dict)
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
            "message": "Email sending failed. Check Resend dashboard and Render logs."
        }, 502


# -----------------------------------------
# RUN APP
# -----------------------------------------

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )
