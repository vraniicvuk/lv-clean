# main.py — FINAL (ispravljena verzija)
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

# --- env first ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
USE_AI_FU = os.getenv("USE_AI_FU", "false").lower() in ("1","true","yes","on")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

client = OpenAI(api_key=OPENAI_API_KEY) if (USE_AI_FU and OPENAI_API_KEY) else None

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN nije setovan u .env")

STOPWORDS = {"vip","free","paid","oll","ock","ra","inb","eep","tsu","jsn","vf","ggn","bcl","hnq","bk","sa","tvn","yll","oftv","kct","trans","sexy","zzz","x","c","g"}

# ---------- TUNABLES ----------
SLEEP_BETWEEN_CALLS = 0.35
CHUNK_SIZE = 24
RETRIES = 5
RETRY_BASE_SLEEP = 0.8
PROGRESS_EVERY_N = 5

KEEP_ROLE_NAMES = {"AFTERNOON", "GRAVEYARD", "MAIN", "OBUKA", "LV CHATTER"}

role_index = {}

# ---------- BOT ----------
INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True
bot = commands.Bot(command_prefix="!", intents=INTENTS)
tree = bot.tree
GUILD_OBJ = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

MM_APPROVAL_NAME_SNIPPET = "mm-approval"
MM_SUMMARY_CHANNEL_ID = 1433577356437491774

AI_BLOCKED_UNTIL = None

# Kanal u koji se šalje raspored
SCHEDULE_CHANNEL = {
    "grave": 1364850505234518067,
    "after": 1364850574205648967,
    "main": 1364850795215982634
}

GRAVE_GENERAL_CHANNEL_ID = 1364850505234518067
AFTER_GENERAL_CHANNEL_ID = 1364850574205648967
MAIN_GENERAL_CHANNEL_ID = 1364850795215982634

