#!/usr/bin/env python3
"""Hearth: a DIY family wall calendar.

Serves the touchscreen app from ./static and stores everything (family,
linked calendars, settings, chores, lists, meals, events) in MariaDB/MySQL
when DB_HOST is set, otherwise in JSON files. All setup happens in the app;
there is no config file to edit.
"""
__version__ = "1.0.0"

import copy
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

try:
    import icalendar
    import recurring_ical_events
except ImportError:
    sys.exit("Missing packages. Run: pip install -r requirements.txt")

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
DATA_PATH = os.environ.get("HEARTH_DATA", os.path.join(ROOT, "data.json"))
SETTINGS_PATH = os.environ.get("HEARTH_SETTINGS", os.path.join(os.path.dirname(DATA_PATH) or ".", "settings.json"))
LEGACY_CONFIG = os.environ.get("HEARTH_CONFIG", os.path.join(ROOT, "config.json"))  # imported once if present
DB_HOST = os.environ.get("DB_HOST", "").strip()
EVERYONE = "Everyone"
DOC_STATE, DOC_SETTINGS = 1, 2  # row ids in the hearth_state table

if DB_HOST:
    try:
        import pymysql
    except ImportError:
        sys.exit("DB_HOST is set but PyMySQL isn't installed. Run: pip install -r requirements.txt")

DEFAULT_STATE = {
    "rev": 0,
    "events": [],
    "chores": [],
    "lists": [
        {"id": "grocery", "name": "Groceries", "items": []},
        {"id": "todo", "name": "To-do", "items": []},
    ],
    "meals": {},
    "pantry": [],
}

DEFAULT_SETTINGS = {
    "setup_done": False,
    "title": "Our Family",
    "week_start": 0,
    "default_view": "week",
    "refresh_minutes": 10,
    "idle_reset_minutes": 3,
    "ui_scale": 1.0,
    "latitude": None,
    "longitude": None,
    "place": "",
    "temperature_unit": "fahrenheit",
    "night_mode": {"start": 22, "end": 6},
    "members": [],
    "calendars": [],
    "pin_hash": "",
    "anthropic_api_key": "",
    "ai_model": "claude-haiku-5-5",
    "spoonacular_api_key": "",
}
SPOON_URL = os.environ.get("HEARTH_SPOON_URL", "https://api.spoonacular.com")  # override only for testing
recipe_cache = {}  # (meal, pantry) -> (fetched_at, result); saves the daily quota
AI_MODELS = {"claude-haiku-5-5": "Fast", "claude-sonnet-5-5": "Smarter"}
ANTHROPIC_URL = os.environ.get("HEARTH_ANTHROPIC_URL", "https://api.anthropic.com/v1/messages")  # override only for testing
ANTHROPIC_VERSION = "2026-10-21"
GENERAL_KEYS = ["setup_done", "title", "week_start", "default_view", "refresh_minutes", "idle_reset_minutes",
                "ui_scale", "latitude", "longitude", "place", "temperature_unit", "night_mode"]

state_lock = threading.Lock()
settings_lock = threading.Lock()
feed_lock = threading.Lock()
feed_cache = {}  # url -> (fetched_at, icalendar.Calendar)
SETTINGS = None


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path, obj):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".hearth-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)  # atomic, so a power cut can't corrupt the file


def deep(obj):
    return copy.deepcopy(obj)


def with_defaults(doc, defaults):
    if not isinstance(doc, dict):
        doc = {}
    for key, value in defaults.items():
        doc.setdefault(key, deep(value))
    return doc


# ---------- database ----------

def db_connect():
    return pymysql.connect(
        host=DB_HOST,
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "hearth"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "hearth"),
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=5,
    )


