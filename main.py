# main.py — FINAL
# /schedule auto-clean TEAM rola (uz KEEP), pa dodela novih
# + PRIJAVA: unknown modeli (skipped/unknown)
# + !mm detekcija (stopira remindere) + AI/FU auto-predlozi u mm-approval kanalima
import os
import re
import unicodedata
import asyncio
import random
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Modal, TextInput
from discord import TextStyle
from dotenv import load_dotenv
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from openai import OpenAI
from difflib import SequenceMatcher
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
STOPWORDS = {
    "vip",
    "free",
    "paid",
    "oll",
    "ock",
    "ra",
    "inb",
    "eep",
    "tsu",
    "jsn",
    "vf",
    "ggn",
    "bcl",
    "hnq",
    "bk",
    "sa",
    "tvn",
    "yll",
    "oftv",
    "kct",
    "trans",
    "sexy",
    "zzz",
    "x",
    "c",
    "g",
}
# ---------- TUNABLES ----------
SLEEP_BETWEEN_CALLS = 0.35
CHUNK_SIZE = 24
RETRIES = 5
RETRY_BASE_SLEEP = 0.8
PROGRESS_EVERY_N = 5
# Role koje SE NIKAD NE DIRAJU kod auto-clean (pre /schedule)
KEEP_ROLE_NAMES = {"AFTERNOON", "GRAVEYARD", "MAIN", "OBUKA", "LV CHATTER"}
role_index = {}

# QC statistika za /qcurrent
qc_history = defaultdict(list)   # (year, month, user_id) -> list of {'date': day, 'count': num_chattera}

# ---------- BOT ----------
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True  # za !mm detekciju
bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree
GUILD_OBJ = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
# kanali i snippet za mm-approval
MM_APPROVAL_NAME_SNIPPET = "mm-approval"
# summary kanal (tvoj)
MM_SUMMARY_CHANNEL_ID = 1433577356437491774
# ==== anti-spam za AI pozive ====
AI_BLOCKED_UNTIL = None


def extract_core_name(name: str):
    name = name.lower()
    name = name.replace("/", " ")
    name = re.sub(r"\d+", " ", name)
    parts = name.split()
    clean = []
    for p in parts:
        if len(p) <= 2:
            continue
        if p in STOPWORDS:
            continue
        clean.append(p)
    if not clean:
        return name.strip()
    return clean[0]


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


