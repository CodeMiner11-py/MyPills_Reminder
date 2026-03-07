import os
import base64
import json
from datetime import datetime
from flask import Flask, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
import resend
import pytz

app = Flask(__name__)

# ── Firebase init ──────────────────────────────────────────────────────────────
def init_firebase():
    if not firebase_admin._apps:
        key_b64 = os.environ.get("FIREBASE_KEY")
        if not key_b64:
            raise RuntimeError("FIREBASE_KEY env variable not set")
        key_dict = json.loads(base64.b64decode(key_b64).decode("utf-8"))
        cred = credentials.Certificate(key_dict)
        firebase_admin.initialize_app(cred)

init_firebase()
db = firestore.client()

# ── Resend init ────────────────────────────────────────────────────────────────
resend.api_key = os.environ.get("RESEND_KEY")

# ── Constants ──────────────────────────────────────────────────────────────────
WEEK_DAYS   = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]
MONTH_NAMES = ["January","February","March","April","May","June","July",
               "August","September","October","November","December"]

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_template(filename):
    with open(os.path.join(TEMPLATES_DIR, filename), "r") as f:
        return f.read()

def fill_template(html, **kwargs):
    for key, value in kwargs.items():
        html = html.replace("{{" + key + "}}", str(value))
    return html

def fmt_time(t):
    """'HH:MM' -> '8:30 AM'"""
    if not t:
        return ""
    h, m = int(str(t)[:2]), int(str(t)[3:5])
    ampm = "AM" if h < 12 else "PM"
    h12  = h % 12 or 12
    return f"{h12}:{str(m).zfill(2)} {ampm}"

def date_key(dt):
    """datetime -> 'MM/DD/YYYY'"""
    return dt.strftime("%m/%d/%Y")

def get_user_now(timezone_str):
    """Returns current datetime in user's timezone."""
    try:
        tz = pytz.timezone(timezone_str)
    except Exception:
        tz = pytz.UTC
    return datetime.now(tz)

def minutes_until_dose(dose_entry, unit, now):
    """
    Returns how many minutes until this dose is due, based on current time.
    Only considers HH:MM for DAILY/WEEKLY/MONTHLY/YEARLY.
    For HOURLY, uses minutes-past-the-hour.
    Returns None if not applicable today.
    """
    if not dose_entry:
        return None

    t = str(dose_entry.get("time", ""))

    if unit == "HOURLY":
        try:
            scheduled_min = int(t)
        except (ValueError, TypeError):
            return None
        diff = scheduled_min - now.minute
        if diff < 0:
            diff += 60
        return diff

    # Check day/month/year constraints first
    if unit == "WEEKLY":
        day_name = WEEK_DAYS[now.weekday() + 1 if now.weekday() < 6 else 0]
        # Python weekday: 0=Mon, but our array is Sun-based
        day_name = WEEK_DAYS[now.isoweekday() % 7]
        if dose_entry.get("day") != day_name:
            return None

    elif unit == "MONTHLY":
        if int(dose_entry.get("day", -1)) != now.day:
            return None

    elif unit == "YEARLY":
        if int(dose_entry.get("month", -1)) != now.month:
            return None
        if int(dose_entry.get("day", -1)) != now.day:
            return None

    # Parse HH:MM
    if not t or ":" not in t:
        return None
    h, m = int(t[:2]), int(t[3:5])
    scheduled_minutes = h * 60 + m
    current_minutes   = now.hour * 60 + now.minute
    diff = scheduled_minutes - current_minutes
    return diff  # negative means it's already past

def already_sent(uid, med_id, dose, day_str, reminder_type):
    """Check if we already sent this reminder today."""
    logs = (
        db.collection("users").document(uid)
        .collection("reminderLogs")
        .where("medicationId", "==", med_id)
        .where("dose", "==", dose)
        .where("day", "==", day_str)
        .where("type", "==", reminder_type)
        .limit(1)
        .get()
    )
    return len(logs) > 0

def log_reminder(uid, med_id, dose, day_str, reminder_type):
    """Save a reminder log so we don't send it again."""
    db.collection("users").document(uid).collection("reminderLogs").add({
        "medicationId": med_id,
        "dose": dose,
        "day": day_str,
        "type": reminder_type,
        "sentAt": firestore.SERVER_TIMESTAMP
    })

