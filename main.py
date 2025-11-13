# main.py — FINAL
# /schedule auto-clean TEAM rola (uz KEEP), pa dodela novih
# + PRIJAVA: unknown modeli (skipped/unknown)
# + !mm detekcija (stopira remindere) + AI/FU auto-predlozi u mm-approval kanalima

import os
import re
import asyncio
import random
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Modal, TextInput
from discord import TextStyle
from dotenv import load_dotenv
from datetime import datetime, time, timedelta
from openai import OpenAI

# --- env first ---
load_dotenv()

TOKEN    = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

USE_AI_FU      = os.getenv("USE_AI_FU", "false").lower() in ("1","true","yes","on")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# build client only after env is loaded
client = OpenAI(api_key=OPENAI_API_KEY) if (USE_AI_FU and OPENAI_API_KEY) else None

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN nije setovan u .env")


# ---------- TUNABLES ----------
SLEEP_BETWEEN_CALLS = 0.35
CHUNK_SIZE          = 24
RETRIES             = 5
RETRY_BASE_SLEEP    = 0.8
PROGRESS_EVERY_N    = 5

# Role koje SE NIKAD NE DIRAJU kod auto-clean (pre /schedule)
KEEP_ROLE_NAMES = {
    "AFTERNOON", "GRAVEYARD", "MAIN", "OBUKA", "LV CHATTER"
}

# ---------- BOT ----------
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True   # za !mm detekciju
bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree
GUILD_OBJ = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

# kanali i snippet za mm-approval
MM_APPROVAL_NAME_SNIPPET = "mm-approval"

# summary kanal (tvoj)
MM_SUMMARY_CHANNEL_ID = 1433577356437491774

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


# ============ MASS REMINDERI + !mm LOGIKA ============
GRAVE_GENERAL_CHANNEL_ID = 1364850505234518067  # #graveyard
AFTER_GENERAL_CHANNEL_ID = 1364850574205648967  # #afternoon
MAIN_GENERAL_CHANNEL_ID  = 1364850795215982634  # #main

GRAVE_ROLE_ID = 1410962300554313870            # @graveyard
AFTER_ROLE_ID = 1410962344124612710            # @afternoon
MAIN_ROLE_ID  = 1410962407454675047            # @main

SUPERVISOR_IDS = [
    886983698321391667,   # ti
    923657835164889119,   # drugi supervizor
]

# koliko cekamo posle DRUGOG generala
SHIFT_FOLLOW_DELAY_MIN = {
    "grave": 30,
    "after": 30,
    "main":  60,
}

# vreme PRVOG generala po smeni
SHIFT_FIRST_TIME = {
    "grave": time(10, 0),
    "after": time(18, 0),
    "main":  time(2, 0),
}

# cuvamo kad je zaista poslat prvi general (UTC)
shift_first_sent_at = {
    "grave": None,
    "after": None,
    "main":  None,
}

# poslednji !mm po kanalu
mm_last_time: dict[int, datetime] = {}  # channel_id -> datetime