# ============ MASS REMINDERI + !mm LOGIKA ============
GRAVE_GENERAL_CHANNEL_ID = 1364850505234518067  # #graveyard
AFTER_GENERAL_CHANNEL_ID = 1364850574205648967  # #afternoon
MAIN_GENERAL_CHANNEL_ID = 1364850795215982634  # #main
GRAVE_ROLE_ID = 1410962300554313870  # @graveyard
AFTER_ROLE_ID = 1410962344124612710  # @afternoon
MAIN_ROLE_ID = 1410962407454675047  # @main
# Kanal u koji se šalje raspored za svaku smenu
SCHEDULE_CHANNEL = {
    "grave": 1364850505234518067,  # graveyard
    "after": 1364850574205648967,  # afternoon
    "main": 1364850795215982634,  # main
}
SUPERVISOR_IDS = [
    886983698321391667,  # ti
    923657835164889119,  # drugi supervizor
]
# koliko cekamo posle DRUGOG generala
SHIFT_FOLLOW_DELAY_MIN = {
    "grave": 30,
    "after": 30,
    "main": 60,
}
# vreme PRVOG generala po smeni
SHIFT_FIRST_TIME = {
    "grave": time(10, 0),
    "after": time(18, 0),
    "main": time(2, 0),
}
# cuvamo kad je zaista poslat prvi general (UTC)
shift_first_sent_at = {
    "grave": None,
    "after": None,
    "main": None,
}
# poslednji !mm po kanalu
mm_last_time: dict[int, datetime] = {}  # channel_id -> datetime
# raspored svih general poruka (sa opomenama i penalima)
SCHEDULE = [
    # ---------- GRAVE ----------
    {
        "time": time(10, 0),
        "channel_id": GRAVE_GENERAL_CHANNEL_ID,
        "text": f"<@&{GRAVE_ROLE_ID}> molim da mass bude poslat najkasnije do 11:30.\nU slučaju neispunjavanja obaveze, sledi opomena. Ukoliko se prekršaj ponovi, ide penal od 50$.",
        "shift": "grave",
        "kind": "first",
    },
    {
        "time": time(11, 0),
        "channel_id": GRAVE_GENERAL_CHANNEL_ID,
        "text": f"<@&{GRAVE_ROLE_ID}> ukoliko mass još nije poslat, molim da ga pošaljete u narednih 30 minuta.\nNeispunjavanje obaveze rezultira opomenom, a ponavljanje prekršaja penalom od 50$.",
        "shift": "grave",
        "kind": "second",
    },
    {
        "time": time(11, 30),
        "channel_id": GRAVE_GENERAL_CHANNEL_ID,
        "text": f"<@&{GRAVE_ROLE_ID}> molim da proverite da li nekom modelu nedostaje mass; ukoliko nedostaje, pošaljite ga odmah.\nUkoliko mass i dalje nije poslat, sledi opomena, a pri ponavljanju prekršaja penal od 50$.",
        "shift": None,
        "kind": None,
    },
    {
        "time": time(14, 0),
        "channel_id": GRAVE_GENERAL_CHANNEL_ID,
        "text": f"<@&{GRAVE_ROLE_ID}> ukoliko drugi mass još nije poslat, molim da ga pošaljete u narednih 30 minuta.\nNeispunjavanje obaveze rezultira opomenom, a ponavljanje prekršaja penalom od 50$.",
        "shift": None,
        "kind": None,
    },
    {
        "time": time(14, 30),
        "channel_id": GRAVE_GENERAL_CHANNEL_ID,
        "text": f"<@&{GRAVE_ROLE_ID}> molim da proverite da li nekom modelu nedostaje drugi mass; ukoliko nedostaje, pošaljite ga odmah.\nUkoliko mass i dalje nije poslat, sledi opomena, a pri ponavljanju prekršaja penal od 50$.",
        "shift": None,
        "kind": None,
    },
    # ---------- AFTERNOON ----------
    {
        "time": time(18, 0),
        "channel_id": AFTER_GENERAL_CHANNEL_ID,
        "text": f"<@&{AFTER_ROLE_ID}> molim da mass bude poslat najkasnije do 19:30.\nU slučaju neispunjavanja obaveze, sledi opomena. Ukoliko se prekršaj ponovi, ide penal od 50$.",
        "shift": "after",
        "kind": "first",
    },
    {
        "time": time(19, 0),
        "channel_id": AFTER_GENERAL_CHANNEL_ID,
        "text": f"<@&{AFTER_ROLE_ID}> ukoliko mass još nije poslat, molim da ga pošaljete u narednih 30 minuta.\nNeispunjavanje obaveze rezultira opomenom, a ponavljanje prekršaja penalom od 50$.",
        "shift": "after",
        "kind": "second",
    },
    {
        "time": time(19, 30),
        "channel_id": AFTER_GENERAL_CHANNEL_ID,
        "text": f"<@&{AFTER_ROLE_ID}> molim da proverite da li nekom modelu nedostaje mass; ukoliko nedostaje, pošaljite ga odmah.\nUkoliko mass i dalje nije poslat, sledi opomena, a pri ponavljanju prekršaja penal od 50$.",
        "shift": None,
        "kind": None,
    },
    {
        "time": time(22, 0),
        "channel_id": AFTER_GENERAL_CHANNEL_ID,
        "text": f"<@&{AFTER_ROLE_ID}> ukoliko mass još nije poslat, molim da ga pošaljete u narednih 30 minuta.\nNeispunjavanje obaveze rezultira opomenom, a ponavljanje prekršaja penalom od 50$.",
        "shift": "after",
        "kind": "second",
    },
    {
        "time": time(22, 30),
        "channel_id": AFTER_GENERAL_CHANNEL_ID,
        "text": f"<@&{AFTER_ROLE_ID}> molim da proverite da li nekom modelu i dalje nedostaje mass; ukoliko nedostaje, pošaljite ga odmah.\nUkoliko mass i dalje nije poslat, sledi opomena, a pri ponavljanju prekršaja penal od 50$.",
        "shift": None,
        "kind": None,
    },
    # ---------- MAIN ----------
    {
        "time": time(2, 0),
        "channel_id": MAIN_GENERAL_CHANNEL_ID,
        "text": f"<@&{MAIN_ROLE_ID}> molim da mass bude poslat najkasnije do 4:00.\nU slučaju neispunjavanja obaveze, sledi opomena. Ukoliko se prekršaj ponovi, ide penal od 50$.",
        "shift": "main",
        "kind": "first",
    },
    {
        "time": time(3, 0),
        "channel_id": MAIN_GENERAL_CHANNEL_ID,
        "text": f"<@&{MAIN_ROLE_ID}> ukoliko mass još nije poslat, molim da ga pošaljete u narednih sat vremena.\nNeispunjavanje obaveze rezultira opomenom, a ponavljanje prekršaja penalom od 50$.",
        "shift": "main",
        "kind": "second",
    },
    {
        "time": time(4, 0),
        "channel_id": MAIN_GENERAL_CHANNEL_ID,
        "text": f"<@&{MAIN_ROLE_ID}> molim da proverite da li nekom modelu nedostaje mass; ukoliko nedostaje, pošaljite ga odmah.\nUkoliko mass i dalje nije poslat, sledi opomena, a pri ponavljanju prekršaja penal od 50$.",
        "shift": None,
        "kind": None,
    },
]


def is_mm_approval_channel(channel: discord.abc.GuildChannel) -> bool:
    from discord import TextChannel

    return (
        isinstance(channel, TextChannel)
        and MM_APPROVAL_NAME_SNIPPET in channel.name.lower()
    )


async def send_shift_followups(shift_name: str):
    delay = SHIFT_FOLLOW_DELAY_MIN[shift_name]
    await asyncio.sleep(delay * 60)
    first_sent = shift_first_sent_at.get(shift_name)
    if not first_sent:
        return
    guild_id_int = int(GUILD_ID) if GUILD_ID else None
    if not guild_id_int:
        return
    guild = bot.get_guild(guild_id_int)
    if not guild:
        return
    role_id = {
        "grave": GRAVE_ROLE_ID,
        "after": AFTER_ROLE_ID,
        "main": MAIN_ROLE_ID,
    }[shift_name]
    for ch in guild.text_channels:
        if not is_mm_approval_channel(ch):
            continue
        last_mm = mm_last_time.get(ch.id)
        # nikad nije bilo !mm ili je bilo pre prvog generala → fali mass
        if (last_mm is None) or (last_mm < first_sent):
            await ch.send(
                f"<@&{role_id}> fali mass, proverite da li je poslat i pošaljite ga ovde."
            )