def db_bootstrap():
    """Create the database and app user using admin credentials
    (DB_ROOT_PASSWORD). Only runs when the app user can't log in yet."""
    name = os.environ.get("DB_NAME", "hearth")
    user = os.environ.get("DB_USER", "hearth")
    password = os.environ.get("DB_PASSWORD", "")
    for label, value in (("DB_NAME", name), ("DB_USER", user)):
        if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", value):
            sys.exit(f"{label} may only contain letters, numbers and underscores.")
    if not password:
        sys.exit("Set DB_PASSWORD: it becomes the password of the new database user.")
    admin = pymysql.connect(
        host=DB_HOST, port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_ROOT_USER", "root"),
        password=os.environ["DB_ROOT_PASSWORD"],
        charset="utf8mb4", autocommit=True, connect_timeout=5)
    with admin, admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        cur.execute(f"CREATE USER IF NOT EXISTS '{user}'@'%%' IDENTIFIED BY %s", (password,))
        cur.execute(f"ALTER USER '{user}'@'%%' IDENTIFIED BY %s", (password,))
        cur.execute(f"GRANT ALL PRIVILEGES ON `{name}`.* TO '{user}'@'%'")  # no args, so a single %
        cur.execute("FLUSH PRIVILEGES")
    print(f"[hearth] created database '{name}' and user '{user}'. "
          "You can now remove DB_ROOT_PASSWORD from the container settings.")


def db_init():
    """Wait for the database (it may boot after us), create the database and
    user if admin credentials were given, then create the table."""
    bootstrapped = False
    for _ in range(60):
        try:
            conn = db_connect()
            break
        except pymysql.err.OperationalError as exc:
            code = exc.args[0] if exc.args else None
            # 1044/1045: user can't log in, 1049: database doesn't exist
            if code in (1044, 1045, 1049) and os.environ.get("DB_ROOT_PASSWORD") and not bootstrapped:
                try:
                    db_bootstrap()
                    bootstrapped = True
                    continue
                except pymysql.err.OperationalError as admin_exc:
                    sys.exit(f"Couldn't create the database with the admin login: {admin_exc.args[-1]}. "
                             "Check DB_ROOT_USER / DB_ROOT_PASSWORD, or create it by hand (see UNRAID.md).")
            if code in (1044, 1045, 1049):
                sys.exit(f"Database login failed: {exc.args[-1]}. Check DB_USER / DB_PASSWORD / DB_NAME, "
                         "or set DB_ROOT_PASSWORD once so Hearth can create them.")
            print(f"[hearth] waiting for database at {DB_HOST} ({exc.args[-1]})", file=sys.stderr)
            time.sleep(5)
    else:
        sys.exit("Couldn't connect to the database. Check DB_HOST and DB_PORT.")
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS hearth_state ("
                " id TINYINT PRIMARY KEY,"
                " rev INT NOT NULL,"
                " data LONGTEXT NOT NULL,"
                " updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
                ") CHARACTER SET utf8mb4")
            cur.execute("SELECT COUNT(*) FROM hearth_state WHERE id = %s", (DOC_STATE,))
            if cur.fetchone()[0] == 0:
                seed = with_defaults(read_json(DATA_PATH), DEFAULT_STATE)
                if os.path.exists(DATA_PATH):
                    print(f"[hearth] importing {DATA_PATH} into the database")
                cur.execute("INSERT INTO hearth_state (id, rev, data) VALUES (%s, %s, %s)",
                            (DOC_STATE, seed.get("rev", 0), json.dumps(seed, ensure_ascii=False)))
        conn.commit()
    print(f"[hearth] using database {os.environ.get('DB_NAME', 'hearth')} on {DB_HOST}")


# ---------- shared state (chores, lists, meals, events added in the app) ----------
# A save is accepted only if it was made from the latest revision; otherwise
# the caller gets the current state back (HTTP 409) and re-applies its change.

def get_state():
    if DB_HOST:
        with db_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT rev, data FROM hearth_state WHERE id = %s", (DOC_STATE,))
            rev, data = cur.fetchone()
        state = with_defaults(json.loads(data), DEFAULT_STATE)
        state["rev"] = rev
        return state
    with state_lock:
        return with_defaults(read_json(DATA_PATH), DEFAULT_STATE)


