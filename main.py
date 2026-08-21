# main.py — lv-clean
# Role/channel management + ticket sistem + farm/ratio/qc + !mm AI/FU + off days
import os
import re
import json
import sqlite3
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
from collections import defaultdict

# --- env first ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
USE_AI_FU = os.getenv("USE_AI_FU", "false").lower() in ("1", "true", "yes", "on")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
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
            UNIQUE(user_id, date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
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
        "SELECT user_id, username, date, shift, message_id, confirmed, group_id FROM off_days ORDER BY date"
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
        }
        for r in rows
    ]


def save_off_days():
    conn = _db()
    conn.execute("DELETE FROM off_days")
    for e in off_days:
        conn.execute(
            "INSERT INTO off_days (user_id, username, date, shift, message_id, confirmed, group_id) VALUES (?,?,?,?,?,?,?)",
            (
                e.get("user_id"),
                e.get("username"),
                e.get("date"),
                e.get("shift"),
                e.get("message_id"),
                1 if e.get("confirmed") else 0,
                e.get("group_id"),
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
    for e in off_days:
        if e.get("shift") == shift:
            try:
                out.add(datetime.strptime(e["date"], "%Y-%m-%d").date())
            except Exception:
                pass
    return out


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
            f"{farm_roles}",
            f"**Novi farm unos** (by {self.opener.mention}):",
            f"- Iznos: `{self.amount.value.strip()}`",
            f"- Model: `{self.model_name.value.strip()}`",
            f"- Fan: `{self.fan_username.value.strip()}`",
        ]
        extra = self.more_details.value.strip() if self.more_details.value else ""
        if extra:
            lines.append(f"- Detalji: {extra}")
        lines.append("")
        lines.append(
            "**Pitanje:** da li je fan dodat na odgovarajuće liste i da li su ažurirane beleške o istom?"
        )
        await interaction.response.send_message("\n".join(lines))
        msg = await interaction.original_response()
        try:
            await msg.add_reaction("✅")
            await msg.add_reaction("🚫")
        except:
            pass


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
        await interaction.response.send_message(f"greška: {error}", ephemeral=True)
    except:
        await interaction.followup.send(f"greška: {error}", ephemeral=True)


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

    lines = []
    for date_iso in sorted(k for k in by_date.keys() if k):
        try:
            d = datetime.strptime(date_iso, "%Y-%m-%d").date()
            wd = SR_WEEKDAYS[d.weekday()]
        except Exception:
            continue
        names = []
        for e in sorted(by_date[date_iso], key=lambda x: str(x.get("username", ""))):
            mark = "✅" if e.get("confirmed") else "⏳"
            names.append(f"{mark} {e.get('username', e.get('user_id'))} ({e.get('shift')})")
        lines.append(f"**{d.strftime('%d.%m.%Y')}** ({wd})\n" + "\n".join(names))

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
        can_delete = (member.id == entries[0].get("user_id")) or member.guild_permissions.manage_roles
        if can_delete:
            off_days[:] = [e for e in off_days if e.get("message_id") != payload.message_id]
            save_off_days()
            if channel:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    await msg.edit(content="❌ Off day je obrisan.")
                    await msg.clear_reactions()
                except Exception as e:
                    print("[OFF] edit delete fail:", e)


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
    except Exception as e:
        print("sync fail:", e)


# ---------- RUN ----------
bot.run(TOKEN)