# ==== MM WINDOW SCANNER (prozor reminderi; ping NA KRAJU prozora) ====
MM_WINDOW_ROLE_BY_SHIFT = {
    "graveyard": 1410962300554313870,  # @graveyard
    "afternoon": 1410962344124612710,  # @afternoon
    "main": 1410962407454675047,  # @main
}
# label, start_h, start_m, end_h, end_m, shift
# po tvom zahtevu: start = reminder_start - 30min, ping na END ako nema !mm u prozoru
MM_WINDOWS = [
    # GRAVEYARD: prvi prozor 09:30–11:30 (za prvi mass), drugi 13:30–16:00 (za drugi mass)
    ("grave-1", 9, 30, 11, 30, "graveyard"),
    ("grave-2", 13, 30, 16, 0, "graveyard"),
    # AFTERNOON: prvi prozor 17:30–19:30, drugi prozor 20:30–23:00
    ("after-1", 17, 30, 19, 30, "afternoon"),
    ("after-2", 20, 30, 23, 0, "afternoon"),
    # MAIN: jedan prozor 01:30–04:00 (reminderi ostaju 02:00/03:00/04:00)
    ("main-1", 1, 30, 4, 0, "main"),
]
# markeri da ne pingujemo više puta po prozoru (key = (channel_id, label, YYYY-MM-DD))
mm_scanner_bumped = set()


def _local_now():
    """Tačno vreme za Beograd (automatski CET/CEST)"""
    return datetime.now(ZoneInfo("Europe/Belgrade"))


def _window_today(start_h, start_m, end_h, end_m):
    now = _local_now()  # datetime, ne .time()
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    return start, end


@tasks.loop(minutes=1)
async def mm_window_scanner():
    """Skener: proverava na KRAJU svakog prozora da li je bilo !mm od 'start' do 'end'.
    Ako nije, pinguje odgovarajuću shift rolu u svim mm-approval kanalima.
    """
    guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
    if not guild:
        return
    now = _local_now()  # datetime
    for label, sh, sm, eh, em, shift in MM_WINDOWS:
        start, end = _window_today(sh, sm, eh, em)
        # pingujemo tek kad izađemo iz prozora (>= end) i još nismo bumpovali taj prozor danas
        if now >= end:
            for ch in guild.text_channels:
                if not is_mm_approval_channel(ch):
                    continue
                key = (ch.id, label, start.date().isoformat())
                if key in mm_scanner_bumped:
                    continue  # već odrađeno za ovaj kanal i ovaj prozor
                last_mm = mm_last_time.get(ch.id)
                # ako nije bilo !mm u prozoru → ping
                if (last_mm is None) or (last_mm < start):
                    try:
                        role_id = MM_WINDOW_ROLE_BY_SHIFT[shift]
                        await ch.send(
                            f"<@&{role_id}> fali mass za {shift} ({label.replace('-', ' ')}) — pošaljite ga ovde."
                        )
                    except Exception as e:
                        print("[MM_SCAN] send fail:", e)
                mm_scanner_bumped.add(key)
    # očisti stare markere malo posle ponoći lokalno
    if now.hour == 0 and now.minute in (3, 4, 5):
        mm_scanner_bumped.clear()
        print("[MM_SCAN] cleared bump cache")


@mm_window_scanner.before_loop
async def _before_mm_window_scanner():
    await bot.wait_until_ready()


# ====== MASS REMINDERI (glavni loop) ======
@tasks.loop(minutes=1)
async def mass_reminder_loop():
    """Šalje general mass reminder poruke po SCHEDULE
    i setuje shift_first_sent_at za 'first' poruke."""
    now_local = _local_now()
    h, m = now_local.hour, now_local.minute
    guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
    if not guild:
        return
    for entry in SCHEDULE:
        t: time = entry["time"]
        if h == t.hour and m == t.minute:
            channel_id = entry["channel_id"]
            text = entry["text"]
            shift = entry.get("shift")
            kind = entry.get("kind")
            ch = bot.get_channel(channel_id)
            if not ch:
                try:
                    ch = await guild.fetch_channel(channel_id)
                except Exception as e:
                    print("[MASS_LOOP] ne mogu da nadjem kanal", channel_id, e)
                    continue
            try:
                await ch.send(text)
            except Exception as e:
                print("[MASS_LOOP] send fail:", e)
                continue
            # ako je prvi general za smenu → zapamti vreme i pokreni followup checker
            if shift and kind == "first":
                # radimo u lokalnom vremenu, da bude u istom sistemu kao mm_window_scanner
                shift_first_sent_at[shift] = now_local
                try:
                    asyncio.create_task(send_shift_followups(shift))
                except Exception as e:
                    print("[MASS_LOOP] followup task fail:", e)


@mass_reminder_loop.before_loop
async def _before_mass_reminder_loop():
    await bot.wait_until_ready()

