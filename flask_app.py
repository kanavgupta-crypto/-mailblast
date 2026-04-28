"""
MailBlast — Flask Web App (Render Edition)
Built-in background scheduler checks every 60 seconds.
Files stored in /tmp (Render free tier) — survives normal operation,
wiped only on full dyno restart (rare).
/ping endpoint keeps the app awake via UptimeRobot.
"""

import json
import os
import smtplib
import base64
import hashlib
import secrets
import threading
import time as time_module
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# On Render free tier, /tmp is writable and survives restarts within the same
# running instance. It is wiped when Render rebuilds/redeploys your app.
# For a personal tool this is fine — just re-enter SMTP credentials after deploys.
STORAGE_DIR   = os.environ.get("STORAGE_DIR", "/tmp/mailblast")
os.makedirs(STORAGE_DIR, exist_ok=True)

SETTINGS_FILE = os.path.join(STORAGE_DIR, "settings.json")
SCHEDULE_FILE = os.path.join(STORAGE_DIR, "scheduled_campaign.json")
LOG_FILE      = os.path.join(STORAGE_DIR, "scheduler.log")

DEFAULT_USER = "admin"
DEFAULT_PASS = "mailblast2024"

# ════════════════════════════════════════════════════
#  LOGGING
# ════════════════════════════════════════════════════

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ════════════════════════════════════════════════════
#  SETTINGS
# ════════════════════════════════════════════════════

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "smtp_host":    "mail.smtp2go.com",
        "smtp_port":    2525,
        "smtp_user":    "",
        "smtp_pass":    "",
        "sender_name":  "",
        "sender_email": "",
        "delay":        15,
        "login_user":   DEFAULT_USER,
        "login_pass":   hashlib.sha256(DEFAULT_PASS.encode()).hexdigest()
    }

def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def check_login(username, password):
    s = load_settings()
    return (
        username == s.get("login_user", DEFAULT_USER) and
        hashlib.sha256(password.encode()).hexdigest() == s.get(
            "login_pass", hashlib.sha256(DEFAULT_PASS.encode()).hexdigest()
        )
    )

# ════════════════════════════════════════════════════
#  SCHEDULE FILE
# ════════════════════════════════════════════════════

_schedule_lock = threading.Lock()

def load_schedule():
    if not os.path.exists(SCHEDULE_FILE):
        return None
    try:
        with open(SCHEDULE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_schedule(data):
    with _schedule_lock:
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

def delete_schedule():
    with _schedule_lock:
        if os.path.exists(SCHEDULE_FILE):
            os.remove(SCHEDULE_FILE)

# ════════════════════════════════════════════════════
#  EMAIL ENGINE
# ════════════════════════════════════════════════════

def build_message(smtp_cfg, sender, contact, subject, body_tpl, attachments):
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = f"{sender['name']} <{sender['email']}>"
    msg["To"]      = contact["email"]

    body = body_tpl \
        .replace("{name}",        contact.get("name", "")) \
        .replace("{company}",     contact.get("company", "")) \
        .replace("{sender_name}", sender.get("name", ""))
    msg.attach(MIMEText(body, "plain"))

    for att in attachments:
        if isinstance(att, dict) and "data" in att:
            try:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(base64.b64decode(att["data"]))
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{att["name"]}"'
                )
                msg.attach(part)
            except Exception as e:
                log(f"  Attachment error ({att.get('name','')}): {e}")
    return msg


def send_one(smtp_cfg, sender, contact, subject, body_tpl, attachments):
    msg    = build_message(smtp_cfg, sender, contact, subject, body_tpl, attachments)
    server = smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg["port"]), timeout=20)
    server.starttls()
    server.login(smtp_cfg["user"], smtp_cfg["pass"])
    server.sendmail(sender["email"], contact["email"], msg.as_string())
    server.quit()


def run_campaign(campaign):
    contacts    = campaign.get("contacts", [])
    delay       = int(campaign.get("delay", 15))
    smtp_cfg    = campaign["smtp"]
    sender      = campaign["sender"]
    subject     = campaign["subject"]
    body_tpl    = campaign.get("body", "")
    attachments = campaign.get("attachments", [])

    log(f"CAMPAIGN START — {len(contacts)} recipients | subject: {subject}")
    sent = failed = 0

    for i, contact in enumerate(contacts):
        try:
            send_one(smtp_cfg, sender, contact, subject, body_tpl, attachments)
            log(f"  SENT [{i+1}/{len(contacts)}] {contact.get('name','')} <{contact['email']}>")
            sent += 1
        except Exception as e:
            log(f"  FAIL [{i+1}/{len(contacts)}] {contact['email']} — {e}")
            failed += 1
        if i < len(contacts) - 1:
            time_module.sleep(delay)

    log(f"CAMPAIGN DONE — Sent: {sent} | Failed: {failed}")
    delete_schedule()