# raspored svih general poruka
SCHEDULE = [
    # ---------- GRAVE ----------
    {"time": time(10, 0), "channel_id": GRAVE_GENERAL_CHANNEL_ID, "text": f"<@&{GRAVE_ROLE_ID}> molim da prvi mass bude poslat najkasnije do 11:30.", "shift": "grave", "kind": "first"},
    {"time": time(11, 0), "channel_id": GRAVE_GENERAL_CHANNEL_ID, "text": f"<@&{GRAVE_ROLE_ID}> ukoliko mass još nije poslat, molim da ga pošaljete u narednih 30 minuta.", "shift": "grave", "kind": "second"},
    {"time": time(11, 30), "channel_id": GRAVE_GENERAL_CHANNEL_ID, "text": f"<@&{GRAVE_ROLE_ID}> molim da proverite da li nekom modelu nedostaje mass; ukoliko nedostaje, pošaljite ga odmah.", "shift": None, "kind": None},
    {"time": time(14, 0), "channel_id": GRAVE_GENERAL_CHANNEL_ID, "text": f"<@&{GRAVE_ROLE_ID}> ukoliko drugi mass još nije poslat, molim da ga pošaljete u narednih 30 minuta.", "shift": None, "kind": None},
    {"time": time(14, 30), "channel_id": GRAVE_GENERAL_CHANNEL_ID, "text": f"<@&{GRAVE_ROLE_ID}> molim da proverite da li nekom modelu nedostaje drugi mass; ukoliko nedostaje, pošaljite ga odmah.", "shift": None, "kind": None},

    # ---------- AFTERNOON ----------
    {"time": time(18, 0), "channel_id": AFTER_GENERAL_CHANNEL_ID, "text": f"<@&{AFTER_ROLE_ID}> molim da mass bude poslat najkasnije do 19:30.", "shift": "after", "kind": "first"},
    {"time": time(19, 0), "channel_id": AFTER_GENERAL_CHANNEL_ID, "text": f"<@&{AFTER_ROLE_ID}> ukoliko mass još nije poslat, molim da ga pošaljete u narednih 30 minuta.", "shift": "after", "kind": "second"},
    {"time": time(19, 30), "channel_id": AFTER_GENERAL_CHANNEL_ID, "text": f"<@&{AFTER_ROLE_ID}> molim da proverite da li nekom modelu nedostaje mass; ukoliko nedostaje, pošaljite ga odmah.", "shift": None, "kind": None},
    {"time": time(22, 0), "channel_id": AFTER_GENERAL_CHANNEL_ID, "text": f"<@&{AFTER_ROLE_ID}> ukoliko mass još nije poslat, molim da ga pošaljete u narednih 30 minuta.", "shift": "after", "kind": "second"},
    {"time": time(22, 30), "channel_id": AFTER_GENERAL_CHANNEL_ID, "text": f"<@&{AFTER_ROLE_ID}> molim da proverite da li nekom modelu i dalje nedostaje mass; ukoliko nedostaje, pošaljite ga odmah.", "shift": None, "kind": None},

    # ---------- MAIN ----------
    {"time": time(2, 0), "channel_id": MAIN_GENERAL_CHANNEL_ID, "text": f"<@&{MAIN_ROLE_ID}> molim da mass bude poslat najkasnije do 4:00.", "shift": "main", "kind": "first"},
    {"time": time(3, 0), "channel_id": MAIN_GENERAL_CHANNEL_ID, "text": f"<@&{MAIN_ROLE_ID}> ukoliko mass još nije poslat, molim da ga pošaljete u narednih sat vremena.", "shift": "main", "kind": "second"},
    {"time": time(4, 0), "channel_id": MAIN_GENERAL_CHANNEL_ID, "text": f"<@&{MAIN_ROLE_ID}> molim da proverite da li nekom modelu nedostaje mass; ukoliko nedostaje, pošaljite ga odmah.", "shift": None, "kind": None},
]

def is_mm_approval_channel(channel: discord.abc.GuildChannel) -> bool:
    from discord import TextChannel
    return isinstance(channel, TextChannel) and MM_APPROVAL_NAME_SNIPPET in channel.name.lower()

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
        "main":  MAIN_ROLE_ID,
    }[shift_name]

    for ch in guild.text_channels:
        if not is_mm_approval_channel(ch):
            continue

        last_mm = mm_last_time.get(ch.id)

        # nikad nije bilo !mm ili je bilo pre prvog generala → fali mass
        if (last_mm is None) or (last_mm < first_sent):
            await ch.send(f"<@&{role_id}> fali mass, proverite da li je poslat i pošaljite ga ovde.")

# ==== MM WINDOW SCANNER (prozor reminderi; ping NA KRAJU prozora) ====
MM_WINDOW_ROLE_BY_SHIFT = {
    "graveyard": 1410962300554313870,  # @graveyard
    "afternoon": 1410962344124612710,  # @afternoon
    "main":      1410962407454675047,  # @main
}

