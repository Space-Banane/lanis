"""
example.py – Example script showing how to use sph_lib

Credentials are read from a .env file (see .env.example).

Usage:
    1. Copy .env.example to .env and fill in your credentials.
    2. Install dependencies:
           pip install -r requirements.txt
    3. Run:
           python example.py
"""

import os
import argparse
from dotenv import load_dotenv
from sph_lib import (
    SPHClient,
    SPHException,
    WrongCredentialsException,
    LoginTimeoutException,
    SPHDownException,
    EncryptionException,
)

# ---------------------------------------------------------------------------
# Argument Parsing
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Example script showing how to use sph_lib")
parser.add_argument("--ignore", type=str, help="Ignore string format: timeframe;IfNot;Teacher;Day;Scope")
parser.add_argument("--day", type=str, help="Specific day name to display (e.g. Monday)")
args = parser.parse_args()

def should_ignore(time_range, teacher, day_name, ignore_str):
    if not ignore_str:
        return False
    
    try:
        # Support both semicolon and comma as separators
        if ";" in ignore_str:
            parts = ignore_str.split(";")
        else:
            parts = ignore_str.split(",")

        if len(parts) < 5:
            return False
            
        target_time, condition, target_teacher, target_day, scope = [p.strip() for p in parts]
        
        # Strip quotes/literal chars from condition if present
        # Handles: IfNot"ÖZTÜ", IfNot'ÖZTÜ', IfNotÖZTÜ
        target_teacher_cond = None
        if condition.startswith("IfNot"):
            target_teacher_cond = condition[5:].strip("'\"")
        else:
            target_teacher_cond = None

        # Check timeframe
        if target_time.lower() != "all" and target_time != time_range:
            return False
            
        # Check day
        if target_day.lower() != "all":
            # Support plural like "Mondays" or singular "Monday"
            if not target_day.lower().startswith(day_name.lower()):
                return False
                
        # Check condition (IfNot Teacher)
        if target_teacher_cond and teacher:
            if teacher.upper() == target_teacher_cond.upper():
                return False
            
        return True
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Load credentials from .env
# ---------------------------------------------------------------------------
load_dotenv()

SCHOOL_ID = os.getenv("SPH_SCHOOL_ID", "")
USERNAME = os.getenv("SPH_USERNAME", "")
PASSWORD = os.getenv("SPH_PASSWORD", "")

if not SCHOOL_ID or not USERNAME or not PASSWORD:
    raise SystemExit(
        "Missing credentials. Copy .env.example to .env and fill in "
        "SPH_SCHOOL_ID, SPH_USERNAME and SPH_PASSWORD."
    )

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
print(f"Logging in as {USERNAME} at school {SCHOOL_ID} …")

client = SPHClient(school_id=SCHOOL_ID, username=USERNAME, password=PASSWORD)

try:
    client.login()
except WrongCredentialsException:
    raise SystemExit("Login failed: wrong username or password.")
except LoginTimeoutException as e:
    raise SystemExit(f"Login failed: {e}")
except SPHDownException:
    raise SystemExit("The school portal is currently unavailable (HTTP 503).")
except EncryptionException as e:
    raise SystemExit(f"Encryption handshake failed: {e}")

print("Login successful.\n")

# ---------------------------------------------------------------------------
# Fetch and display the timetable (Stundenplan)
# ---------------------------------------------------------------------------
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

print("=" * 60)
print("TIMETABLE (Stundenplan)")
print("=" * 60)

try:
    timetable = client.get_timetable()

    if timetable.week_badge:
        print(f"Week badge: {timetable.week_badge}\n")

    if not timetable.days:
        print("No timetable data available.")
    else:
        for day in timetable.days:
            day_name = (
                DAY_NAMES[day.day_index]
                if day.day_index < len(DAY_NAMES)
                else f"Day {day.day_index}"
            )

            if args.day and args.day.lower() not in day_name.lower():
                continue

            print(f"\n{day_name}:")
            if not day.subjects:
                print("  (no lessons)")
            else:
                for subject in day.subjects:
                    name = subject.name or "(free)"
                    time_range = f"{subject.start_time}-{subject.end_time}"
                    
                    if args.ignore and should_ignore(time_range, subject.teacher, day_name, args.ignore):
                        continue
                        
                    details = []
                    if subject.room:
                        details.append(f"Room: {subject.room}")
                    if subject.teacher:
                        details.append(f"Teacher: {subject.teacher}")
                    if subject.badge:
                        details.append(f"Week: {subject.badge}")
                    detail_str = "  |  ".join(details)
                    print(f"  [{time_range}]  {name}  —  {detail_str}")

except SPHException as e:
    print(f"Could not fetch timetable: {e}")

# ---------------------------------------------------------------------------
# Fetch and display the substitution plan (Vertretungsplan)
# ---------------------------------------------------------------------------
print("\n")
print("=" * 60)
print("SUBSTITUTIONS (Vertretungsplan)")
print("=" * 60)

try:
    plan = client.get_substitutions()

    if plan.last_updated:
        print(f"Last updated: {plan.last_updated}\n")

    if not plan.days:
        print("No substitutions available.")
    else:
        for day in plan.days:
            print(f"\n{day.date}:")

            if day.info_headers:
                print("  Announcements:")
                for header in day.info_headers:
                    print(f"    • {header}")

            if not day.substitutions:
                print("  (no substitution entries)")
            else:
                for sub in day.substitutions:
                    parts = [f"Lesson {sub.lesson}"]
                    if sub.class_name:
                        parts.append(f"Class: {sub.class_name}")
                    if sub.subject:
                        subj = sub.subject
                        if sub.subject_alt and sub.subject_alt != sub.subject:
                            subj += f" (was: {sub.subject_alt})"
                        parts.append(f"Subject: {subj}")
                    if sub.type:
                        parts.append(f"Type: {sub.type}")
                    if sub.substitute:
                        teacher_info = sub.substitute
                        if sub.teacher and sub.teacher != sub.substitute:
                            teacher_info += f" (replaces: {sub.teacher})"
                        parts.append(f"Sub: {teacher_info}")
                    elif sub.teacher:
                        parts.append(f"Teacher: {sub.teacher}")
                    if sub.room:
                        room_info = sub.room
                        if sub.room_alt and sub.room_alt != sub.room:
                            room_info += f" (was: {sub.room_alt})"
                        parts.append(f"Room: {room_info}")
                    if sub.note:
                        parts.append(f"Note: {sub.note}")
                    print("  " + "  |  ".join(parts))

except SPHException as e:
    print(f"Could not fetch substitutions: {e}")
