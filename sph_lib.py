"""
sph_lib.py – Python library for the Hessian School Portal (Schulportal Hessen, SPH)

Documented from the lanis-mobile Flutter/Dart source code:
  https://github.com/Space-Banane/lanis

Key source files this library is based on:
  lib/core/sph/session.dart    – Authentication, login flow, session management
  lib/core/sph/cryptor.dart    – RSA/AES jCryption handshake and decryption
  lib/applets/timetable/student/parser.dart   – Stundenplan HTML parsing
  lib/applets/substitutions/parser.dart       – Vertretungsplan HTML/AJAX parsing
  lib/models/timetable.dart    – Timetable data models
  lib/models/substitution.dart – Substitution data models

== Authentication flow ==
1. POST credentials to https://login.schulportal.hessen.de/?i={school_id}
2. Follow redirect to https://connect.schulportal.hessen.de → get login URL
3. GET the login URL with the main session to establish portal cookies
4. GET RSA public key, generate random 46-byte AES passphrase
5. RSA-PKCS1 encrypt the passphrase; POST it in a handshake (jCryption)
6. Decrypt the server's AES-encrypted challenge and verify it equals the passphrase
7. All subsequent HTML responses with <encoded>…</encoded> tags are transparently
   decrypted using the shared AES key (CryptoJS EVP_BytesToKey / AES-256-CBC)

== Usage ==
    from sph_lib import SPHClient

    client = SPHClient(school_id="1234", username="max.mustermann", password="secret")
    client.login()

    timetable = client.get_timetable()
    substitutions = client.get_substitutions()
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup, NavigableString
from Crypto.Cipher import AES
from Crypto.Cipher import PKCS1_v1_5 as PKCS1_cipher
from Crypto.PublicKey import RSA

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TimetableSubject:
    """A single subject slot in the timetable (Unterrichtsstunde).

    Attributes:
        id:           Stable identifier derived from data-mix attribute or hashed from
                      name/room (matches Dart's id field).
        name:         Subject name (Fach), may be None for free periods.
        room:         Room identifier (Raum), extracted from direct text nodes in the cell.
        teacher:      Teacher abbreviation/name (Lehrer), from <small> tag.
        badge:        A/B-week badge string (e.g. "A"), from .badge element.
        duration:     Number of periods this slot spans (rowspan value).
        start_time:   Start time string "HH:MM".
        end_time:     End time string "HH:MM".
        lesson_index: Row index in the timetable table (Stunde).
    """

    id: Optional[str]
    name: Optional[str]
    room: Optional[str]
    teacher: Optional[str]
    badge: Optional[str]
    duration: int
    start_time: str
    end_time: str
    lesson_index: Optional[int]

    def __repr__(self) -> str:
        return (
            f"TimetableSubject(name={self.name!r}, room={self.room!r}, "
            f"teacher={self.teacher!r}, {self.start_time}–{self.end_time})"
        )


@dataclass
class TimetableDay:
    """All subjects for a single weekday.

    Attributes:
        day_index: 0 = Monday, 1 = Tuesday, …, 4 = Friday.
        subjects:  Ordered list of TimetableSubject for this day.
    """

    day_index: int
    subjects: list[TimetableSubject] = field(default_factory=list)

    _DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    def __repr__(self) -> str:
        name = (
            self._DAY_NAMES[self.day_index]
            if self.day_index < len(self._DAY_NAMES)
            else str(self.day_index)
        )
        return f"TimetableDay({name}, {len(self.subjects)} subjects)"


@dataclass
class Timetable:
    """Full timetable for the current or displayed week.

    Attributes:
        days:       List of TimetableDay objects (up to 5, Mon–Fri).
        week_badge: Current week badge string (e.g. "A" or "B" for A/B schedules),
                    sourced from the #aktuelleWoche element.
    """

    days: list[TimetableDay]
    week_badge: Optional[str] = None


@dataclass
class Substitution:
    """A single substitution entry (Vertretung).

    Field names mirror the German API field names used by SPH's AJAX endpoint.

    Attributes:
        date:        Date string "dd.MM.yyyy" (Tag).
        date_en:     Date string "dd_MM_yyyy" (Tag_en), may be empty in non-AJAX format.
        lesson:      Lesson period, e.g. "1" or "2 - 3" (Stunde).
        substitute:  Substitute teacher (Vertreter).
        teacher:     Original teacher (Lehrer).
        class_name:  Class affected (Klasse).
        class_alt:   Alternate class identifier (Klasse_alt).
        subject:     Subject (Fach).
        subject_alt: Alternate/original subject (Fach_alt).
        room:        Assigned room (Raum).
        room_alt:    Original room (Raum_alt).
        note:        Note/comment (Hinweis).
        note2:       Secondary note (Hinweis2).
        type:        Type of substitution (Art), e.g. "Vertretung", "Entfall".
    """

    date: str
    date_en: str
    lesson: str
    substitute: Optional[str] = None
    teacher: Optional[str] = None
    class_name: Optional[str] = None
    class_alt: Optional[str] = None
    subject: Optional[str] = None
    subject_alt: Optional[str] = None
    room: Optional[str] = None
    room_alt: Optional[str] = None
    note: Optional[str] = None
    note2: Optional[str] = None
    type: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"Substitution(date={self.date!r}, lesson={self.lesson!r}, "
            f"subject={self.subject!r}, type={self.type!r})"
        )


@dataclass
class SubstitutionDay:
    """All substitutions and announcements for a single day.

    Attributes:
        date:          Date string "dd.MM.yyyy".
        substitutions: List of Substitution entries.
        info_headers:  List of announcement/info header strings extracted from
                       .infos tables on the page (header row text).
    """

    date: str
    substitutions: list[Substitution] = field(default_factory=list)
    info_headers: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"SubstitutionDay({self.date!r}, {len(self.substitutions)} entries)"


@dataclass
class SubstitutionPlan:
    """Complete substitution plan returned by the portal.

    Attributes:
        days:         List of SubstitutionDay objects.
        last_updated: ISO-8601 timestamp of the last server update, parsed from
                      "Letzte Aktualisierung: …" text on the page.
    """

    days: list[SubstitutionDay]
    last_updated: Optional[str] = None

    def all_substitutions(self) -> list[Substitution]:
        """Flatten all substitutions across all days into a single list."""
        result: list[Substitution] = []
        for day in self.days:
            result.extend(day.substitutions)
        return result


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class SPHException(Exception):
    """Base exception for all SPH errors."""


class WrongCredentialsException(SPHException):
    """Raised when the username/password combination is rejected."""


class LoginTimeoutException(SPHException):
    """Raised when the account is temporarily locked after too many failed logins."""


class SPHDownException(SPHException):
    """Raised when the school portal returns HTTP 503 (maintenance)."""


class EncryptionException(SPHException):
    """Raised when the RSA/AES handshake fails or the challenge check fails."""


# ---------------------------------------------------------------------------
# Cryptography helpers  (jCryption / CryptoJS AES-CBC with EVP_BytesToKey)
# ---------------------------------------------------------------------------
# Background:
#   Lanis uses jCryption (an old, unmaintained JS library) on top of CryptoJS for
#   its encryption layer.  CryptoJS derives AES keys using OpenSSL's EVP_BytesToKey
#   with MD5 hashing.  The on-wire format is:
#       base64( "Salted__" + 8-byte-salt + AES-256-CBC-PKCS7-ciphertext )
#
#   References:
#     https://www.openssl.org/docs/man3.1/man3/EVP_BytesToKey.html
#     https://gist.github.com/suehok/dfc4a6989537e4a3ba4058669289737f  (Dart original)


def _evp_bytes_to_key(
    password: bytes,
    salt: bytes,
    key_len: int = 32,
    iv_len: int = 16,
) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey key/IV derivation compatible with CryptoJS.

    Iteratively hashes (password + salt) with MD5 until enough bytes are
    accumulated to fill a *key_len*-byte key and *iv_len*-byte IV.

    Source: cryptor.dart → bytesToKeys()
    """
    d = b""
    d_i = b""
    while len(d) < key_len + iv_len:
        d_i = hashlib.md5(d_i + password + salt).digest()
        d += d_i
    return d[:key_len], d[key_len : key_len + iv_len]


def _decrypt_aes_bytes(encrypted: bytes, key_bytes: bytes) -> Optional[bytes]:
    """Decrypt raw AES-256-CBC CryptoJS-formatted bytes, returning raw bytes.

    Expected input format: b"Salted__" + 8-byte-salt + AES-CBC-PKCS7-ciphertext

    Used for the RSA handshake challenge verification where the payload is binary.
    Source: cryptor.dart → decryptWithKey()
    """
    try:
        if encrypted[:8] != b"Salted__":
            return None
        salt = encrypted[8:16]
        ciphertext = encrypted[16:]
        key, iv = _evp_bytes_to_key(key_bytes, salt)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(ciphertext)
        pad_len = decrypted[-1]
        if pad_len == 0 or pad_len > 16:
            return None
        return decrypted[:-pad_len]
    except Exception:
        return None


def _decrypt_aes_cryptojs(encrypted_b64: str, key_bytes: bytes) -> Optional[str]:
    """Decrypt a base64-encoded CryptoJS AES string, returning a UTF-8 string.

    Used to decrypt <encoded>…</encoded> HTML tags in portal responses.
    Source: cryptor.dart → decryptWithKeyString() / decryptEncodedTags()
    """
    try:
        result = _decrypt_aes_bytes(base64.b64decode(encrypted_b64), key_bytes)
        return result.decode("utf-8") if result is not None else None
    except Exception:
        return None


def _decrypt_html(html: str, key_bytes: bytes) -> str:
    """Decrypt all ``<encoded>…</encoded>`` tags inside an HTML string.

    Lanis wraps sensitive content (e.g. encrypted personal data) in these tags
    after the RSA/AES handshake is completed.  Each tag's content is a
    base64-encoded CryptoJS AES-encrypted string.

    Source: cryptor.dart → decryptEncodedTags()
    """

    def _replace(match: re.Match) -> str:
        decrypted = _decrypt_aes_cryptojs(match.group(1), key_bytes)
        return decrypted if decrypted is not None else ""

    return re.sub(r"<encoded>(.*?)</encoded>", _replace, html, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------


class SPHClient:
    """HTTP client for the Hessian School Portal (Schulportal Hessen).

    Handles authentication, RSA/AES session encryption, and provides methods
    to fetch the timetable (Stundenplan) and substitution plan (Vertretungsplan).

    Typical usage::

        client = SPHClient(school_id="1234", username="max.mustermann", password="s3cr3t")
        client.login()

        timetable = client.get_timetable()
        for day in timetable.days:
            print(day)
            for subject in day.subjects:
                print(" ", subject)

        plan = client.get_substitutions()
        for day in plan.days:
            print(day)
            for sub in day.substitutions:
                print(" ", sub)
    """

    BASE_URL = "https://start.schulportal.hessen.de"
    LOGIN_URL = "https://login.schulportal.hessen.de"
    CONNECT_URL = "https://connect.schulportal.hessen.de"

    def __init__(self, school_id: str, username: str, password: str) -> None:
        self.school_id = school_id
        self.username = username
        self.password = password

        # Main persistent HTTP session (holds portal cookies)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "SPH-Python-Client/1.0"})

        # Shared AES key established during the jCryption handshake
        self._aes_key: Optional[bytes] = None
        self._authenticated = False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self) -> None:
        """Authenticate with the school portal and set up session encryption.

        This is the only method you need to call before using get_timetable()
        or get_substitutions().

        Steps performed (source: session.dart → authenticate()):
          1. Obtain a one-time login URL via credential-based POST.
          2. Visit the login URL to establish main session cookies.
          3. Perform the RSA/AES jCryption handshake for encrypted responses.

        Raises:
            WrongCredentialsException:  Invalid username or password.
            LoginTimeoutException:      Account temporarily locked.
            SPHDownException:           Portal returns HTTP 503.
            EncryptionException:        Handshake/challenge verification failed.
        """
        login_url = self._get_login_url()
        self._session.get(login_url)  # establishes portal session cookies
        self._initialize_encryption()
        self._authenticated = True

    def _get_login_url(self) -> str:
        """Return a one-time URL that logs the main session into the portal.

        Uses a **separate** temporary requests.Session so that credential
        cookies (from login.schulportal.hessen.de) never mix with the main
        session cookies.

        Source: session.dart → getLoginURL()

        Flow:
          POST credentials → redirect → GET connect.schulportal.hessen.de
          → Location header = the usable login URL
        """
        temp_session = requests.Session()
        temp_session.headers.update({"User-Agent": "SPH-Python-Client/1.0"})

        # Step 1 – POST credentials
        resp1 = temp_session.post(
            f"{self.LOGIN_URL}/?i={self.school_id}",
            data={
                "user": f"{self.school_id}.{self.username}",
                "user2": self.username,
                "password": self.password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=False,
        )

        if resp1.status_code == 503:
            raise SPHDownException("Schulportal Hessen is currently unavailable (503).")

        # Check for account lock-out
        soup = BeautifulSoup(resp1.text, "html.parser")
        timeout_el = soup.find(id="authErrorLocktime")
        if timeout_el:
            raise LoginTimeoutException(
                f"Too many failed login attempts. "
                f"Wait {timeout_el.get_text(strip=True)} seconds before retrying."
            )

        location = resp1.headers.get("location")
        if not location:
            raise WrongCredentialsException("Login failed: invalid credentials.")

        # Step 2 – Follow redirect through connect.schulportal.hessen.de
        resp2 = temp_session.get(self.CONNECT_URL, allow_redirects=False)
        login_url = resp2.headers.get("location", "")
        if not login_url:
            raise WrongCredentialsException(
                "Login failed: no redirect returned from connect endpoint."
            )
        return login_url

    def _initialize_encryption(self) -> None:
        """Perform the RSA/AES (jCryption) handshake with the portal server.

        Source: cryptor.dart → initialize()

        Protocol:
          1. GET the server's RSA public key from ajax.php?f=rsaPublicKey.
          2. Generate 46 random bytes as the shared AES passphrase.
             (Lanis originally used a UUID-like string; we use cryptographically
              secure random bytes instead, which is more secure.)
          3. RSA-PKCS1 encrypt the passphrase and POST it to the handshake
             endpoint (ajax.php?f=rsaHandshake).
          4. The server AES-encrypts the passphrase and returns it as a
             "challenge".  Decrypt it and verify it equals our passphrase.
          5. If the check passes the shared AES key is stored for decrypting
             subsequent <encoded>…</encoded> HTML responses.

        Raises:
            EncryptionException: If the public key cannot be fetched, if the
                handshake returns no challenge, or if the challenge does not
                match the expected passphrase.
        """
        # 1 – Fetch server RSA public key
        resp = self._session.post(
            f"{self.BASE_URL}/ajax.php",
            params={"f": "rsaPublicKey"},
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        public_key_pem: Optional[str] = resp.json().get("publickey")
        if not public_key_pem:
            raise EncryptionException("Failed to retrieve RSA public key from server.")

        # 2 – Generate random 46-byte AES passphrase (mirrors Lanis UUID length)
        passphrase = secrets.token_bytes(46)

        # 3 – RSA-PKCS1 encrypt and send the passphrase
        rsa_key = RSA.import_key(public_key_pem)
        cipher_rsa = PKCS1_cipher.new(rsa_key)
        encrypted_passphrase = cipher_rsa.encrypt(passphrase)
        encrypted_b64 = base64.b64encode(encrypted_passphrase).decode("utf-8")

        resp2 = self._session.post(
            f"{self.BASE_URL}/ajax.php",
            params={"f": "rsaHandshake", "s": secrets.randbelow(2001)},
            data={"key": encrypted_b64},
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        challenge_b64: Optional[str] = resp2.json().get("challenge")
        if not challenge_b64:
            raise EncryptionException("RSA handshake failed: no challenge returned.")

        # 4 – Decrypt challenge and verify it equals our passphrase
        challenge_bytes = base64.b64decode(challenge_b64)
        decrypted_bytes = _decrypt_aes_bytes(challenge_bytes, passphrase)
        if decrypted_bytes is None or decrypted_bytes != passphrase:
            raise EncryptionException(
                "Encryption check failed: decrypted challenge does not match passphrase."
            )

        # 5 – Store the shared AES key
        self._aes_key = passphrase

    # ------------------------------------------------------------------
    # Session maintenance
    # ------------------------------------------------------------------

    def refresh_session(self) -> None:
        """Send a keep-alive request to prevent being logged out.

        Without periodic refresh calls Lanis logs users out after ~3–4 minutes
        of inactivity (even during active usage of the app).

        Source: session.dart → preventLogout()

        You only need this for long-running scripts.  Simple fetch → process
        workflows finish fast enough that the session stays alive on its own.
        """
        sid = self._session.cookies.get("sid")
        if not sid:
            return
        self._session.post(
            f"{self.BASE_URL}/ajax_login.php",
            data={"name": sid},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

    # ------------------------------------------------------------------
    # Internal HTTP helper
    # ------------------------------------------------------------------

    def _get_html(self, url: str, **kwargs) -> str:
        """GET *url* and transparently decrypt any ``<encoded>`` tags in the response."""
        resp = self._session.get(url, **kwargs)
        html = resp.text
        if self._aes_key and "<encoded>" in html:
            html = _decrypt_html(html, self._aes_key)
        return html

    # ------------------------------------------------------------------
    # Timetable (Stundenplan)
    # ------------------------------------------------------------------

    def get_timetable(self) -> Timetable:
        """Fetch and parse the current timetable (Stundenplan).

        Endpoint: GET https://start.schulportal.hessen.de/stundenplan.php

        Source: applets/timetable/student/parser.dart → TimetableStudentParser

        The portal renders the timetable as an HTML table inside a ``#all``
        element.  A second ``#own`` table may be present for teachers or
        students with personalised course selections (not parsed here – the
        ``#all`` table is always available and contains all subjects).

        The table structure:
          - Row 0:    header row with day labels.
          - Rows 1+:  one row per lesson period.
          - Column 0: time slot ("VonBis") for each row.
          - Columns 1–N: one column per weekday (Mon–Fri).
          - Cells may have ``rowspan > 1`` for double/triple periods.
          - Each cell contains one or more ``.stunde`` divs with subject data.

        Returns:
            A Timetable object.  ``days`` is a list of up to five TimetableDay
            objects (Mon–Fri).  Days that contain no subjects are included only
            if the portal returns data for them.

        Raises:
            SPHException: If the timetable page cannot be fetched or parsed.
        """
        html = self._get_timetable_html()
        if html is None:
            raise SPHException("Failed to retrieve timetable page.")

        soup = BeautifulSoup(html, "html.parser")

        week_badge_el = soup.find(id="aktuelleWoche")
        week_badge = week_badge_el.get_text(strip=True) if week_badge_el else None

        all_tbody = soup.select_one("#all tbody")
        if all_tbody is None:
            raise SPHException("Timetable table (#all tbody) not found in response.")

        days = self._parse_timetable_table(all_tbody)
        return Timetable(days=days, week_badge=week_badge)

    def _get_timetable_html(self) -> Optional[str]:
        """Fetch the timetable HTML, following a redirect if necessary.

        Source: applets/timetable/student/parser.dart → getTimetableDocument()

        The portal sometimes redirects to a URL with a query parameter before
        serving the actual timetable.  We handle this transparently.
        """
        resp = self._session.get(
            f"{self.BASE_URL}/stundenplan.php",
            allow_redirects=False,
        )
        html = resp.text
        if self._aes_key and "<encoded>" in html:
            html = _decrypt_html(html, self._aes_key)

        soup = BeautifulSoup(html, "html.parser")
        if soup.find(id="all"):
            return html

        location = resp.headers.get("location")
        if location:
            redirect_url = (
                location
                if location.startswith("http")
                else f"{self.BASE_URL}/{location.lstrip('/')}"
            )
            return self._get_html(redirect_url)

        return None

    def _parse_timetable_table(self, tbody) -> list[TimetableDay]:
        """Parse a timetable ``<tbody>`` into a list of TimetableDay objects.

        Source: applets/timetable/student/parser.dart → parseRoomPlan()

        Algorithm:
          - Build a 2-D ``occupied[row][day]`` boolean grid to handle rowspans.
          - For each row (skipping row 0) and each cell (skipping column 0):
              • Find the first unoccupied day column.
              • Mark the cell's rowspan columns as occupied for subsequent rows.
              • Parse ``.stunde`` divs inside the cell into TimetableSubject objects.
        """
        rows = tbody.find_all("tr", recursive=False)
        if not rows:
            return []

        first_row_cells = rows[0].find_all(["td", "th"], recursive=False)
        day_count = len(first_row_cells) - 1
        if day_count <= 0:
            return []

        # Collect time slots (VonBis) from the first column of each lesson row
        time_slots: list[tuple[str, str]] = []
        for row in rows[1:]:
            von_bis = row.find(class_="VonBis")
            if von_bis:
                time_str = von_bis.get_text(strip=True)
                parts = time_str.split(" - ")
                if len(parts) == 2:
                    time_slots.append((parts[0].strip(), parts[1].strip()))

        # timeslot_offset: True when the very first header cell has text content.
        # Source: applets/timetable/student/parser.dart →
        #   timeslotOffsetFirstRow = tbody.children[0].children[0].text.trim() != ""
        timeslot_offset: bool = first_row_cells[0].get_text(strip=True) != ""

        # Occupied grid: occupied[row_index][day_index]
        occupied = [[False] * day_count for _ in range(len(rows) + 1)]
        result = [TimetableDay(day_index=i) for i in range(day_count)]

        for row_idx, row in enumerate(rows):
            if row_idx == 0:
                continue  # skip header row

            col_cells = row.find_all(["td", "th"], recursive=False)
            actual_day = 0  # tracks the logical day column for this row

            for col_idx, cell in enumerate(col_cells):
                if col_idx == 0:
                    continue  # skip time info column

                # Advance past day columns already covered by a rowspan above
                while actual_day < day_count and occupied[row_idx][actual_day]:
                    actual_day += 1

                if actual_day >= day_count:
                    break

                rowspan = int(cell.get("rowspan", 1))
                for span_offset in range(rowspan):
                    if row_idx + span_offset < len(occupied):
                        occupied[row_idx + span_offset][actual_day] = True

                subjects = self._parse_lesson_cell(
                    cell, row_idx, time_slots, timeslot_offset, actual_day
                )
                result[actual_day].subjects.extend(subjects)
                actual_day += 1

        return result

    def _parse_lesson_cell(
        self,
        cell,
        row_index: int,
        time_slots: list[tuple[str, str]],
        timeslot_offset: bool,
        day: int,
    ) -> list[TimetableSubject]:
        """Parse one timetable cell into a list of TimetableSubject objects.

        A single cell can contain multiple ``.stunde`` divs (e.g. in split
        classes or when two subjects share a time slot).

        Source: applets/timetable/student/parser.dart → parseSingeHour()

        Time index mapping:
          - If ``timeslot_offset`` is True:  ``time_slots[row_index]``
          - If ``timeslot_offset`` is False: ``time_slots[row_index - 1]``
          (The offset accounts for whether the first header row carries a time
           entry or not.)
        """
        result: list[TimetableSubject] = []

        for stunde_div in cell.find_all(class_="stunde"):
            name_el = stunde_div.find("b")
            name = name_el.get_text(strip=True) if name_el else None

            # Room = direct text nodes in the .stunde div (not inside child tags)
            room_parts = [
                str(node).strip()
                for node in stunde_div.children
                if isinstance(node, NavigableString) and str(node).strip()
            ]
            room = " ".join(room_parts).strip() or None

            teacher_el = stunde_div.find("small")
            teacher = teacher_el.get_text(strip=True) if teacher_el else None

            badge_el = stunde_div.find(class_="badge")
            badge = badge_el.get_text(strip=True) if badge_el else None

            parent = stunde_div.parent
            duration = int(parent.get("rowspan", 1)) if parent else 1

            # Map row_index to the time_slots list
            ts_idx = row_index if timeslot_offset else row_index - 1
            end_ts_idx = ts_idx + duration - 1
            start_time = time_slots[ts_idx][0] if 0 <= ts_idx < len(time_slots) else "?"
            end_time = (
                time_slots[end_ts_idx][1]
                if 0 <= end_ts_idx < len(time_slots)
                else "?"
            )

            # Build a stable unique ID (mirrors Dart's id logic)
            raw_id = stunde_div.get("data-mix", "")
            if not raw_id:
                raw_id = hashlib.md5(
                    f"{name or ''}{room or ''}".encode()
                ).hexdigest()
            subject_id = f"{raw_id}-{day}-{start_time.replace(':', '')}"

            result.append(
                TimetableSubject(
                    id=subject_id,
                    name=name,
                    room=room,
                    teacher=teacher,
                    badge=badge,
                    duration=duration,
                    start_time=start_time,
                    end_time=end_time,
                    lesson_index=row_index,
                )
            )

        return result

    # ------------------------------------------------------------------
    # Substitution plan (Vertretungsplan)
    # ------------------------------------------------------------------

    def get_substitutions(self) -> SubstitutionPlan:
        """Fetch and parse the substitution plan (Vertretungsplan).

        Endpoint: GET https://start.schulportal.hessen.de/vertretungsplan.php

        Source: applets/substitutions/parser.dart → SubstitutionsParser.getHome()

        The portal serves substitutions in one of two formats:

        **AJAX format** (used by most schools):
          The main page lists available dates as ``data-tag="dd.MM.yyyy"``
          attributes.  Each day's data is fetched individually via:
              POST /vertretungsplan.php?a=my  {tag, ganzerPlan}
          The response is a JSON array of substitution objects.

        **Non-AJAX format** (older schools):
          All substitution data is embedded directly in the main page HTML as
          tables with ``id="vtable{dd_MM_yyyy}"``.

        Returns:
            A SubstitutionPlan object.  ``last_updated`` is set when the page
            contains a "Letzte Aktualisierung: …" timestamp.

        Raises:
            SPHException: If the page returns an error/unauthorised message.
        """
        html = self._get_html(f"{self.BASE_URL}/vertretungsplan.php")

        if "Fehler - Schulportal Hessen" in html:
            raise SPHException(
                "Access denied for substitution plan (not authorised or no data)."
            )

        last_updated = self._parse_last_edit_date(html)
        dates = self._get_substitution_dates(html)
        soup = BeautifulSoup(html, "html.parser")

        plan = SubstitutionPlan(days=[], last_updated=last_updated)

        if not dates:
            # Non-AJAX fallback
            plan.days = self._parse_substitutions_non_ajax(soup).days
        else:
            # AJAX format – one request per available date
            for date in dates:
                day_data = self._get_substitutions_ajax(date)

                # Attach info/announcement tables from the main page
                date_key = date.replace(".", "_")
                info_el = soup.find(id=f"tag{date_key}")
                if info_el:
                    day_data.info_headers = self._parse_info_tables(info_el)

                plan.days.append(day_data)

        # Drop empty days (no substitutions and no announcements)
        plan.days = [d for d in plan.days if d.substitutions or d.info_headers]
        return plan

    def _get_substitutions_ajax(self, date: str) -> SubstitutionDay:
        """Fetch substitutions for *date* via the AJAX endpoint.

        Source: applets/substitutions/parser.dart → getSubstitutionsAJAX()

        Args:
            date: Date string in "dd.MM.yyyy" format.

        Returns:
            A SubstitutionDay populated from the JSON response array.
        """
        resp = self._session.post(
            f"{self.BASE_URL}/vertretungsplan.php",
            params={"a": "my"},
            data={"tag": date, "ganzerPlan": "true"},
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        entries = resp.json()
        substitutions = [
            Substitution(
                date=e.get("Tag", date),
                date_en=e.get("Tag_en", ""),
                lesson=self._parse_hours(e.get("Stunde", "")),
                substitute=e.get("Vertreter"),
                teacher=e.get("Lehrer"),
                class_name=e.get("Klasse"),
                class_alt=e.get("Klasse_alt"),
                subject=e.get("Fach"),
                subject_alt=e.get("Fach_alt"),
                room=e.get("Raum"),
                room_alt=e.get("Raum_alt"),
                note=e.get("Hinweis"),
                note2=e.get("Hinweis2"),
                type=e.get("Art"),
            )
            for e in entries
        ]
        return SubstitutionDay(date=date, substitutions=substitutions)

    def _parse_substitutions_non_ajax(self, soup: BeautifulSoup) -> SubstitutionPlan:
        """Parse substitution data embedded directly in the main page HTML.

        Source: applets/substitutions/parser.dart → parseSubstitutionsNonAJAX()

        Used when the school portal does not support the AJAX endpoint.  Data
        is contained in tables with ``id="vtable{dd_MM_yyyy}"``.
        """
        from datetime import datetime

        plan = SubstitutionPlan(days=[])
        for el in soup.select("[data-tag]"):
            date_str = el.get("data-tag", "")
            try:
                parsed = datetime.strptime(date_str, "%d_%m_%Y")
                readable = parsed.strftime("%d.%m.%Y")
            except ValueError:
                continue

            day = SubstitutionDay(date=readable)
            vtable = soup.find(id=f"vtable{date_str}")
            if vtable is None:
                plan.days.append(day)
                continue

            headers = [th.get("data-field", "") for th in vtable.select("th")]

            for row in vtable.select("tbody tr"):
                if row.select("td[colspan]"):
                    continue  # "no entries" placeholder row
                cells = row.select("td")
                if not cells:
                    continue

                def _f(name: str) -> Optional[str]:
                    if name in headers:
                        idx = headers.index(name)
                        return cells[idx].get_text(strip=True) if idx < len(cells) else None
                    return None

                day.substitutions.append(
                    Substitution(
                        date=readable,
                        date_en=date_str,
                        lesson=self._parse_hours(_f("Stunde") or ""),
                        subject=_f("Fach"),
                        type=_f("Art"),
                        room=_f("Raum"),
                        note=_f("Hinweis"),
                        teacher=_f("Lehrer"),
                        substitute=_f("Vertreter"),
                        class_name=_f("Klasse"),
                    )
                )
            plan.days.append(day)

        return plan

    def _get_substitution_dates(self, html: str) -> list[str]:
        """Extract available substitution dates from the main page HTML.

        Source: applets/substitutions/parser.dart → getSubstitutionDates()

        Dates appear as ``data-tag="dd.MM.yyyy"`` attributes.
        Returns a deduplicated list in the order they appear on the page.
        An empty list signals that the non-AJAX page format should be used.
        """
        pattern = re.compile(r'data-tag="(\d{2})\.(\d{2})\.(\d{4})"')
        seen: list[str] = []
        for m in pattern.finditer(html):
            date_str = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
            if date_str not in seen:
                seen.append(date_str)
        return seen

    def _parse_last_edit_date(self, html: str) -> Optional[str]:
        """Parse the last-update timestamp from the substitution plan page.

        Source: applets/substitutions/parser.dart → parseLastEditDate()

        Looks for the pattern:
            "Letzte Aktualisierung: DD.MM.YYYY um HH:MM:SS Uhr"

        Returns an ISO-8601 string (``YYYY-MM-DDTHH:MM:SS``) or None.
        """
        pattern = re.compile(
            r"Letzte\s+Aktualisierung:\s*"
            r"(\d{2})\.(\d{2})\.(\d{4})\s+um\s+"
            r"(\d{2}):(\d{2}):(\d{2})\s+Uhr",
            re.IGNORECASE,
        )
        match = pattern.search(html)
        if match:
            d, mo, y, hh, mm, ss = match.groups()
            return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}"
        return None

    @staticmethod
    def _parse_hours(hours: str) -> str:
        """Normalise a lesson-period string to "N" or "N - M" format.

        Source: applets/substitutions/parser.dart → parseHours()

        Examples:
            "1"            → "1"
            "2-3"          → "2 - 3"
            "2. Stunde"    → "2"
            "ab 3. Stunde" → "3"
        """
        numbers = re.findall(r"\d+", hours)
        if not numbers or len(numbers) > 2:
            return hours
        return f"{numbers[0]} - {numbers[1]}" if len(numbers) == 2 else numbers[0]

    @staticmethod
    def _parse_info_tables(element) -> list[str]:
        """Extract info/announcement header strings from a day section element.

        Source: applets/substitutions/parser.dart → parseInformationTables()

        Info tables are ``<table class="infos">`` elements.  Header rows are
        identified by a CSS class containing "header".  Only the text of the
        first cell in each header row is returned.
        """
        info_headers: list[str] = []
        tables = element.find_all(class_="infos")
        if not tables:
            return []
        for row in tables[0].select("tr"):
            classes = " ".join(row.get("class", []))
            if "header" in classes:
                cells = row.select("td")
                if cells:
                    info_headers.append(cells[0].get_text(strip=True))
        return info_headers