# label, start_h, start_m, end_h, end_m, shift
# po tvom zahtevu: start = reminder_start - 30min, ping na END ako nema !mm u prozoru
MM_WINDOWS = [
    # GRAVEYARD: prvi prozor 09:30–11:30 (za prvi mass), drugi 13:30–16:00 (za drugi mass)
    ("grave-1",  9, 30, 11, 30, "graveyard"),
    ("grave-2", 13, 30, 16,  0, "graveyard"),

    # AFTERNOON: prvi prozor 17:30–19:30, drugi prozor 20:30–23:00
    ("after-1", 17, 30, 19, 30, "afternoon"),
    ("after-2", 20, 30, 23,  0, "afternoon"),

    # MAIN: jedan prozor 01:30–04:00 (reminderi ostaju 02:00/03:00/04:00)
    ("main-1",   1, 30,  4,  0, "main"),
]

# markeri da ne pingujemo više puta po prozoru (key = (channel_id, label, YYYY-MM-DD))
mm_scanner_bumped = set()

def _local_now():
    # Beograd ~ UTC+1 (bez fine DST logike, isto kao ranije)
    return datetime.utcnow() + timedelta(hours=1)

def _window_today(start_h, start_m, end_h, end_m):
    now = _local_now()  # datetime, ne .time()

    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end   = now.replace(hour=end_h,   minute=end_m,   second=0, microsecond=0)

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
                        await ch.send(f"<@&{role_id}> fali mass za {shift} ({label.replace('-', ' ')}) — pošaljite ga ovde.")
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
            text       = entry["text"]
            shift      = entry.get("shift")
            kind       = entry.get("kind")

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



