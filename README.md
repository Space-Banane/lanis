# SPH Python Library

Python library for the Hessian School Portal (Schulportal Hessen, SPH).

Documented and ported from the original [lanis-mobile](https://github.com/alessioC42/lanis-mobile)
Flutter/Dart app.  Provides easy access to **Stundenplan** (timetable) and
**Vertretungsplan** (substitution plan) with a simple Python API.

## Quick start

### 1. Install dependencies

```shell
pip install -r requirements.txt
```

### 2. Configure credentials

```shell
cp .env.example .env
# Edit .env with your school ID, username and password
```

### 3. Run the example

```shell
python example.py
```

## Library usage

```python
from sph_lib import SPHClient

client = SPHClient(school_id="1234", username="max.mustermann", password="s3cr3t")
client.login()

# Timetable (Stundenplan)
timetable = client.get_timetable()
for day in timetable.days:
    print(day)
    for subject in day.subjects:
        print(" ", subject)

# Substitutions (Vertretungsplan)
plan = client.get_substitutions()
for day in plan.days:
    print(day)
    for sub in day.substitutions:
        print(" ", sub)
```

## Files

| File | Description |
|------|-------------|
| `sph_lib.py` | Python library – authentication, encryption, timetable & substitutions |
| `example.py` | Example script that prints timetable and substitutions |
| `requirements.txt` | Python package dependencies |
| `.env.example` | Credential template – copy to `.env` and fill in your data |

## Implementation notes

Authentication flow (from `session.dart`):
1. POST credentials to `https://login.schulportal.hessen.de/?i={school_id}`
2. Follow redirect through `connect.schulportal.hessen.de` to get a login URL
3. GET the login URL with the main session to establish portal cookies

Encryption (from `cryptor.dart`):
- The portal uses **jCryption** (a JS library using CryptoJS AES-256-CBC)
- Key exchange: RSA-PKCS1 encrypted random passphrase sent to the server
- Server responds with an AES-encrypted challenge that must match the passphrase
- All HTML responses may contain `<encoded>…</encoded>` tags decrypted with the shared key

Timetable parser (from `applets/timetable/student/parser.dart`):
- `GET /stundenplan.php` → HTML table `#all tbody`
- Cells may span multiple rows; a rowspan-tracking grid resolves day assignments

Substitutions parser (from `applets/substitutions/parser.dart`):
- `GET /vertretungsplan.php` → extract date list
- AJAX format: `POST /vertretungsplan.php?a=my` per date → JSON array
- Non-AJAX fallback: parse embedded `#vtable{date}` tables from the main page