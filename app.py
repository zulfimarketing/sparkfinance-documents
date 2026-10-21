import os, io, re, json, uuid, shutil, sqlite3, secrets
from datetime import datetime, timedelta
from flask import Flask, request, session, redirect, render_template, send_file, jsonify, abort, Response

BASE = os.path.dirname(os.path.abspath(__file__))
DB, UP = os.path.join(BASE, "portal.db"), os.path.join(BASE, "uploads")
os.makedirs(UP, exist_ok=True)
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")
app.config.update(MAX_CONTENT_LENGTH=60 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# ---- EDIT HERE ----
FIELDS = [  # key, label, kind, required
    ("name", "Full name", "text", True),
    ("email", "Email address", "email", True),
    ("photo_in", "Business photo: inside (stock, dining or working area)", "file", True),
    ("photo_out", "Business photo: outside (shop board or name visible)", "file", True),
    ("bank", "Latest bank statement (account you want the loan in)", "file", True),
    ("id_front", "Director ID front (driving licence or passport)", "file", True),
    ("id_back", "Director ID back (not needed for a passport)", "file", False),
]
ALLOWED, MAX_FILE = {"pdf", "jpg", "jpeg", "png"}, 10 * 1024 * 1024
DEFAULT_OPEN_MIN, AUTO_DELETE_HOURS = 30, 2
# -------------------
LABELS = {k: l for k, l, kind, r in FIELDS if kind == "file"}

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY, status TEXT, customer TEXT, expires TEXT, submitted TEXT, folder TEXT, name TEXT, email TEXT, files TEXT)")
    c.execute("INSERT OR IGNORE INTO state VALUES(1,'closed','','','','','','','{}')")
    return c

def wipe(c):
    f = c.execute("SELECT folder FROM state").fetchone()[0]
    if f: shutil.rmtree(os.path.join(UP, f), ignore_errors=True)
    c.execute("UPDATE state SET status='closed', folder='', name='', email='', files='{}' WHERE id=1"); c.commit()

def state():
    c = db(); s = dict(c.execute("SELECT * FROM state").fetchone()); now = datetime.utcnow()
    if s["status"] == "open" and now > datetime.fromisoformat(s["expires"]):
        c.execute("UPDATE state SET status='closed' WHERE id=1 AND status='open'"); c.commit(); s["status"] = "closed"
    if s["status"] == "submitted" and now > datetime.fromisoformat(s["submitted"]) + timedelta(hours=AUTO_DELETE_HOURS):
        wipe(c); s["status"] = "closed"
    c.close(); s["files"] = json.loads(s["files"] or "{}"); return s

def page(code=200, **kw):
    return render_template("portal.html", fields=FIELDS, is_open=state()["status"] == "open", **kw), code

@app.after_request
def headers(r):
    r.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"; r.headers["Cache-Control"] = "no-store"; return r

@app.route("/robots.txt")
def robots(): return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

@app.route("/", methods=["GET", "POST"])
def portal():
    if request.method == "GET": return page()
    if state()["status"] != "open": return page(403)
    name, email = request.form.get("name", "").strip()[:80], request.form.get("email", "").strip()[:120]
    if not name or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return page(400, error="Enter your full name and a valid email address.")
    folder = uuid.uuid4().hex; path = os.path.join(UP, folder); os.makedirs(path); saved = {}
    try:
        for key, label, kind, req in FIELDS:
            if kind != "file": continue
            f = request.files.get(key)
            if not f or not f.filename:
                if req: raise ValueError(label)
                continue
            ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
            data = f.read()
            if ext not in ALLOWED or not data or len(data) > MAX_FILE: raise ValueError(label)
            saved[key] = f"{key}.{ext}"
            with open(os.path.join(path, saved[key]), "wb") as out: out.write(data)
    except ValueError as e:
        shutil.rmtree(path, ignore_errors=True)
        return page(400, error=f"Check “{e}”: upload a PDF, JPG or PNG up to 10 MB.")
    c = db()  # first submit wins and closes the portal in the same step
    cur = c.execute("UPDATE state SET status='submitted', submitted=?, folder=?, name=?, email=?, files=? WHERE id=1 AND status='open'",
                    (datetime.utcnow().isoformat(), folder, name, email, json.dumps(saved)))
    c.commit(); c.close()
    if cur.rowcount == 0:
        shutil.rmtree(path, ignore_errors=True); return page(403)
    return page(done=True)

def guard():
    if not session.get("a"): abort(401)

@app.route("/admin", methods=["GET", "POST"])
def admin():
    err = ""
    if request.method == "POST" and "password" in request.form:
        if secrets.compare_digest(request.form["password"], ADMIN_PASSWORD): session["a"] = True; return redirect("/admin")
        err = "Wrong password."
    if not session.get("a"): return render_template("admin.html", login=True, err=err)
    return render_template("admin.html", login=False, s=state(), labels=LABELS, hours=AUTO_DELETE_HOURS, mins=DEFAULT_OPEN_MIN)

@app.post("/admin/open")
def open_portal():
    guard()
    if state()["status"] == "submitted": return redirect("/admin")
    m = min(max(int(request.form.get("minutes") or DEFAULT_OPEN_MIN), 5), 240); c = db()
    c.execute("UPDATE state SET status='open', customer=?, expires=?, submitted='' WHERE id=1",
              (request.form.get("customer", "").strip()[:80], (datetime.utcnow() + timedelta(minutes=m)).isoformat()))
    c.commit(); return redirect("/admin")

@app.post("/admin/close")
def close_portal():
    guard(); c = db(); c.execute("UPDATE state SET status='closed' WHERE id=1 AND status='open'"); c.commit(); return redirect("/admin")

@app.get("/admin/status")
def status(): guard(); return jsonify(status=state()["status"])

@app.get("/admin/file/<key>")
def get_file(key):  # original file, untouched; ?dl=1 forces download
    guard(); s = state(); fn = s["files"].get(key)
    if s["status"] != "submitted" or not fn: abort(404)
    ext = fn.rsplit(".", 1)[1]; who = re.sub(r"[^A-Za-z0-9]+", "-", s["name"]).strip("-") or "customer"
    return send_file(os.path.join(UP, s["folder"], fn), as_attachment=bool(request.args.get("dl")),
                     download_name=f"{who}_{key}.{ext}")

@app.post("/admin/confirm")
def confirm():
    guard(); c = db(); wipe(c); return redirect("/admin")

if __name__ == "__main__": app.run(debug=True)