@tasks.loop(minutes=1)
async def qc_reminder_task():
    now = _local_now()
    if now.hour == 2 and now.minute == 0:
        channel = bot.get_channel(1493996105380266114)
        if channel:
            await channel.send(f"<@&1474070997274464379> **Deadline za slanje QC je prošao.**\nMolim te ukoliko nisi do sada, popuni formu za daily QC pomoću komande `/qc`.")
        # Čuvamo podatke za /qcurrent
        month_key = (now.year, now.month, interaction.user.id)
        qc_history[month_key].append({
            'date': now.day,
            'count': len(chatters),
            'timestamp': now
        })

@qc_reminder_task.before_loop
async def before_qc_reminder():
    await bot.wait_until_ready()

# ---------- SUMMARY REPORT (def before on_ready) ----------
mm_sent_log = []  # (user_id, timestamp_local, shift_name)


@tasks.loop(minutes=1)
async def mm_summary_report():
    ch = bot.get_channel(MM_SUMMARY_CHANNEL_ID)
    if not ch:
        return
    now_local = _local_now()
    h, m = now_local.hour, now_local.minute

    def _report_for(shift: str) -> str:
        end = _local_now()
        start = end - timedelta(hours=8)
        users = [u for u, t, s in mm_sent_log if s == shift and start <= t <= end]
        if not users:
            return f"nema !mm komandi za {shift} smenu."
        counts: dict[int, int] = {}
        for uid in users:
            counts[uid] = counts.get(uid, 0) + 1
        lines = [f"<@{u}> – {c}x" for u, c in counts.items()]
        return f"rezime {shift} smene:\n" + "\n".join(lines)

    if h == 18 and m == 0:
        await ch.send(_report_for("graveyard"))
    if h == 10 and m == 0:
        await ch.send(_report_for("main"))
    if h == 2 and m == 0:
        await ch.send(_report_for("afternoon"))


@mm_summary_report.before_loop
async def _before_mm_summary_report():
    await bot.wait_until_ready()


async def sort_team_roles(guild):
    bot_member = guild.me
    non_team = [r for r in guild.roles if not r.name.upper().startswith("TEAM ")]
    team = [
        r
        for r in guild.roles
        if r.name.upper().startswith("TEAM ") and r < bot_member.top_role
    ]
    team_sorted = sorted(team, key=lambda r: r.name.lower())
    start_pos = max(r.position for r in non_team)
    pos = start_pos + 1
    for role in team_sorted:
        await role.edit(position=pos)
        pos += 1
        await asyncio.sleep(0.35)
    print("TEAM roles sorted")


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

# ========== AUTO SCHEDULE TASK - 1h20min + 20min pre smene ==========
@tasks.loop(minutes=1)
async def auto_schedule_task():
    now = _local_now()
    h, m = now.hour, now.minute

    # Novi triggeri: 1 sat i 20 min pre + 20 min pre
    triggers = {
        "grave": [(8, 40), (9, 40)],   # 10:00 smena → 8:40 i 9:40
        "after": [(16, 40), (17, 40)], # 18:00 smena → 16:40 i 17:40
        "main": [(0, 40), (1, 40)],    # 02:00 smena → 00:40 i 01:40
    }

    for shift, times in triggers.items():
        for th, tm in times:
            if h == th and m == tm:
                print(f"[AUTO SCHEDULE] Pokrećem za {shift.upper()} u {h:02d}:{m:02d}")
                asyncio.create_task(run_auto_schedule(shift))
                break


# ========== RUN AUTO SCHEDULE + BLANKO LISTA ==========
async def run_auto_schedule(shift: str):
    guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
    if not guild:
        return

    channel = bot.get_channel(SCHEDULE_CHANNEL.get(shift))
    mgmt_channel = bot.get_channel(1498220907775262750)  # tvoj management kanal

    if not channel or not mgmt_channel:
        return

    try:
        messages = [msg async for msg in channel.history(limit=50)]
        schedule_msg = None
        for msg in messages:
            if "@" in msg.content and any(x in msg.content for x in [":", "/", ","]):
                age = (_local_now() - msg.created_at.replace(tzinfo=ZoneInfo("Europe/Belgrade"))).total_seconds()
                if age < 86400:
                    schedule_msg = msg
                    break

        if not schedule_msg:
            await channel.send(f"⚠️ Auto Schedule za **{shift.upper()}**: Nije pronađen validan raspored.")
            return

        schedule_text = schedule_msg.content.strip()
        print(f"[AUTO SCHEDULE] Pronađen raspored za {shift} → primenjujem...")

        chatter_count = await apply_schedule_logic(guild, schedule_text)

        # === Glavna poruka u general kanal (kao i ranije) ===
        role_id = {"grave": GRAVE_ROLE_ID, "after": AFTER_ROLE_ID, "main": MAIN_ROLE_ID}.get(shift)
        role_mention = f"<@&{role_id}> " if role_id else ""

        final_message = f"""{role_mention}**Role za modele koje imate na rasporedu su vam dodeljene** (dodeljeno za **{chatter_count}** chattera).

Ukoliko vam fali role za nekog modela, molim vas da se obratite direktno nekome iz tima, i nakon provere rola da se clock inujete na Telegram kanalu vaše smene u formatu **!ci model1/model2/itd.** kako bi tim znao da ste aktivni.

**Potrebno dostavljati ratios** za sledeće kreatorke na kraju smene dok se ne navrši period pumpe:  
**Chloe igtvn, Paige, Brenda, Rebeca, Rachel, Elena, mad maddie 2 c, michelle**.

Na svim kreatorkama **sve masseve po ulasku u smenu unsend**, a na svim OFTV modelima **STROGO** zabranjeno slati sexual mms.  
Massevi su SCHEDULOVANI na enya, dia kim ra, dia kim ra n, tracy2. Ako se ne pošalje u prvih sat, pišite privatno nekome iz management tima."""

        await channel.send(final_message)

        # === NOVA BLANKO LISTA ZA COPY (u management kanal) ===
        # Parsiramo chatter-e iz rasporeda
        text = schedule_text
        pattern = re.compile(r'(@[\.\w]+|<\@!?\d+>)(.*?)((?=@[\.\w]+|<\@!?\d+>)|$)', re.S)
        blocks = pattern.findall(text)

        chatter_names = []
        for first_user, content, _ in blocks:
            assignees = re.findall(r'(@[\.\w]+|<\@!?\d+>)', first_user + content)
            for token in assignees:
                member = member_from_token(guild, token)
                name = member.display_name if member else token
                chatter_names.append(name)

        unique_names = list(dict.fromkeys(chatter_names))
        blanko_lista = ", ".join(unique_names)

        await mgmt_channel.send(
            f"{blanko_lista}"
        )

        print(f"[AUTO SCHEDULE] Uspešno završeno za {shift.upper()} ({len(unique_names)} chattera)")

    except Exception as e:
        print(f"[AUTO SCHEDULE] Greška za {shift}: {e}")
        await channel.send(f"❌ Greška u auto schedule za {shift.upper()}: {e}")