GRAVE_ROLE_ID = 1410962300554313870
AFTER_ROLE_ID = 1410962344124612710
MAIN_ROLE_ID = 1410962407454675047

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
            await interaction.response.send_message("Nemaš Manage Channels permisiju.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

# ==================== AUTO SCHEDULE ====================
@tasks.loop(minutes=1)
async def auto_schedule_task():
    now = _local_now()
    h, m = now.hour, now.minute
    triggers = {
        "grave": (9, 45),
        "after": (17, 45),
        "main": (1, 45),
    }
    for shift, (th, tm) in triggers.items():
        if h == th and m == tm:
            await run_auto_schedule(shift)
            break


async def run_auto_schedule(shift: str):
    guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
    if not guild:
        return
    channel = bot.get_channel(SCHEDULE_CHANNEL.get(shift))
    if not channel:
        return

    try:
        messages = [msg async for msg in channel.history(limit=50)]

        schedule_msg = None
        for msg in messages:
            content = msg.content
            if ("@" in content and any(x in content for x in [":", "/", ","])):
                age = (_local_now() - msg.created_at.replace(tzinfo=ZoneInfo("Europe/Belgrade"))).total_seconds()
                if age < 86400:
                    schedule_msg = msg
                    break

        if not schedule_msg:
            await channel.send(f"⚠️ Auto Schedule za **{shift.upper()}**: Nije pronađen validan raspored.")
            return

        schedule_text = schedule_msg.content.strip()

        print(f"[AUTO SCHEDULE] Pronađen raspored za {shift} → primenjujem...")
        await apply_schedule_logic(guild, schedule_text)

        role_id = {"grave": GRAVE_ROLE_ID, "after": AFTER_ROLE_ID, "main": MAIN_ROLE_ID}[shift]
        await channel.send(
            f"<@&{role_id}> **Role za modele koje imate na rasporedu su vam dodeljene.**\n\n"
            "Ukoliko vam fali role za nekog modela, molim vas da se obratite direktno nekome iz tima.\n\n"
            "Nakon provere rola, **clock inujte se** na Telegram kanalu vaše smene u formatu:\n"
            "`ci model1/model2/model3/...`"
        )

        print(f"[AUTO SCHEDULE] Uspešno završeno za {shift.upper()}")

    except Exception as e:
        print(f"[AUTO SCHEDULE] Greška za {shift}: {e}")
        await channel.send(f"❌ Greška u auto schedule za {shift.upper()}: {e}")


async def apply_schedule_logic(guild, text: str):
    """Identčna logika kao /schedule apply=true"""
    bot_member = guild.me
    global role_index

    role_index = {}
    for r in guild.roles:
        if r.name.lower().startswith("team "):
            base = normalize_model_name(r.name[5:])
            role_index.setdefault(base, []).append(r)

    text_norm = (text or "").replace("⁄", "/").replace("／", "/")
    raw_blocks = []
    pattern = re.compile(r"(@\S+|\<@!?[\d]+\>)(.*?)(?=(?:@\S+|\<@!?[\d]+\>)|$)", re.S)
    for m in pattern.finditer(text_norm):
        raw_blocks.append((m.group(1).strip(), (m.group(2) or "").strip()))

    def parse_roles_list_with_unknowns(roles_text: str):
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
                unknown.append(base)
        return wanted, unknown

    def split_assignees_and_roles(first_user: str, tail: str):
        roles_text = tail.strip()
        header = ""
        if ":" in roles_text:
            header, roles_text = roles_text.split(":", 1)
        else:
            m = re.match(r"^(\s*(?:@[\.\w]+|\<@!?[\d]+\>)(?:\s*[\/,|]\s*(?:@[\.\w]+|\<@!?[\d]+\>))*)", roles_text)
            if m:
                header = m.group(1).strip()
                roles_text = roles_text[len(header):].strip()

        assignees = re.findall(r"@[\.\w]+|\<@!?[\d]+\>", header or first_user)
        if not assignees:
            assignees = [first_user]
        return assignees, roles_text.strip()

    for _, (assignees_raw, roles_text) in enumerate(raw_blocks, start=1):
        desired_roles, _ = parse_roles_list_with_unknowns(roles_text)
        assignees, _ = split_assignees_and_roles("", assignees_raw)

        for user_token in assignees:
            member = member_from_token(guild, user_token)
            if not member:
                continue

            # CLEAN
            bot_touchable_model_roles = [
                r for r in member.roles
                if r.name.upper().startswith("TEAM ") and r.name.upper() not in KEEP_ROLE_NAMES and can_touch_role(bot_member, r)
            ]
            if bot_touchable_model_roles:
                await safe_remove_roles(member, bot_touchable_model_roles, reason="auto schedule")

            # ASSIGN
            touchable_assign = [r for r in desired_roles if can_touch_role(bot_member, r)]
            if touchable_assign:
                await safe_add_roles(member, touchable_assign, reason="auto schedule")

    print("[AUTO APPLY] Raspored primenjen")


# ====== AI/FU HELPERI ======
# (ostaje isto kao što si imao - nisam menjao ovaj deo)
# ... (tvoj AI deo ostaje nepromenjen)

# ---------- ROLE LOOKUP i ostale funkcije ----------
# (ostaje isto)

# ========== /newm ==========
@tree.command(name="newm", description="Napravi novi model", guild=GUILD_OBJ)
@need_manage_roles()
@need_manage_channels()
async def new_model(interaction: discord.Interaction, ime: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    role_name = f"TEAM {ime.strip().upper()}"
    category_name = f"TEAM {ime.strip().upper()}"

    try:
        new_role = await guild.create_role(
            name=role_name,
            colour=discord.Colour(0x2b2d31),
            mentionable=True,
            reason=f"/newm by {interaction.user}"
        )

        await asyncio.sleep(2)
        await sort_team_roles(guild)

        new_category = await guild.create_category(name=category_name)

        general = await new_category.create_text_channel("general")
        whales = await new_category.create_text_channel("whales")

        await asyncio.sleep(3)
        await new_category.set_permissions(guild.default_role, view_channel=False)
        await new_category.set_permissions(new_role, view_channel=True)

        await general.send("Dobrodošli u novi model kanal.")

        embed = discord.Embed(title="✅ Model kreiran", color=0x00ff00)
        embed.add_field(name="Rola", value=role_name, inline=False)
        embed.add_field(name="Kategorija", value=category_name, inline=False)
        embed.add_field(name="Kanali", value=f"{general.mention}\n{whales.mention}", inline=False)

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Greška: {e}", ephemeral=True)


# ========== TESTAUTO ==========
@tree.command(name="testauto", description="Ručno pokreće auto schedule za test", guild=GUILD_OBJ)
@need_manage_roles()
async def test_auto_schedule(interaction: discord.Interaction, shift: str):
    await interaction.response.defer(ephemeral=True)
   
    if shift not in ["grave", "after", "main"]:
        return await interaction.followup.send("❌ Dozvoljene vrednosti: grave, after, main", ephemeral=True)

    await interaction.followup.send(f"🔄 Pokrećem Auto Schedule test za **{shift.upper()}** smenu...", ephemeral=True)
    asyncio.create_task(run_auto_schedule(shift))
    await interaction.followup.send("✅ Test je pokrenut u pozadini. Proveri odgovarajući general kanal.", ephemeral=True)


# ---------- /resync ----------
@tree.command(name="resync", description="force guild sync instant", guild=GUILD_OBJ)
@need_manage_roles()
async def resync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if GUILD_OBJ is None:
            return await interaction.followup.send("GUILD_ID nije setovan.", ephemeral=True)
        tree.copy_global_to(guild=GUILD_OBJ)
        cmds = await tree.sync(guild=GUILD_OBJ)
        names = ", ".join(sorted(c.name for c in cmds))
        await interaction.followup.send(f"Guild sync OK. {len(cmds)} komandi → {names}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Resync FAIL: {e}", ephemeral=True)


# ---------- global error ----------
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    try:
        await interaction.response.send_message(f"greška: {error}", ephemeral=True)
    except:
        await interaction.followup.send(f"greška: {error}", ephemeral=True)


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
    except Exception as e:
        print("sync fail:", e)


# ---------- RUN ----------
bot.run(TOKEN)