def put_state(incoming):
    """Returns (saved, state)."""
    if DB_HOST:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT rev, data FROM hearth_state WHERE id = %s FOR UPDATE", (DOC_STATE,))
                rev, data = cur.fetchone()
                if incoming.get("rev") != rev:
                    conn.rollback()
                    current = with_defaults(json.loads(data), DEFAULT_STATE)
                    current["rev"] = rev
                    return False, current
                incoming["rev"] = rev + 1
                cur.execute("UPDATE hearth_state SET rev = %s, data = %s WHERE id = %s",
                            (rev + 1, json.dumps(incoming, ensure_ascii=False), DOC_STATE))
            conn.commit()
        return True, incoming
    with state_lock:
        current = with_defaults(read_json(DATA_PATH), DEFAULT_STATE)
        if incoming.get("rev") != current.get("rev"):
            return False, current
        incoming["rev"] = current.get("rev", 0) + 1
        write_json(DATA_PATH, incoming)
        return True, incoming


# ---------- settings (family, calendars, display) ----------

def load_settings_doc():
    if DB_HOST:
        with db_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT data FROM hearth_state WHERE id = %s", (DOC_SETTINGS,))
            row = cur.fetchone()
        return json.loads(row[0]) if row else None
    return read_json(SETTINGS_PATH)


def save_settings_doc(settings):
    if DB_HOST:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO hearth_state (id, rev, data) VALUES (%s, 0, %s) "
                            "ON DUPLICATE KEY UPDATE data = VALUES(data)",
                            (DOC_SETTINGS, json.dumps(settings, ensure_ascii=False)))
            conn.commit()
    else:
        write_json(SETTINGS_PATH, settings)


def new_id():
    return secrets.token_hex(5)


def settings_from_legacy_config(cfg):
    """Older versions used a config.json; bring it over once."""
    s = deep(DEFAULT_SETTINGS)
    for key in GENERAL_KEYS:
        if key in cfg:
            s[key] = cfg[key]
    for m in cfg.get("members", []):
        s["members"].append({"name": m["name"], "color": m.get("color", "#9b8f84")})
        for url in m.get("calendars", []):
            if "PASTE" not in url:
                s["calendars"].append({"id": new_id(), "member": m["name"], "url": url, "label": ""})
    for url in cfg.get("shared_calendars", []):
        s["calendars"].append({"id": new_id(), "member": EVERYONE, "url": url, "label": ""})
    s["setup_done"] = bool(s["members"])
    return s


def init_settings():
    global SETTINGS
    s = load_settings_doc()
    if s is None:
        legacy = read_json(LEGACY_CONFIG)
        if isinstance(legacy, dict):
            s = settings_from_legacy_config(legacy)
            print(f"[hearth] imported {LEGACY_CONFIG}; from now on, change settings in the app")
        else:
            s = deep(DEFAULT_SETTINGS)
        save_settings_doc(s)
    SETTINGS = with_defaults(s, DEFAULT_SETTINGS)


def mask_url(url):
    p = urlparse(normalize_url(url))
    tail = re.sub(r"[^A-Za-z0-9]", "", p.path + p.query)[-4:]
    return f"{p.netloc} · …{tail}" if tail else p.netloc


def public_config():
    """What browsers see. Calendar links are secrets, so only a hint is sent."""
    s = SETTINGS
    cfg = {k: s[k] for k in GENERAL_KEYS}
    cfg["members"] = s["members"]
    cfg["calendars"] = [{"id": c["id"], "member": c["member"],
                         "label": c.get("label") or urlparse(normalize_url(c["url"])).netloc,
                         "hint": mask_url(c["url"])} for c in s["calendars"]]
    cfg["pin"] = bool(s.get("pin_hash"))
    key = s.get("anthropic_api_key") or ""
    cfg["ai_enabled"] = bool(key)
    cfg["ai_key_hint"] = f"…{key[-4:]}" if key else ""
    cfg["ai_model"] = s.get("ai_model") or "claude-haiku-5-5"
    skey = s.get("spoonacular_api_key") or ""
    cfg["recipes_enabled"] = bool(skey)
    cfg["recipes_key_hint"] = f"…{skey[-4:]}" if skey else ""
    return cfg


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 100_000).hex()


def pin_ok(handler):
    stored = SETTINGS.get("pin_hash")
    if not stored:
        return True
    salt, digest = stored.split("$", 1)
    return secrets.compare_digest(hash_pin(handler.headers.get("X-Hearth-Pin", ""), salt), digest)


HEX = re.compile(r"#[0-9a-fA-F]{6}")


