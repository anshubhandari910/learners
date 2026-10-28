from flask import Flask, render_template

app = Flask(__name__)

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/health")
def health():
    return {"status": "ok", "app": "Learners Page"}
    @app.route("/db-test")
def db_test():
    import os
    import psycopg

    try:
        with psycopg.connect(
            os.environ["DATABASE_URL"],
            connect_timeout=10
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, title, price
                    FROM books
                    ORDER BY id DESC
                """)
                rows = cur.fetchall()

        return {
            "status": "success",
            "app": "Learners Page",
            "books": [
                {
                    "id": row[0],
                    "title": row[1],
                    "price": float(row[2])
                }
                for row in rows
            ]
        }

    except Exception:
        app.logger.exception("Database test failed")
        return {
            "status": "error",
            "message": "Database connection failed. Check Render logs."
        }, 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