# ====== AI/FU HELPERI ======
def _sanitize_mm_text(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[\U00010000-\U0010ffff]", "", s)  # skini emojije (van BMP)
    s = s.replace("—", " ").replace("-", " ")      # zabrana crtica i duge crte
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
            ("fu1: taster or toucher",
             "fu2: i’ll keep it just out of reach till you ask nice",
             "fu3: where do you want me first"),
            ("fu1: slow or rough tonight",
             "fu2: i set the pace you just try to keep up",
             "fu3: what safe word are you not going to use"),
            ("fu1: hands behind your back or on my hips",
             "fu2: i make you work for every inch",
             "fu3: what do you want me to say when you break"),
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
            key = "fu1:" if i == 1 else ("fu1.5:" if i == 2 else ("fu2:" if i == 3 else "fu2.5:"))
            labeled.append(f"{key} {ln.lower()}")
        i += 1
    return labeled[:4]


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
    "ANITA2USA":       "ANITA",
    "ANITA":           "ANITA",
    "SKYLARONLYF":     "SKYLAR ONLYF",
    "SKYLARONLYFYY":   "SKYLAR ONLYF",
    "SKYLAR":          "SKYLAR ONLYF",
    "AMBEREMERSONT":   "AMBER EMERSON T",
    "AMBEREMERSON":    "AMBER EMERSON T",
    "AMBER":           "AMBER EMERSON T",
    "DIAX":            "DIA",
    "DIAVIP":          "DIA",
    "DIA":             "DIA",
    "MIAROUGE":        "MIA ROUGE",
    "MIAROGUE":        "MIA ROUGE",
    "MIA":             "MIA ROUGE",
    "KASSIEX":         "KASSIE X",
    "KASSIE":          "KASSIE X",
    "EMILYONLYF":      "EMILY ONLYF",
    "EVAG":            "EVA",
    "LARAG":           "LARA",
    "MAYAFOXEY":       "MAYA FOXY",
    "SKAYLARONLYF":    "SKYLAR ONLYF",
    "SYNDEY":          "SYDNEY",
    "HANAS":           "HANNAS",
    "MIAPOZZZP":       "MIAPOZZZ P",
    "LEKESSIAT":       "LEKESSIA",
    "EMILYKOIVC":      "EMILYKOI",
    "MOLLYVC":         "MOLLY",
    "RAVENSA":         "RAVEN",
    "MIAPOPZZ":        "MIAPOZZZ P",
    "MACCMKATIE":      "CCM KATIE",
    "KENDALLTINDER":   "KENDAL TINDER",
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
    toks = [t for t in re.split(r"\s+", s) if t and t.upper() not in NOISE_WORDS_IN_PHRASE]
    s = " ".join(toks).strip()
    s = re.sub(r"\b([A-Za-z]+)\s+2\b", r"\1", s)
    return s

def _resolve_alias_to_base(base: str) -> str | None:
    nb = norm(base)
    for key in ALIAS_KEYS_BY_LEN:
        if key in nb:
            return ALIAS_TO_BASE[key]
    return None

def role_from_phrase(guild: discord.Guild, phrase: str):
    base = clean_role_phrase(phrase)
    if not base:
        return None

    resolved = _resolve_alias_to_base(base)
    if resolved:
        base = resolved

    by_norm, by_no_team = build_role_index(guild)

    r = discord.utils.get(guild.roles, name=base)
    if r:
        return r

    team_name = f"TEAM {base}"
    r = discord.utils.get(guild.roles, name=team_name)
    if r:
        return r

    n_base = norm(base)
    if n_base in by_norm:
        return by_norm[n_base]

    n_team = norm(team_name)
    if n_team in by_norm:
        return by_norm[n_team]

    if n_base in by_no_team:
        return by_no_team[n_base]

    return None

def parse_roles_from_text(guild: discord.Guild, text: str) -> list[discord.Role]:
    ids = re.findall(r"<@&(\d+)>", text or "")
    return [guild.get_role(int(x)) for x in ids if guild.get_role(int(x))]

def parse_user_ids(text: str) -> list[int]:
    return [int(x) for x in re.findall(r"<@!?(\d+)>", text or "")]

async def ensure_member(guild: discord.Guild, user_id: int):
    m = guild.get_member(user_id)
    if m: return m
    try:
        return await guild.fetch_member(user_id)
    except:
        return None

def member_from_token(guild: discord.Guild, token: str):
    ids = parse_user_ids(token)
    if ids:
        return guild.get_member(ids[0]) or None
    cleaned = token.replace("@", "").strip()
    if not cleaned: return None
    for m in guild.members:
        if m.display_name.lower() == cleaned.lower() or (m.name and m.name.lower() == cleaned.lower()):
            return m
    target = norm(cleaned)
    for m in guild.members:
        if norm(m.display_name) == target or norm(m.name) == target:
            return m
    return None

def can_touch_role(bot_member: discord.Member, role: discord.Role) -> bool:
    if role is None: return False
    if role.is_default(): return False
    if role.managed: return False
    return bot_member.guild_permissions.manage_roles and bot_member.top_role > role

def why_blocked(bot_member: discord.Member, role: discord.Role):
    r = []
    if role.is_default(): r.append("everyone")
    if role.managed: r.append("managed")
    if not bot_member.guild_permissions.manage_roles: r.append("no Manage Roles")
    if bot_member.top_role <= role: r.append("bot below role")
    return r or ["ok"]

def is_model_role(role: discord.Role) -> bool:
    return role.name.upper().startswith("TEAM ")

def is_keep_role(role: discord.Role) -> bool:
    return role.name.upper() in KEEP_ROLE_NAMES


# ---------- /assign /deassign /clean /a /cleanmulti ----------
def need_manage_roles():
    def predicate(interaction: discord.Interaction):
        gp = interaction.user.guild_permissions
        if gp.manage_roles or gp.administrator:
            return True
        raise app_commands.CheckFailure("treba ti Manage Roles.")
    return app_commands.check(predicate)

async def safe_add_roles(member: discord.Member, roles: list[discord.Role], reason: str):
    added = []
    for i in range(0, len(roles), CHUNK_SIZE):
        chunk = roles[i:i+CHUNK_SIZE]
        for attempt in range(1, RETRIES+1):
            try:
                if chunk:
                    await member.add_roles(*chunk, reason=reason)
                    added.extend(chunk)
                await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                break
            except discord.Forbidden:
                raise
            except Exception:
                if attempt >= RETRIES: raise
                await asyncio.sleep(RETRY_BASE_SLEEP * attempt)
    return added

async def safe_remove_roles(member: discord.Member, roles: list[discord.Role], reason: str):
    removed = []
    for i in range(0, len(roles), CHUNK_SIZE):
        chunk = roles[i:i+CHUNK_SIZE]
        for attempt in range(1, RETRIES+1):
            try:
                if chunk:
                    await member.remove_roles(*chunk, reason=reason)
                    removed.extend(chunk)
                await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                break
            except discord.Forbidden:
                raise
            except Exception:
                if attempt >= RETRIES: raise
                await asyncio.sleep(RETRY_BASE_SLEEP * attempt)
    return removed

@tree.command(description="dodeli više rola jednom useru", guild=GUILD_OBJ)
@need_manage_roles()
async def assign(interaction: discord.Interaction, user: discord.Member, roles: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild; bot_member = guild.me
    role_objs = parse_roles_from_text(guild, roles)
    if not role_objs:
        return await interaction.followup.send("pinguj role: @Role1 @Role2", ephemeral=True)
    ok  = [r for r in role_objs if can_touch_role(bot_member, r)]
    bad = [r for r in role_objs if r not in ok]
    try:
        added = await safe_add_roles(user, ok, reason=f"by {interaction.user}")
        msg = [f"dodato {user.display_name}: {', '.join(r.name for r in added) or 'ništa'}"]
        for r in bad: msg.append(f"preskočeno {r.name}: {' / '.join(why_blocked(bot_member, r))}")
        await interaction.followup.send("```\n" + "\n".join(msg) + "\n```", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"fail: {e}", ephemeral=True)

@tree.command(description="skini konkretne role sa usera", guild=GUILD_OBJ)
@need_manage_roles()
async def deassign(interaction: discord.Interaction, user: discord.Member, roles: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild; bot_member = guild.me
    role_objs = parse_roles_from_text(guild, roles)
    if not role_objs:
        return await interaction.followup.send("pinguj role: @Role1 @Role2", ephemeral=True)
    ok  = [r for r in role_objs if can_touch_role(bot_member, r)]
    bad = [r for r in role_objs if r not in ok]
    try:
        removed = await safe_remove_roles(user, ok, reason=f"by {interaction.user}")
        msg = [f"skinuto {user.display_name}: {', '.join(r.name for r in removed) or 'ništa'}"]
        for r in bad: msg.append(f"preskočeno {r.name}: {' / '.join(why_blocked(bot_member, r))}")
        await interaction.followup.send("```\n" + "\n".join(msg) + "\n```", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"fail: {e}", ephemeral=True)

@tree.command(description="skini sve role koje bot sme (jedan user)", guild=GUILD_OBJ)
@need_manage_roles()
async def clean(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild; bot_member = guild.me
    removable = [r for r in user.roles if can_touch_role(bot_member, r)]
    blocked   = [r for r in user.roles if r not in removable and not r.is_default()]
    if not removable:
        return await interaction.followup.send(f"nema šta da skidam sa {user.display_name}", ephemeral=True)
    removed = await safe_remove_roles(user, removable, reason=f"by {interaction.user}")
    msg = [f"obrisano {user.display_name}: {', '.join(r.name for r in removed) or 'ništa'}"]
    if blocked:
        msg.append("preskočeno:")
        for r in blocked: msg.append(f"- {r.name}: {' / '.join(why_blocked(bot_member, r))}")
    await interaction.followup.send("```\n" + "\n".join(msg) + "\n```", ephemeral=True)

@tree.command(name="a", description="batch assign: @u1 @r1 @r2 ; @u2 @r3 ...", guild=GUILD_OBJ)
@need_manage_roles()
async def a_batch(interaction: discord.Interaction, payload: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild; bot_member = guild.me
    text = payload.replace(";", " ")
    tokens = re.findall(r"<@!?(\d+)>|<@&(\d+)>", text)
    batches, current_uid, current_roles = [], None, []
    for uid, rid in tokens:
        if uid:
            if current_uid and current_roles:
                batches.append((current_uid, current_roles)); current_roles=[]
            current_uid = int(uid)
        else:
            role = guild.get_role(int(rid))
            if current_uid: current_roles.append(role)
    if current_uid and current_roles: batches.append((current_uid, current_roles))
    if not batches:
        return await interaction.followup.send("nisam našao user+role kombinacije", ephemeral=True)

    lines = []
    for idx, (uid, roles) in enumerate(batches, start=1):
        member = await ensure_member(guild, uid)
        if not member: lines.append(f"[{idx}] user nije nađen"); continue
        ok  = [r for r in roles if can_touch_role(bot_member, r)]
        try:
            added = await safe_add_roles(member, ok, reason=f"batch by {interaction.user}")
            lines.append(f"[{idx}] {member.display_name} dodato: {', '.join(r.name for r in added) or 'ništa'}")
        except Exception as e:
            lines.append(f"[{idx}] {member.display_name} FAIL: {e}")
        if idx % PROGRESS_EVERY_N == 0:
            await interaction.followup.send(f"napredak: {idx}/{len(batches)} gotovih…", ephemeral=True)

    msg = "rezime:\n" + "\n".join(lines)
    for i in range(0, len(msg), 1800):
        await interaction.followup.send(f"```\n{msg[i:i+1800]}\n```", ephemeral=True)

@tree.command(name="cleanmulti", description="clean više usera; zadrži navedene role (keep)", guild=GUILD_OBJ)
@need_manage_roles()
async def clean_multi(interaction: discord.Interaction, users: str, keep: str = ""):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild; bot_member = guild.me
    user_ids   = parse_user_ids(users)
    keep_roles = parse_roles_from_text(guild, keep or "")
    keep_ids   = {r.id for r in keep_roles}
    if not user_ids:
        return await interaction.followup.send("nisi tagovao korisnike", ephemeral=True)

    lines = []
    for idx, uid in enumerate(user_ids, start=1):
        member = await ensure_member(guild, uid)
        if not member: lines.append(f"[{idx}] user nije nađen"); continue
        removable = [r for r in member.roles if can_touch_role(bot_member, r) and r.id not in keep_ids]
        blocked   = [r for r in member.roles if (r.id in keep_ids) or (not can_touch_role(bot_member, r) and not r.is_default())]
        try:
            removed = await safe_remove_roles(member, removable, reason=f"cleanmulti by {interaction.user}")
            ok_names = ", ".join(r.name for r in removed) if removed else "ništa"
            if blocked:
                why = "; ".join(
                    f"{r.name} [{' / '.join(['KEEP'] if r.id in keep_ids else why_blocked(bot_member, r))}]"
                    for r in blocked if r
                )
                lines.append(f"[{idx}] {member.display_name} obrisano: {ok_names}   preskočeno: {why}")
            else:
                lines.append(f"[{idx}] {member.display_name} obrisano: {ok_names}")
        except Exception as e:
            lines.append(f"[{idx}] {member.display_name} FAIL: {e}")
        if idx % PROGRESS_EVERY_N == 0:
            await interaction.followup.send(f"napredak: {idx}/{len(user_ids)} gotovih…", ephemeral=True)

    msg = "rezime /cleanmulti:\n" + "\n".join(lines)
    for i in range(0, len(msg), 1800):
        await interaction.followup.send(f"```\n{msg[i:i+1800]}\n```", ephemeral=True)


# ---------- /farm (modal forma) ----------
class FarmModal(Modal, title="Farm unos"):
    def __init__(self, opener: discord.Member):
        super().__init__(timeout=None)
        self.opener = opener
        self.amount = TextInput(label="Iznos", placeholder="npr. 25 ili $25", required=True, max_length=32)
        self.model_name = TextInput(label="Ime modela", placeholder="npr. cami / haley / ...", required=True, max_length=100)
        self.fan_username = TextInput(label="Username fana", placeholder="npr. @fan123 ili fan#0001", required=True, max_length=100)
        self.more_details = TextInput(label="Više detalja", style=TextStyle.paragraph, placeholder="optionalno: linkovi, napomena…", required=False, max_length=1000)
        self.add_item(self.amount); self.add_item(self.model_name); self.add_item(self.fan_username); self.add_item(self.more_details)

    async def on_submit(self, interaction: discord.Interaction):
        lines = [
            f"**Novi farm unos** (by {self.opener.mention}):",
            f"- Iznos: `{self.amount.value.strip()}`",
            f"- Model: `{self.model_name.value.strip()}`",
            f"- Fan: `{self.fan_username.value.strip()}`",
        ]
        extra = self.more_details.value.strip() if self.more_details.value else ""
        if extra: lines.append(f"- Detalji: {extra}")
        lines.append("")
        lines.append("**Pitanje:** da li je fan dodat na odgovarajuće liste i da li su ažurirane beleške o istom?")
        await interaction.response.send_message("\n".join(lines))
        msg = await interaction.original_response()
        try:
            await msg.add_reaction("✅"); await msg.add_reaction("🚫")
        except: pass

@tree.command(name="farm", description="Otvori formu za farm unos", guild=GUILD_OBJ)
async def farm(interaction: discord.Interaction):
    await interaction.response.send_modal(FarmModal(opener=interaction.user))


# ---------- /schedule — CLEAN-THEN-ASSIGN + unknown models report ----------
@tree.command(
    name="schedule",
    description="Nalepi raspored (podržava @u1 / @u2), auto: očisti TEAM role pa dodeli nove; apply=false=preview",
    guild=GUILD_OBJ
)
@need_manage_roles()
async def schedule(interaction: discord.Interaction, text: str, apply: bool = False):
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    bot_member = guild.me

    # normalizacija slash / i razbijanje po blokovima @user...
    text_norm = (text or "").replace("⁄", "/").replace("／", "/")
    raw_blocks = []
    pattern = re.compile(r"(@\S+|\<@!?[\d]+\>)(.*?)(?=(?:@\S+|\<@!?[\d]+\>)|$)", re.S)
    for m in pattern.finditer(text_norm):
        head_user = m.group(1).strip()
        tail      = (m.group(2) or "").strip()
        raw_blocks.append((head_user, tail))
    if not raw_blocks:
        return await interaction.followup.send("nisam našao blokove '@user' → role…", ephemeral=True)

    # pomocne funkcije
    def parse_roles_list_with_unknowns(guild: discord.Guild, roles_text: str):
        txt = (roles_text or "").replace("\\", "/")
        segs = [s.strip() for s in re.split(r"[\/,;|]+", txt) if s.strip()]
        wanted, unknown, seen = [], [], set()
        for seg in segs:
            base = clean_role_phrase(seg)
            if not base:
                continue
            r = role_from_phrase(guild, base)
            if r:
                if r.id not in seen:
                    wanted.append(r); seen.add(r.id)
            else:
                unknown.append(base)
        return wanted, unknown

    def split_assignees_and_roles(first_user: str, tail: str):
        roles_text = tail
        header_left = ""
        # format "@u1 / @u2 : roles..." ili "@u1 / @u2 roles..."
        if ":" in tail:
            header_left, roles_text = tail.split(":", 1)
        else:
            m2 = re.match(r"^\s*((?:[@<].*?>|\@\S+)(?:\s*[/,;|]\s*(?:[@<].*?>|\@\S+))*)\s+(.*)$", tail)
            if m2:
                header_left = m2.group(1)
                roles_text  = m2.group(2)
        assignees = [first_user]
        if header_left:
            assignees += re.findall(r"(@\S+|\<@!?[\d]+\>)", header_left)
        return assignees, roles_text.strip()

    report = []
    total_ops_add = 0
    total_ops_rm  = 0
    global_unknown = []

    blocks = [split_assignees_and_roles(u, t) for (u, t) in raw_blocks]

    for idx, (assignees, roles_text) in enumerate(blocks, start=1):
        desired_roles, unknown_here = parse_roles_list_with_unknowns(guild, roles_text)
        if unknown_here:
            global_unknown.extend(unknown_here)

        for a_idx, user_token in enumerate(assignees, start=1):
            tag = f"{idx}.{a_idx}"
            member = member_from_token(guild, user_token)
            if not member:
                report.append(f"[{tag}] ❌ user nije nađen: {user_token}")
                continue

            # CLEAN: skini samo TEAM * role (ne diraj KEEP_ROLE_NAMES)
            bot_touchable_model_roles = [
                r for r in member.roles
                if r.name.upper().startswith("TEAM ") and (r.name.upper() not in KEEP_ROLE_NAMES) and can_touch_role(bot_member, r)
            ]
            blocked_models = [
                r for r in member.roles
                if r.name.upper().startswith("TEAM ") and (r.name.upper() not in KEEP_ROLE_NAMES) and r not in bot_touchable_model_roles
            ]

            # ASSIGN: samo sto bot sme
            touchable_assign = [r for r in desired_roles if can_touch_role(bot_member, r)]
            blocked_assign   = [r for r in desired_roles if r not in touchable_assign]

            if not apply:
                msg = (f"[{tag}] PREVIEW {member.display_name}: "
                       f"clean → {', '.join(r.name for r in bot_touchable_model_roles) or '—'}"
                       f"{' | blocked-clean: ' + ', '.join(r.name for r in blocked_models) if blocked_models else ''} ; "
                       f"assign → {', '.join(r.name for r in touchable_assign) or '—'}"
                       f"{' | blocked-assign: ' + ', '.join(r.name for r in blocked_assign) if blocked_assign else ''}")
                if unknown_here:
                    msg += f" | unknown: {', '.join(unknown_here)}"
                report.append(msg)
                continue

            # APPLY
            try:
                if bot_touchable_model_roles:
                    removed = await safe_remove_roles(member, bot_touchable_model_roles, reason=f"schedule auto-clean by {interaction.user}")
                    total_ops_rm += len(removed)
                if touchable_assign:
                    added = await safe_add_roles(member, touchable_assign, reason=f"schedule assign by {interaction.user}")
                    total_ops_add += len(added)

                msg = (f"[{tag}] ✅ {member.display_name} "
                       f"(clean {len(bot_touchable_model_roles)} / assign {len(touchable_assign)})")
                if blocked_models:
                    msg += f" | blocked-clean: {', '.join(r.name for r in blocked_models)}"
                if blocked_assign:
                    msg += f" | blocked-assign: {', '.join(r.name for r in blocked_assign)}"
                if unknown_here:
                    msg += f" | unknown: {', '.join(unknown_here)}"
                report.append(msg)
            except discord.Forbidden:
                report.append(f"[{tag}] ❌ {member.display_name} – nemam Manage Roles/poziciju.")
            except Exception as e:
                report.append(f"[{tag}] ❌ {member.display_name} – fail: {e}")

        if idx % PROGRESS_EVERY_N == 0:
            await interaction.followup.send(f"schedule napredak: {idx}/{len(blocks)}…", ephemeral=True)

    header = ("SCHEDULE PREVIEW (auto CLEAN model roles → ASSIGN)\n"
              if not apply else
              f"SCHEDULE APPLY done (removed={total_ops_rm}, added={total_ops_add})\n")
    out = header + "\n".join(report)

    if global_unknown:
        dedup = sorted({u for u in global_unknown})
        out += "\n\nUNKNOWN MODELS (no matching role found):\n- " + "\n- ".join(dedup)

    for i in range(0, len(out), 1800):
        await interaction.followup.send(f"```\n{out[i:i+1800]}\n```", ephemeral=True)


# ---------- /resync ----------
@tree.command(name="resync", description="force purge global + resync guild", guild=GUILD_OBJ)
@need_manage_roles()
async def resync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        tree.clear_commands(guild=None); await tree.sync()  # global = 0
        if GUILD_OBJ is None:
            return await interaction.followup.send("Nema GUILD_ID u .env — ne mogu guild resync.", ephemeral=True)
        tree.clear_commands(guild=GUILD_OBJ)
        cmds = await tree.sync(guild=GUILD_OBJ)
        names = ", ".join(sorted(c.name for c in cmds))
        await interaction.followup.send(f"Resync OK. Global: 0. Guild: {len(cmds)} → {names}", ephemeral=True)
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

        mentions = " ".join(f"<@{uid}>" for uid in [886983698321391667, 1301678435776598107])
        await message.channel.send(f"{mentions} {message.author.mention} je upravo poslao !mm.")

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
    except Exception as e:
        print("sync fail:", e)


# ---------- RUN ----------
bot.run(TOKEN)
