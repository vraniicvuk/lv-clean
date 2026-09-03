# main.py — lv-clean
# Role/channel management + ticket sistem + farm/ratio/qc + !mm AI/FU + off days
import os
import re
import json
import sqlite3
import time
import asyncio
import random
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Modal, TextInput
from discord import TextStyle
from dotenv import load_dotenv
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from openai import OpenAI
from aiohttp import web
from collections import defaultdict

# --- env first ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
USE_AI_FU = os.getenv("USE_AI_FU", "false").lower() in ("1", "true", "yes", "on")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# bridge (telegram -> discord)
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "")
BRIDGE_PORT = int(os.getenv("PORT", os.getenv("BRIDGE_PORT", "8080")))
AS_CHECK_CHANNEL_ID = os.getenv("AS_CHECK_CHANNEL_ID", "")
# build client only after env is loaded
client = OpenAI(api_key=OPENAI_API_KEY) if (USE_AI_FU and OPENAI_API_KEY) else None
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN nije setovan u .env")
# ---------- TUNABLES ----------
SLEEP_BETWEEN_CALLS = 0.35
CHUNK_SIZE = 24
RETRIES = 5
RETRY_BASE_SLEEP = 0.8
PROGRESS_EVERY_N = 5

# QC statistika za /qcurrent
qc_history = defaultdict(list)   # (year, month, user_id) -> list of {'date': day, 'count': num_chattera}

# ---------- BOT ----------
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True  # za !mm detekciju
bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree
GUILD_OBJ = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
# ==== OFF DAYS ====
OFF_DAY_CHANNEL_ID = 1539560126061478049
OFF_CANCEL_ROLE_ID = 1410958063824801802
off_cancel_requests = {}  # cancel_msg_id -> {"off_message_id": int, "user_id": int}
OFF_DAYS_FILE = "off_days.json"   # legacy (migrira se jednom u bazu)
OFF_DAYS_DB = os.getenv("OFF_DAYS_DB", "off_days.db")

SHIFT_ROLES = {
    "afternoon": 1410962344124612710,
    "graveyard": 1410962300554313870,
    "main": 1410962407454675047,
}

# raspored kanala po smeni (koristi /cic)
SCHEDULE_CHANNEL = {
    "grave": 1364850505234518067,
    "after": 1364850574205648967,
    "main": 1364850795215982634,
}

SR_WEEKDAYS = ["ponedeljak", "utorak", "sreda", "četvrtak", "petak", "subota", "nedelja"]

# kome se taguje podsetnik za off dan
REMINDER_TARGET_USER_ID = 923657835164889119

off_days = []


def _db():
    parent = os.path.dirname(OFF_DAYS_DB)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(OFF_DAYS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS off_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            date TEXT NOT NULL,
            shift TEXT,
            message_id INTEGER,
            confirmed INTEGER DEFAULT 0,
            group_id TEXT,
            channel_id INTEGER,
            UNIQUE(user_id, date)
        )
        """
    )
    try:
        conn.execute("ALTER TABLE off_days ADD COLUMN channel_id INTEGER")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reassigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT,
            date TEXT,
            chatter TEXT,
            fans TEXT,
            created_at TEXT,
            done INTEGER DEFAULT 0,
            channel_id INTEGER,
            user_id INTEGER,
            ticket_msg_id INTEGER,
            overview_msg_id INTEGER
        )
        """
    )
    for col in ["done INTEGER DEFAULT 0", "channel_id INTEGER", "user_id INTEGER", "ticket_msg_id INTEGER", "overview_msg_id INTEGER"]:
        try:
            conn.execute(f"ALTER TABLE reassigns ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def migrate_from_json():
    if not os.path.exists(OFF_DAYS_FILE):
        return
    try:
        with open(OFF_DAYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            data = []
    except Exception as e:
        print("[DB] čitanje off_days.json nije uspelo:", e)
        return
    conn = _db()
    inserted = 0
    for e in data:
        if not e.get("date"):
            continue
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO off_days (user_id, username, date, shift, message_id, confirmed, group_id) VALUES (?,?,?,?,?,?,?)",
                (
                    e.get("user_id"),
                    e.get("username"),
                    e.get("date"),
                    e.get("shift"),
                    e.get("message_id"),
                    1 if e.get("confirmed") else 0,
                    None,
                ),
            )
            if cur.rowcount:
                inserted += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    try:
        os.rename(OFF_DAYS_FILE, OFF_DAYS_FILE + ".migrated")
    except Exception:
        pass
    if inserted:
        print(f"[DB] migrirano {inserted} off dana iz off_days.json u bazu")


def load_off_days():
    global off_days
    init_db()
    migrate_from_json()
    conn = _db()
    rows = conn.execute(
        "SELECT user_id, username, date, shift, message_id, confirmed, group_id, channel_id FROM off_days ORDER BY date"
    ).fetchall()
    conn.close()
    off_days = [
        {
            "user_id": r["user_id"],
            "username": r["username"],
            "date": r["date"],
            "shift": r["shift"],
            "message_id": r["message_id"],
            "confirmed": bool(r["confirmed"]),
            "group_id": r["group_id"],
            "channel_id": r["channel_id"],
        }
        for r in rows
    ]


def save_off_days():
    conn = _db()
    conn.execute("DELETE FROM off_days")
    for e in off_days:
        conn.execute(
            "INSERT INTO off_days (user_id, username, date, shift, message_id, confirmed, group_id, channel_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                e.get("user_id"),
                e.get("username"),
                e.get("date"),
                e.get("shift"),
                e.get("message_id"),
                1 if e.get("confirmed") else 0,
                e.get("group_id"),
                e.get("channel_id"),
            ),
        )
    conn.commit()
    conn.close()


def get_state(key):
    conn = _db()
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_state(key, value):
    conn = _db()
    conn.execute(
        "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()
    conn.close()


def get_user_shift(member):
    found = [name for name, rid in SHIFT_ROLES.items() if any(r.id == rid for r in member.roles)]
    if len(found) == 1:
        return found[0]
    if len(found) == 0:
        return None
    return "multiple"


def taken_dates_for_shift(shift):
    out = set()
    today = _local_now().date()
    for e in off_days:
        if e.get("shift") == shift:
            try:
                d = datetime.strptime(e["date"], "%Y-%m-%d").date()
                if d >= today:
                    out.add(d)
            except Exception:
                pass
    return out


def count_month_off_days(user_id):
    month = _local_now().strftime("%Y-%m")
    return sum(1 for e in off_days if e.get("user_id") == user_id and (e.get("date") or "").startswith(month))


def build_date_range():
    today = _local_now().date()
    start = today + timedelta(days=2)  # prvi dostupan dan = prekosutra
    return [start + timedelta(days=i) for i in range(60)]  # narednih 60 dana


load_off_days()

# ==== anti-spam za AI pozive ====
AI_BLOCKED_UNTIL = None


def ai_available():
    return (AI_BLOCKED_UNTIL is None) or (datetime.utcnow() >= AI_BLOCKED_UNTIL)


def backoff_ai(minutes=30):
    global AI_BLOCKED_UNTIL
    AI_BLOCKED_UNTIL = datetime.utcnow() + timedelta(minutes=minutes)


async def safe_generate_fus(mm_line: str, channel_id: int) -> list[str]:
    """Proba AI, ako ne moze ili nema AI, pada na offline, bez rate limita."""
    # ako nema AI ili smo u backoff stanju → offline
    if (not USE_AI_FU) or (not client) or (not ai_available()):
        fus = await generate_fus_offline(mm_line)
        print(f"[AI_FU] offline only, got {len(fus)} fus")
        return fus
    try:
        fus = await generate_fus(mm_line)
        if fus:
            print(f"[AI_FU] ai ok, got {len(fus)} fus")
            return fus
        print("[AI_FU] ai returned empty, using offline fallback")
        return await generate_fus_offline(mm_line)
    except Exception as e:
        text = str(e).lower()
        if "insufficient_quota" in text or "error code: 429" in text or "quota" in text:
            backoff_ai(60)
            print("[AI_FU] quota hit, switching to offline for 60 minutes")
        else:
            print("[AI_FU] fail, offline fallback:", e)
        return await generate_fus_offline(mm_line)


async def generate_fus_offline(mm_line: str) -> list[str]:
    txt = await gen_fu_offline(mm_line)
    lines = [ln for ln in txt.splitlines() if ln.strip().startswith("fu")]
    return lines[:4]





def _local_now():
    """Tačno vreme za Beograd (automatski CET/CEST)"""
    return datetime.now(ZoneInfo("Europe/Belgrade"))









async def sort_team_categories(guild):
    categories = guild.categories
    non_team = [c for c in categories if not c.name.upper().startswith("TEAM ")]
    team = [c for c in categories if c.name.upper().startswith("TEAM ")]
    team_sorted = sorted(team, key=lambda c: c.name.lower())
    start_pos = max(c.position for c in non_team)
    pos = start_pos + 1
    for cat in team_sorted:
        await cat.edit(position=pos)
        pos += 1
        await asyncio.sleep(0.35)
    print("TEAM categories sorted")



# ====== AI/FU HELPERI ======
def _sanitize_mm_text(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[\U00010000-\U0010ffff]", "", s)  # skini emojije (van BMP)
    s = s.replace("—", " ").replace("-", " ")  # zabrana crtica i duge crte
    s = re.sub(r"\s+", " ", s)
    return s


async def gen_fu_offline(question: str) -> str:
    q = _sanitize_mm_text(question)
    if any(k in q for k in ["bath", "shower", "tub"]):
        fu1 = "fu1: you like it hot or just steamy"
        fu2 = "fu2: i’ll sit on the edge and make the water useless"
        fu3 = "fu3: which part do you soap first when i step in"
    elif any(k in q for k in ["bed", "couch", "sofa", "ride"]):
        fu1 = "fu1: on your lap or on my face"
        fu2 = "fu2: i bounce till your legs shake"
        fu3 = "fu3: how long till you beg me to slow down"
    elif any(k in q for k in ["minute", "last", "control", "tease"]):
        fu1 = "fu1: bite or kiss first"
        fu2 = "fu2: i keep you on the edge till you whine"
        fu3 = "fu3: where do you lose control the fastest"
    else:
        banks = [
            (
                "fu1: taster or toucher",
                "fu2: i’ll keep it just out of reach till you ask nice",
                "fu3: where do you want me first",
            ),
            (
                "fu1: slow or rough tonight",
                "fu2: i set the pace you just try to keep up",
                "fu3: what safe word are you not going to use",
            ),
            (
                "fu1: hands behind your back or on my hips",
                "fu2: i make you work for every inch",
                "fu3: what do you want me to say when you break",
            ),
        ]
        fu1, fu2, fu3 = random.choice(banks)
    return f"!mma\n{q}\n\n{fu1}\n{fu2}\n{fu3}"


AI_FU_SYSTEM = (
    "from now on you write engaging follow ups.\n"
    "format and rules:\n"
    "!mma\n"
    "<short simple question already written by the user>\n\n"
    "you never write the question yourself.\n"
    "you only write follow ups for an existing !mma line.\n\n"
    "critical logic:\n"
    "- before writing follow ups you must silently infer the most likely fan reply to the !mma question.\n"
    "- the inferred reply is never shown.\n"
    "- do not answer the !mma question.\n"
    "- do not react directly to the !mma question.\n"
    "- always assume the fan has already replied.\n"
    "- fu1 must react to the inferred fan reply.\n"
    "- for vague questions assume the fan replied with curiosity confusion surprise or a request for clarification.\n"
    "- write follow ups that work for the most likely category of replies rather than one exact reply.\n\n"
    "fu1: response to the inferred fan reply. statement only. no question.\n"
    "fu1.5: open ended question related to fu1.\n"
    "fu2: statement that deepens the conversation.\n"
    "fu2.5: open ended question related to fu2.\n"
    "fu3: final engaging statement.\n"
    "fu3.5: one more open ended question.\n\n"
    "hard style rules:\n"
    "- everything must be in lowercase.\n"
    "- no bold no emojis.\n"
    "- no commas no dashes. only periods and spaces.\n"
    "- vary structure and wording every time.\n"
    "- never repeat lines.\n"
    "- fu1.5 fu2.5 and fu3.5 cannot be answered with yes or no.\n"
    "- never use the phrase either way.\n"
    "- never start with soft intros like ever wondered or what if.\n\n"
    "output rules:\n"
    "- output only fu lines.\n"
    "- never output !mma.\n"
    "- never output the question.\n"
    "- output only lines starting with: fu1: fu1.5: fu2: fu2.5: fu3: fu3.5:\n"
)


def _fu_prompt(mm_line: str) -> str:
    return (
        "user mm line:\n"
        f"!mma {mm_line}\n\n"
        "generate fus for this mm.\n"
        "follow all system rules.\n"
        "do not rewrite the question.\n"
        "do not add anything except fu1 fu1.5 fu2 fu2.5 fu3 fu3.5 lines.\n"
    )


async def generate_fus(mm_line: str) -> list[str]:
    if not client:
        return []
    prompt = _fu_prompt(mm_line)
    # OpenAI python lib je sync; izvrši u thread-u da ne blokira event loop
    def _call():
        rsp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": AI_FU_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.9,
            max_tokens=120,
        )
        return rsp.choices[0].message.content.strip()

    text = await asyncio.to_thread(_call)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    labeled, i = [], 1
    for ln in lines:
        if ":" in ln[:8].lower():
            labeled.append(ln)
        else:
            key = (
                "fu1:"
                if i == 1
                else ("fu1.5:" if i == 2 else ("fu2:" if i == 3 else "fu2.5:"))
            )
            labeled.append(f"{key} {ln.lower()}")
        i += 1
    return labeled[:4]


# ---------- ROLE LOOKUP ----------
def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (s or "").upper())


