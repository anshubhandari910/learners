from flask import Flask, render_template
import os
import psycopg

app = Flask(__name__)


def get_books():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")

    with psycopg.connect(
        database_url,
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


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False
    )
