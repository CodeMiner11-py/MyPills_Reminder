import os
import base64
import json
from datetime import datetime
from flask import Flask, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
import resend
import pytz
from flask_cors import CORS


app = Flask(__name__)
CORS(app, origins=["https://mypills.kidslearninglab.com"])

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


# ── Appointment email templates (inline) ──────────────────────────────────────
def appt_email_html(reminder_type, user_name, appt_title, doctor, provider, appt_datetime_str, address):
    minutes = "15" if reminder_type == "15MIN" else "5"
    badge   = "⏰ &nbsp;15 Minutes Away" if reminder_type == "15MIN" else "🚨 &nbsp;5 Minutes — Leave Now"
    heading = f"Heads up,<br><span style=\"color:#c0392b;\">{user_name}</span>" if reminder_type == "15MIN" else f"Time to go,<br><span style=\"color:#c0392b;\">{user_name}!</span>"
    subtext = f"Your doctor appointment is in <strong style=\"color:#1a1a1a;\">{minutes} minutes</strong>. Make sure you have everything ready." if reminder_type == "15MIN" else f"Your appointment is in <strong style=\"color:#c0392b;\">{minutes} minutes</strong>. Head out now so you're not late."
    maps_url = f"https://www.google.com/maps/search/?api=1&query={address.replace(' ', '+')}"

    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Appointment Reminder – MyPills</title></head>
<body style="margin:0;padding:0;background:#f7f7f5;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f7f7f5;padding:40px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:20px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

<tr><td style="background:#c0392b;padding:28px 40px;text-align:left;">
  <table cellpadding="0" cellspacing="0"><tr>
    <td style="vertical-align:middle;"><img src="https://mypills.kidslearninglab.com/pill.png" alt="MyPills" width="42" height="42" style="display:inline-block;vertical-align:middle;margin-right:12px;border-radius:50%;background:rgba(255,255,255,0.2);"></td>
    <td style="vertical-align:middle;"><span style="color:#ffffff;font-size:22px;font-weight:700;letter-spacing:-0.5px;">MyPills</span></td>
  </tr></table>
</td></tr>

<tr><td style="padding:36px 40px 0;text-align:center;">
  <span style="display:inline-block;background:#fff0ef;color:#c0392b;font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;padding:6px 16px;border-radius:100px;">{badge}</span>
</td></tr>

<tr><td style="padding:20px 40px 8px;text-align:center;">
  <h1 style="margin:0;font-size:28px;font-weight:700;color:#1a1a1a;letter-spacing:-0.5px;line-height:1.2;">{heading}</h1>
</td></tr>

<tr><td style="padding:8px 40px 28px;text-align:center;">
  <p style="margin:0;font-size:15px;color:#888;line-height:1.6;">{subtext}</p>
</td></tr>

<tr><td style="padding:0 40px;"><div style="height:1px;background:#f0f0ee;"></div></td></tr>

<tr><td style="padding:28px 40px;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f7f7f5;border-radius:14px;padding:20px 24px;">
    <tr><td>
      <p style="margin:0 0 4px;font-size:11px;font-weight:700;color:#bbb;text-transform:uppercase;letter-spacing:0.08em;">Appointment</p>
      <p style="margin:0;font-size:20px;font-weight:700;color:#1a1a1a;letter-spacing:-0.3px;">{appt_title}</p>
    </td></tr>
    <tr><td style="padding-top:16px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="50%">
            <p style="margin:0 0 3px;font-size:11px;font-weight:700;color:#bbb;text-transform:uppercase;letter-spacing:0.08em;">Doctor</p>
            <p style="margin:0;font-size:15px;font-weight:600;color:#1a1a1a;">{doctor}</p>
          </td>
          <td width="50%">
            <p style="margin:0 0 3px;font-size:11px;font-weight:700;color:#bbb;text-transform:uppercase;letter-spacing:0.08em;">Provider</p>
            <p style="margin:0;font-size:15px;font-weight:600;color:#1a1a1a;">{provider}</p>
          </td>
        </tr>
        <tr><td colspan="2" style="padding-top:12px;">
          <p style="margin:0 0 3px;font-size:11px;font-weight:700;color:#bbb;text-transform:uppercase;letter-spacing:0.08em;">Time</p>
          <p style="margin:0;font-size:15px;font-weight:600;color:#c0392b;">{appt_datetime_str}</p>
        </td></tr>
        <tr><td colspan="2" style="padding-top:12px;">
          <p style="margin:0 0 3px;font-size:11px;font-weight:700;color:#bbb;text-transform:uppercase;letter-spacing:0.08em;">Address</p>
          <p style="margin:0;font-size:15px;font-weight:600;color:#1a1a1a;">{address}</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</td></tr>