def parse_schedule_text(text: str):

    results = []

    for raw_line in text.splitlines():

        line = raw_line.strip()

        if not line:
            continue

        parts = [
            p.strip()
            for p in re.split(r"\s*/\s*", line)
            if p.strip()
        ]

        chatters = []
        models = []

        model_started = False

        for part in parts:

            # chatter segment
            if (
                not model_started
                and (
                    part.startswith("@")
                    or re.match(r"<@!?\d+>", part)
                )
            ):
                chatters.append(part)

            else:
                model_started = True
                models.append(part)

        results.append((chatters, models))

    return results

async def apply_schedule_logic(guild, text: str):

    bot_member = guild.me

    global role_index
    role_index = {}

    for r in guild.roles:
        if r.name.lower().startswith("team "):
            base = normalize_model_name(r.name[5:])
            role_index.setdefault(base, []).append(r)

    parsed = parse_schedule_text(text)

    desired_map = {}
    unknown_models = set()

    # =========================
    # BUILD DESIRED ROLE MAP
    # =========================

    for idx, (chatter_tokens, model_tokens) in enumerate(parsed, start=1):

        desired_roles = []

        for model in model_tokens:

            role = role_from_phrase(guild, model)

            if role and can_touch_role(bot_member, role):
                desired_roles.append(role)
            else:
                unknown_models.add(model)

        for chatter_token in chatter_tokens:

            member = member_from_token(guild, chatter_token)

            if not member:
                print(f"[SCHEDULE] member not found: {chatter_token}")
                continue

            if member.id not in desired_map:
                desired_map[member.id] = set()

            desired_map[member.id].update(desired_roles)

    removed_total = 0
    added_total = 0

    print("\nSCHEDULE APPLY START\n")

    # =========================
    # CLEAN + ASSIGN
    # =========================

    for member_id, desired_roles in desired_map.items():

        member = guild.get_member(member_id)

        if not member:
            continue

        removable = [
            r for r in member.roles
            if (
                r.name.upper().startswith("TEAM ")
                and r.name.upper() not in KEEP_ROLE_NAMES
                and can_touch_role(bot_member, r)
            )
        ]

        removed_count = 0
        added_count = 0

        # CLEAN
        if removable:

            await safe_remove_roles(
                member,
                removable,
                reason="schedule clean"
            )

            removed_count = len(removable)
            removed_total += removed_count

        # ASSIGN
        desired_roles = list(desired_roles)

        if desired_roles:

            await safe_add_roles(
                member,
                desired_roles,
                reason="schedule assign"
            )

            added_count = len(desired_roles)
            added_total += added_count

        print(
            f"✅ {member.display_name} "
            f"(clean {removed_count} / assign {added_count})"
        )

    print(
        f"\nSCHEDULE APPLY done "
        f"(removed={removed_total}, added={added_total})"
    )

    if unknown_models:

        print("\nUNKNOWN MODELS:")

        for m in sorted(unknown_models):
            print(f"- {m}")

    return len(desired_map)

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
    "from now on you write flirty girly catchy dirty minded onlyfans mass follow ups.\n"
    "format and rules:\n"
    "!mma\n"
    "<short simple question already written by the user>\n\n"
    "you never write the question yourself.\n"
    "you only write follow ups for an existing !mma line.\n\n"
    "fu1: flirty response that fits any fan answer to the question. statement only. no question.\n"
    "fu1.5: short follow up question related to fu1. cannot be answered with yes or no.\n"
    "fu2: statement that escalates the scene or adds sexual undertone. no question.\n"
    "fu2.5: short flirty question related to fu2. also cannot be answered with yes or no.\n"
    "fu3: final teasing or suggestive statement. no question.\n"
    "fu3.5: one more open ended flirty question to deepen the scene or intimacy.\n\n"
    "hard style rules:\n"
    "- everything must be in lowercase.\n"
    "- no bold no emojis.\n"
    "- no commas no dashes. only periods and spaces.\n"
    "- tone is dirty flirty girly and playful teasing and immersive.\n"
    "- scenario tone and structure must change every time. never repeat lines.\n"
    "- fu1 must always be something that works after any answer.\n"
    "- fu1.5 fu2.5 and fu3.5 are always questions that avoid yes or no answers.\n"
    "- never use this or that questions unless the user asks for them.\n"
    "- never use the phrase either way.\n"
    "- prompts are short and catchy. follow ups can be a bit longer but still punchy.\n"
    "- default is one set of fus per request.\n"
    "- never start with soft intros like ever wondered or what if.\n"
    "output rules:\n"
    "- you only output fu lines.\n"
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