# ════════════════════════════════════════════════════
#  BACKGROUND SCHEDULER
#  Wakes every 60 seconds and checks for due campaigns.
#  Maximum possible delay = 59 seconds.
# ════════════════════════════════════════════════════

_campaign_running = False
_campaign_lock    = threading.Lock()


def scheduler_loop():
    global _campaign_running
    log("Scheduler started — checking every 60 seconds")

    while True:
        time_module.sleep(60)

        campaign = load_schedule()
        if not campaign:
            continue

        send_at_str = campaign.get("send_at")
        if not send_at_str:
            delete_schedule()
            continue

        try:
            send_at = datetime.fromisoformat(send_at_str)
        except ValueError:
            log(f"Bad send_at: {send_at_str} — clearing")
            delete_schedule()
            continue

        now = datetime.now()

        if now >= send_at:
            with _campaign_lock:
                if _campaign_running:
                    log("Campaign already running — skipping")
                    continue
                _campaign_running = True

            log(f"Trigger! Scheduled {send_at} — launching now")

            def run_and_clear():
                global _campaign_running
                try:
                    run_campaign(campaign)
                finally:
                    with _campaign_lock:
                        _campaign_running = False

            threading.Thread(target=run_and_clear, daemon=True).start()
        else:
            diff = int((send_at - now).total_seconds() / 60)
            log(f"Pending — {diff} min until {send_at.strftime('%a %d %b %H:%M')}")


# ════════════════════════════════════════════════════
#  ROUTES — Keep-alive ping for UptimeRobot
#  UptimeRobot pings /ping every 5 minutes.
#  Render free tier sleeps after 15 min inactivity —
#  this keeps it permanently awake at zero extra cost.
# ════════════════════════════════════════════════════


# ════════════════════════════════════════════════════
#  ROUTES — Auth
# ════════════════════════════════════════════════════
@app.route("/ping")
def ping():
    return "OK", 200