def apply_settings_changes(s, ch):
    """Validate and apply a settings change from the app. Raises ValueError
    with a message the app can show."""
    def num(key, lo, hi, cast=int):
        try:
            v = cast(ch[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number")
        if not lo <= v <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        return v

    if "setup_done" in ch:
        s["setup_done"] = bool(ch["setup_done"])
    if "title" in ch:
        s["title"] = str(ch["title"]).strip()[:60] or "Our Family"
    if "week_start" in ch:
        s["week_start"] = 1 if ch["week_start"] == 1 else 0
    if "default_view" in ch:
        if ch["default_view"] not in ("week", "month", "agenda"):
            raise ValueError("Unknown view")
        s["default_view"] = ch["default_view"]
    if "ui_scale" in ch:
        s["ui_scale"] = num("ui_scale", 0.75, 2.0, float)
    if "idle_reset_minutes" in ch:
        s["idle_reset_minutes"] = num("idle_reset_minutes", 1, 120)
    if "refresh_minutes" in ch:
        s["refresh_minutes"] = num("refresh_minutes", 1, 240)
    if "temperature_unit" in ch:
        s["temperature_unit"] = "celsius" if ch["temperature_unit"] == "celsius" else "fahrenheit"
    if "latitude" in ch or "longitude" in ch:
        if ch.get("latitude") is None or ch.get("longitude") is None:
            s["latitude"] = s["longitude"] = None
            s["place"] = ""
        else:
            s["latitude"] = num("latitude", -90, 90, float)
            s["longitude"] = num("longitude", -180, 180, float)
    if "place" in ch:
        s["place"] = str(ch["place"] or "")[:80]
    if "night_mode" in ch:
        nm = ch["night_mode"]
        if nm is None:
            s["night_mode"] = None
        else:
            s["night_mode"] = {"start": int(nm["start"]) % 24, "end": int(nm["end"]) % 24}
    if "members" in ch:
        members, seen = [], set()
        for m in ch["members"]:
            name = str(m.get("name", "")).strip()[:24]
            if not name:
                raise ValueError("Every person needs a name")
            if name.lower() == EVERYONE.lower():
                raise ValueError(f'"{EVERYONE}" is reserved; pick another name')
            if name.lower() in seen:
                raise ValueError(f'There are two people called "{name}"')
            seen.add(name.lower())
            color = m.get("color") if HEX.fullmatch(str(m.get("color", ""))) else "#9b8f84"
            members.append({"name": name, "color": color})
        renames = ch.get("renames") or {}
        for cal in s["calendars"]:
            cal["member"] = renames.get(cal["member"], cal["member"])
        names = {m["name"] for m in members}
        # Calendars of people who were removed go to Everyone rather than vanishing.
        for cal in s["calendars"]:
            if cal["member"] != EVERYONE and cal["member"] not in names:
                cal["member"] = EVERYONE
        s["members"] = members
    if "ai_model" in ch:
        if ch["ai_model"] not in AI_MODELS:
            raise ValueError("Unknown model")
        s["ai_model"] = ch["ai_model"]
    if "anthropic_api_key" in ch:
        key = str(ch["anthropic_api_key"] or "").strip()
        if key:
            ask_claude(key, s.get("ai_model") or "claude-haiku-5-5", "Reply with OK.", "Say OK.", 5)  # raises if the key doesn't work
        s["anthropic_api_key"] = key
    if "spoonacular_api_key" in ch:
        key = str(ch["spoonacular_api_key"] or "").strip()
        if key:
            spoon_get(key, "/recipes/findByIngredients", {"ingredients": "egg", "number": 1})  # raises if the key doesn't work
        s["spoonacular_api_key"] = key
        recipe_cache.clear()
    if "pin" in ch:
        pin = str(ch["pin"] or "")
        if pin and not re.fullmatch(r"\d{4,8}", pin):
            raise ValueError("The PIN must be 4 to 8 digits")
        if pin:
            salt = secrets.token_hex(8)
            s["pin_hash"] = f"{salt}${hash_pin(pin, salt)}"
        else:
            s["pin_hash"] = ""


# ---------- meal ideas (optional, uses the Claude API) ----------

def ask_claude(key, model, system, prompt, max_tokens=2000):
    """Call the Claude Messages API. Returns the reply text, or raises
    ValueError with a message the app can show."""
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(ANTHROPIC_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read()).get("error", {}).get("message", "")
        except Exception:
            pass
        print(f"[hearth] Claude API error {exc.code}: {detail}", file=sys.stderr)
        if exc.code in (401, 403):
            raise ValueError("Claude didn't accept that API key. Check it was copied completely.")
        if exc.code == 429:
            raise ValueError("Claude is busy or the account is out of credit. Try again in a minute, "
                             "or check billing in the Claude Console.")
        if exc.code == 400 and "credit" in detail.lower():
            raise ValueError("The Claude account is out of credit. Add credit in the Claude Console.")
        raise ValueError(f"Claude returned an error ({exc.code}). Try again in a minute.")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("Couldn't reach Claude. Check the server's internet connection.")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


MEAL_SYSTEM = """You help a family decide what to cook using food they already have.
Reply with JSON only (no markdown, no commentary) in exactly this shape:
{"ideas": [{"name": "short dish name", "description": "one appetising sentence",
  "time_minutes": 30, "servings": 4, "uses": ["items from their list that this uses"],
  "missing": ["ingredients they would need to buy"], "steps": ["3 to 6 short steps"]}]}
Rules: build each idea mostly from their list; keep "missing" to at most 3 everyday items, and use []
when nothing is needed; assume salt, pepper, cooking oil and water are always available and never list
them as missing; write "uses" with the same wording as their list; keep steps short and practical."""


def meal_ideas(body):
    key = SETTINGS.get("anthropic_api_key")
    if not key:
        raise ValueError("Meal ideas are turned off. Add a Claude API key in Settings.")
    pantry = [str(i.get("text", "")).strip() for i in get_state().get("pantry", [])]
    pantry = [p for p in pantry if p][:150]
    if not pantry:
        raise ValueError("Add some food to the pantry list first.")
    meal = body.get("meal") if body.get("meal") in ("breakfast", "lunch", "dinner") else "dinner"
    count = max(1, min(int(body.get("count") or 3), 5))
    people = max(1, len(SETTINGS["members"]))
    notes = str(body.get("notes") or "").strip()[:300]
    prompt = ("Food we have at home:\n" + "\n".join(f"- {p}" for p in pantry) +
              f"\n\nSuggest {count} different {meal} ideas for {people} "
              f"{'person' if people == 1 else 'people'}." + (f"\nAlso: {notes}" if notes else ""))
    text = ask_claude(key, SETTINGS.get("ai_model") or "claude-haiku-5-5", MEAL_SYSTEM, prompt)
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
        raw = data["ideas"]
    except (ValueError, KeyError, TypeError):
        print(f"[hearth] couldn't read meal ideas reply: {text[:300]!r}", file=sys.stderr)
        raise ValueError("Claude's answer came back garbled. Try again.")

    def strs(v, n):
        return [str(x).strip()[:80] for x in (v if isinstance(v, list) else []) if str(x).strip()][:n]
    ideas = []
    for it in raw[:count]:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        try:
            minutes = int(it.get("time_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        ideas.append({"name": str(it["name"]).strip()[:80], "description": str(it.get("description", "")).strip()[:240],
                      "time_minutes": minutes, "uses": strs(it.get("uses"), 15),
                      "missing": strs(it.get("missing"), 5), "steps": strs(it.get("steps"), 8)})
    if not ideas:
        raise ValueError("Claude didn't come up with anything. Try again or add more food.")
    return {"ideas": ideas, "meal": meal}


# ---------- recipe database (optional, uses Spoonacular) ----------

def spoon_get(key, path, params):
    req = urllib.request.Request(f"{SPOON_URL}{path}?{urlencode(params)}",
                                 headers={"x-api-key": key, "User-Agent": "Hearth/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"[hearth] Spoonacular error {exc.code} on {path}", file=sys.stderr)
        if exc.code == 401:
            raise ValueError("Spoonacular didn't accept that API key. Check it was copied completely.")
        if exc.code == 402:
            raise ValueError("Today's free recipe searches are used up. They reset every day, "
                             "and recipes you've already found still show.")
        if exc.code == 429:
            raise ValueError("Too many searches at once. Wait a few seconds and try again.")
        raise ValueError(f"The recipe service returned an error ({exc.code}). Try again in a minute.")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("Couldn't reach the recipe service. Check the server's internet connection.")
    except ValueError:
        raise ValueError("The recipe service sent back something unreadable. Try again.")


MEAL_DISH_TYPES = {
    "breakfast": {"breakfast", "brunch", "morning meal"},
    "lunch": {"lunch", "main course", "main dish", "salad", "soup", "sandwich"},
    "dinner": {"dinner", "main course", "main dish", "soup"},
}


def find_recipes(body):
    key = SETTINGS.get("spoonacular_api_key")
    if not key:
        raise ValueError("The recipe database is turned off. Add a Spoonacular API key in Settings.")
    pantry = [str(i.get("text", "")).strip() for i in get_state().get("pantry", [])]
    pantry = [p for p in pantry if p][:40]
    if not pantry:
        raise ValueError("Add some food to the pantry list first.")
    meal = body.get("meal") if body.get("meal") in MEAL_DISH_TYPES else "dinner"
    cache_key = tuple(sorted(p.lower() for p in pantry))
    hit = recipe_cache.get(cache_key)
    if hit and time.time() - hit[0] < 6 * 3600:
        found, details = hit[1]
    else:
        found, details = search_spoonacular(key, pantry)
        recipe_cache[cache_key] = (time.time(), (found, details))
    want = MEAL_DISH_TYPES[meal]
    ideas = []
    for r in found:
        info = details.get(r["id"], {})
        steps = [st.get("step", "").strip() for block in (info.get("analyzedInstructions") or [])[:1]
                 for st in block.get("steps", [])]
        if not steps and info.get("instructions"):
            plain = re.sub(r"<[^>]+>", " ", info["instructions"])
            steps = [t.strip() for t in re.split(r"(?<=[.!?])\s+", plain) if t.strip()]
        dish_types = {d.lower() for d in info.get("dishTypes") or []}
        ideas.append({
            "name": str(r.get("title", "Recipe"))[:100],
            "image": r.get("image") or info.get("image") or "",
            "time_minutes": int(info.get("readyInMinutes") or 0),
            "servings": int(info.get("servings") or 0),
            "uses": [u.get("name", "") for u in r.get("usedIngredients", [])][:15],
            "missing": [m.get("name", "") for m in r.get("missedIngredients", [])][:10],
            "ingredients": [x.get("original", "") for x in info.get("extendedIngredients", [])][:40],
            "steps": [st[:500] for st in steps if st][:25],
            "source_name": info.get("sourceName") or "Spoonacular",
            "source_url": info.get("sourceUrl") or info.get("spoonacularSourceUrl") or "",
            "_fits": bool(dish_types & want),
        })
    ideas.sort(key=lambda x: (not x["_fits"], len(x["missing"])))
    for idea in ideas:
        idea.pop("_fits")
    return {"ideas": ideas[:6], "meal": meal, "source": "spoonacular"}


def search_spoonacular(key, pantry):
    """One search (about 5 quota points) serves breakfast, lunch and dinner."""
    found = spoon_get(key, "/recipes/findByIngredients", {
        "ingredients": ",".join(pantry), "number": 10, "ranking": 2, "ignorePantry": "true"})
    if not found:
        raise ValueError("No recipes matched. Try adding a few more ingredients.")
    ids = ",".join(str(r["id"]) for r in found)
    details = {i["id"]: i for i in spoon_get(key, "/recipes/informationBulk", {"ids": ids, "includeNutrition": "false"})}
    return found, details


# ---------- calendar feeds ----------

def normalize_url(url):
    url = url.strip()
    return "https://" + url[len("webcal://"):] if url.lower().startswith("webcal://") else url


def download_calendar(url):
    req = urllib.request.Request(normalize_url(url), headers={"User-Agent": "Hearth/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return icalendar.Calendar.from_ical(resp.read())


def fetch_calendar(url):
    ttl = SETTINGS.get("refresh_minutes", 10) * 60
    with feed_lock:
        hit = feed_cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        cal = download_calendar(url)
    except Exception as exc:  # keep showing the last good copy if a fetch fails
        print(f"[hearth] couldn't fetch calendar from {urlparse(normalize_url(url)).netloc}: {exc}", file=sys.stderr)
        return hit[1] if hit else None
    with feed_lock:
        feed_cache[url] = (time.time(), cal)
    return cal


last_manual_refresh = 0.0


def refresh_calendars():
    """The refresh button: re-download every linked calendar now. Limited to
    once every 30 seconds so repeated taps don't hammer Google."""
    global last_manual_refresh
    if time.time() - last_manual_refresh < 30:
        return {"ok": True, "skipped": True}
    last_manual_refresh = time.time()
    failed = []
    for cal in list(SETTINGS["calendars"]):
        try:
            fresh = download_calendar(cal["url"])
            with feed_lock:
                feed_cache[cal["url"]] = (time.time(), fresh)
        except Exception as exc:
            print(f"[hearth] refresh: couldn't fetch {urlparse(normalize_url(cal['url'])).netloc}: {exc}", file=sys.stderr)
            failed.append(cal.get("label") or "a calendar")
    return {"ok": True, "failed": failed}


def check_calendar_link(url):
    """Try a link the user pasted. Returns (name, event_count) or raises
    ValueError with a plain-English explanation."""
    url = url.strip()
    if not re.match(r"(?i)^(https?|webcal)://", url):
        raise ValueError("That doesn't look like a calendar link. It should start with https:// or webcal://")
    try:
        cal = download_calendar(url)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise ValueError("The calendar service refused that link. For Google, make sure you copied the "
                             "\"Secret address in iCal format\" (not the public address).")
        raise ValueError(f"The calendar service answered with an error ({exc.code}). Try again in a minute.")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("Couldn't reach that address. Check the link was copied completely.")
    except ValueError:
        raise ValueError("That link opened, but it isn't a calendar file. Look for a link labelled iCal or ICS.")
    with feed_lock:
        feed_cache[url] = (time.time(), cal)
    name = str(cal.get("X-WR-CALNAME", "")).strip()
    count = sum(1 for _ in cal.walk("VEVENT"))
    return name, count


def local_naive(value):
    """Convert timezone-aware datetimes to local time (TZ); leave dates alone."""
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def iso(value):
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%dT%H:%M")
    return value.isoformat()


def events_between(start, end):
    out = []
    for cal_cfg in list(SETTINGS["calendars"]):
        member, url = cal_cfg["member"], cal_cfg["url"]
        cal = fetch_calendar(url)
        if cal is None:
            continue
        try:
            occurrences = recurring_ical_events.of(cal).between(start, end)
        except Exception as exc:
            print(f"[hearth] couldn't expand calendar from {urlparse(normalize_url(url)).netloc}: {exc}", file=sys.stderr)
            continue
        for ev in occurrences:
            s = local_naive(ev["DTSTART"].dt)
            all_day = not isinstance(s, dt.datetime)
            if "DTEND" in ev:
                e = local_naive(ev["DTEND"].dt)
            elif "DURATION" in ev:
                e = s + ev["DURATION"].dt
            else:
                e = s + dt.timedelta(days=1) if all_day else s
            if all_day and isinstance(e, dt.datetime):
                e = e.date()
            out.append({
                "id": f"{ev.get('UID', '')}|{iso(s)}",
                "title": str(ev.get("SUMMARY", "Busy")),
                "location": str(ev.get("LOCATION", "")),
                "start": iso(s),
                "end": iso(e),
                "allDay": all_day,
                "member": member,
                "source": "feed",
            })
    return out


# ---------- HTTP ----------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC, **kwargs)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        # Make browsers check for a new version of the app on every load, so
        # updates show up without anyone having to clear the cache.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 5_000_000:
            raise ValueError("too large")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def change_settings(self, mutate):
        """Run a settings change: PIN check, validation, save, reply."""
        if not pin_ok(self):
            return self.send_json({"error": "pin"}, 403)
        try:
            body = self.read_body()
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, 400)
        with settings_lock:
            updated = deep(SETTINGS)
            try:
                extra = mutate(updated, body) or {}
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
            except KeyError as exc:
                return self.send_json({"error": f"missing {exc}"}, 400)
            try:
                save_settings_doc(updated)
            except Exception as exc:
                print(f"[hearth] couldn't save settings: {exc}", file=sys.stderr)
                return self.send_json({"error": "Couldn't save. Is the database running?"}, 503)
            globals()["SETTINGS"] = updated
        return self.send_json({"config": public_config(), **extra})

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/health":
            return self.send_json({"ok": True, "version": __version__})
        if url.path == "/api/config":
            return self.send_json(public_config())
        if url.path == "/api/state":
            try:
                return self.send_json(get_state())
            except Exception as exc:
                print(f"[hearth] couldn't read state: {exc}", file=sys.stderr)
                return self.send_json({"error": "storage unavailable"}, 503)
        if url.path == "/api/events":
            q = parse_qs(url.query)
            try:
                start = dt.date.fromisoformat(q["start"][0])
                end = dt.date.fromisoformat(q["end"][0])
            except (KeyError, ValueError):
                return self.send_json({"error": "start and end (YYYY-MM-DD) are required"}, 400)
            if not 0 < (end - start).days <= 120:
                return self.send_json({"error": "range must be 1-120 days"}, 400)
            return self.send_json(events_between(start, end))
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == "/api/events/refresh":
            return self.send_json(refresh_calendars())
        if urlparse(self.path).path in ("/api/meal-ideas", "/api/recipes"):
            handler = meal_ideas if self.path.startswith("/api/meal-ideas") else find_recipes
            try:
                return self.send_json(handler(self.read_body()))
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
            except Exception as exc:
                print(f"[hearth] meal ideas failed: {exc}", file=sys.stderr)
                return self.send_json({"error": "Something went wrong. Try again."}, 500)
        if urlparse(self.path).path != "/api/calendars":
            return self.send_json({"error": "not found"}, 404)

        def add(s, body):
            member = body.get("member") or EVERYONE
            if member != EVERYONE and member not in {m["name"] for m in s["members"]}:
                raise ValueError("Pick who this calendar belongs to")
            url = str(body.get("url", "")).strip()
            if any(c["url"] == url for c in s["calendars"]):
                raise ValueError("That calendar is already linked")
            name, count = check_calendar_link(url)
            label = str(body.get("label") or "").strip()[:40] or name[:40]
            s["calendars"].append({"id": new_id(), "member": member, "url": url, "label": label})
            return {"calendar": {"label": label or urlparse(normalize_url(url)).netloc, "events": count}}
        return self.change_settings(add)

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            try:
                incoming = self.read_body()
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
            try:
                saved, state = put_state(incoming)
            except Exception as exc:
                print(f"[hearth] couldn't save state: {exc}", file=sys.stderr)
                return self.send_json({"error": "storage unavailable"}, 503)
            # Not saved = another screen or phone saved first; they get the
            # latest version back and re-apply their change on top.
            return self.send_json(state, 200 if saved else 409)
        if path == "/api/settings":
            return self.change_settings(apply_settings_changes)
        m = re.fullmatch(r"/api/calendars/([0-9a-f]+)", path)
        if m:
            def edit(s, body):
                cal = next((c for c in s["calendars"] if c["id"] == m.group(1)), None)
                if not cal:
                    raise ValueError("That calendar no longer exists")
                if "member" in body:
                    if body["member"] != EVERYONE and body["member"] not in {p["name"] for p in s["members"]}:
                        raise ValueError("Unknown person")
                    cal["member"] = body["member"]
                if "label" in body:
                    cal["label"] = str(body["label"] or "").strip()[:40]
            return self.change_settings(edit)
        return self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/calendars/([0-9a-f]+)", urlparse(self.path).path)
        if not m:
            return self.send_json({"error": "not found"}, 404)

        def remove(s, body):
            s["calendars"] = [c for c in s["calendars"] if c["id"] != m.group(1)]
        return self.change_settings(remove)


def main():
    if DB_HOST:
        db_init()
    init_settings()
    port = int(os.environ.get("HEARTH_PORT", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Hearth {__version__} is running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