STATUS_WORDS = {
    "ra",
    "n",
    "vip",
    "x",
    "oll",
    "ock",
    "inb",
    "zzz",
    "vf",
    "eep",
    "jsn",
    "bcl",
    "bk",
    "hnq",
    "jaa",
    "sa",
    "ggn",
    "yll",
    "oftv",
    "rco",
    "tvn",
    "tsu",
    "kct",
    "yr",
    "oll",
}


def extract_model_name(entry: str) -> str:
    words = entry.lower().strip().split()
    model_parts = []
    for w in words:
        if w.isdigit():
            continue
        if w in STATUS_WORDS:
            break
        model_parts.append(w)
    return " ".join(model_parts)


def normalize_model_name(name: str):
    core = extract_core_name(name)
    core = unicodedata.normalize("NFKD", core)
    core = "".join(c for c in core if not unicodedata.combining(c))
    core = re.sub(r"[^a-z0-9]", "", core)
    return core


def similarity(a: str, b: str):
    return SequenceMatcher(None, a, b).ratio()


# ---------- ROLE LOOKUP ----------
def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (s or "").upper())


def build_role_index(guild: discord.Guild):
    by_norm = {}
    by_norm_no_team = {}
    for r in guild.roles:
        by_norm[norm(r.name)] = r
        if r.name.upper().startswith("TEAM "):
            stripped = r.name[5:]
            by_norm_no_team[norm(stripped)] = r
    return by_norm, by_norm_no_team


# alias normalizacija + resolve
ALIAS_TO_BASE = {
    "ANITA2USASOPHIE": "ANITA",
    "ANITA2USA": "ANITA",
    "ANITA": "ANITA",
    "SKYLARONLYF": "SKYLAR ONLYF",
    "SKYLARONLYFYY": "SKYLAR ONLYF",
    "SKYLAR": "SKYLAR ONLYF",
    "AMBEREMERSONT": "AMBER EMERSON T",
    "AMBEREMERSON": "AMBER EMERSON T",
    "AMBER": "AMBER EMERSON T",
    "DIAX": "DIA",
    "DIAVIP": "DIA",
    "DIA": "DIA",
    "MIAROUGE": "MIA ROUGE",
    "MIAROGUE": "MIA ROUGE",
    "MIA": "MIA ROUGE",
    "KASSIEX": "KASSIE X",
    "KASSIE": "KASSIE X",
    "EMILYONLYF": "EMILY ONLYF",
    "EVAG": "EVA G",
    "LARAG": "LARA G",
    "MAYAFOXEY": "MAYA FOXY",
    "SKAYLARONLYF": "SKYLAR ONLYF",
    "SYNDEY": "SYDNEY",
    "HANAS": "HANNAS",
    "MIAPOZZZP": "MIAPOZZZ P",
    "LEKESSIAT": "LEKESSIA",
    "EMILYKOIVC": "EMILYKOI V C",
    "MOLLYVC": "MOLLY V C",
    "RAVENSA": "RAVEN",
    "MIAPOPZZ": "MIAPOZZZ P",
    "MACCMKATIE": "CCM KATIE",
    "KENDALLTINDER": "KENDAL TINDER",
}
ALIAS_KEYS_BY_LEN = sorted(ALIAS_TO_BASE.keys(), key=len, reverse=True)
NOISE_WORDS_IN_PHRASE = {"YY"}