@app.route("/")
def index():
    if not session.get("logged_in"):
        return redirect(url_for("login"))
    return render_template("app.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if check_login(username, password):
            session["logged_in"] = True
            session.permanent    = True
            app.permanent_session_lifetime = timedelta(days=30)
            return redirect(url_for("index"))
        error = "Incorrect username or password"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ════════════════════════════════════════════════════
#  ROUTES — Settings
# ════════════════════════════════════════════════════

@app.route("/api/settings", methods=["GET"])
def get_settings():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    s    = load_settings()
    safe = {k: v for k, v in s.items() if k not in ("login_pass", "smtp_pass")}
    safe["smtp_pass"] = "••••••••" if s.get("smtp_pass") else ""
    return jsonify({"ok": True, "settings": safe})

@app.route("/api/settings", methods=["POST"])
def update_settings():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    data = request.get_json()
    s    = load_settings()
    for key in ("smtp_host", "smtp_port", "smtp_user", "sender_name", "sender_email", "delay"):
        if key in data:
            s[key] = data[key]
    if data.get("smtp_pass") and data["smtp_pass"] != "••••••••":
        s["smtp_pass"] = data["smtp_pass"]
    if data.get("new_username"):
        s["login_user"] = data["new_username"]
    if data.get("new_password"):
        s["login_pass"] = hashlib.sha256(data["new_password"].encode()).hexdigest()
    save_settings(s)
    return jsonify({"ok": True})

# ════════════════════════════════════════════════════
#  ROUTES — Test Connection
# ════════════════════════════════════════════════════

@app.route("/api/test", methods=["POST"])
def test_connection():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    data = request.get_json()
    s    = load_settings()
    smtp_pass = data.get("smtp_pass", "")
    if smtp_pass == "••••••••" or not smtp_pass:
        smtp_pass = s["smtp_pass"]
    try:
        server = smtplib.SMTP(
            data.get("smtp_host", s["smtp_host"]),
            int(data.get("smtp_port", s["smtp_port"])),
            timeout=15
        )
        server.starttls()
        server.login(data.get("smtp_user", s["smtp_user"]), smtp_pass)
        server.quit()
        return jsonify({"ok": True})
    except smtplib.SMTPAuthenticationError:
        return jsonify({"ok": False, "error": "Authentication failed — check username and password"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ════════════════════════════════════════════════════
#  ROUTES — Send immediate
# ════════════════════════════════════════════════════

@app.route("/api/send", methods=["POST"])
def send_email():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    data = request.get_json()
    s    = load_settings()

    smtp_pass = data.get("smtp_pass", "")
    if smtp_pass == "••••••••" or not smtp_pass:
        smtp_pass = s["smtp_pass"]

    contact  = {"name": data.get("to_name",""), "email": data["to_email"], "company": data.get("to_company","")}
    smtp_cfg = {"host": data.get("smtp_host", s["smtp_host"]), "port": data.get("smtp_port", s["smtp_port"]), "user": data.get("smtp_user", s["smtp_user"]), "pass": smtp_pass}
    sender   = {"name": data.get("sender_name", s["sender_name"]), "email": data.get("sender_email", s["sender_email"])}

    try:
        send_one(smtp_cfg, sender, contact, data["subject"], data.get("body",""), data.get("attachments",[]))
        log(f"SENT  {contact['name']} <{contact['email']}>")
        return jsonify({"ok": True})
    except smtplib.SMTPAuthenticationError:
        return jsonify({"ok": False, "error": "Authentication failed — check SMTP credentials in Settings"})
    except smtplib.SMTPRecipientsRefused:
        return jsonify({"ok": False, "error": "Recipient email address was refused"})
    except Exception as e:
        log(f"FAIL  {contact['email']} — {e}")
        return jsonify({"ok": False, "error": str(e)})

# ════════════════════════════════════════════════════
#  ROUTES — Schedule
# ════════════════════════════════════════════════════

@app.route("/api/schedule", methods=["POST"])
def schedule_campaign():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    data = request.get_json()
    s    = load_settings()

    send_at_str = data.get("send_at")
    if not send_at_str:
        return jsonify({"ok": False, "error": "send_at is required"})
    try:
        send_at = datetime.fromisoformat(send_at_str)
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid date/time format"})
    if send_at <= datetime.now():
        return jsonify({"ok": False, "error": "Scheduled time must be in the future"})

    smtp_pass = data.get("smtp_pass", "")
    if smtp_pass == "••••••••" or not smtp_pass:
        smtp_pass = s["smtp_pass"]

    campaign = {
        "send_at":     send_at_str,
        "contacts":    data.get("contacts", []),
        "smtp":        {"host": data.get("smtp_host", s["smtp_host"]), "port": data.get("smtp_port", s["smtp_port"]), "user": data.get("smtp_user", s["smtp_user"]), "pass": smtp_pass},
        "sender":      {"name": data.get("sender_name", s["sender_name"]), "email": data.get("sender_email", s["sender_email"])},
        "subject":     data["subject"],
        "body":        data.get("body", ""),
        "delay":       int(data.get("delay", s.get("delay", 15))),
        "attachments": data.get("attachments", []),
    }

    save_schedule(campaign)
    mins = int((send_at - datetime.now()).total_seconds() / 60)
    log(f"SCHEDULED — {len(campaign['contacts'])} recipients at {send_at_str}")
    return jsonify({"ok": True, "message": f"Scheduled for {send_at.strftime('%a %d %b at %H:%M')} (~{mins} min from now)"})

@app.route("/api/schedule/status", methods=["GET"])
def schedule_status():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    c = load_schedule()
    if c:
        return jsonify({"ok": True, "scheduled": True, "send_at": c["send_at"], "recipients": len(c.get("contacts",[])), "subject": c.get("subject","")})
    return jsonify({"ok": True, "scheduled": False})

@app.route("/api/schedule/cancel", methods=["POST"])
def cancel_schedule():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    delete_schedule()
    log("SCHEDULE CANCELLED by user")
    return jsonify({"ok": True})

@app.route("/api/log", methods=["GET"])
def get_log():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    if not os.path.exists(LOG_FILE):
        return jsonify({"ok": True, "lines": []})
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        return jsonify({"ok": True, "lines": [l.rstrip() for l in lines[-100:]]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ════════════════════════════════════════════════════
#  START SCHEDULER ON BOOT
# ════════════════════════════════════════════════════

def start_background_scheduler():
    t = threading.Thread(target=scheduler_loop, daemon=True, name="MailBlastScheduler")
    t.start()
    log("Background scheduler thread started — checks every 60 seconds")

start_background_scheduler()

if __name__ == "__main__":
    app.run(debug=False, use_reloader=False)