def parse_roles_from_text(guild: discord.Guild, text: str) -> list[discord.Role]:
    ids = re.findall(r"<@&(\d+)>", text or "")
    return [guild.get_role(int(x)) for x in ids if guild.get_role(int(x))]


def parse_user_ids(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"<@!?(\d+)>", text or "")]


async def ensure_member(guild: discord.Guild, user_id: int):
    m = guild.get_member(user_id)
    if m:
        return m
    try:
        return await guild.fetch_member(user_id)
    except:
        return None


def member_from_token(guild: discord.Guild, token: str):
    ids = parse_user_ids(token)
    if ids:
        return guild.get_member(ids[0]) or None
    cleaned = token.replace("@", "").strip()
    if not cleaned:
        return None
    for m in guild.members:
        if m.display_name.lower() == cleaned.lower() or (
            m.name and m.name.lower() == cleaned.lower()
        ):
            return m
    target = norm(cleaned)
    for m in guild.members:
        if norm(m.display_name) == target or norm(m.name) == target:
            return m
    return None


def can_touch_role(bot_member: discord.Member, role: discord.Role) -> bool:
    if role is None:
        return False
    if role.is_default():
        return False
    if role.managed:
        return False
    return bot_member.guild_permissions.manage_roles and bot_member.top_role > role


def why_blocked(bot_member: discord.Member, role: discord.Role):
    r = []
    if role.is_default():
        r.append("everyone")
    if role.managed:
        r.append("managed")
    if not bot_member.guild_permissions.manage_roles:
        r.append("no Manage Roles")
    if bot_member.top_role <= role:
        r.append("bot below role")
    return r or ["ok"]


# ==================== PERMISSION CHECKS ====================
def need_manage_roles():
    def predicate(interaction: discord.Interaction):
        gp = interaction.user.guild_permissions
        if gp.manage_roles or gp.administrator:
            return True
        raise app_commands.CheckFailure("treba ti Manage Roles.")

    return app_commands.check(predicate)


def need_manage_channels():
    async def predicate(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "Nemaš Manage Channels permisiju.", ephemeral=True
            )
            return False
        return True

    return app_commands.check(predicate)




async def safe_add_roles(
    member: discord.Member, roles: list[discord.Role], reason: str
):
    added = []
    for i in range(0, len(roles), CHUNK_SIZE):
        chunk = roles[i : i + CHUNK_SIZE]
        for attempt in range(1, RETRIES + 1):
            try:
                if chunk:
                    await member.add_roles(*chunk, reason=reason)
                    added.extend(chunk)
                await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                break
            except discord.Forbidden:
                raise
            except Exception:
                if attempt >= RETRIES:
                    raise
                await asyncio.sleep(RETRY_BASE_SLEEP * attempt)
    return added


async def safe_remove_roles(
    member: discord.Member, roles: list[discord.Role], reason: str
):
    removed = []
    for i in range(0, len(roles), CHUNK_SIZE):
        chunk = roles[i : i + CHUNK_SIZE]
        for attempt in range(1, RETRIES + 1):
            try:
                if chunk:
                    await member.remove_roles(*chunk, reason=reason)
                    removed.extend(chunk)
                await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                break
            except discord.Forbidden:
                raise
            except Exception:
                if attempt >= RETRIES:
                    raise
                await asyncio.sleep(RETRY_BASE_SLEEP * attempt)
    return removed