def clean_role_phrase(phrase: str) -> str:
    if not phrase:
        return ""
    s = phrase.strip()
    if s.upper() in {"X"}:
        return ""
    s = re.sub(r"\b(inbox|inb)\s*([0-9]+)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(inbox[0-9]+|inb[0-9]+)\b", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(free|paid|full)\b", "", s, flags=re.IGNORECASE)
    toks = [
        t for t in re.split(r"\s+", s) if t and t.upper() not in NOISE_WORDS_IN_PHRASE
    ]
    s = " ".join(toks).strip()
    s = re.sub(r"\b([A-Za-z]+)\s+2\b", r"\1", s)
    return s


def _resolve_alias_to_base(base: str) -> str | None:
    nb = norm(base)
    for key in ALIAS_KEYS_BY_LEN:
        if key in nb:
            return ALIAS_TO_BASE[key]
    return None


def role_from_phrase(guild, phrase):
    base = normalize_model_name(phrase)
    if base in role_index:
        return role_index[base][0]
    best = None
    best_score = 0
    for k, roles in role_index.items():
        score = similarity(base, k)
        if score > best_score:
            best_score = score
            best = roles[0]
    if best_score >= 0.72:
        return best
    return None


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


def is_model_role(role: discord.Role) -> bool:
    return role.name.upper().startswith("TEAM ")


def is_keep_role(role: discord.Role) -> bool:
    return role.name.upper() in KEEP_ROLE_NAMES


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


# ---------- /assign /deassign /clean /a /cleanmulti ----------
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
        lines = [
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

from collections import defaultdict
import calendar

# Globalni counter za QC po korisniku po mesecu
qc_counter = defaultdict(int)   # (user_id, year, month) -> broj QC-a

def get_current_month_key():
    now = datetime.now()
    return (now.year, now.month)

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


# Automatski reset countera 1. u mesecu
@tasks.loop(hours=1)
async def monthly_qc_reset():
    now = datetime.now()
    if now.day == 1 and now.hour == 0:
        qc_counter.clear()
        print("✅ QC counter resetovan - novi mesec")

@monthly_qc_reset.before_loop
async def before_monthly_reset():
    await bot.wait_until_ready()

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

# ========== /schedule — TAČNA LOGIKA (sa svim funkcijama unutra) ==========
@tree.command(
    name="schedule",
    description="Čisti TEAM role i dodeljuje nove po blokovima (@user1 modeli... @user2 modeli...)",
    guild=GUILD_OBJ,
)
@need_manage_roles()
async def schedule(interaction: discord.Interaction, text: str, apply: bool = False):
    await interaction.response.defer(ephemeral=True, thinking=True)

    guild = interaction.guild
    bot_member = guild.me

    # Refresh role index
    global role_index
    role_index = {}
    for r in guild.roles:
        if r.name.upper().startswith("TEAM "):
            base = normalize_model_name(r.name[5:])
            role_index.setdefault(base, []).append(r)

    text_norm = (text or "").replace("⁄", "/").replace("／", "/").replace("\r", "\n")

    report = []
    total_rm = 0
    total_add = 0
    unknowns = []

    # Glavni regex za blokove
    pattern = re.compile(r'(@[\.\w]+|<\@!?\d+>)(.*?)((?=@[\.\w]+|<\@!?\d+>)|$)', re.S)
    blocks = pattern.findall(text_norm)

    def parse_roles_list_with_unknowns(roles_text: str):
        """Lokalna funkcija - sve je ovde"""
        txt = (roles_text or "").replace("\\", "/")
        segs = [s.strip() for s in re.split(r"[\/,;|]+", txt) if s.strip()]
        wanted = []
        unknown = []
        seen = set()
        for seg in segs:
            model = extract_model_name(seg)
            base = clean_role_phrase(model)
            if not base:
                continue
            r = role_from_phrase(guild, base)
            if r:
                if r.id not in seen:
                    wanted.append(r)
                    seen.add(r.id)
            else:
                if base not in unknown:
                    unknown.append(base)
        return wanted, unknown

    for idx, (first_user, content, _) in enumerate(blocks, start=1):
        # Svi useri u bloku (@user1 / @user2)
        assignees = re.findall(r'(@[\.\w]+|<\@!?\d+>)', first_user + content)
        
        # Modeli = sve posle poslednjeg @usera u bloku
        models_text = content.split('@')[-1] if '@' in content else content

        desired_roles, unk = parse_roles_list_with_unknowns(models_text)
        if unk:
            unknowns.extend(unk)

        for a_idx, token in enumerate(assignees, start=1):
            tag = f"{idx}.{a_idx}"
            member = member_from_token(guild, token)
            if not member:
                report.append(f"[{tag}] ❌ User nije nađen: {token}")
                continue

            # CLEAN stare TEAM role
            old_roles = [
                r for r in member.roles
                if r.name.upper().startswith("TEAM ")
                and r.name.upper() not in KEEP_ROLE_NAMES
                and can_touch_role(bot_member, r)
            ]

            # ASSIGN nove
            touchable_assign = [r for r in desired_roles if can_touch_role(bot_member, r)]

            if not apply:
                report.append(f"[{tag}] PREVIEW {member.display_name} → {len(touchable_assign)} role")
                continue

            # REAL APPLY
            try:
                if old_roles:
                    rem = await safe_remove_roles(member, old_roles, reason=f"schedule by {interaction.user}")
                    total_rm += len(rem)
                if touchable_assign:
                    add = await safe_add_roles(member, touchable_assign, reason=f"schedule by {interaction.user}")
                    total_add += len(add)

                report.append(f"[{tag}] ✅ {member.display_name} (clean {len(old_roles)} / assign {len(touchable_assign)})")
            except Exception as e:
                report.append(f"[{tag}] ❌ {member.display_name} — greška: {e}")

    header = "SCHEDULE PREVIEW\n" if not apply else f"SCHEDULE APPLY done (removed={total_rm}, added={total_add})\n"
    out = header + "\n".join(report)

    if unknowns:
        out += "\n\nUNKNOWN MODELS:\n- " + "\n- ".join(sorted(set(unknowns)))

    for i in range(0, len(out), 1800):
        await interaction.followup.send(f"```\n{out[i:i+1800]}\n```", ephemeral=True)


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
    await mgmt_channel.send(f"{blanko_lista}")

    await interaction.followup.send(
        f"✅ Gotovo! Poslao sam **{len(unique_names)}** chattera u management kanal.\n"
        "Na dnu je **blanko lista** koju možeš desni klik → Copy.", 
        ephemeral=True
    )


# /newm
@tree.command(
    name="newm",
    description="Napravi novi model",
    guild=GUILD_OBJ
)
@need_manage_roles()
@need_manage_channels()
async def new_model(interaction: discord.Interaction, ime: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild

    model_name = ime.strip().upper()
    role_name = f"TEAM {model_name}"
    category_name = f"TEAM {model_name}"

    if discord.utils.get(guild.roles, name=role_name):
        return await interaction.followup.send(f"❌ Rola **{role_name}** već postoji!", ephemeral=True)

    try:
        # 1. Kreiraj rolu
        new_role = await guild.create_role(
            name=role_name,
            colour=discord.Colour(0x2b2d31),
            mentionable=True,
            reason=f"/newm by {interaction.user}"
        )

        await asyncio.sleep(3)
        await sort_team_roles(guild)

        # 2. Kreiraj kategoriju (bez overwrites)
        new_category = await guild.create_category(name=category_name)

        # 3. Kreiraj kanale
        general = await new_category.create_text_channel("general")
        whales = await new_category.create_text_channel("whales")

        # 4. Postavi permisije sa dužim čekanjem
        await asyncio.sleep(5)
        await new_category.set_permissions(guild.default_role, view_channel=False)
        await asyncio.sleep(3)
        await new_category.set_permissions(new_role, view_channel=True)

        # 5. Welcome poruka
        welcome_message = (
            "Ovo je kanal u koji se upisuju sve bitne stavke vezane za model, spendere, ostale fanove i slično.\n\n"
            "Ukoliko ste imali farmu, nju upisujete u kanalu **#whales** koristeći komandu `/farm` uz sve adekvatne podatke.\n\n"
            "Ako vam je potrebno više informacija od onih koje već imate o modelu, obavezno to napišite u grupnom chatu vaše smene na Telegramu, "
            "uz odgovarajuće tagove (supervizor / management communications – npr. joshiepooh, daddysmurf itd.).\n\n"
            "Što se tiče customa – ako nema dovoljno informacija, a fan je mali spender, možete odokativno napraviti pitch za nešto „ekskluzivno“ za određenu sumu. "
            "Ako prođe i uzmu se pare, tada se dodatni detalji mogu tražiti u grupi.\n\n"
            "Ne pitati za custome fanove koji su potrošili 0 ili su tek došli.\n\n"
            "Za sve lične podatke koji nisu navedeni u postojećim informacijama, dozvoljeno je odokativno improvizovati, uz obavezno upisivanje u notes šta je izmišljeno. "
            "**Bitno: ne lagati o ozbiljnim i lako proverljivim stvarima (npr. porodica, osetljive teme). Sitnice poput omiljene boje su okej.**"
        )
        await general.send(welcome_message)

        await sort_team_categories(guild)

        embed = discord.Embed(title="✅ Novi model uspešno kreiran!", color=0x00ff00)
        embed.add_field(name="Rola", value=f"`{role_name}`", inline=False)
        embed.add_field(name="Kategorija", value=f"`{category_name}`", inline=False)
        embed.add_field(name="Kanali", value=f"{general.mention}\n{whales.mention}", inline=False)
        await interaction.followup.send(embed=embed)

    except discord.Forbidden as e:
        await interaction.followup.send(
            f"❌ **Missing Permissions (50013)**\n\n"
            "Bot nema prava da kreira kategoriju/kanale.\n"
            "Proveri da li bot rola ima **Manage Channels** i da je **skroz na vrhu** liste rola.\n"
            "Pokušaj da pomeriš rolu dole pa nazad gore i restartuj bota.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Greška: {e}", ephemeral=True)


# ========== TESTAUTO - Ručno testiranje auto schedule ==========
@tree.command(
    name="testauto", description="Ručno pokreće auto schedule za test", guild=GUILD_OBJ
)
@need_manage_roles()
async def test_auto_schedule(interaction: discord.Interaction, shift: str):
    await interaction.response.defer(ephemeral=True)

    if shift not in ["grave", "after", "main"]:
        return await interaction.followup.send(
            "❌ Dozvoljene vrednosti: `grave`, `after`, `main`", ephemeral=True
        )
    await interaction.followup.send(
        f"🔄 Pokrećem Auto Schedule test za **{shift.upper()}** smenu...", ephemeral=True
    )
    # Pokrećemo u pozadini da ne blokira interakciju
    asyncio.create_task(run_auto_schedule(shift))
    await interaction.followup.send(
        "✅ Test je pokrenut u pozadini. Proveri odgovarajući general kanal (graveyard / afternoon / main).",
        ephemeral=True,
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


def _detect_shift_now():
    now = _local_now().time()
    h = now.hour
    if 10 <= h < 18:
        return "graveyard"
    if h >= 18 or h < 2:
        return "afternoon"
    return "main"


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    content_raw = message.content or ""
    content = content_raw.strip().lower()
    if content.startswith("!mm"):
        now_local = _local_now()
        mm_last_time[message.channel.id] = now_local
        mm_sent_log.append((message.author.id, now_local, _detect_shift_now()))
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
        if not mass_reminder_loop.is_running():
            mass_reminder_loop.start()
        if not mm_window_scanner.is_running():
            mm_window_scanner.start()
        if not mm_summary_report.is_running():
            mm_summary_report.start()
        if not auto_schedule_task.is_running():
            auto_schedule_task.start()
            print("✅ Auto Schedule task je pokrenut")
        if not qc_reminder_task.is_running():
            qc_reminder_task.start()
            print("✅ QC reminder task pokrenut (2:00 AM)")
    except Exception as e:
        print("sync fail:", e)


# ---------- RUN ----------
bot.run(TOKEN)
