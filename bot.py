import asyncio
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, jsonify, request

# ==================== CONSTANTS ====================
TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")

TIERS = ["HT1", "LT1", "HT2", "LT2", "HT3", "LT3", "HT4", "LT4", "HT5", "LT5"]
GAMEMODES = [
    "Vanilla",
    "UHC",
    "Pot",
    "Sword",
    "Axe",
    "Mace",
    "Rocket Mace",
    "Spear Elytra",
    "Spear Mace",
    "SMP",
    "Diamond SMP",
    "NethOP",
]
REGIONS = ["NA", "EU", "AS", "SA", "OCE"]

TIER_POINTS = {
    "HT1": 100,
    "LT1": 80,
    "HT2": 65,
    "LT2": 50,
    "HT3": 40,
    "LT3": 30,
    "HT4": 20,
    "LT4": 15,
    "HT5": 10,
    "LT5": 5,
}

# ==================== DATABASE SETUP ====================
DB_NAME = "mctiers.db"


def init_db():
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            discord_id INTEGER PRIMARY KEY,
            ign TEXT NOT NULL,
            region TEXT NOT NULL,
            verified INTEGER DEFAULT 1
        )
    """)
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS queue (
            discord_id INTEGER PRIMARY KEY,
            joined_at REAL NOT NULL,
            priority INTEGER DEFAULT 0
        )
    """)
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS cooldowns (
            discord_id INTEGER PRIMARY KEY,
            expires_at REAL NOT NULL
        )
    """)
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS testers (
            discord_id INTEGER PRIMARY KEY
        )
    """)
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            discord_id INTEGER PRIMARY KEY
        )
    """)
  conn.commit()
  conn.close()


init_db()


def get_config(key, default=None):
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
  row = cursor.fetchone()
  conn.close()
  return row[0] if row else default


def set_config(key, value):
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value))
  )
  conn.commit()
  conn.close()


# ==================== FLASK REST API ====================
app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
  response.headers["Access-Control-Allow-Origin"] = "*"
  response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
  response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
  return response


@app.route("/api/leaderboard", methods=["GET"])
def api_leaderboard():
  guild_id_str = get_config("guild_id")
  if not guild_id_str:
    return jsonify({"error": "Guild not configured in bot."}), 400

  guild = bot.get_guild(int(guild_id_str))
  if not guild:
    return jsonify({"error": "Guild not found by bot cache."}), 404

  leaderboard_data = []

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()

  for member in guild.members:
    if member.bot:
      continue
    cursor.execute(
        "SELECT ign, region FROM users WHERE discord_id = ?", (member.id,)
    )
    user_row = cursor.fetchone()
    ign = user_row[0] if user_row else member.name
    region = user_row[1] if user_row else "UNKNOWN"

    total_points = 0
    earned_tiers = {}

    for role in member.roles:
      if "-" in role.name:
        parts = role.name.split("-", 1)
        gm, tier = parts[0].strip(), parts[1].strip()
        if gm in GAMEMODES and tier in TIERS:
          earned_tiers[gm] = tier
          total_points += TIER_POINTS.get(tier, 0)

    if earned_tiers:
      leaderboard_data.append({
          "discord_id": str(member.id),
          "ign": ign,
          "region": region,
          "avatar": member.display_avatar.url,
          "points": total_points,
          "tiers": earned_tiers,
      })

  conn.close()
  leaderboard_data.sort(key=lambda x: x["points"], reverse=True)
  return jsonify(leaderboard_data)


def run_flask():
  app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)


# ==================== DISCORD BOT SETUP ====================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True


class MCTiersBot(commands.Bot):

  def __init__(self):
    super().__init__(command_prefix="!", intents=intents)

  async def setup_hook(self):
    self.add_view(VerificationView())
    self.add_view(QueueView())
    self.add_view(HTApplicationView())
    await self.tree.sync()
    print(f"Synced slash commands for {self.user}.")


bot = MCTiersBot()


# ==================== PERMISSIONS & HELPERS ====================
def is_staff_or_tester(interaction: discord.Interaction) -> bool:
  if interaction.user.guild_permissions.administrator:
    return True
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT 1 FROM staff WHERE discord_id = ?", (interaction.user.id,)
  )
  is_staff_db = cursor.fetchone()

  custom_staff_role = get_config("staff_role_id")
  has_role = (
      custom_staff_role
      and any(r.id == int(custom_staff_role) for r in interaction.user.roles)
  )

  cursor.execute(
      "SELECT 1 FROM testers WHERE discord_id = ?", (interaction.user.id,)
  )
  is_tester = cursor.fetchone()
  conn.close()

  return bool(is_staff_db or has_role or is_tester)


def is_tester_only(interaction: discord.Interaction) -> bool:
  if interaction.user.guild_permissions.administrator:
    return True
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT 1 FROM testers WHERE discord_id = ?", (interaction.user.id,)
  )
  is_tester = cursor.fetchone()
  conn.close()
  return bool(is_tester or is_staff_or_tester(interaction))


# ==================== MODALS & VIEWS ====================
class VerificationModal(discord.ui.Modal, title="Minecraft PvP Verification"):
  ign = discord.ui.TextInput(
      label="Minecraft IGN",
      placeholder="Enter your exact in-game username",
      required=True,
      max_length=16,
  )
  region = discord.ui.Select(
      placeholder="Select Your Region",
      options=[discord.SelectOption(label=r, value=r) for r in REGIONS],
  )

  def __init__(self):
    super().__init__()
    self.region_select = self.region

  async def on_submit(self, interaction: discord.Interaction):
    ign_val = self.ign.value.strip()
    region_val = self.region.values[0] if self.region.values else "NA"

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO users (discord_id, ign, region, verified) VALUES"
        " (?, ?, ?, 1)",
        (interaction.user.id, ign_val, region_val),
    )
    conn.commit()
    conn.close()

    guild = interaction.guild
    verified_role = discord.utils.get(guild.roles, name="Verified")
    try:
      if verified_role:
        await interaction.user.add_roles(verified_role)
      new_nick = f"{ign_val} [{region_val}]"
      if len(new_nick) <= 32:
        await interaction.user.edit(nick=new_nick)
    except Exception as e:
      print(f"Failed to update member profile: {e}")

    await interaction.response.send_message(
        f"✅ Successfully verified as **{ign_val}** (`{region_val}`)!",
        ephemeral=True,
    )


class VerificationView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="Verify IGN & Region",
      style=discord.ButtonStyle.green,
      custom_id="persistent_verify_btn",
  )
  async def verify_button(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    await interaction.response.send_modal(VerificationModal())


class QueueView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="Join Queue",
      style=discord.ButtonStyle.blurple,
      custom_id="persistent_queue_join",
  )
  async def join_queue(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT verified FROM users WHERE discord_id = ?", (interaction.user.id,)
    )
    user = cursor.fetchone()
    if not user or not user[0]:
      conn.close()
      await interaction.response.send_message(
          "❌ You must verify your Minecraft IGN first using the verification"
          " panel!",
          ephemeral=True,
      )
      return

    cursor.execute(
        "SELECT expires_at FROM cooldowns WHERE discord_id = ?",
        (interaction.user.id,),
    )
    cd = cursor.fetchone()
    if cd and time.time() < cd[0]:
      remaining = int(cd[0] - time.time())
      hrs, rem = divmod(remaining, 3600)
      mins, _ = divmod(rem, 60)
      conn.close()
      await interaction.response.send_message(
          f"⏳ You are on test cooldown for another {hrs}h {mins}m.",
          ephemeral=True,
      )
      return

    cursor.execute(
        "SELECT 1 FROM queue WHERE discord_id = ?", (interaction.user.id,)
    )
    in_queue = cursor.fetchone()
    if in_queue:
      conn.close()
      await interaction.response.send_message(
          "⚠️ You are already in the testing queue!", ephemeral=True
      )
      return

    is_priority = (
        1
        if any(
            r.name.lower() in ["booster", "priority", "donor"]
            for r in interaction.user.roles
        )
        else 0
    )

    cursor.execute(
        "INSERT INTO queue (discord_id, joined_at, priority) VALUES (?, ?, ?)",
        (interaction.user.id, time.time(), is_priority),
    )
    conn.commit()
    conn.close()

    badge = "⚡ " if is_priority else ""
    await interaction.response.send_message(
        f"✅ Successfully joined the queue! {badge}", ephemeral=True
    )

  @discord.ui.button(
      label="Leave Queue",
      style=discord.ButtonStyle.red,
      custom_id="persistent_queue_leave",
  )
  async def leave_queue(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM queue WHERE discord_id = ?", (interaction.user.id,)
    )
    rowcount = cursor.rowcount
    conn.commit()
    conn.close()

    if rowcount > 0:
      await interaction.response.send_message(
          "📤 You have left the testing queue.", ephemeral=True
      )
    else:
      await interaction.response.send_message(
          "⚠️ You weren't in the queue.", ephemeral=True
      )


class HTApplicationModal(discord.ui.Modal, title="High Tier Application"):
  gamemode = discord.ui.TextInput(
      label="Gamemode & Target Tier",
      placeholder="e.g., Mace HT2, Vanilla HT1",
      required=True,
  )
  clips = discord.ui.TextInput(
      label="Montage / Unlisted Video Links",
      placeholder="Provide YouTube or Streamable links",
      style=discord.TextStyle.paragraph,
      required=True,
  )

  async def on_submit(self, interaction: discord.Interaction):
    guild = interaction.guild
    ht_cat_id = get_config("ht_category_id")
    category = (
        guild.get_channel(int(ht_cat_id)) if ht_cat_id else guild.category_channels[0]
    )

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(
            read_messages=True, send_messages=True
        ),
        guild.me: discord.PermissionOverwrite(read_messages=True),
    }

    ticket_channel = await guild.create_text_channel(
        name=f"ht-app-{interaction.user.name}",
        category=category,
        overwrites=overwrites,
    )

    embed = discord.Embed(
        title="🔥 High Tier Application Received",
        color=discord.Color.gold(),
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="Applicant", value=interaction.user.mention, inline=False)
    embed.add_field(
        name="Target Gamemode/Tier", value=self.gamemode.value, inline=False
    )
    embed.add_field(name="Clips / Proof", value=self.clips.value, inline=False)

    await ticket_channel.send(
        content=f"{interaction.user.mention} | Staff will review your clips"
        " shortly.",
        embed=embed,
        view=CloseTicketView(),
    )
    await interaction.response.send_message(
        f"✅ Application submitted! Created ticket: {ticket_channel.mention}",
        ephemeral=True,
    )


class HTApplicationView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="Apply for High Tier",
      style=discord.ButtonStyle.blurple,
      custom_id="persistent_ht_apply",
  )
  async def apply_button(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    await interaction.response.send_modal(HTApplicationModal())


class CloseTicketView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="Close Ticket",
      style=discord.ButtonStyle.danger,
      custom_id="persistent_close_ticket",
  )
  async def close_ticket(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if not isinstance(interaction.channel, discord.TextChannel):
      return
    await interaction.response.send_message(
        "🔒 Closing ticket in 5 seconds..."
    )
    await asyncio.sleep(5)
    await interaction.channel.delete()


# ==================== DISCORD SLASH COMMANDS ====================
@bot.tree.command(
    name="setup_panel", description="Sends the verification portal panel."
)
@app_commands.checks.has_permissions(administrator=True)
async def setup_panel(interaction: discord.Interaction):
  embed = discord.Embed(
      title="🛡️ Minecraft PvP Verification Portal",
      description=(
          "Click the button below to verify your Minecraft IGN and Region.\nThis"
          " unlocks queues and tier testing channels."
      ),
      color=discord.Color.blue(),
  )
  await interaction.channel.send(embed=embed, view=VerificationView())
  await interaction.response.send_message(
      "✅ Verification panel deployed!", ephemeral=True
  )


@bot.tree.command(
    name="setup_ht", description="Sends the High Tier application panel."
)
@app_commands.checks.has_permissions(administrator=True)
async def setup_ht(interaction: discord.Interaction):
  embed = discord.Embed(
      title="⚡ High Tier Application Portal",
      description=(
          "Think you deserve HT1-HT3? Click below to submit your application"
          " and gameplay clips."
      ),
      color=discord.Color.gold(),
  )
  await interaction.channel.send(embed=embed, view=HTApplicationView())
  await interaction.response.send_message(
      "✅ High Tier application panel deployed!", ephemeral=True
  )


@bot.tree.command(
    name="set_queue_channel", description="Sets the active queue channel."
)
@app_commands.checks.has_permissions(administrator=True)
async def set_queue_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
):
  set_config("queue_channel_id", channel.id)
  set_config("guild_id", interaction.guild.id)
  await interaction.response.send_message(
      f"✅ Queue channel set to {channel.mention}", ephemeral=True
  )


@bot.tree.command(
    name="queue_toggle", description="Toggles queue open or closed."
)
@app_commands.checks.has_permissions(administrator=True)
async def queue_toggle(interaction: discord.Interaction, status: bool):
  set_config("queue_open", "1" if status else "0")
  state = "OPENED 🟢" if status else "CLOSED 🔴"
  await interaction.response.send_message(
      f"✅ Testing queue status: {state}", ephemeral=True
  )


@bot.tree.command(
    name="queue_list", description="Displays the current testing queue."
)
async def queue_list(interaction: discord.Interaction):
  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT discord_id, priority FROM queue ORDER BY priority DESC,"
      " joined_at ASC"
  )
  queue_rows = cursor.fetchall()
  conn.close()

  if not queue_rows:
    await interaction.response.send_message(
        "📭 The testing queue is currently empty.", ephemeral=True
    )
    return

  embed = discord.Embed(
      title="📋 Current Testing Queue", color=discord.Color.blurple()
  )
  desc = []
  for idx, (uid, priority) in enumerate(queue_rows, 1):
    member = interaction.guild.get_member(uid)
    name = member.mention if member else f"<@!{uid}>"
    badge = "⚡ " if priority else ""
    desc.append(f"`#{idx}` {badge}{name}")

  embed.description = "\n".join(desc)
  await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="next_test",
    description="Calls the next player in queue and opens a test ticket.",
)
async def next_test(interaction: discord.Interaction):
  if not is_tester_only(interaction):
    await interaction.response.send_message(
        "❌ You do not have permission to run tests.", ephemeral=True
    )
    return

  conn = sqlite3.connect(DB_NAME)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT discord_id, priority FROM queue ORDER BY priority DESC,"
      " joined_at ASC LIMIT 1"
  )
  row = cursor.fetchone()

  if not row:
    conn.close()
    await interaction.response.send_message(
import os
import threading
import discord
from discord import app_commands
from flask import Flask
import sqlite3

# --- Flask Web Server Setup for Render ---
app = Flask(__name__)

@app.route("/")
def home():
    return "Minecraft PvP Tier Bot is Live!"

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")

bot = MyClient()

# --- Configuration & Constants ---
GAMEMODES = ["Mace", "Sword", "SMP", "UHC", "NethOP"]
TIERS = ["HT1", "LT1", "HT2", "LT2", "HT3", "LT3"]

def is_tester_only(interaction: discord.Interaction):
    # আপনার টেস্টার বা রোল চেকিং লজিক এখানে কাজ করবে
    return True

# --- Commands ---
@bot.tree.command(
    name="result",
    description="Assigns test results, tier role, and sets cooldown.",
)
@app_commands.describe(
    member="The player being tested",
    gamemode="Choose gamemode",
    tier="Choose tier result",
)
@app_commands.choices(
    gamemode=[app_commands.Choice(name=gm, value=gm) for gm in GAMEMODES],
    tier=[app_commands.Choice(name=t, value=t) for t in TIERS],
)
async def result(
    interaction: discord.Interaction,
    member: discord.Member,
    gamemode: str,
    tier: str,
):
    if not is_tester_only(interaction):
        await interaction.response.send_message(
            "❌ You do not have permission to log test results.", ephemeral=True
        )
        return

    role_name = f"{gamemode}-{tier}"
    role = discord.utils.get(interaction.guild.roles, name=role_name)
    
    await interaction.response.send_message(
        f"✅ Test result logged for {member.mention}. Role: {role_name}", ephemeral=True
    )

# --- Main Entry Point ---
if __name__ == "__main__":
    # ব্যাকগ্রাউন্ডে Flask সার্ভার চালু করা (Render পোর্ট হ্যান্ডেল করার জন্য)
    threading.Thread(target=run_web, daemon=True).start()

    # ডিসকর্ড টোকেন রিড করা
    token = (
        os.environ.get("DISCORD_TOKEN")
        or os.environ.get("TOKEN")
        or os.environ.get("BOT_TOKEN")
    )
    
    if not token:
        print("ERROR: Render Environment-এ বটের Token পাওয়া যায়নি!")
    else:
        bot.run(token)