@tree.command(description="dodeli više rola jednom useru", guild=GUILD_OBJ)
@need_manage_roles()
async def assign(interaction: discord.Interaction, user: discord.Member, roles: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    bot_member = guild.me
    role_objs = parse_roles_from_text(guild, roles)
    if not role_objs:
        return await interaction.followup.send(
            "pinguj role: @Role1 @Role2", ephemeral=True
        )
    ok = [r for r in role_objs if can_touch_role(bot_member, r)]
    bad = [r for r in role_objs if r not in ok]
    try:
        added = await safe_add_roles(user, ok, reason=f"by {interaction.user}")
        msg = [
            f"dodato {user.display_name}: {', '.join(r.name for r in added) or 'ništa'}"
        ]
        for r in bad:
            msg.append(f"preskočeno {r.name}: {' / '.join(why_blocked(bot_member, r))}")
        await interaction.followup.send(
            "```\n" + "\n".join(msg) + "\n```", ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"fail: {e}", ephemeral=True)

# ========== /d - Brisanje trenutnog kanala (bez potvrde) ==========
@tree.command(
    name="d",
    description="Briše trenutni kanal u kojem se komanda koristi",
    guild=GUILD_OBJ
)
@need_manage_channels()
async def delete_channel(interaction: discord.Interaction):
    channel = interaction.channel

    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Ova komanda radi samo u tekstualnim kanalima.", ephemeral=True)
        return

    channel_name = channel.name

    try:
        # Prvo odgovori, pa onda obriši
        await interaction.response.send_message(
            f"🗑 Brišem kanal `{channel_name}`...",
            ephemeral=True
        )
        await asyncio.sleep(0.8)  # malo vremena da se poruka pošalje
        await channel.delete(reason=f"/d by {interaction.user}")
    except discord.Forbidden:
        await interaction.followup.send("❌ Nemam dozvolu da obrišem ovaj kanal.", ephemeral=True)
    except Exception as e:
        try:
            await interaction.followup.send(f"❌ Greška: {e}", ephemeral=True)
        except:
            pass

# ========== /dcat - BRISANJE KANALA + KATEGORIJE ==========
@tree.command(
    name="dcat",
    description="Briše trenutni kanal + celu kategoriju u kojoj se nalazi (OPREZNO!)",
    guild=GUILD_OBJ
)
@need_manage_channels()  # ili @need_admin() ako imaš tu dekorator
async def dcat(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    channel = interaction.channel
    if not channel:
        await interaction.followup.send("❌ Nije moguće dohvatiti kanal.", ephemeral=True)
        return

    category = channel.category
    if not category:
        await interaction.followup.send("❌ Ovaj kanal nije u kategoriji.", ephemeral=True)
        return

    channel_name = channel.name
    category_name = category.name
    channel_count = len(category.channels)

    try:
        # Prvo šaljemo potvrdu
        await interaction.followup.send(
            f"🗑 **Brišem kanal** `{channel_name}`\n"
            f"📁 **I kategoriju** `{category_name}` (ima {channel_count} kanala)\n\n"
            f"**Ovo se ne može vratiti!** Potvrdi sa `da` ako si siguran.",
            ephemeral=True
        )

        # Čekamo potvrdu od korisnika (30 sekundi)
        def check(msg):
            return msg.author == interaction.user and msg.channel == interaction.channel and msg.content.lower() in ["da", "yes", "ok"]

        try:
            msg = await bot.wait_for('message', check=check, timeout=30.0)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Vreme je isteklo. Ništa nije obrisano.", ephemeral=True)
            return

        # Brisanje
        await channel.delete(reason=f"dcat by {interaction.user}")
        await asyncio.sleep(1)  # malo pauze

        await category.delete(reason=f"dcat by {interaction.user}")

        await interaction.followup.send(
            f"✅ **Uspešno obrisano!**\n"
            f"Kanal: `{channel_name}`\n"
            f"Kategorija: `{category_name}`",
            ephemeral=True
        )

    except discord.Forbidden:
        await interaction.followup.send("❌ Nemam dozvolu da brišem kanale/kategorije.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Greška: {e}", ephemeral=True)

@tree.command(description="skini konkretne role sa usera", guild=GUILD_OBJ)
@need_manage_roles()
async def deassign(interaction: discord.Interaction, user: discord.Member, roles: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    bot_member = guild.me
    role_objs = parse_roles_from_text(guild, roles)
    if not role_objs:
        return await interaction.followup.send(
            "pinguj role: @Role1 @Role2", ephemeral=True
        )
    ok = [r for r in role_objs if can_touch_role(bot_member, r)]
    bad = [r for r in role_objs if r not in ok]
    try:
        removed = await safe_remove_roles(user, ok, reason=f"by {interaction.user}")
        msg = [
            f"skinuto {user.display_name}: {', '.join(r.name for r in removed) or 'ništa'}"
        ]
        for r in bad:
            msg.append(f"preskočeno {r.name}: {' / '.join(why_blocked(bot_member, r))}")
        await interaction.followup.send(
            "```\n" + "\n".join(msg) + "\n```", ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"fail: {e}", ephemeral=True)


@tree.command(description="skini sve role koje bot sme (jedan user)", guild=GUILD_OBJ)
@need_manage_roles()
async def clean(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    bot_member = guild.me
    removable = [r for r in user.roles if can_touch_role(bot_member, r)]
    blocked = [r for r in user.roles if r not in removable and not r.is_default()]
    if not removable:
        return await interaction.followup.send(
            f"nema šta da skidam sa {user.display_name}", ephemeral=True
        )
    removed = await safe_remove_roles(user, removable, reason=f"by {interaction.user}")
    msg = [
        f"obrisano {user.display_name}: {', '.join(r.name for r in removed) or 'ništa'}"
    ]
    if blocked:
        msg.append("preskočeno:")
        for r in blocked:
            msg.append(f"- {r.name}: {' / '.join(why_blocked(bot_member, r))}")
    await interaction.followup.send("```\n" + "\n".join(msg) + "\n```", ephemeral=True)

# ========== /migrateprefix - Zamena TEAM → > prefix ==========
@tree.command(
    name="migrateprefix",
    description="Jednokratno: menja sve TEAM role u > prefix",
    guild=GUILD_OBJ
)
@need_manage_roles()
async def migrate_prefix(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    bot_member = guild.me

    # Pronalazimo sve TEAM role
    team_roles = [
        r for r in guild.roles 
        if r.name.upper().startswith("TEAM ") 
        and r < bot_member.top_role 
        and not r.managed
    ]

    if not team_roles:
        return await interaction.followup.send("❌ Nisam pronašao nijednu TEAM rolu.", ephemeral=True)

    await interaction.followup.send(
        f"🔄 Počinjem migraciju **{len(team_roles)}** rola...\n"
        "Ovo može potrajati. Ne zatvaraj Discord.",
        ephemeral=True
    )

    success = 0
    errors = []

    for role in team_roles:
        try:
            old_name = role.name
            new_name = ">" + old_name[5:].strip()  # TEAM XXX → >XXX

            if new_name == old_name:
                continue

            await role.edit(name=new_name, reason=f"Migrate prefix by {interaction.user}")
            success += 1
            print(f"✅ {old_name} → {new_name}")
            await asyncio.sleep(1.2)  # da ne bi rate limit

        except discord.Forbidden:
            errors.append(f"❌ Nema permisiju za: {role.name}")
        except Exception as e:
            errors.append(f"❌ Greška kod {role.name}: {e}")

    # Završni izveštaj
    embed = discord.Embed(
        title="✅ Migracija prefixa završena",
        color=0x00ff88
    )
    embed.add_field(name="Uspešno promenjeno", value=f"{success}/{len(team_roles)} rola", inline=False)

    if errors:
        embed.add_field(name="Greške", value="\n".join(errors[:15]), inline=False)  # max 15 grešaka

    embed.set_footer(text=f"Izvršio: {interaction.user}")

    await interaction.followup.send(embed=embed)

@tree.command(
    name="a", description="batch assign: @u1 @r1 @r2 ; @u2 @r3 ...", guild=GUILD_OBJ
)
@need_manage_roles()
async def a_batch(interaction: discord.Interaction, payload: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    bot_member = guild.me
    text = payload.replace(";", " ")
    tokens = re.findall(r"<@!?(\d+)>|<@&(\d+)>", text)
    batches, current_uid, current_roles = [], None, []
    for uid, rid in tokens:
        if uid:
            if current_uid and current_roles:
                batches.append((current_uid, current_roles))
                current_roles = []
            current_uid = int(uid)
        else:
            role = guild.get_role(int(rid))
            if current_uid:
                current_roles.append(role)
    if current_uid and current_roles:
        batches.append((current_uid, current_roles))
    if not batches:
        return await interaction.followup.send(
            "nisam našao user+role kombinacije", ephemeral=True
        )
    lines = []
    for idx, (uid, roles) in enumerate(batches, start=1):
        member = await ensure_member(guild, uid)
        if not member:
            lines.append(f"[{idx}] user nije nađen")
            continue
        ok = [r for r in roles if can_touch_role(bot_member, r)]
        try:
            added = await safe_add_roles(
                member, ok, reason=f"batch by {interaction.user}"
            )
            lines.append(
                f"[{idx}] {member.display_name} dodato: {', '.join(r.name for r in added) or 'ništa'}"
            )
        except Exception as e:
            lines.append(f"[{idx}] {member.display_name} FAIL: {e}")
        if idx % PROGRESS_EVERY_N == 0:
            await interaction.followup.send(
                f"napredak: {idx}/{len(batches)} gotovih…", ephemeral=True
            )
    msg = "rezime:\n" + "\n".join(lines)
    for i in range(0, len(msg), 1800):
        await interaction.followup.send(f"```\n{msg[i:i+1800]}\n```", ephemeral=True)


@tree.command(
    name="cleanmulti",
    description="clean više usera; zadrži navedene role (keep)",
    guild=GUILD_OBJ,
)
@need_manage_roles()
async def clean_multi(interaction: discord.Interaction, users: str, keep: str = ""):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    bot_member = guild.me
    user_ids = parse_user_ids(users)
    keep_roles = parse_roles_from_text(guild, keep or "")
    keep_ids = {r.id for r in keep_roles}
    if not user_ids:
        return await interaction.followup.send("nisi tagovao korisnike", ephemeral=True)
    lines = []
    for idx, uid in enumerate(user_ids, start=1):
        member = await ensure_member(guild, uid)
        if not member:
            lines.append(f"[{idx}] user nije nađen")
            continue
        removable = [
            r
            for r in member.roles
            if can_touch_role(bot_member, r) and r.id not in keep_ids
        ]
        blocked = [
            r
            for r in member.roles
            if (r.id in keep_ids)
            or (not can_touch_role(bot_member, r) and not r.is_default())
        ]
        try:
            removed = await safe_remove_roles(
                member, removable, reason=f"cleanmulti by {interaction.user}"
            )
            ok_names = ", ".join(r.name for r in removed) if removed else "ništa"
            if blocked:
                why = "; ".join(
                    f"{r.name} [{' / '.join(['KEEP'] if r.id in keep_ids else why_blocked(bot_member, r))}]"
                    for r in blocked
                    if r
                )
                lines.append(
                    f"[{idx}] {member.display_name} obrisano: {ok_names} preskočeno: {why}"
                )
            else:
                lines.append(f"[{idx}] {member.display_name} obrisano: {ok_names}")
        except Exception as e:
            lines.append(f"[{idx}] {member.display_name} FAIL: {e}")
        if idx % PROGRESS_EVERY_N == 0:
            await interaction.followup.send(
                f"napredak: {idx}/{len(user_ids)} gotovih…", ephemeral=True
            )
    msg = "rezime /cleanmulti:\n" + "\n".join(lines)
    for i in range(0, len(msg), 1800):
        await interaction.followup.send(f"```\n{msg[i:i+1800]}\n```", ephemeral=True)


# ---------- /farm (modal forma) ----------
FARM_REMINDER_TIMES = [3600, 7200, 10800]  # 1h, 2h, 3h
farm_reminders = {}  # message_id -> {"user_id", "channel_id", "created_at", "sent":[b,b,b], "done": bool}


class FarmModal(Modal, title="Farm unos"):
    def __init__(self, opener: discord.Member):
        super().__init__(timeout=None)
        self.opener = opener
        self.amount = TextInput(
            label="Iznos", placeholder="npr. 25 ili $25", required=True, max_length=32
        )
        self.model_name = TextInput(
            label="Ime modela",
            placeholder="npr. cami / haley / ...",
            required=True,
            max_length=100,
        )
        self.fan_username = TextInput(
            label="Username fana",
            placeholder="npr. @fan123 ili fan#0001",
            required=True,
            max_length=100,
        )
        self.more_details = TextInput(
            label="Više detalja",
            style=TextStyle.paragraph,
            placeholder="optionalno: linkovi, napomena…",
            required=False,
            max_length=1000,
        )
        self.add_item(self.amount)
        self.add_item(self.model_name)
        self.add_item(self.fan_username)
        self.add_item(self.more_details)

    async def on_submit(self, interaction: discord.Interaction):
        farm_roles = " ".join(
            f"<@&{rid}>" for rid in [1410958063824801802, 1410962105749995591, 1474070997274464379]
        )
        lines = [
            f"**Novi farm unos** (by {self.opener.mention}):",
            f"- Iznos: `{self.amount.value.strip()}`",
            f"- Model: `{self.model_name.value.strip()}`",
            f"- Fan: `{self.fan_username.value.strip()}`",
        ]
        extra = self.more_details.value.strip() if self.more_details.value else ""
        if extra:
            lines.append(f"- Detalji: {extra}")

        # 1) forma (bez pitanja)
        await interaction.response.send_message("\n".join(lines))
        msg = await interaction.original_response()
        try:
            await msg.add_reaction("✅")
            await msg.add_reaction("🚫")
        except:
            pass

        # 2) pitanje + tag onog ko je poslao + ✅ react + reminder tracking
        try:
            q_msg = await interaction.channel.send(
                f"**Pitanje:** da li je fan dodat na odgovarajuće liste i da li su ažurirane beleške o istom?\n{self.opener.mention}"
            )
            await q_msg.add_reaction("✅")
            farm_reminders[q_msg.id] = {
                "user_id": self.opener.id,
                "channel_id": interaction.channel.id,
                "created_at": time.time(),
                "sent": [False, False, False],
                "done": False,
            }
        except Exception as e:
            print("[FARM] question send fail:", e)

        # 3) zasebna poruka sa tagom rola
        try:
            await interaction.channel.send(farm_roles)
        except Exception as e:
            print("[FARM] role ping fail:", e)


@tree.command(name="farm", description="Otvori formu za farm unos", guild=GUILD_OBJ)
async def farm(interaction: discord.Interaction):
    await interaction.response.send_modal(FarmModal(opener=interaction.user))


# ========== QC - JEDNOSTAVNA VERZIJA SA ČUVANJEM PODATAKA ==========
@tree.command(name="qc", description="Pošalji Daily QC listu chattera", guild=GUILD_OBJ)
async def qc(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Daily QC - Unos chattera**\n\n"
        "Pošalji listu chattera (jedno ime po liniji):",
        ephemeral=True
    )

    def check(m):
        return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

    try:
        msg = await bot.wait_for('message', check=check, timeout=300)
        
        chatters = [line.strip() for line in msg.content.strip().split("\n") if line.strip()]
        
        if not chatters:
            return await interaction.followup.send("❌ Nisi uneo nijednog chattera.", ephemeral=True)

        now = datetime.now()

        # === ČUVANJE ZA /QCURRENT ===
        month_key = (now.year, now.month, interaction.user.id)
        qc_history[month_key].append({
            'date': now.day,
            'count': len(chatters),
            'timestamp': now
        })

        # Broj QC-a ovog meseca
        monthly_count = len(qc_history[month_key])

        # Pravljenje reporta
        report = f"**qc broj {monthly_count} ovog meseca**\n"
        report += f"**Datum i vreme:** {now.strftime('%d.%m.%Y %H:%M')}\n"
        report += f"**Broj chattera:** {len(chatters)}\n"
        report += f"**Popunio:** {interaction.user.mention}\n\n"
        report += "**Chatteri:**\n"
        
        for i, chatter in enumerate(chatters, 1):
            report += f"{i}. {chatter}\n"

        qc_channel = interaction.guild.get_channel(1493996105380266114)
        if qc_channel:
            await qc_channel.send(report)
            await qc_channel.send(
                f"<@{923657835164889119}> <@{886983698321391667}> "
                f"**Daily QC je poslat (broj {monthly_count} ovog meseca).**"
            )

        await interaction.followup.send(
            f"✅ QC uspešno poslat (ovo je tvoj **{monthly_count}.** QC ovog meseca).", 
            ephemeral=True
        )

    except asyncio.TimeoutError:
        await interaction.followup.send("Vreme za unos liste je isteklo.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Greška: {e}", ephemeral=True)



@tree.command(name="qcurrent", description="Pregled QC reporta za trenutni mesec", guild=GUILD_OBJ)
@need_manage_roles()
async def qcurrent(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    now = datetime.now()
    current_month = (now.year, now.month)

    user_stats = defaultdict(list)

    for key, entries in qc_history.items():
        year, month, user_id = key
        if (year, month) == current_month:
            user = interaction.guild.get_member(user_id)
            username = user.display_name if user else f"@{user_id}"
            for entry in entries:
                user_stats[username].append((entry['date'], entry['count']))

    if not user_stats:
        return await interaction.followup.send("Još nema QC reporta ovog meseca.", ephemeral=True)

    lines = []
    total_reports = 0

    for username, data in sorted(user_stats.items()):
        data = sorted(data)
        count = len(data)
        total_reports += count
        dates_str = ", ".join([f"{day}. ({num})" for day, num in data])
        lines.append(f"**{username}** - {count} qc reportova\n{dates_str}")

    response = f"**QC Report - {now.strftime('%B %Y')}**\n\n"
    response += "\n\n".join(lines)
    response += f"\n\n**Ukupno: {total_reports} QC reportova ovog meseca**"

    embed = discord.Embed(description=response, color=0x00ff88)
    embed.set_footer(text=f"Generisano: {now.strftime('%d.%m.%Y %H:%M')}")

    await interaction.followup.send(embed=embed, ephemeral=True)

# ========== /ratio - RATIO REPORT (sa 20% odbitkom + ko je poslao) ==========
class RatioModal(Modal, title="Ratio Report"):
    def __init__(self):
        super().__init__(timeout=None)

        self.model = TextInput(
            label="Ime modela",
            placeholder="npr. Celeste ili Kara",
            required=True,
            max_length=100
        )
        self.shift = TextInput(
            label="Shift",
            placeholder="GRAVEYARD / AFTERNOON / MAIN",
            required=True,
            max_length=50
        )
        self.gross_made = TextInput(
            label="Gross Made ($)",
            placeholder="npr. 1550",
            required=True,
            max_length=20
        )
        self.new_subs = TextInput(
            label="New Subs",
            placeholder="npr. 45",
            required=True,
            max_length=10
        )
        self.avg_sub_price = TextInput(
            label="Avg Sub Price ($)",
            placeholder="npr. 8.5",
            required=True,
            max_length=10
        )

        self.add_item(self.model)
        self.add_item(self.shift)
        self.add_item(self.gross_made)
        self.add_item(self.new_subs)
        self.add_item(self.avg_sub_price)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            model_name = self.model.value.strip()
            shift_name = self.shift.value.strip().upper()
            gross_made = float(self.gross_made.value.replace(',', '.').strip())
            new_subs = int(self.new_subs.value.strip())
            avg_sub_price = float(self.avg_sub_price.value.replace(',', '.').strip())

            if new_subs <= 0:
                ratio_formatted = "N/A (0 subs)"
            else:
                net_made = gross_made * 0.8
                effective_sub_price = avg_sub_price * 0.8
                expected = new_subs * effective_sub_price
                ratio_value = net_made / expected

                if ratio_value >= 1:
                    ratio_formatted = f"1:{ratio_value:.1f}"
                else:
                    ratio_formatted = f"{ratio_value:.1f}:1"

            now = _local_now()

            # === PUNA PORUKA U PRIVATNI KANAL (sa imenom chattera) ===
            full_report = (
                f"**Ratio Report**\n"
                f"**Poslao:** {interaction.user.mention} ({interaction.user.display_name})\n"
                f"**Datum i vreme:** {now.strftime('%d.%m.%Y %H:%M')}\n"
                f"**Model:** {model_name}\n"
                f"**Shift:** {shift_name}\n"
                f"**Gross Made:** ${gross_made:,.0f}\n"
                f"**Net Made (posle 20%):** ${net_made:,.0f}\n"
                f"**New Subs:** {new_subs}\n"
                f"**Avg Sub Price:** ${avg_sub_price:.2f}\n"
                f"**Effective Sub Price (posle 20%):** ${effective_sub_price:.2f}\n"
                f"**Ratio:** `{ratio_formatted}`"
            )

            private_channel = bot.get_channel(1496174722151354418)
            if private_channel:
                await private_channel.send(full_report)

            # === KRATKA POTVRDA U JAVNOM KANALU ===
            short_confirm = (
                f"✅ {interaction.user.mention} je uspešno poslao ratio za **{model_name}** "
                f"({now.strftime('%d.%m.%Y %H:%M')}, {shift_name} smena).\n"
            )
            await interaction.response.send_message(short_confirm, ephemeral=False)

        except ValueError:
            await interaction.response.send_message(
                "❌ Greška pri unosu brojeva. Proveri da li si uneo validne brojeve (Gross Made, New Subs, Avg Sub Price).",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Neočekivana greška: {e}", ephemeral=True)


@tree.command(
    name="ratio",
    description="Popuni i pošalji Ratio Report (sa 20% odbitkom)",
    guild=GUILD_OBJ
)
async def ratio_command(interaction: discord.Interaction):
    await interaction.response.send_modal(RatioModal())


@tree.command(name="sortteamroles", description="Bulk sort TEAM roles", guild=GUILD_OBJ)
@need_manage_roles()
async def sortteamroles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    bot_top = guild.me.top_role.position
    editable = [
        r
        for r in guild.roles
        if not r.managed and r != guild.default_role and r.position < bot_top
    ]
    team = sorted(
        [r for r in editable if r.name.startswith("TEAM ")],
        key=lambda r: r.name.lower(),
    )
    non_team = sorted(
        [r for r in editable if not r.name.startswith("TEAM ")],
        key=lambda r: r.position,
        reverse=True,  # da zadrži postojeći red gore
    )
    final_stack = non_team + team
    # 🔥 OVO JE KLJUČ
    final_stack.reverse()
    payload = {role: i + 1 for i, role in enumerate(final_stack)}
    await guild.edit_role_positions(payload)
    await interaction.followup.send(
        "Roles sorted: NON TEAM top → TEAM A-Z bottom", ephemeral=True
    )

@tree.command(
    name="sortteamcats",
    description="Sortira TEAM kategorije A-Z (od vrha ka dnu)",
    guild=GUILD_OBJ
)
@need_manage_roles()
async def sortteamcats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    
    try:
        await sort_team_categories(guild)
        await interaction.followup.send("✅ TEAM kategorije su uspešno sortirane (A-Z).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Greška pri sortiranju kategorija: {e}", ephemeral=True)

# ========== /cic - Clock In Check + Blanko lista za copy ==========
@tree.command(
    name="cic",
    description="Clock In Check - pojedinačne poruke + blanko lista za copy",
    guild=GUILD_OBJ
)
@need_manage_roles()
async def cic(interaction: discord.Interaction, shift: str = None):
    await interaction.response.defer(ephemeral=True)

    if shift is None:
        ch_name = interaction.channel.name.lower()
        if "grave" in ch_name: shift = "grave"
        elif "after" in ch_name or "afternoon" in ch_name: shift = "after"
        elif "main" in ch_name: shift = "main"
        else:
            return await interaction.followup.send("❌ Koristi: `/cic grave` / `after` / `main`", ephemeral=True)

    mgmt_channel = bot.get_channel(1498220907775262750)
    schedule_channel = bot.get_channel(SCHEDULE_CHANNEL.get(shift))

    if not mgmt_channel or not schedule_channel:
        return await interaction.followup.send("❌ Neki kanal nije pronađen.", ephemeral=True)

    # Pronalazak poslednjeg rasporeda
    messages = [msg async for msg in schedule_channel.history(limit=30)]
    schedule_msg = None
    for msg in messages:
        if "@" in msg.content and any(x in msg.content for x in [":", "/", ","]):
            schedule_msg = msg
            break

    if not schedule_msg:
        return await interaction.followup.send(f"❌ Nisam pronašao raspored u {shift.upper()} kanalu.", ephemeral=True)

    # Parsiranje chattera
    text = schedule_msg.content
    pattern = re.compile(r'(@[\.\w]+|<\@!?\d+>)(.*?)((?=@[\.\w]+|<\@!?\d+>)|$)', re.S)
    blocks = pattern.findall(text)

    chatter_names = []
    for first_user, content, _ in blocks:
        assignees = re.findall(r'(@[\.\w]+|<\@!?\d+>)', first_user + content)
        for token in assignees:
            member = member_from_token(interaction.guild, token)
            name = member.display_name if member else token
            chatter_names.append(name)

    unique_names = list(dict.fromkeys(chatter_names))  # uklanja duplikate

    # === 1. Header poruka ===
    await mgmt_channel.send(
        f"**🕒 {shift.upper()} CLOCK IN CHECK**\n"
        f"**Raspored:** {schedule_msg.jump_url}\n"
        f"**Ukupno:** {len(unique_names)} chattera\n"
        "────────────────────"
    )

    # === 2. Pojedinačne poruke sa ✅ ===
    for i, name in enumerate(unique_names, 1):
        msg = await mgmt_channel.send(f"`{i:2d}.` **{name}**")
        await msg.add_reaction("✅")

    # === 3. BLANKO PORUKA ZA COPY (ono što si tražio) ===
    blanko_lista = ", ".join(unique_names)
    await mgmt_channel.send(f"!check {blanko_lista}")

    await interaction.followup.send(
        f"✅ Gotovo! Poslao sam **{len(unique_names)}** chattera u management kanal.\n"
        "Na dnu je **blanko lista** koju možeš desni klik → Copy.", 
        ephemeral=True
    )

# ========== /vis - Dodeli vidljivost roli za sve TEAM kategorije ==========
@tree.command(
    name="vis",
    description="Dodeljuje @roli vidljivost na sve TEAM kategorije i kanale u njima",
    guild=GUILD_OBJ
)
@need_manage_channels()
async def vis(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    team_categories = [cat for cat in guild.categories if cat.name.upper().startswith("TEAM ")]

    if not team_categories:
        return await interaction.followup.send("❌ Nisam pronašao nijednu TEAM kategoriju.", ephemeral=True)

    success = 0
    errors = []

    await interaction.followup.send(f"🔄 Počinjem dodeljivanje vidljivosti roli **{role.name}** na {len(team_categories)} TEAM kategorija...", ephemeral=True)

    for category in team_categories:
        try:
            # 1. Dodeli permisiju na kategoriju
            await category.set_permissions(role, view_channel=True, reason=f"/vis by {interaction.user}")
            await asyncio.sleep(1.2)

            # 2. Dodeli permisiju na sve kanale unutar kategorije
            for channel in category.channels:
                if isinstance(channel, discord.TextChannel) or isinstance(channel, discord.VoiceChannel):
                    await channel.set_permissions(role, view_channel=True, reason=f"/vis by {interaction.user}")
                    await asyncio.sleep(1.0)

            success += 1
            print(f"[VIS] Uspešno za kategoriju: {category.name}")

        except discord.Forbidden:
            errors.append(f"❌ Nema permisiju za kategoriju {category.name}")
        except Exception as e:
            errors.append(f"❌ Greška kod {category.name}: {e}")

    # Završni izveštaj
    embed = discord.Embed(
        title="✅ /vis završeno",
        color=0x00ff88
    )
    embed.add_field(name="Uspešno obrađeno", value=f"{success}/{len(team_categories)} TEAM kategorija", inline=False)
    
    if errors:
        embed.add_field(name="Greške", value="\n".join(errors[:10]), inline=False)  # max 10 grešaka
    else:
        embed.add_field(name="Status", value="Sve TEAM kategorije i kanali su uspešno ažurirani.", inline=False)

    embed.set_footer(text=f"Izvršio: {interaction.user}")

    await interaction.followup.send(embed=embed)

# ========== /ticket - Uslovna vidljivost po shiftu ==========
TICKET_CATEGORY_ID = 1504735887307837513
TRANSCRIPT_CATEGORY_ID = 1504739040706953228

# Shift roles i njihovi supervizori
SHIFT_SUPERVISOR_MAP = {
    1410962300554313870: 1504564869569970196,   # Graveyard Shift → Graveyard Supervisor
    # Ako kasnije imaš i za Afternoon/Main, dodaj ovde:
    # 1410962344124612710: 1234567890,         # Afternoon Shift → Supervisor
    # 1410962407454675047: 9876543210,         # Main Shift → Supervisor
}

OTHER_SUPPORT_ROLES = [
    1410962105749995591,
    1410958063824801802,
    1474070997274464379,
    1504564869569970196
]

@tree.command(
    name="ticket",
    description="Otvori novi ticket",
    guild=GUILD_OBJ
)
@app_commands.describe(razlog="Razlog otvaranja ticketa")
async def ticket(interaction: discord.Interaction, razlog: str):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    category = guild.get_channel(TICKET_CATEGORY_ID)

    ticket_name = f"ticket-{interaction.user.name}"

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_messages=True),
    }

    # Dodajemo ostale support role
    for role_id in OTHER_SUPPORT_ROLES:
        role = guild.get_role(role_id)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_messages=True)

    # === USLOVNA VIDLJIVOST ZA SUPERVIZORA ===
    supervisor_to_ping = None
    for shift_role_id, sup_role_id in SHIFT_SUPERVISOR_MAP.items():
        if any(r.id == shift_role_id for r in interaction.user.roles):
            supervisor_to_ping = sup_role_id
            break

    if supervisor_to_ping:
        sup_role = guild.get_role(supervisor_to_ping)
        if sup_role:
            overwrites[sup_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_messages=True)

    try:
        ticket_channel = await guild.create_text_channel(
            name=ticket_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket by {interaction.user}"
        )

        # === Pingovi van embeda ===
        mentions = [f"<@{interaction.user.id}>"]
        
        if supervisor_to_ping:
            mentions.append(f"<@&{supervisor_to_ping}>")
        
        for rid in OTHER_SUPPORT_ROLES:
            mentions.append(f"<@&{rid}>")
        
        await ticket_channel.send(" ".join(mentions))

        # === Embed ===
        embed = discord.Embed(
            title="🎟️ Novi Ticket",
            description=f"**Korisnik:** {interaction.user.mention}\n**Razlog:** {razlog}",
            color=0x00b0f4,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="Koristite /close za zatvaranje sa transcriptom\n/delete za brisanje bez transcripta")

        await ticket_channel.send(embed=embed)

        await interaction.followup.send(
            f"✅ Ticket je uspešno otvoren! → {ticket_channel.mention}",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(f"❌ Greška: {e}", ephemeral=True)

# ========== /close - Zatvara ticket + čuva transcript ==========
@tree.command(
    name="close",
    description="Zatvori ticket i sačuva transcript",
    guild=GUILD_OBJ
)
async def close_ticket(interaction: discord.Interaction):
    if not interaction.channel.name.startswith("ticket-"):
        return await interaction.response.send_message(
            "❌ Ova komanda radi samo unutar ticketa.", 
            ephemeral=True
        )

    await interaction.response.send_message("**Zatvaram ticket i čuvam transcript...**", ephemeral=False)

    transcript_cat = interaction.guild.get_channel(1504739040706953228)

    # Čuvanje transcripta
    if transcript_cat:
        try:
            messages = [msg async for msg in interaction.channel.history(limit=1000, oldest_first=True)]
            transcript_text = f"**Transcript ticketa:** {interaction.channel.name}\n"
            transcript_text += f"**Vreme zatvaranja:** {discord.utils.utcnow().strftime('%d.%m.%Y %H:%M')}\n"
            transcript_text += "="*60 + "\n\n"

            for msg in messages:
                time_str = msg.created_at.strftime("%d.%m.%Y %H:%M")
                transcript_text += f"[{time_str}] {msg.author.display_name}: {msg.content}\n"
                if msg.attachments:
                    transcript_text += f"   Prilozi: {', '.join([a.url for a in msg.attachments])}\n"

            transcript_channel = await transcript_cat.create_text_channel(
                name=f"transcript-{interaction.channel.name.replace('ticket-', '')}"
            )
            await transcript_channel.send(transcript_text)
        except Exception as e:
            print(f"Transcript error: {e}")

    await asyncio.sleep(2)
    try:
        await interaction.channel.delete(reason=f"Closed with transcript by {interaction.user}")
    except:
        await interaction.followup.send("Ticket zatvoren, ali transcript nije uspeo da se sačuva.", ephemeral=True)


# ========== /delete - Potpuno brisanje bez transcripta ==========
@tree.command(
    name="delete",
    description="Potpuno obriši ticket bez čuvanja transcripta",
    guild=GUILD_OBJ
)
async def delete_ticket(interaction: discord.Interaction):
    if not interaction.channel.name.startswith("ticket-"):
        return await interaction.response.send_message(
            "❌ Ova komanda radi samo unutar ticketa.", 
            ephemeral=True
        )

    await interaction.response.send_message("**Brišem ticket zauvek...**", ephemeral=False)
    await asyncio.sleep(2)
    
    try:
        await interaction.channel.delete(reason=f"Deleted by {interaction.user}")
    except Exception as e:
        await interaction.followup.send(f"❌ Greška pri brisanju: {e}", ephemeral=True)

# /newm
@tree.command(
    name="newm",
    description="Napravi novi model",
    guild=GUILD_OBJ
)
@need_manage_roles()
@need_manage_channels()
async def new_model(interaction: discord.Interaction, ime: str):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild

    model_name = ime.strip().upper()
    role_name = f"> {model_name}"
    category_name = f"TEAM {model_name}"

    try:
        print("START")

        # ROLE
        new_role = await guild.create_role(
            name=role_name,
            colour=discord.Colour(0x2b2d31),
            mentionable=True
        )
        print("ROLE CREATED")

        # CATEGORY
        new_category = await guild.create_category(
            name=category_name
        )
        
        team_categories = sorted(
            [c for c in guild.categories if c.name.startswith("TEAM ")],
            key=lambda c: c.name.upper()
        )
        
        for category in team_categories:
            if category == new_category:
                continue
        
            if category.name.upper() > new_category.name.upper():
                await new_category.edit(position=category.position)
                break
        
        print("CATEGORY CREATED")

        # CHANNELS
        general = await new_category.create_text_channel(
            "general"
        )
        print("GENERAL CREATED")

        whales = await new_category.create_text_channel(
            "whales"
        )
        print("WHALES CREATED")

        welcome_message = (
            "Ovo je kanal u koji se upisuju sve bitne stavke vezane za model, spendere, ostale fanove i slično.\n\n"
            "Ukoliko ste imali farmu, nju upisujete u kanalu **#whales** koristeći komandu `/farm` uz sve adekvatne podatke.\n\n"
            "Ako vam je potrebno više informacija od onih koje već imate o modelu, obavezno to napišite u grupnom chatu vaše smene na Telegramu.\n\n"
            "Što se tiče customa – ako nema dovoljno informacija, a fan je mali spender, možete odokativno napraviti pitch za nešto ekskluzivno za određenu sumu.\n\n"
            "Ne pitati za custome fanove koji su potrošili 0 ili su tek došli.\n\n"
            "Za sve lične podatke koji nisu navedeni u postojećim informacijama, dozvoljeno je odokativno improvizovati uz upisivanje u notes šta je izmišljeno."
        )

        await general.send(welcome_message)

        # PERMISSIONS
        await new_category.set_permissions(
            guild.default_role,
            view_channel=False
        )

        await new_category.set_permissions(
            new_role,
            view_channel=True
        )

        print("PERMISSIONS SET")

        await interaction.followup.send(
            f"✅ Napravljeno:\n"
            f"Role: {role_name}\n"
            f"Category: {category_name}",
            ephemeral=True
        )

    except Exception as e:
        import traceback

        traceback.print_exc()

        await interaction.followup.send(
            f"❌ {type(e).__name__}: {e}",
            ephemeral=True
        )




# ---------- /resync ----------
@tree.command(name="resync", description="force guild sync instant", guild=GUILD_OBJ)
@need_manage_roles()
async def resync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if GUILD_OBJ is None:
            return await interaction.followup.send(
                "GUILD_ID nije setovan.", ephemeral=True
            )
        # FORCE COPY GLOBAL → GUILD
        tree.copy_global_to(guild=GUILD_OBJ)
        cmds = await tree.sync(guild=GUILD_OBJ)
        names = ", ".join(sorted(c.name for c in cmds))
        await interaction.followup.send(
            f"Guild sync OK. {len(cmds)} komandi → {names}", ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"Resync FAIL: {e}", ephemeral=True)


# ---------- global error ----------
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"greška: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"greška: {error}", ephemeral=True)
    except Exception:
        pass


# ---------- MM HOOKS ----------
def _mm_text_from_message(content: str) -> str:
    raw = (content or "").strip()
    if raw.lower().startswith("!mm"):
        return raw[3:].strip(": \n\t")
    return raw


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    content_raw = message.content or ""
    content = content_raw.strip().lower()
    if content.startswith("!mm"):
        mentions = " ".join(
            f"<@{uid}>" for uid in [886983698321391667, 1301678435776598107]
        )
        await message.channel.send(
            f"{mentions} {message.author.mention} je upravo poslao !mm."
        )
        # auto FU
        mm_line = _mm_text_from_message(message.content)
        if mm_line:
            fus = await safe_generate_fus(mm_line, message.channel.id)
            if fus:
                block = "```\n" + "\n".join(fus) + "\n```"
                await message.channel.send(block)
    await bot.process_commands(message)


# ========== AUTO SCHEDULE (/as) ==========
AS_CHANNELS = {
    "1": 1517504193735426148,
    "2": 1520415486725193768,
    "3": 1520415502458028032,
    "4": 1520415519679840417,
    "5": 1520415544304472134,
    "6": 1520415560519782482,
    "7": 1520415584171331604,
    "8": 1520415611522515055,
    "9": 1520415630296092682,
    "10": 1520415659089985566,
}
COVER_CATEGORY_ID = 1520385768214888530


def cover_channel_name(name):
    return "cover-team-" + re.sub(r"\s+", "-", (name or "").strip().lower())


async def find_cover_channel(guild, name):
    target = cover_channel_name(name)
    cat = guild.get_channel(COVER_CATEGORY_ID)
    if cat is not None:
        for ch in cat.channels:
            if isinstance(ch, discord.TextChannel) and ch.name.lower() == target:
                return ch
    for ch in guild.text_channels:
        if ch.name.lower() == target:
            return ch
    return None


def extract_chatters(chatter_str):
    out = []
    for p in re.split(r"[/,]", chatter_str or ""):
        p = p.strip()
        if p and p.lower() != "off":
            out.append(p)
    return out


def _find_member_by_name(guild, name, role):
    target = (name or "").strip().lower()
    if not target:
        return None

    def exact(m):
        return (m.display_name or "").lower() == target or (m.name or "").lower() == target

    def partial(m):
        return target in (m.display_name or "").lower() or target in (m.name or "").lower()

    members = [m for m in guild.members if not m.bot]
    if role is not None:
        for m in members:
            if role in m.roles and exact(m):
                return m
    for m in members:
        if exact(m):
            return m
    if role is not None:
        for m in members:
            if role in m.roles and partial(m):
                return m
    for m in members:
        if partial(m):
            return m
    return None


def find_chatter_mentions(guild, chatter_str, shift):
    names = extract_chatters(chatter_str)
    role_id = SHIFT_ROLES.get(shift) if shift else None
    role = guild.get_role(role_id) if role_id else None
    mentions = []
    for name in names:
        member = _find_member_by_name(guild, name, role)
        if member:
            mentions.append(member.mention)
    return mentions


def parse_schedule(text):
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    token_re = re.compile(r"(COVER\s+TEAM|TEAM\s*\d+)\s*\(\s*([^)]*?)\s*\)", re.IGNORECASE)
    blocks = []
    current = None
    for ln in lines:
        m = token_re.search(ln)
        is_header = bool(m) and (ln[: m.start()].strip() == "")
        if is_header:
            if current is not None:
                blocks.append(current)
            targets = []
            for raw_label, raw_name in token_re.findall(ln):
                label = raw_label.strip()
                name = raw_name.strip()
                if label.upper().startswith("COVER"):
                    targets.append({"kind": "cover", "num": None, "name": name, "chatter": name})
                else:
                    nm = re.search(r"\d+", label)
                    num = nm.group(0) if nm else None
                    targets.append({"kind": "team", "num": num, "name": None, "chatter": name})
            current = {"header": ln.strip(), "targets": targets, "models": []}
        else:
            if current is not None:
                for p in re.split(r"/", ln):
                    p = p.strip()
                    if p:
                        current["models"].append(p)
    if current is not None:
        blocks.append(current)
    return blocks


def parse_schedule_meta(text):
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.search(
            r"(\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)\s*(?:[-–—]\s*)?(graveyard|grave|afternoon|after|main)",
            ln,
            re.IGNORECASE,
        )
        if m:
            date_str = m.group(1)
            raw = m.group(2).lower()
            if raw in ("grave", "graveyard"):
                shift = "graveyard"
            elif raw in ("after", "afternoon"):
                shift = "afternoon"
            else:
                shift = "main"
            return {"date": date_str, "shift": shift}
        break
    return None


SHIFT_LABELS = {"afternoon": "AFTERNOON", "graveyard": "GRAVEYARD", "main": "MAIN"}


def format_channel_schedule(info):
    models_text = " / ".join(info["models"])
    date_str = info.get("date")
    mentions = info.get("mentions") or []
    shift = info.get("shift")
    part = info.get("part")  # None / "first" / "second"

    shift_label = SHIFT_LABELS.get(shift) if shift else None

    parts = []
    if shift_label:
        if part == "first":
            parts.append(f"prvi deo {shift_label} smene")
        elif part == "second":
            parts.append(f"drugi deo {shift_label} smene")
        else:
            parts.append(shift_label)
    if date_str:
        parts.append(date_str)
    if mentions:
        parts.append(" ".join(mentions))

    header = " ".join(parts)
    if header:
        return f"{header}\n{models_text}"
    return models_text


async def route_schedule(guild, text, check_channel=None):
    blocks = parse_schedule(text)
    if not blocks:
        return {"ok": False, "error": "Nisam pronašao nijedan TEAM u tekstu."}

    meta = parse_schedule_meta(text)
    date_str = meta.get("date") if meta else None
    shift = meta.get("shift") if meta else None

    sent = []
    skipped = []
    for block in blocks:
        models = block["models"]
        if not models:
            skipped.append(f"{block['header']} (off)")
            continue
        for i, target in enumerate(block["targets"]):
            if target["kind"] == "team":
                num = target["num"]
                if not (num and num in AS_CHANNELS):
                    skipped.append(f"{block['header']} (nepoznat tim {num})")
                    continue
                tgt = bot.get_channel(AS_CHANNELS[num])
                desc = f"TEAM {num}"
            else:
                tgt = await find_cover_channel(guild, target["name"])
                desc = f"COVER {target['name']}"
                if not tgt:
                    skipped.append(f"{block['header']} (cover kanal nije nađen: {cover_channel_name(target['name'])})")
                    continue

            mentions = find_chatter_mentions(guild, target["chatter"], shift)
            if not mentions and shift:
                role_id = SHIFT_ROLES.get(shift)
                if role_id:
                    mentions = [f"<@&{role_id}>"]

            part = None
            if len(block["targets"]) > 1:
                part = "first" if i == 0 else "second"

            info = {"models": models, "date": date_str, "mentions": mentions, "shift": shift, "part": part}
            try:
                await tgt.send(format_channel_schedule(info))
                sent.append(desc)
            except Exception as e:
                skipped.append(f"{block['header']} → {desc} (greška)")
                print("[AS] auto send fail:", e)

    first_half = []
    second_half = []
    for block in blocks:
        targets = block["targets"]
        for i, target in enumerate(targets):
            names = extract_chatters(target["chatter"])
            if len(targets) == 1 or i == 0:
                first_half.extend(names)
            else:
                second_half.extend(names)

    def _dedupe(lst):
        seen = set()
        out = []
        for c in lst:
            k = c.lower()
            if k not in seen:
                seen.add(k)
                out.append(c)
        return out

    check_line = "!check " + ", ".join(_dedupe(first_half)) if first_half else "!check"
    check_line_second = "!check " + ", ".join(_dedupe(second_half)) if second_half else ""

    if check_channel is not None:
        try:
            full = ", ".join(_dedupe(first_half + second_half))
            await check_channel.send("!check " + full if full else "!check")
        except Exception as e:
            print("[AS] check send fail:", e)

    return {
        "ok": True,
        "sent": sent,
        "skipped": skipped,
        "check_line": check_line,
        "check_line_second": check_line_second,
    }


class AsModal(Modal, title="Auto Schedule"):
    def __init__(self):
        super().__init__(timeout=None)
        self.schedule = TextInput(
            label="Zalepi raspored",
            style=TextStyle.paragraph,
            placeholder="Zalepi ceo raspored ovde...",
            required=True,
            max_length=4000,
        )
        self.add_item(self.schedule)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result = await route_schedule(
            interaction.guild, self.schedule.value, check_channel=interaction.channel
        )
        if not result.get("ok"):
            return await interaction.followup.send(
                f"❌ {result['error']}", ephemeral=True
            )
        sent = result["sent"]
        skipped = result["skipped"]
        summary = f"✅ Poslao {len(sent)} poruka"
        if sent:
            summary += ": " + ", ".join(sent)
        if skipped:
            summary += "\nPreskočeno: " + ", ".join(skipped)
        await interaction.followup.send(summary, ephemeral=True)


@tree.command(name="as", description="Auto schedule: podeli raspored po timovima i rutiraj", guild=GUILD_OBJ)
async def as_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(AsModal())


# ========== REASSIGN ==========
REASSIGN_CHANNEL_ID = 1543240286615117925
pending_reassigns = {}  # user_id -> {"model","date","reassign_to","fans":[(name,sale),...],"preview_msg": Message}
reassign_msg_ids = set()  # cache id-jeva poruka (pending) da ne querijemo DB na svaku reakciju


def load_reassign_msg_ids():
    global reassign_msg_ids
    conn = _db()
    rows = conn.execute("SELECT ticket_msg_id, overview_msg_id FROM reassigns WHERE done=0").fetchall()
    conn.close()
    reassign_msg_ids = set()
    for r in rows:
        if r["ticket_msg_id"]:
            reassign_msg_ids.add(r["ticket_msg_id"])
        if r["overview_msg_id"]:
            reassign_msg_ids.add(r["overview_msg_id"])


def save_reassign(data, channel_id=None, user_id=None):
    conn = _db()
    cur = conn.execute(
        "INSERT INTO reassigns (model, date, chatter, fans, created_at, channel_id, user_id) VALUES (?,?,?,?,?,?,?)",
        (
            data["model"],
            data["date"],
            data["reassign_to"],
            json.dumps(data["fans"], ensure_ascii=False),
            _local_now().isoformat(),
            channel_id,
            user_id,
        ),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def set_reassign_messages(rid, ticket_msg_id, overview_msg_id):
    conn = _db()
    conn.execute(
        "UPDATE reassigns SET ticket_msg_id=?, overview_msg_id=? WHERE id=?",
        (ticket_msg_id, overview_msg_id, rid),
    )
    conn.commit()
    conn.close()
    if ticket_msg_id:
        reassign_msg_ids.add(ticket_msg_id)
    if overview_msg_id:
        reassign_msg_ids.add(overview_msg_id)


def get_reassign_by_message(message_id):
    conn = _db()
    row = conn.execute(
        "SELECT id, channel_id, user_id, ticket_msg_id, overview_msg_id, done FROM reassigns WHERE ticket_msg_id=? OR overview_msg_id=?",
        (message_id, message_id),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "user_id": row["user_id"],
        "ticket_msg_id": row["ticket_msg_id"],
        "overview_msg_id": row["overview_msg_id"],
        "done": row["done"],
    }


def mark_reassign_done(rid):
    conn = _db()
    conn.execute("UPDATE reassigns SET done=1 WHERE id=?", (rid,))
    conn.commit()
    conn.close()


def get_reassigns(done=False):
    conn = _db()
    rows = conn.execute(
        "SELECT id, model, date, chatter, fans FROM reassigns WHERE done=? ORDER BY created_at DESC, id DESC",
        (1 if done else 0,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            fans = json.loads(r["fans"]) if r["fans"] else []
        except Exception:
            fans = []
        out.append({"id": r["id"], "model": r["model"], "date": r["date"], "chatter": r["chatter"], "fans": fans})
    return out


def format_reassign(data):
    lines = [f"MODEL: {data['model']}"]
    for name, sale in data["fans"]:
        lines.append(f"FAN'S NAME (ne fan's @): {name}")
        lines.append(f"SALE: ${sale}")
    lines.append(f"DATE: {data['date']}")
    lines.append(f"REASSIGN TO: {data['reassign_to']}")
    return "\n".join(lines)


class ReassignActionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    @discord.ui.button(label="➕ Dodaj još", style=discord.ButtonStyle.secondary)
    async def add_fan(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddFanModal())

    @discord.ui.button(label="✅ Završi", style=discord.ButtonStyle.success)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = pending_reassigns.pop(interaction.user.id, None)
        if not data:
            await interaction.response.send_message("❌ Nema započetog reassign-a.", ephemeral=True)
            return
        original_channel = interaction.channel
        info_text = f"🔄 **REASSIGN**\n\n{format_reassign(data)}"

        rid = save_reassign(data, channel_id=original_channel.id, user_id=interaction.user.id)

        ticket_msg_id = None
        overview_msg_id = None
        # 1) info u kanalu gde je komanda pokrenuta (npr. ticket)
        try:
            m1 = await original_channel.send(info_text)
            await m1.add_reaction("✅")
            ticket_msg_id = m1.id
        except Exception as e:
            print("[REASSIGN] ticket send fail:", e)

        # 2) info u preglednom kanalu
        overview = bot.get_channel(REASSIGN_CHANNEL_ID)
        if overview:
            try:
                m2 = await overview.send(info_text)
                await m2.add_reaction("✅")
                overview_msg_id = m2.id
            except Exception as e:
                print("[REASSIGN] overview send fail:", e)

        set_reassign_messages(rid, ticket_msg_id, overview_msg_id)

        await interaction.response.edit_message(content="✅ Reassign poslat.", view=None)


class ReassignModal(Modal, title="Reassign unos"):
    def __init__(self):
        super().__init__(timeout=None)
        self.model = TextInput(label="Model", placeholder="npr. TRIXXY B", required=True, max_length=100)
        self.fan = TextInput(label="Fan's name (ne @)", required=True, max_length=100)
        self.sale = TextInput(label="Sale ($)", required=True, max_length=20)
        self.date = TextInput(label="Datum", placeholder="npr. 28.8.", required=True, max_length=20)
        self.reassign_to = TextInput(label="Reassign to", placeholder="npr. veljkoo", required=True, max_length=100)
        self.add_item(self.model)
        self.add_item(self.fan)
        self.add_item(self.sale)
        self.add_item(self.date)
        self.add_item(self.reassign_to)

    async def on_submit(self, interaction: discord.Interaction):
        data = {
            "model": self.model.value.strip(),
            "date": self.date.value.strip(),
            "reassign_to": self.reassign_to.value.strip(),
            "fans": [(self.fan.value.strip(), self.sale.value.strip())],
        }
        pending_reassigns[interaction.user.id] = data
        await interaction.response.send_message(
            "**Reassign (u izradi):**\n\n" + format_reassign(data),
            view=ReassignActionsView(),
            ephemeral=True,
        )
        msg = await interaction.original_response()
        data["preview_msg"] = msg


class AddFanModal(Modal, title="Dodaj fan-a"):
    def __init__(self):
        super().__init__(timeout=None)
        self.fan = TextInput(label="Fan's name (ne @)", required=True, max_length=100)
        self.sale = TextInput(label="Sale ($)", required=True, max_length=20)
        self.add_item(self.fan)
        self.add_item(self.sale)

    async def on_submit(self, interaction: discord.Interaction):
        data = pending_reassigns.get(interaction.user.id)
        if not data:
            await interaction.response.send_message("❌ Nema započetog reassign-a.", ephemeral=True)
            return
        data["fans"].append((self.fan.value.strip(), self.sale.value.strip()))
        preview_msg = data.get("preview_msg")
        if preview_msg:
            try:
                await preview_msg.edit(
                    content="**Reassign (u izradi):**\n\n" + format_reassign(data),
                    view=ReassignActionsView(),
                )
            except Exception as e:
                print("[REASSIGN] preview edit fail:", e)
        await interaction.response.defer()


@tree.command(name="reassign", description="Prijavi reassign prodaje", guild=GUILD_OBJ)
async def reassign_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(ReassignModal())


def _fans_str(fans):
    return ", ".join(f"{name} (${sale})" for name, sale in fans)


async def _send_chunks(interaction, lines, limit=1900):
    chunks = []
    cur = []
    cur_len = 0
    for ln in lines:
        if cur and cur_len + len(ln) + 1 > limit:
            chunks.append("\n".join(cur))
            cur = []
            cur_len = 0
        cur.append(ln)
        cur_len += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    for ch in chunks:
        await interaction.followup.send(ch, ephemeral=True)


@tree.command(name="listr", description="Lista reassignova po modelu + datumu", guild=GUILD_OBJ)
async def listr(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    reassigns = get_reassigns()
    if not reassigns:
        return await interaction.followup.send("Nema reassignova.", ephemeral=True)

    groups = {}
    for r in reassigns:
        key = (r["model"], r["date"])
        groups.setdefault(key, []).append(r)

    lines = []
    for (model, date), rs in sorted(groups.items(), key=lambda x: x[0][1] + x[0][0]):
        lines.append(f"**{model} — {date}**")
        for r in rs:
            lines.append(f"• {r['chatter']}: {_fans_str(r['fans'])}")
        lines.append("")

    await _send_chunks(interaction, lines)


@tree.command(name="listch", description="Lista reassignova po chatteru + datumu", guild=GUILD_OBJ)
async def listch(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    reassigns = get_reassigns()
    if not reassigns:
        return await interaction.followup.send("Nema reassignova.", ephemeral=True)

    groups = {}
    for r in reassigns:
        key = (r["chatter"], r["date"])
        groups.setdefault(key, []).append(r)

    lines = []
    for (chatter, date), rs in sorted(groups.items(), key=lambda x: x[0][1] + x[0][0]):
        lines.append(f"**{chatter} — {date}**")
        for r in rs:
            lines.append(f"• {r['model']}: {_fans_str(r['fans'])}")
        lines.append("")

    await _send_chunks(interaction, lines)


# ========== OFF DAYS ==========
class DayPickSelect(discord.ui.Select):
    def __init__(self, dates, taken):
        options = []
        for d in dates:
            options.append(
                discord.SelectOption(
                    label=d.strftime("%d.%m.%Y"),
                    value=d.isoformat(),
                    description="zauzeto" if d in taken else SR_WEEKDAYS[d.weekday()],
                )
            )
        super().__init__(
            placeholder="🗓 Izaberi datum",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        d = datetime.strptime(self.values[0], "%Y-%m-%d").date()
        await self.view.handle_date(interaction, d)


class OffDayPickerView(discord.ui.View):
    def __init__(self, taken, all_dates, on_pick):
        super().__init__(timeout=600)
        self.taken = taken
        self.all_dates = all_dates
        self.on_pick = on_pick
        self.page = 0
        self._render()

    @property
    def total_pages(self):
        return (len(self.all_dates) + 24) // 25

    def _page_dates(self):
        return self.all_dates[self.page * 25:(self.page + 1) * 25]

    def _render(self):
        self.clear_items()
        self.add_item(DayPickSelect(self._page_dates(), self.taken))
        prev_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji="◀", row=1, disabled=(self.page == 0)
        )
        prev_btn.callback = self._prev
        next_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary, emoji="▶", row=1,
            disabled=(self.page >= self.total_pages - 1),
        )
        next_btn.callback = self._next
        self.add_item(prev_btn)
        self.add_item(next_btn)

    async def _prev(self, interaction: discord.Interaction):
        self.page -= 1
        self._render()
        await interaction.response.edit_message(view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page += 1
        self._render()
        await interaction.response.edit_message(view=self)

    async def handle_date(self, interaction: discord.Interaction, d):
        await self.on_pick(interaction, d)


async def book_off_days(interaction, start_date, end_date, shift):
    days = []
    d = start_date
    while d <= end_date:
        days.append(d)
        d += timedelta(days=1)

    taken = taken_dates_for_shift(shift)
    conflict = sorted(x for x in days if x in taken)
    if conflict:
        msg = ", ".join(x.strftime("%d.%m.%Y") for x in conflict)
        await interaction.response.send_message(
            f"❌ Ne može — već zauzeti dan(i) u tvojoj smeni: {msg}", ephemeral=True
        )
        return

    # limit: maksimalno 4 off dana mesečno po chatteru
    month_count = count_month_off_days(interaction.user.id)
    if month_count + len(days) > 4:
        remaining = 4 - month_count
        await interaction.response.send_message(
            f"❌ Maksimalno 4 off dana mesečno. Ovog meseca si već uzeo/la {month_count} — ostalo ti je {remaining}.",
            ephemeral=True,
        )
        return

    group_id = f"{interaction.user.id}-{int(datetime.now().timestamp())}"
    for day in days:
        off_days.append({
            "user_id": interaction.user.id,
            "username": interaction.user.display_name,
            "date": day.isoformat(),
            "shift": shift,
            "message_id": None,
            "confirmed": False,
            "group_id": group_id,
            "channel_id": interaction.channel.id,
        })
    save_off_days()

    if len(days) == 1:
        date_str = f"{days[0].strftime('%d.%m.%Y')} ({SR_WEEKDAYS[days[0].weekday()]})"
    else:
        date_str = (
            f"{days[0].strftime('%d.%m.%Y')} → {days[-1].strftime('%d.%m.%Y')} ({len(days)} dana)"
        )

    channel = bot.get_channel(OFF_DAY_CHANNEL_ID)
    if channel:
        try:
            await channel.send(
                f"📅 **OFF DAY**\n"
                f"**Datum:** {date_str}\n"
                f"**Chatter:** {interaction.user.mention} ({interaction.user.display_name})\n"
                f"**Smena:** {shift}"
            )
        except Exception as e:
            print("[OFF] slanje u management kanal nije uspelo:", e)

    await interaction.response.send_message(
        f"📅 **{interaction.user.mention} je rezervisao/la off day**\n"
        f"**Datum:** {date_str}\n"
        f"**Smena:** {shift}\n"
        f"**Preostalo off dana ovog meseca:** {4 - (month_count + len(days))}\n"
        f"Potvrdi klikom na ✅ da si video/la, ili klikni ❌ da obrišeš."
    )
    msg = await interaction.original_response()
    for e in off_days:
        if e.get("group_id") == group_id:
            e["message_id"] = msg.id
    save_off_days()
    try:
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
    except Exception as e:
        print("[OFF] dodavanje reakcija nije uspelo:", e)


async def _require_shift(interaction):
    shift = get_user_shift(interaction.user)
    if shift is None:
        await interaction.response.send_message(
            "❌ Moraš imati tačno jednu smensku rolu (graveyard / afternoon / main).",
            ephemeral=True,
        )
        return None
    if shift == "multiple":
        await interaction.response.send_message(
            "❌ Imaš više smenskih rola — javi se managementu.",
            ephemeral=True,
        )
        return None
    return shift


@tree.command(name="off", description="Rezerviši slobodan dan (off day)", guild=GUILD_OBJ)
async def off(interaction: discord.Interaction):
    shift = await _require_shift(interaction)
    if not shift:
        return
    taken = taken_dates_for_shift(shift)
    dates = build_date_range()

    async def on_pick(i, d):
        await book_off_days(i, d, d, shift)

    view = OffDayPickerView(taken, dates, on_pick)
    note = ""
    if taken:
        taken_lines = "\n".join(
            f"- ~~{x.strftime('%d.%m.%Y')}~~" for x in sorted(taken)
        )
        note = f"\n\n**Već zauzeti dani ({shift} smena):**\n{taken_lines}"
    await interaction.response.send_message(
        f"🗓 **Off day** — izaberi datum (narednih 60 dana):{note}",
        view=view,
        ephemeral=True,
    )


@tree.command(name="multioff", description="Rezerviši više uzastopnih dana (putovanje, svadba...)", guild=GUILD_OBJ)
async def multioff(interaction: discord.Interaction):
    shift = await _require_shift(interaction)
    if not shift:
        return
    taken = taken_dates_for_shift(shift)
    dates = build_date_range()

    async def on_start(i, d):
        if d in taken:
            await i.response.send_message(
                "❌ Taj datum je već zauzet u tvojoj smeni — izaberi drugi.", ephemeral=True
            )
            return
        end_dates = [x for x in dates if x > d]
        if not end_dates:
            await i.response.send_message("❌ Nema dostupnih datuma posle tog dana.", ephemeral=True)
            return

        async def on_end(i2, d2):
            await book_off_days(i2, d, d2, shift)

        view2 = OffDayPickerView(taken, end_dates, on_end)
        await i.response.edit_message(
            content=f"🗓 **Multi off** — početak: **{d.strftime('%d.%m.%Y')}**. Sada izaberi **poslednji dan**:",
            view=view2,
        )

    view = OffDayPickerView(taken, dates, on_start)
    await interaction.response.send_message(
        "🗓 **Multi off** — izaberi **prvi dan** (narednih 60 dana):",
        view=view,
        ephemeral=True,
    )


@tree.command(name="loff", description="Pregled svih off dana po datumima", guild=GUILD_OBJ)
async def loff(interaction: discord.Interaction):
    await interaction.response.defer()
    if not off_days:
        return await interaction.followup.send("Nema nijednog off dana.")

    by_date = defaultdict(list)
    for e in off_days:
        by_date[e.get("date")].append(e)

    today = _local_now().date()
    lines = []
    for date_iso in sorted(k for k in by_date.keys() if k):
        try:
            d = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except Exception:
            continue
        if d < today:
            continue
        wd = SR_WEEKDAYS[d.weekday()]
        names = []
        for e in sorted(by_date[date_iso], key=lambda x: str(x.get("username", ""))):
            mark = "✅" if e.get("confirmed") else "⏳"
            names.append(f"{mark} {e.get('username', e.get('user_id'))} ({e.get('shift')})")
        lines.append(f"**{d.strftime('%d.%m.%Y')}** ({wd})\n" + "\n".join(names))

    if not lines:
        return await interaction.followup.send("Nema nijednog budućeg off dana.")

    embed = discord.Embed(
        title="📅 Off dani",
        description="\n\n".join(lines),
        color=0x00b0f4,
    )
    embed.set_footer(text=f"Ukupno: {len(off_days)} off dana")
    await interaction.followup.send(embed=embed)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    if not guild:
        return
    member = payload.member
    if member is None:
        try:
            member = await guild.fetch_member(payload.user_id)
        except Exception:
            return
    if member.bot:
        return

    # /reassign potvrda
    if payload.message_id in reassign_msg_ids:
        info = get_reassign_by_message(payload.message_id)
        if info is not None and str(payload.emoji) == "✅" and not info["done"]:
            mark_reassign_done(info["id"])
            reassign_msg_ids.discard(payload.message_id)
            if info.get("ticket_msg_id"):
                reassign_msg_ids.discard(info["ticket_msg_id"])
            if info.get("overview_msg_id"):
                reassign_msg_ids.discard(info["overview_msg_id"])
            target = bot.get_channel(info["channel_id"])
            if target:
                try:
                    ticket_msg_id = info.get("ticket_msg_id")
                    if ticket_msg_id:
                        msg = await target.fetch_message(ticket_msg_id)
                        await msg.reply(
                            f"✅ **Uspešno reassignovano** — <@{info['user_id']}>",
                            mention_author=False,
                        )
                    else:
                        await target.send(f"✅ **Uspešno reassignovano** — <@{info['user_id']}>")
                except Exception as e:
                    print("[REASSIGN] notify fail:", e)
        return

    # /farm potvrda
    if payload.message_id in farm_reminders:
        info = farm_reminders.get(payload.message_id)
        if info and str(payload.emoji) == "✅" and member.id == info["user_id"]:
            info["done"] = True
            channel = bot.get_channel(payload.channel_id)
            if channel:
                try:
                    qmsg = await channel.fetch_message(payload.message_id)
                    await qmsg.edit(content=f"{qmsg.content}\n\n✅ Odgovoreno.")
                except Exception as e:
                    print("[FARM] confirm edit fail:", e)
        return

    # /off otkazivanje — potvrda (✅ na cancel request poruci, role required)
    if payload.message_id in off_cancel_requests:
        info = off_cancel_requests.get(payload.message_id)
        if info and str(payload.emoji) == "✅":
            has_role = any(r.id == OFF_CANCEL_ROLE_ID for r in member.roles)
            if has_role:
                off_message_id = info["off_message_id"]
                user_id = info["user_id"]
                off_channel_id = info.get("off_channel_id")
                off_days[:] = [e for e in off_days if e.get("message_id") != off_message_id]
                save_off_days()
                off_cancel_requests.pop(payload.message_id, None)
                # edit reply (zahtev) na potvrđeno
                channel = bot.get_channel(payload.channel_id)
                if channel:
                    try:
                        msg = await channel.fetch_message(payload.message_id)
                        await msg.edit(content=f"✅ Off day je obrisan — <@{user_id}>")
                        await msg.clear_reactions()
                    except Exception as e:
                        print("[OFF] cancel confirm edit fail:", e)
                # edit originalnu off-day poruku
                chatter_channel = bot.get_channel(off_channel_id) if off_channel_id else None
                if chatter_channel:
                    try:
                        off_msg = await chatter_channel.fetch_message(off_message_id)
                        await off_msg.edit(content=f"{off_msg.content}\n\n❌ Off day je obrisan.")
                    except Exception as e:
                        print("[OFF] cancel orig edit fail:", e)
        return

    entries = [e for e in off_days if e.get("message_id") == payload.message_id]
    if not entries:
        return

    emoji = str(payload.emoji)
    channel = bot.get_channel(payload.channel_id)

    if emoji == "✅":
        if member.id == entries[0].get("user_id") and not entries[0].get("confirmed"):
            for e in entries:
                e["confirmed"] = True
            save_off_days()
            if channel:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    await msg.edit(
                        content=f"{msg.content}\n\n✅ Potvrđeno od strane {member.mention}"
                    )
                except Exception as e:
                    print("[OFF] edit confirm fail:", e)

    elif emoji == "❌":
        # admin (manage_roles) može direktno da obriše
        if member.guild_permissions.manage_roles:
            off_days[:] = [e for e in off_days if e.get("message_id") != payload.message_id]
            save_off_days()
            if channel:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    await msg.edit(content="❌ Off day je obrisan.")
                    await msg.clear_reactions()
                except Exception as e:
                    print("[OFF] edit delete fail:", e)
        # chatter traži otkazivanje (čeka potvrdu role)
        elif member.id == entries[0].get("user_id"):
            dates = ", ".join(sorted({e.get("date") for e in entries if e.get("date")}))
            # reply na originalnu poruku — taguje rolu
            try:
                orig_msg = await channel.fetch_message(payload.message_id)
                cm = await orig_msg.reply(
                    f"🔔 <@&{OFF_CANCEL_ROLE_ID}> — potvrdi brisanje off dana za <@{member.id}> ({dates}).",
                    mention_author=False,
                )
                await cm.add_reaction("✅")
                off_cancel_requests[cm.id] = {"off_message_id": payload.message_id, "user_id": member.id, "off_channel_id": payload.channel_id}
            except Exception as e:
                print("[OFF] cancel request send fail:", e)
            # edit originalnu poruku
            try:
                msg = await channel.fetch_message(payload.message_id)
                await msg.edit(content=f"{msg.content}\n\n⏳ Otkazivanje zatraženo — čeka potvrdu.")
            except Exception as e:
                print("[OFF] cancel edit fail:", e)


@tasks.loop(minutes=1)
async def off_day_reminder_loop():
    now = _local_now()
    if now.hour != 10:
        return
    today_iso = now.date().isoformat()
    if get_state("last_off_reminder_date") == today_iso:
        return
    channel = bot.get_channel(OFF_DAY_CHANNEL_ID)
    if not channel:
        return

    tomorrow = now.date() + timedelta(days=1)
    tomorrow_iso = tomorrow.isoformat()
    entries = [e for e in off_days if e.get("date") == tomorrow_iso]
    if entries:
        lines = []
        for e in sorted(entries, key=lambda x: str(x.get("username", ""))):
            lines.append(f"• <@{e.get('user_id')}> — {e.get('shift')}")
        date_str = tomorrow.strftime("%d.%m.%Y")
        try:
            await channel.send(
                f"🔔 **Podsetnik — off dani za sutra** ({date_str}):\n"
                + "\n".join(lines)
                + f"\n\n<@{REMINDER_TARGET_USER_ID}>"
            )
        except Exception as e:
            print("[OFF] reminder send fail:", e)
            return
    set_state("last_off_reminder_date", today_iso)


@off_day_reminder_loop.before_loop
async def _before_off_reminder():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def farm_reminder_loop():
    now = time.time()
    for msg_id in list(farm_reminders.keys()):
        info = farm_reminders.get(msg_id)
        if not info:
            continue
        if info["done"]:
            farm_reminders.pop(msg_id, None)
            continue
        elapsed = now - info["created_at"]
        channel = bot.get_channel(info["channel_id"])
        for i, threshold in enumerate(FARM_REMINDER_TIMES):
            if elapsed >= threshold and not info["sent"][i]:
                info["sent"][i] = True
                if channel:
                    try:
                        qmsg = await channel.fetch_message(msg_id)
                        await qmsg.reply(
                            f"⏰ <@{info['user_id']}> podsetnik: reaguj ✅ na pitanje o farmi (da li je fan dodat na liste i ažurirane beleške).",
                            mention_author=False,
                        )
                    except Exception as e:
                        print("[FARM] reminder fail:", e)
        if elapsed >= FARM_REMINDER_TIMES[-1] and all(info["sent"]):
            farm_reminders.pop(msg_id, None)


@farm_reminder_loop.before_loop
async def _before_farm_reminder():
    await bot.wait_until_ready()


@tasks.loop(minutes=30)
async def off_confirm_reminder_loop():
    today = _local_now().date()
    for e in list(off_days):
        if e.get("confirmed"):
            continue
        try:
            d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if d < today:
            continue
        ch = bot.get_channel(e.get("channel_id")) if e.get("channel_id") else None
        if not ch:
            continue
        try:
            await ch.send(
                f"⏳ <@{e.get('user_id')}> — nisi potvrdio/la svoj off day ({d.strftime('%d.%m.%Y')}). "
                f"Klikni ✅ (potvrda) ili ❌ (otkazivanje) na svoju off-day poruku."
            )
        except Exception as ex:
            print("[OFF] confirm reminder fail:", ex)


@off_confirm_reminder_loop.before_loop
async def _before_off_confirm_reminder():
    await bot.wait_until_ready()


# ---------- BRIDGE (telegram -> discord) ----------
async def handle_health(request):
    return web.Response(text="ok")


async def handle_bridge_as(request):
    if not BRIDGE_TOKEN:
        return web.json_response({"ok": False, "error": "bridge disabled"}, status=403)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    if data.get("token") != BRIDGE_TOKEN:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    text = (data.get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "empty text"}, status=400)
    guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
    if not guild:
        return web.json_response({"ok": False, "error": "guild not found"}, status=500)
    check_channel = None
    if AS_CHECK_CHANNEL_ID:
        try:
            check_channel = bot.get_channel(int(AS_CHECK_CHANNEL_ID))
        except Exception:
            check_channel = None
    result = await route_schedule(guild, text, check_channel=check_channel)
    return web.json_response(result)


async def start_bridge_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_post("/as", handle_bridge_as)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", BRIDGE_PORT)
    await site.start()
    print(f"[BRIDGE] HTTP server sluša na portu {BRIDGE_PORT}")


# ---------- SEED (jednokratno vraćanje izgubljenih off dana) ----------
SEED_OFF_DAYS = [
    ("kvu", "graveyard", ["2026-08-28", "2026-08-29"]),
    ("khazperix", "afternoon", ["2026-08-28", "2026-08-23", "2026-09-07"]),
    ("ckevuz", "main", ["2026-08-30", "2026-09-06", "2026-08-27"]),
    ("Dejan", "graveyard", ["2026-08-22", "2026-08-23", "2026-08-30"]),
    ("Nemac", "main", ["2026-08-23", "2026-09-01"]),
]


async def seed_off_days(guild):
    added = 0
    missing = []
    for name, shift, dates in SEED_OFF_DAYS:
        member = _find_member_by_name(guild, name, None)
        if not member:
            missing.append(name)
            continue
        for d in dates:
            if any(e.get("user_id") == member.id and e.get("date") == d for e in off_days):
                continue
            off_days.append({
                "user_id": member.id,
                "username": member.display_name or name,
                "date": d,
                "shift": shift,
                "message_id": None,
                "confirmed": True,
                "group_id": None,
            })
            added += 1
    if added:
        save_off_days()
        print(f"[SEED] vraćeno {added} off dana")
    if missing:
        print(f"[SEED] upozorenje: nije nađen member za: {', '.join(missing)}")


# ---------- on_ready ----------
@bot.event
async def on_ready():
    try:
        if GUILD_OBJ:
            cmds = await tree.sync(guild=GUILD_OBJ)
            print(f"synced {len(cmds)} slash komandi na server {GUILD_ID}")
        else:
            cmds = await tree.sync()
            print(f"synced {len(cmds)} globalnih slash komandi")
        print(f"✅ logged in as {bot.user}")
        if not off_day_reminder_loop.is_running():
            off_day_reminder_loop.start()
            print("✅ Off day reminder task pokrenut")
        if not farm_reminder_loop.is_running():
            farm_reminder_loop.start()
            print("✅ Farm reminder task pokrenut")
        if not off_confirm_reminder_loop.is_running():
            off_confirm_reminder_loop.start()
            print("✅ Off confirm reminder task pokrenut")
        asyncio.create_task(start_bridge_server())
        guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
        if guild:
            await seed_off_days(guild)
        load_reassign_msg_ids()
    except Exception as e:
        print("sync fail:", e)


# ---------- RUN ----------
bot.run(TOKEN)