<tr><td style="padding:0 40px 36px;">
  <table width="100%" cellpadding="0" cellspacing="0"><tr>
    <td width="48%" style="padding-right:8px;">
      <a href="https://mypills.kidslearninglab.com/appointments.html" style="display:block;text-align:center;background:#c0392b;color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;padding:14px 20px;border-radius:10px;">🩺 &nbsp;View Appointment</a>
    </td>
    <td width="48%" style="padding-left:8px;">
      <a href="{maps_url}" style="display:block;text-align:center;background:#ffffff;color:#c0392b;text-decoration:none;font-size:14px;font-weight:700;padding:14px 20px;border-radius:10px;border:1.5px solid #c0392b;">📍 &nbsp;Open in Maps</a>
    </td>
  </tr></table>
</td></tr>

<tr><td style="padding:0 40px;"><div style="height:1px;background:#f0f0ee;"></div></td></tr>
<tr><td style="padding:24px 40px;text-align:center;">
  <p style="margin:0;font-size:12px;color:#bbb;line-height:1.6;">You\'re receiving this because you have an appointment set in MyPills.<br>
  <a href="https://mypills.kidslearninglab.com" style="color:#c0392b;text-decoration:none;font-weight:600;">mypills.kidslearninglab.com</a></p>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>'''

def already_sent_appt(uid, appt_id, reminder_type):
    """Check if appt reminder already sent."""
    logs = (
        db.collection("users").document(uid)
        .collection("reminderLogs")
        .where("apptId", "==", appt_id)
        .where("type", "==", reminder_type)
        .limit(1)
        .get()
    )
    return len(logs) > 0

def log_appt_reminder(uid, appt_id, reminder_type):
    db.collection("users").document(uid).collection("reminderLogs").add({
        "apptId": appt_id,
        "type": reminder_type,
        "sentAt": firestore.SERVER_TIMESTAMP
    })

def send_appt_reminder(user_email, user_name, appt, appt_id, reminder_type):
    appt_dt = appt.get("datetime")
    if isinstance(appt_dt, datetime):
        appt_dt_str = appt_dt.strftime("%-I:%M %p")
    else:
        appt_dt_str = "Soon"

    minutes = "15" if reminder_type == "15MIN" else "5"
    html = appt_email_html(
        reminder_type=reminder_type,
        user_name=user_name,
        appt_title=appt.get("title", "Appointment"),
        doctor=appt.get("doctor", "N/A"),
        provider=appt.get("provider", "N/A"),
        appt_datetime_str=appt_dt_str,
        address=appt.get("address", "N/A")
    )
    subject = (
        f"⏰ {appt.get('title', 'Appointment')} in 15 minutes"
        if reminder_type == "15MIN"
        else f"🚨 {appt.get('title', 'Appointment')} in 5 minutes"
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

        # ── Check appointments ─────────────────────────────────────────────────
        try:
            appts = db.collection("users").document(uid).collection("appts").get()
        except Exception as e:
            errors.append(f"uid={uid} appts fetch error: {e}")
            appts = []

        for appt_doc in appts:
            appt    = appt_doc.to_dict() or {}
            appt_id = appt_doc.id
            if not appt.get("title"):
                continue

            appt_dt = appt.get("datetime")
            if not isinstance(appt_dt, datetime):
                continue

            # Make appt_dt timezone-aware using user's timezone
            try:
                tz = pytz.timezone(timezone_str)
            except Exception:
                tz = pytz.UTC
            if appt_dt.tzinfo is None:
                appt_dt = tz.localize(appt_dt)

            diff_minutes = (appt_dt - now).total_seconds() / 60

            for reminder_type, window_min, window_max in [("15MIN", 14, 16), ("5MIN", 4, 6)]:
                if window_min <= diff_minutes <= window_max:
                    if already_sent_appt(uid, appt_id, reminder_type):
                        skip_count += 1
                        continue
                    try:
                        send_appt_reminder(user_email, user_name, appt, appt_id, reminder_type)
                        log_appt_reminder(uid, appt_id, reminder_type)
                        sent_count += 1
                    except Exception as e:
                        errors.append(f"uid={uid} appt={appt_id} {reminder_type}: {e}")

    return jsonify({
        "status": "done",
        "sent": sent_count,
        "skipped_already_sent": skip_count,
        "errors": errors
    })

# ── Note / caregiver message email ────────────────────────────────────────────
def note_email_html(user_name, message, sender_name=None):
    from_label = sender_name if sender_name else user_name
    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>New Message – MyPills</title></head>
<body style="margin:0;padding:0;background:#f7f7f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f7f7f5;padding:40px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:20px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

  <!-- Header -->
  <tr><td style="background:#c0392b;padding:28px 40px;text-align:left;">
    <table cellpadding="0" cellspacing="0"><tr>
      <td style="vertical-align:middle;"><img src="https://mypills.kidslearninglab.com/pill.png" alt="MyPills" width="42" height="42" style="display:inline-block;vertical-align:middle;margin-right:12px;border-radius:50%;background:rgba(255,255,255,0.2);"></td>
      <td style="vertical-align:middle;"><span style="color:#ffffff;font-size:22px;font-weight:700;letter-spacing:-0.5px;">MyPills</span></td>
    </tr></table>
  </td></tr>

  <!-- Badge -->
  <tr><td style="padding:36px 40px 0;text-align:center;">
    <span style="display:inline-block;background:#fff0ef;color:#c0392b;font-size:12px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;padding:6px 16px;border-radius:100px;">💬 &nbsp;New Message</span>
  </td></tr>

  <!-- Heading -->
  <tr><td style="padding:20px 40px 8px;text-align:center;">
    <h1 style="margin:0;font-size:28px;font-weight:700;color:#1a1a1a;letter-spacing:-0.5px;line-height:1.2;">
      You have a new message
    </h1>
  </td></tr>

  <!-- Subtext -->
  <tr><td style="padding:8px 40px 28px;text-align:center;">
    <p style="margin:0;font-size:15px;color:#888;line-height:1.6;">
      <strong style="color:#1a1a1a;">{from_label}</strong> sent you a note via MyPills.
    </p>
  </td></tr>

  <tr><td style="padding:0 40px;"><div style="height:1px;background:#f0f0ee;"></div></td></tr>

  <!-- Message bubble -->
  <tr><td style="padding:28px 40px;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f7f7f5;border-radius:14px;padding:24px 28px;">
      <tr><td>
        <p style="margin:0 0 10px;font-size:11px;font-weight:700;color:#bbb;text-transform:uppercase;letter-spacing:0.08em;">Message</p>
        <p style="margin:0;font-size:16px;color:#1a1a1a;line-height:1.7;white-space:pre-wrap;">{message}</p>
      </td></tr>
    </table>
  </td></tr>

  <!-- CTA -->
  <tr><td style="padding:0 40px 36px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td align="center">
        <a href="https://mypills.kidslearninglab.com" style="display:inline-block;text-align:center;background:#c0392b;color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;padding:14px 32px;border-radius:10px;">Open MyPills</a>
      </td>
    </tr></table>
  </td></tr>

  <tr><td style="padding:0 40px;"><div style="height:1px;background:#f0f0ee;"></div></td></tr>

  <!-- Footer -->
  <tr><td style="padding:24px 40px;text-align:center;">
    <p style="margin:0;font-size:12px;color:#bbb;line-height:1.6;">
      You're receiving this because someone shared a note with you via MyPills.<br>
      <a href="https://mypills.kidslearninglab.com" style="color:#c0392b;text-decoration:none;font-weight:600;">mypills.kidslearninglab.com</a>
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>'''


@app.route("/add_note", methods=["GET", "POST"])
def add_note():
    """
    Send a caregiver note email to any address.

    Query params / JSON body:
      - to        (required) : recipient email address
      - message   (required) : the note body
      - from_name (optional) : display name of the sender (shown in email)
    """
    from flask import request

    # Accept both GET params and JSON body
    if request.is_json:
        payload = request.get_json() or {}
    else:
        payload = request.args if request.method == "GET" else request.form

    to_email   = payload.get("to", "").strip()
    message    = payload.get("message", "").strip()
    from_name  = payload.get("from_name", "").strip() or "Your patient"

    if not to_email:
        return jsonify({"status": "error", "message": "Missing required field: to"}), 400
    if not message:
        return jsonify({"status": "error", "message": "Missing required field: message"}), 400

    try:
        html = note_email_html(
            user_name=from_name,
            message=message,
            sender_name=from_name,
        )
        resend.Emails.send({
            "from": "MyPills <reminders@mypills.kidslearninglab.com>",
            "to": to_email,
            "subject": f"💬 New message from {from_name} via MyPills",
            "html": html,
        })
        return jsonify({"status": "sent", "to": to_email})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

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