def send_reminder(user_email, user_name, med, dose_index, reminder_type, now):
    """Build and send the reminder email."""
    unit       = med["schedule"][1] if isinstance(med.get("schedule"), list) else "DAILY"
    total      = med["schedule"][0] if isinstance(med.get("schedule"), list) else 1
    times      = med.get("times", [])
    dose_entry = times[dose_index - 1] if len(times) >= dose_index else {}
    dosage     = f"{med['dosage'][0]} {med['dosage'][1]}" if isinstance(med.get("dosage"), list) else ""
    dose_label = f"Dose {dose_index} of {total}" if total > 1 else "Single dose"

    # Build time label
    t = str(dose_entry.get("time", ""))
    if unit == "HOURLY":
        time_label = f":{t.zfill(2)} past each hour"
    else:
        time_label = fmt_time(t)

    template_file = "reminder_15.html" if reminder_type == "15MIN" else "reminder_5.html"
    html = load_template(template_file)
    html = fill_template(
        html,
        user_name=user_name,
        medication_name=med.get("name", ""),
        dosage=dosage,
        dose_time=time_label,
        dose_number=dose_label
    )

    subject = (
        f"⏰ {med.get('name', 'Medication')} in 15 minutes"
        if reminder_type == "15MIN"
        else f"🚨 {med.get('name', 'Medication')} due in 5 minutes"
    )

    resend.Emails.send({
        "from": "MyPills <reminders@mypills.kidslearninglab.com>",
        "to": user_email,
        "subject": subject,
        "html": html
    })

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "MyPills Reminder Service"})

@app.route("/remind", methods=["GET", "POST"])
def remind():
    sent_count   = 0
    skip_count   = 0
    errors       = []

    users = db.collection("users").get()

    for user_doc in users:
        uid  = user_doc.id
        data = user_doc.to_dict() or {}

        user_email   = data.get("email")
        user_name    = data.get("displayName") or data.get("email", "there")
        timezone_str = data.get("timezone", "UTC")

        if not user_email:
            continue

        now     = get_user_now(timezone_str)
        day_str = date_key(now)

        try:
            meds = db.collection("users").document(uid).collection("medications").get()
        except Exception as e:
            errors.append(f"uid={uid} meds fetch error: {e}")
            continue

        for med_doc in meds:
            med    = med_doc.to_dict() or {}
            med_id = med_doc.id

            # Skip _init placeholder docs
            if med_id == "_init" or not med.get("name"):
                continue

            # Check medication is active today
            def to_dt(v):
                if not v:
                    return None
                return v if isinstance(v, datetime) else None

            start  = med.get("startDate")
            stop   = med.get("stopDate")
            expiry = med.get("expiryDate")

            # Python Firestore SDK returns datetime directly
            to_dt = lambda v: v if isinstance(v, datetime) else None
            start_dt  = to_dt(start)
            stop_dt   = to_dt(stop)
            expiry_dt = to_dt(expiry)

            today = now.date()
            if start_dt  and today < start_dt.date():  continue
            if stop_dt   and today > stop_dt.date():   continue
            if expiry_dt and today > expiry_dt.date(): continue

            schedule = med.get("schedule", [1, "DAILY"])
            unit     = schedule[1] if len(schedule) > 1 else "DAILY"
            total    = int(schedule[0]) if schedule else 1
            times    = med.get("times", [])

            for dose_index in range(1, total + 1):
                dose_entry = times[dose_index - 1] if len(times) >= dose_index else {}
                diff = minutes_until_dose(dose_entry, unit, now)

                if diff is None:
                    continue

                for reminder_type, window_min, window_max in [("15MIN", 14, 16), ("5MIN", 4, 6)]:
                    if window_min <= diff <= window_max:
                        if already_sent(uid, med_id, dose_index, day_str, reminder_type):
                            skip_count += 1
                            continue
                        try:
                            send_reminder(user_email, user_name, med, dose_index, reminder_type, now)
                            log_reminder(uid, med_id, dose_index, day_str, reminder_type)
                            sent_count += 1
                        except Exception as e:
                            errors.append(f"uid={uid} med={med_id} dose={dose_index} {reminder_type}: {e}")

    return jsonify({
        "status": "done",
        "sent": sent_count,
        "skipped_already_sent": skip_count,
        "errors": errors
    })

@app.route("/test")
def test_email():
    try:
        html = load_template("reminder_15.html")
        html = fill_template(
            html,
            user_name="Eshaan",
            medication_name="Metformin",
            dosage="500 MG",
            dose_time="8:00 AM",
            dose_number="Dose 1 of 2"
        )
        resend.Emails.send({
            "from": "MyPills <reminders@mypills.kidslearninglab.com>",
            "to": "hello@kidslearninglab.com",
            "subject": "⏰ Test – Metformin in 15 minutes",
            "html": html
        })
        return jsonify({"status": "sent", "to": "hello@kidslearninglab.com"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=False)