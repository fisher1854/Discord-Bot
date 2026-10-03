import discord
from discord import ui
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, View, Select
import datetime
import asyncio
import json
import os
import struct
from aiohttp import web
import aiohttp
from urllib.parse import quote

# 1. Setup Bot Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class IsleShopBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
    async def on_ready(self):
        print(f'Logged in as {self.user.name}')
        load_database()
        import primeval_boot
        primeval_boot.attach_all(self, globals())
        bot.add_view(StaffAppPanelView)
        # Command Sync Logic
        try:
            for g in self.guilds:
                await self.tree.sync(guild=g)
            synced = await self.tree.sync()
            print(f"[LIVE TALLY] Successfully synced {len(synced)} command(s)")
        except Exception as e:
            print(f"Failed to sync commands: {e}")

        # DIAGNOSTIC START: Run the webhook server safely and catch hidden crashes
        try:
            app = web.Application()
            app.router.add_post('/game-logs', handle_game_log)
            runner = web.AppRunner(app)
            await runner.setup()
            
            # Use 127.0.0.1 locally to guarantee Windows firewall doesn't block local tests
            site = web.TCPSite(runner, '127.0.0.1', 8094)
            await site.start()
            print("[LIVE TALLY] Webhook data interceptor port strictly listening on port 8094...")
        except Exception as e:
            print(f"[CRITICAL ERROR] Webhook server failed to bind to port 8094: {e}")

        # Start loop engines
        self.update_population_channels.start()

# --- AUTOMATED DISCORD LOGGING TASK LOOP ---
    # Changed to 10 minutes to safely respect Discord's strict channel-rename rate limits
@tasks.loop(minutes=2)
async def update_population_channels(self):
        await self.wait_until_ready()
        import re, time, json as _json
        from datetime import datetime, timezone
        global LIVE_HEADCOUNT, SPECIES_CAPS
        now = int(time.time())
        stamp = "Last updated <t:%s:f> (<t:%s:R>)" % (now, now)
        aliases = {"tyrannosaurus":"Tyrannosaurus","trex":"Tyrannosaurus","gallimimus":"Galli","galli":"Galli","allosaurus":"Allo","allo":"Allo","deinosuchus":"Deinosuchus","deino":"Deinosuchus","triceratops":"Triceratops","stegosaurus":"Stegosaurus","diabloceratops":"Diabloceratops","diablo":"Diabloceratops","austroraptor":"Austroraptor","austro":"Austroraptor","beipiaosaurus":"Beipiaosaurus","pteranodon":"Pteranodon","kentrosaurus":"Kentrosaurus","kentro":"Kentrosaurus","ceratosaurus":"Ceratosaurus","maiasaura":"Maiasaura","carnotaurus":"Carnotaurus","dilophosaurus":"Dilophosaurus","omniraptor":"Omniraptor","herrerasaurus":"Herrerasaurus","hypsilophodon":"Hypsilophodon","dryosaurus":"Dryosaurus","pachycephalosaurus":"Pachycephalosaurus","troodon":"Troodon","tenontosaurus":"Tenontosaurus"}
        def canon(raw):
            blob = str(raw or "")
            picked = ""
            cm = re.search(r"\bClass\s*:\s*([A-Za-z]+)", blob, re.I)
            if cm: picked = cm.group(1)
            if not picked:
                bm = re.search(r"BP_([A-Za-z]+)", blob)
                if bm: picked = bm.group(1)
            if not picked and len(blob) <= 40 and "PlayerData" not in blob:
                picked = blob.strip()
            key = re.sub(r"[^a-z]", "", picked.lower())
            if not key: return ""
            if key.startswith("bp"): key = key[2:]
            for suffix in ("character", "pawn"):
                if key.endswith(suffix) and len(key) > len(suffix) + 4: key = key[:-len(suffix)]
            if key in aliases: return aliases[key]
            for name in LIVE_HEADCOUNT:
                if re.sub(r"[^a-z]", "", str(name).lower()) == key: return str(name)
            if len(key) >= 4: return key[0].upper() + key[1:]
            return ""
        counts = {k: 0 for k in LIVE_HEADCOUNT}
        online = 0
        spawned = 0
        source = "RCON playerlist"
        try:
            from primeval_panels import _isle_file
            st, txt = await _isle_file("GET", "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/census.json")
            cen = None
            if st == 200 and txt and str(txt).strip().startswith("{"):
                try: cen = _json.loads(txt)
                except Exception: cen = None
            if not (cen and int(cen.get("capturedAt") or 0) >= now - 90):
                await _isle_file("POST", "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/cmd.flag", "census 0\n")
                await asyncio.sleep(2.2)
                st, txt = await _isle_file("GET", "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/census.json")
                if st == 200 and txt and str(txt).strip().startswith("{"):
                    try: cen = _json.loads(txt)
                    except Exception: cen = None
            if cen and int(cen.get("capturedAt") or 0) >= now - 120:
                source = "game census"
                rows = cen.get("players") or []
                online = int(cen.get("online") or len(rows) or 0)
                for row in rows:
                    if not row.get("spawned", True): continue
                    spawned += 1
                    mapped = canon(row.get("species") or "")
                    if mapped: counts[mapped] = counts.get(mapped, 0) + 1
        except Exception as e:
            print("[TALLY] census read failed:", e)
        if source != "game census":
            try:
                blob = await send_game_command("playerlist")
                steams = []
                for m in re.finditer(r"7656\d{13}", str(blob or "")):
                    if m.group(0) not in steams: steams.append(m.group(0))
                online = len(steams)
                for steam in steams[:20]:
                    mapped = ""
                    try:
                        data = await send_game_command("getplayerdata " + steam)
                        mapped = canon(data)
                    except Exception:
                        mapped = ""
                    if mapped:
                        spawned += 1
                        counts[mapped] = counts.get(mapped, 0) + 1
            except Exception as e:
                print("[TALLY] rcon failed:", e)
        for k, v in counts.items():
            LIVE_HEADCOUNT[k] = v
        total = sum(int(v or 0) for v in counts.values())
        counts = {k: int(counts.get(k, 0) or 0) for k in LIVE_HEADCOUNT}
        lines = []
        for name in sorted(counts, key=lambda n: (-int(counts[n] or 0), str(n))):
            n = int(counts.get(name, 0) or 0)
            mark = "*" if n else " "
            lines.append("`%s` **%s** `%s`" % (mark, name, n))
        body = "\n".join(lines) or "No tracked species yet."
        embed = discord.Embed(title="Primeval Island - Live tally", description="%s\nPlayers connected: **%s**\nSpawned dinos: **%s**\nTotal counted: **%s**" % (stamp, online, spawned, total), color=discord.Color.dark_green(), timestamp=datetime.fromtimestamp(now, tz=timezone.utc))
        embed.add_field(name="Species", value=body[:1020], inline=False)
        embed.set_footer(text="Source: %s - refreshes every 2 minutes" % source)
        lock_lines = []
        for dino, cap in SPECIES_CAPS.items():
            cap = int(cap or 0)
            n = int(LIVE_HEADCOUNT.get(dino, 0) or 0)
            tag = "LOCKED" if n >= cap else "OPEN"
            lock_lines.append("**%s** %s `%s/%s`" % (tag, dino, n, cap))
        embed_locks = discord.Embed(title="Primeval Island - Point store locks", description=stamp + "\nCapped species lock at capacity and reopen below cap.", color=discord.Color.orange(), timestamp=datetime.fromtimestamp(now, tz=timezone.utc))
        embed_locks.add_field(name="Store status", value=("\n".join(lock_lines) or "No capped species configured.")[:1020], inline=False)
        embed_locks.set_footer(text="Source: %s - refreshes every 2 minutes" % source)
        async def upsert(channel_id, emb, cache_key):
            ch = self.get_channel(channel_id)
            if ch is None:
                try: ch = await self.fetch_channel(channel_id)
                except Exception as e:
                    print("[TALLY] missing channel", channel_id, e)
                    return
            path = "primeval_tally.json"
            state = {}
            try:
                with open(path, "r", encoding="utf-8") as f: state = _json.load(f) or {}
            except Exception: state = {}
            mid = int(state.get(cache_key) or 0)
            msg = None
            if mid:
                try: msg = await ch.fetch_message(mid)
                except Exception: msg = None
            view = None
            if cache_key == "locks_message_id":
                try:
                    import primeval_qol
                    view = primeval_qol.locks_wait_view()
                except Exception:
                    view = None
            if msg is None:
                msg = await ch.send(embed=emb, view=view)
                state[cache_key] = msg.id
            else:
                await msg.edit(embed=emb, view=view)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    _json.dump(state, f, indent=2)
                    f.write("\n")
            except Exception as e:
                print("[TALLY] state save", e)
        try: await upsert(1540827821067477064, embed, "tally_message_id")
        except Exception as e: print("[TALLY] pop post", e)
        try: await upsert(1541014100971360326, embed_locks, "locks_message_id")
        except Exception as e: print("[TALLY] lock post", e)
        try:
            ch = self.get_channel(1540827821067477064)
            if ch is None: ch = await self.fetch_channel(1540827821067477064)
            name = ("Players Online: %s" % online)[:100]
            if ch and ch.name != name:
                last = getattr(self, "_tally_last_rename", 0)
                if now - last >= 600:
                    await ch.edit(name=name)
                    self._tally_last_rename = now
        except Exception as e:
            print("[TALLY] rename", e)
        try:
            import primeval_qol
            path = "primeval_tally.json"
            state = {}
            try:
                with open(path, "r", encoding="utf-8") as f: state = _json.load(f) or {}
            except Exception:
                state = {}
            state = await primeval_qol.on_census(self, counts, state) or state
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(state, f, indent=2)
                f.write("\n")
        except Exception as e:
            print("[QOL] census", e)
        print("[TALLY]", source, "online", online, "spawned", spawned, counts)

bot = IsleShopBot()

# --- LIVE SERVER CONFIGURATION DIRECTORY ---
RCON_IP = "38.133.189.10"        
RCON_PORT = 27562           
RCON_PASSWORD = "yXV38wwI"   
BOT_RECEIVER_PORT = 8094    

# --- CUSTOM ASSIGNED POPULATION CAPS ---
SPECIES_CAPS = {
    "Tyrannosaurus": 12,
    "Triceratops": 18,
    "Deinosuchus": 20,
    "Stegosaurus": 22,
    "Diabloceratops": 30,
    "Allo": 30
}

# --- MASTER HEADCOUNT DIRECTORY ---
LIVE_HEADCOUNT = {
    "Tyrannosaurus": 0, "Triceratops": 0, "Deinosuchus": 0, "Stegosaurus": 0, 
    "Diabloceratops": 0, "Allo": 0, "Herrerasaurus": 0, "Hypsilophodon": 0, 
    "Dryosaurus": 0, "Galli": 0, "Pachycephalosaurus": 0, "Troodon": 0, 
    "Tenontosaurus": 0, "Dilophosaurus": 0, "Omniraptor": 0, "Ceratosaurus": 0, 
    "Maiasaura": 0, "Carnotaurus": 0, "Austroraptor": 0, "Beipiaosaurus": 0, 
    "Pteranodon": 0, "Kentrosaurus": 0
}

# --- CORE FILE DATABASE MANAGER ---
TOKEN_FILE = "tokens.json"
STEAM_FILE = "steam_links.json"

PLAYER_TOKENS = {}          
STEAM_LINKS = {}            
USED_PERFECT_DIET = set()    
USED_MUTATION_ROLL = set()   
REFILL_COOLDOWNS = {}       
def load_database():
    global PLAYER_TOKENS, STEAM_LINKS
    print("[DB MANAGER] Loading database parameters...")
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                raw_data = json.load(f)
                PLAYER_TOKENS = {int(k): v for k, v in raw_data.items()}
                print(f" Loaded {len(PLAYER_TOKENS)} player token profiles.")
        except Exception as e:
            print(f"Failed to parse tokens file: {e}")
    if os.path.exists(STEAM_FILE):
        try:
            with open(STEAM_FILE, "r") as f:
                raw_data = json.load(f)
                STEAM_LINKS = {int(k): v for k, v in raw_data.items()}
                print(f" Loaded {len(STEAM_LINKS)} linked Steam profiles.")
        except Exception as e:
            print(f"Failed to parse steam links file: {e}")

def save_tokens():
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump(PLAYER_TOKENS, f, indent=4)
    except Exception as e:
        print(f"Error writing tokens database: {e}")

def save_steam_links():
    try:
        with open(STEAM_FILE, "w") as f:
            json.dump(STEAM_LINKS, f, indent=4)
    except Exception as e:
        print(f"Error writing steam links database: {e}")

def get_linked_steam_id(user_id):
    uid = str(user_id)
    try:
        with open(STEAM_FILE, "r") as f:
            steam_db = json.load(f)
        steam_id = steam_db.get(uid)
        if steam_id:
            return str(steam_id).strip()
    except Exception:
        pass
    try:
        steam_id = STEAM_LINKS.get(int(user_id)) or STEAM_LINKS.get(user_id)
        if steam_id:
            return str(steam_id).strip()
    except Exception:
        pass
    return None

def _load_token_db():
    if not os.path.exists(TOKEN_FILE):
        return {}
    try:
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def get_steam_token_balance(steam_id):
    steam_key = str(steam_id)
    profile = _load_token_db().get(steam_key)
    if isinstance(profile, dict):
        return int(profile.get("tokens", 0) or 0)
    if profile is not None:
        try:
            return int(profile)
        except Exception:
            return 0
    return 0

def _sync_token_memory(steam_key, discord_key, value):
    try:
        PLAYER_TOKENS[int(steam_key)] = value
        did = int(discord_key)
        if did in PLAYER_TOKENS:
            del PLAYER_TOKENS[did]
    except Exception:
        pass

def charge_steam_tokens(steam_id, discord_id, cost):
    steam_key = str(steam_id)
    discord_key = str(discord_id)
    token_db = _load_token_db()
    profile = token_db.get(steam_key, 0)
    if isinstance(profile, dict):
        tokens = int(profile.get("tokens", 0) or 0)
        if tokens < cost:
            return None
        token_db[steam_key]["tokens"] = tokens - cost
        new_balance = token_db[steam_key]["tokens"]
        mem_value = token_db[steam_key]
    else:
        try:
            tokens = int(profile or 0)
        except Exception:
            tokens = 0
        if tokens < cost:
            return None
        new_balance = tokens - cost
        token_db[steam_key] = new_balance
        mem_value = new_balance
    if discord_key in token_db:
        del token_db[discord_key]
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump(token_db, f, indent=4)
    except Exception as e:
        print(f"Error writing tokens database: {e}")
        return None
    _sync_token_memory(steam_key, discord_key, mem_value)
    return new_balance

def set_steam_token_balance(steam_id, discord_id, amount):
    steam_key = str(steam_id)
    discord_key = str(discord_id)
    token_db = _load_token_db()
    amount = max(0, int(amount))
    if isinstance(token_db.get(steam_key), dict):
        token_db[steam_key]["tokens"] = amount
        mem_value = token_db[steam_key]
    else:
        token_db[steam_key] = amount
        mem_value = amount
    if discord_key in token_db:
        del token_db[discord_key]
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_db, f, indent=4)
    _sync_token_memory(steam_key, discord_key, mem_value)
    return amount

# --- WEBHOOK LOG STREAMING INTERCEPTOR ENGINE ---
async def handle_game_log(request):
    try:
        # The bot tries to parse the incoming text as a JSON object
        data = await request.json()
    except Exception:
        # ✅ If data is blank or unformatted text, this catches the failure 
        # and safely drops it without breaking your live bot console stream!
        return web.Response(text="Ignored unformatted payload", status=400)
        
    # If the data IS valid, the bot safely moves past the filter to your normal logic:
    steam_id = data.get('steam_id')
    player_name = data.get('player_name')
    message = data.get('message')
    
    # (Your bot's existing profile/token tracking or Discord logging logic goes here)
    print(f"[GAME] {player_name} ({steam_id}): {message}")
    return web.Response(text="Success", status=200)


async def start_webhook_receiver():
    try:
        app = web.Application()
        app.router.add_post('/game-logs', handle_game_log)
        runner = web.AppRunner(app)
        await runner.setup()
        
        # Listening to the public internet on your specific Sparked Host port
        site = web.TCPSite(runner, '0.0.0.0', 8094)
        await site.start()
        print("[LIVE TALLY] Webhook data interceptor port strictly listening on port 8094...")
        
        # Keep alive loop to prevent the background port from closing
        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        print(f"[CRITICAL ERROR] Webhook server failed to bind to port 8094: {e}")


# --- DINOSAUR TIERS & PRICING CONFIGURATION ---
DINO_TIERS = {
    "Herrerasaurus": 1, "Hypsilophodon": 1, "Dryosaurus": 1,
    "Galli": 2, "Pachycephalosaurus": 2, "Troodon": 2,
    "Tenontosaurus": 3, "Dilophosaurus": 3, "Omniraptor": 3,
    "Diabloceratops": 4, "Ceratosaurus": 4, "Maiasaura": 4,
    "Stegosaurus": 5, "Carnotaurus": 5, "Allo": 5,
    "Deinosuchus": 6, "Tyrannosaurus": 6, "Triceratops": 6,
    "Beipiaosaurus": 2, "Pteranodon": 2, "Austroraptor": 3, "Kentrosaurus": 4
}
TIER_PRICES = {1: 2, 2: 2, 3: 3, 4: 5, 5: 8, 6: 12}

# --- STABLE NATIVE SOCKET HANDLER ---
async def parse_ban_seconds(duration: str) -> int:
    raw = (duration or "0").strip().lower()
    if raw in ("0", "perm", "permanent", "forever", "infinite"):
        return 0
    try:
        if raw.endswith("s") and raw[:-1].isdigit():
            return int(raw[:-1])
        if raw.endswith("m") and raw[:-1].isdigit():
            return int(raw[:-1]) * 60
        if raw.endswith("h") and raw[:-1].isdigit():
            return int(raw[:-1]) * 3600
        if raw.endswith("d") and raw[:-1].isdigit():
            return int(raw[:-1]) * 86400
        return max(0, int(raw))
    except Exception:
        return 0

async def send_evrima_rcon(opcode: int, payload: str = "") -> str:
    """Talk to The Isle Evrima using its custom RCON protocol, not Source RCON."""
    reader = writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(RCON_IP, RCON_PORT), timeout=6.0)
        auth_packet = b"\x01" + RCON_PASSWORD.encode("utf-8") + b"\x00"
        writer.write(auth_packet)
        await writer.drain()
        auth_resp = await asyncio.wait_for(reader.read(4096), timeout=4.0)
        auth_text = auth_resp.decode("utf-8", errors="ignore")
        if "Accepted" not in auth_text and "Authenticated" not in auth_text:
            return "RCON Auth Failed: " + (auth_text.strip() or "no reply")
        body = (payload or "").encode("utf-8")
        cmd_packet = b"\x02" + bytes([opcode]) + body + b"\x00"
        writer.write(cmd_packet)
        await writer.drain()
        try:
            raw_resp = await asyncio.wait_for(reader.read(8192), timeout=4.0)
            response_text = raw_resp.decode("utf-8", errors="ignore").replace("\x00", "").strip()
        except asyncio.TimeoutError:
            response_text = "Command sent (no response)"
        return response_text if response_text else "Command Executed"
    except Exception as e:
        return "RCON Connection Error: " + str(e)
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

async def send_game_command(command: str):
    command = (command or "").strip()
    if not command:
        return "Empty command"
    name, _, rest = command.partition(" ")
    name = name.lower().strip()
    rest = rest.strip()
    opcode_map = {
        "announce": 0x10, "broadcast": 0x10,
        "directmessage": 0x11, "dm": 0x11,
        "serverdetails": 0x12,
        "wipecorpses": 0x13,
        "ban": 0x20,
        "kick": 0x30,
        "playerlist": 0x40,
        "save": 0x50,
        "pause": 0x60,
        "custom": 0x70,
        "getplayerdata": 0x77,
        "togglewhitelist": 0x81,
        "addwhitelist": 0x82,
        "removewhitelist": 0x83,
        "toggleglobalchat": 0x84,
        "toggleai": 0x90,
        "setgrowthmultiplier": 0x22
    }
    if name in ("grow", "heal", "slay", "revive", "addadmin"):
        custom_result = await send_evrima_rcon(0x70, command)
        steam_guess = rest.split(" ")[0] if rest else ""
        if steam_guess.isdigit() and len(steam_guess) == 17:
            note = steam_guess + ",Primeval Island: your " + name + " request was received and is being processed."
            dm_result = await send_evrima_rcon(0x11, note)
            return "Custom " + name + ": " + custom_result + " | In-game DM: " + dm_result
        return "Custom " + name + ": " + custom_result + " (Evrima has no native " + name + " opcode)"
    opcode = opcode_map.get(name)
    if opcode is None:
        return await send_evrima_rcon(0x70, command)
    payload = rest
    if name == "kick" and rest:
        bits = rest.split(" ", 1)
        payload = bits[0] if len(bits) == 1 else bits[0] + "," + bits[1]
    elif name == "ban" and rest:
        bits = rest.split(" ", 2)
        steam = bits[0]
        duration = bits[1] if len(bits) > 1 else "0"
        reason = bits[2] if len(bits) > 2 else "Banned by Discord admin"
        seconds = await parse_ban_seconds(duration)
        payload = steam + "," + reason + "," + str(seconds)
    elif name in ("directmessage", "dm") and rest:
        bits = rest.split(" ", 1)
        payload = bits[0] if len(bits) == 1 else bits[0] + "," + bits[1]
    print("[EVRIMA RCON]", name, hex(opcode), payload)
    return await send_evrima_rcon(opcode, payload)

def resolve_steam_target(member: discord.Member, steam_id: str):
    if steam_id:
        clean_id = steam_id.strip()
        if clean_id.isdigit() and len(clean_id) == 17:
            return clean_id
    if member and member.id in STEAM_LINKS:
        return STEAM_LINKS[member.id]
    return None
# --- INTERACTIVE GROW DROPDOWN & VIEW ---
class GrowDinoDropdown(Select):
    def __init__(self):
        options = []
        for dino, tier in DINO_TIERS.items():
            price = TIER_PRICES.get(tier, 2)
            if dino in LIVE_HEADCOUNT and dino in SPECIES_CAPS:
                current_count = LIVE_HEADCOUNT[dino]
                max_allowed = SPECIES_CAPS[dino]
                if current_count >= max_allowed:
                    options.append(discord.SelectOption(label=f"[SOLD OUT] {dino}", description=f"🚫 Max capacity reached ({current_count}/{max_allowed} alive)", value=f"LOCKED_{dino}"))
                    continue
            options.append(discord.SelectOption(label=f"{dino}", description=f"Tier {tier} Species — Cost: {price} Tokens", value=dino))
        super().__init__(placeholder="Select your active live dinosaur species...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        # 1. DEFEND AGAINST TIMEOUTS IMMEDIATELY
        await interaction.response.defer(ephemeral=True)
        
        user_id = str(interaction.user.id)
        species = self.values[0] if self.values else ""
        
        # 2. RUN YOUR SPECIES LOCKING CHECK
        if species.startswith("LOCKED_"):
            await interaction.followup.send("❌ This species is currently locked due to map capacity!", ephemeral=True)
            return

        # 3. STRICT STEAM ID WALLET VALIDATION ENGINE
        tokens = 0
        steam_id = None
        try:
            with open('steam_links.json', 'r') as f:
                steam_db = json.load(f)
            with open('tokens.json', 'r') as f:
                token_db = json.load(f)

            # 1. Translate Discord ID to Steam ID
            steam_id = steam_db.get(user_id)
            print(f"[STRICT LOOKUP] Discord User: {user_id} | Verifying Linked SteamID: {steam_id}")

            if not steam_id:
                await interaction.followup.send(
                    "❌ **Link Requirement Failed:** Your Discord account is not linked to a Steam ID!\n"
                    "Please run the `/link` command first to register your Steam profile before buying perks.", 
                    ephemeral=True
                )
                return

            # 2. Extract balance strictly using the Steam ID key
            steam_key = str(steam_id)
            if steam_key in token_db:
                user_profile = token_db[steam_key]
                tokens = user_profile.get('tokens', 0) if isinstance(user_profile, dict) else int(user_profile)
            else:
                print(f"[STRICT LOOKUP] Warning: Steam ID {steam_key} has no entry inside tokens.json yet.")
                tokens = 0
                
            print(f"[LIVE TALLY] User has {tokens} tokens. Selected species: {species}")
            
        except Exception as e:
            print(f"[CRITICAL ERROR] Strict lookup engine crashed: {e}")
            await interaction.followup.send("❌ An economy data validation failure occurred.", ephemeral=True)
            return

        # 4. PRICING RULES CHECKING
        tier = DINO_TIERS.get(species, 1)
        cost = TIER_PRICES.get(tier, 2)

        if tokens < cost:
            await interaction.followup.send(f"❌ Growing a **{species}** requires **{cost} tokens**. You currently have **{tokens}**.", ephemeral=True)
            return

        # 5. DEDUCT TOKENS AND SELF-CLEAN DATABASE
        try:
            steam_key = str(steam_id)
            discord_key = str(user_id)

            with open('tokens.json', 'r') as f:
                token_db = json.load(f)
                
            # Charge the Steam ID account line
            if isinstance(token_db.get(steam_key), dict):
                token_db[steam_key]['tokens'] = tokens - cost
            else:
                token_db[steam_key] = tokens - cost

            # Automatically delete the old duplicate Discord balance line permanently
            if discord_key in token_db:
                del token_db[discord_key]
                print(f"[STRICT SAVING] Successfully purged old Discord ID record ({discord_key}) from tokens.json.")

            with open('tokens.json', 'w') as f:
                json.dump(token_db, f, indent=4)
                
            try:
                save_tokens()
            except Exception:
                pass
                
            # 6. BROADCAST THE RCON TRANSMISSION PACKET
            asyncio.create_task(send_game_command(f"grow {steam_id}"))
            
            await interaction.followup.send(
                f"📈 **Growth Purchase Successful!** Spent {cost} tokens. Remaining Balance: {tokens - cost} tokens.\n"
                f"Account Key Verified: `{steam_id}`.\n\n"
                f"⚠️ **CRITICAL REQUIREMENT:** You must be actively logged into the server as a juvenile **{species}** for the growth engine to apply!", 
                ephemeral=True
            )
        except Exception as e:
            print(f"[CRITICAL ERROR] Failed to finalize strict growth transaction: {e}")
            await interaction.followup.send("❌ Internal data transaction processing failure. Your tokens were not charged.", ephemeral=True)

        # 4. PRICING RULES CHECKING
        tier = DINO_TIERS.get(species, 1)
        cost = TIER_PRICES.get(tier, 2)

        if tokens < cost:
            await interaction.followup.send(f"❌ Growing a **{species}** requires **{cost} tokens**. You currently have **{tokens}**.", ephemeral=True)
            return

        # 5. DEDUCT TOKENS AND SELF-CLEAN DATABASE
        try:
            steam_key = str(steam_id)
            discord_key = str(user_id)

            with open('tokens.json', 'r') as f:
                token_db = json.load(f)
                
            # Charge the Steam ID account line
            if isinstance(token_db.get(steam_key), dict):
                token_db[steam_key]['tokens'] = tokens - cost
            else:
                token_db[steam_key] = tokens - cost

            # Automatically delete the old duplicate Discord balance line permanently
            if discord_key in token_db:
                del token_db[discord_key]
                print(f"[STRICT SAVING] Successfully purged old Discord ID record ({discord_key}) from tokens.json.")

            with open('tokens.json', 'w') as f:
                json.dump(token_db, f, indent=4)
                
            try:
                save_tokens()
            except Exception:
                pass
                
            # 6. BROADCAST THE RCON TRANSMISSION PACKET
            asyncio.create_task(send_game_command(f"grow {steam_id}"))
            
            await interaction.followup.send(
                f"📈 **Growth Purchase Successful!** Spent {cost} tokens. Remaining Balance: {tokens - cost} tokens.\n"
                f"Account Key Verified: `{steam_id}`.\n\n"
                f"⚠️ **CRITICAL REQUIREMENT:** You must be actively logged into the server as a juvenile **{species}** for the growth engine to apply!", 
                ephemeral=True
            )
        except Exception as e:
            print(f"[CRITICAL ERROR] Failed to finalize strict growth transaction: {e}")
            await interaction.followup.send("❌ Internal data transaction processing failure. Your tokens were not charged.", ephemeral=True)

        # 4. PRICING RULES CHECKING
        tier = DINO_TIERS.get(species, 1)
        cost = TIER_PRICES.get(tier, 2)

        if tokens < cost:
            await interaction.followup.send(f"❌ Growing a **{species}** requires **{cost} tokens**. You currently have **{tokens}**.", ephemeral=True)
            return

        # 5. Process the Growth Purchase Transaction
        if not steam_id:
            await interaction.followup.send("❌ Growth Engine failed: Your Discord account is not linked to a valid Steam ID yet. Use the link command first!", ephemeral=True)
            return

        try:
            # Force define the key string immediately so Python 3.11 handles it globally
            steam_key = str(steam_id)
            discord_key = str(user_id)

            # Open the live database file
            with open('tokens.json', 'r') as f:
                token_db = json.load(f)
                
            # Deduct the cost and strictly apply it to the 17-digit Steam ID key
            if isinstance(token_db.get(steam_key), dict):
                token_db[steam_key]['tokens'] = tokens - cost
            else:
                token_db[steam_key] = tokens - cost

            # --- THE CLEANING MECHANISM ---
            # Automatically checks for and permanently deletes the old Discord ID entry
            if discord_key in token_db:
                del token_db[discord_key]
                print(f"[STRICT SAVING] Successfully purged old Discord ID record ({discord_key}) from tokens.json.")

            # Write the clean, Steam-only database back to the file
            with open('tokens.json', 'w') as f:
                json.dump(token_db, f, indent=4)
                
            try:
                save_tokens()
            except Exception:
                pass
                
            # 6. Broadcast the In-Game Console Growth Packet
            asyncio.create_task(send_game_command(f"grow {steam_id}"))
            
            await interaction.followup.send(
                f"📈 **Growth Purchase Successful!** Spent {cost} tokens. Remaining Balance: {tokens - cost} tokens.\n"
                f"Account Key Verified: `{steam_id}`.\n\n"
                f"⚠️ **CRITICAL REQUIREMENT:** You must be actively logged into the server as a juvenile **{species}** for the growth engine to apply!", 
                ephemeral=True
            )
        except Exception as e:
            print(f"[CRITICAL ERROR] Failed to finalize strict growth transaction: {e}")
            await interaction.followup.send("❌ Internal data transaction failure. Your tokens were not charged.", ephemeral=True)
# --- RE-ADDING THE PERK VIEW CONTAINER ---
class GrowPerkView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900.0)
        self.add_item(GrowDinoDropdown())

# --- MAIN DINOSAUR PURCHASE DROPDOWN & SHOP VIEW ---
class DinoPurchaseDropdown(Select):
    def __init__(self):
        options = []
        for dino, tier in list(DINO_TIERS.items())[:25]:
            if dino in LIVE_HEADCOUNT and dino in SPECIES_CAPS:
                if LIVE_HEADCOUNT[dino] >= SPECIES_CAPS[dino]:
                    options.append(discord.SelectOption(label=f"[SOLD OUT] {dino}", description=f"🚫 Capped at {SPECIES_CAPS[dino]} alive", value=f"LOCKED_{dino}"))
                    continue
            options.append(discord.SelectOption(label=f"{dino} (Tier {tier})", value=dino))
        super().__init__(placeholder="Choose a dinosaur species to buy...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        species = self.values[0] if self.values else ""
        if species.startswith("LOCKED_"):
            await interaction.response.send_message("🛑 **SOLD OUT:** Species cap met. Choose another dinosaur or try again later.", ephemeral=True)
            return
        steam_id = get_linked_steam_id(interaction.user.id)
        if not steam_id:
            await interaction.response.send_message("❌ Profile verification error. Please run `/link_steam` first!", ephemeral=True)
            return
        cost = 5
        tokens = get_steam_token_balance(steam_id)
        if tokens < cost:
            await interaction.response.send_message(f"❌ You need **{cost} tokens** to buy a dinosaur. You currently have **{tokens}**.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        new_balance = charge_steam_tokens(steam_id, interaction.user.id, cost)
        if new_balance is None:
            await interaction.followup.send("❌ Could not update your Steam wallet. Your tokens were not charged.", ephemeral=True)
            return
        await interaction.followup.send(f"🦖 **Dino Ticket Purchased!** Spent {cost} tokens. Remaining Balance: {new_balance} tokens. A juvenile **{species}** request has been cued for SteamID: `{steam_id}`!\n\n⚠️ **STAFF QUEUE RULE:** Because Evrima console prevents species swapping, please open a support ticket so an Admin can manually spawn your new **{species}** species in-game!", ephemeral=True)

class TokenShopView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📈 Growth Center", style=discord.ButtonStyle.green, custom_id="perk_grow")
    async def grow_perk(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("To use the dynamic growth upgrades, please run the separate command: `/grow_dino`", ephemeral=True)


# --- PUBLIC PLAYER SLASH COMMANDS ---
@bot.tree.command(name="shop", description="Opens the interactive Token Redemption Shop UI.")
async def shop(interaction: discord.Interaction):
    embed = discord.Embed(title="🛒 Primeval Island Marketplace", description="Spend tokens to redeem packages. Capped dinosaurs will instantly read [SOLD OUT].", color=discord.Color.gold())
    steam_id = get_linked_steam_id(interaction.user.id)
    user_tokens = get_steam_token_balance(steam_id) if steam_id else 0
    embed.add_field(name="Your Wallet Balance", value=f"🪙 `{user_tokens} Tokens`", inline=False)
    view = TokenShopView()
    view.add_item(DinoPurchaseDropdown())
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="grow_dino", description="Opens the growth redemption center to upgrade your active creature to 70% growth.")
async def grow_dino(interaction: discord.Interaction):
    embed = discord.Embed(title="📈 Character Growth Upgrade Center", description="Select your dinosaur species. If a slot reads [SOLD OUT], the maximum map capacity has been reached.", color=discord.Color.green())
    steam_id = get_linked_steam_id(interaction.user.id)
    user_tokens = get_steam_token_balance(steam_id) if steam_id else 0
    embed.add_field(name="Your Wallet Balance", value=f"🪙 `{user_tokens} Tokens`", inline=False)
    await interaction.response.send_message(embed=embed, view=GrowPerkView(), ephemeral=True)

@bot.tree.command(name="link_steam", description="Links your 17-digit SteamID64 to your Discord account.")
@app_commands.describe(steam_id="Enter your 17-digit SteamID64")
async def link_steam(interaction: discord.Interaction, steam_id: str):
    clean_id = steam_id.strip()
    if len(clean_id) != 17 or not clean_id.isdigit():
        await interaction.response.send_message("❌ Error: Invalid format.", ephemeral=True)
        return
    STEAM_LINKS[interaction.user.id] = clean_id
    save_steam_links() 
    await interaction.response.send_message(f"🔗 **Linked to SteamID:** `{clean_id}`.", ephemeral=True)

@bot.tree.command(name="prime", description="Displays the 8 steps to achieve Prime Elder status.")
async def prime(interaction: discord.Interaction):
    embed = discord.Embed(title="🧬 Prime Elder Checklist", color=discord.Color.green())
    embed.add_field(name="Criteria Rule", value="Complete **ANY 5** of the 8 steps before hitting 75% growth to access your 4th mutation slot.", inline=False)
    embed.add_field(name="The Objectives", value="1. Visit Sanctuary\n2. Perfect Diet\n3. Visit 2 Migration Zones\n4. Visit 4 Patrol Zones\n5. Nest Birth\n6. Raise Hatchling to 50%\n7. Never Hit 0 Nutrients\n8. No Cannibalism", inline=False)
    await interaction.response.send_message(embed=embed)

# --- MASTER ADMINISTRATIVE & CONTROLS SECTION ---
@bot.tree.command(name="add_tokens", description="Admin only: Add tokens to a player.")
@app_commands.checks.has_permissions(administrator=True)
async def add_tokens(interaction: discord.Interaction, member: discord.Member, amount: int):
    steam_id = get_linked_steam_id(member.id)
    if not steam_id:
        await interaction.response.send_message("❌ That player must `/link_steam` before tokens can be added to the Steam wallet.", ephemeral=True)
        return
    new_balance = set_steam_token_balance(steam_id, member.id, get_steam_token_balance(steam_id) + amount)
    await interaction.response.send_message(f"🪙 Added **{amount} tokens** to {member.mention}'s Steam wallet (`{steam_id}`). New balance: `{new_balance}`.")

@bot.tree.command(name="remove_tokens", description="Admin only: Remove tokens from a player's account balance.")
@app_commands.checks.has_permissions(administrator=True)
async def remove_tokens(interaction: discord.Interaction, member: discord.Member, amount: int):
    steam_id = get_linked_steam_id(member.id)
    if not steam_id:
        await interaction.response.send_message("❌ That player must `/link_steam` before tokens can be changed on the Steam wallet.", ephemeral=True)
        return
    current = get_steam_token_balance(steam_id)
    new_balance = set_steam_token_balance(steam_id, member.id, max(0, current - amount))
    await interaction.response.send_message(f"🔻 Removed **{amount} tokens** from {member.mention}'s Steam wallet (`{steam_id}`). Total balance: `{new_balance}`.")

@bot.tree.command(name="set_tokens", description="Admin only: Force set a player's account balance to an exact number.")
@app_commands.checks.has_permissions(administrator=True)
async def set_tokens(interaction: discord.Interaction, member: discord.Member, amount: int):
    steam_id = get_linked_steam_id(member.id)
    if not steam_id:
        await interaction.response.send_message("❌ That player must `/link_steam` before tokens can be set on the Steam wallet.", ephemeral=True)
        return
    target_amount = set_steam_token_balance(steam_id, member.id, amount)
    await interaction.response.send_message(f"⚙️ Force set {member.mention}'s Steam wallet (`{steam_id}`) exactly to: `🪙 {target_amount} Tokens`.")

@bot.tree.command(name="view_balance", description="Admin only: Look up any player's token count and linked Steam profile data.")
@app_commands.checks.has_permissions(administrator=True)
async def view_balance(interaction: discord.Interaction, member: discord.Member):
    steam_id = get_linked_steam_id(member.id)
    tokens = get_steam_token_balance(steam_id) if steam_id else 0
    steam_id = steam_id or "No Steam account linked yet."
    embed = discord.Embed(title=f"📋 Profile Audit: {member.name}", color=discord.Color.blue())
    embed.add_field(name="Discord Target", value=member.mention, inline=True)
    embed.add_field(name="Current Bank Balance", value=f"🪙 `{tokens} Tokens`", inline=True)
    embed.add_field(name="Linked SteamID64", value=f"`{steam_id}`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="set_headcount", description="Admin only: Force override the bot's internal headcount counter for a specific species.")
@app_commands.checks.has_permissions(administrator=True)
async def set_headcount(interaction: discord.Interaction, species: str, count: int):
    if species in LIVE_HEADCOUNT:
        LIVE_HEADCOUNT[species] = max(0, count)
        await interaction.response.send_message(f"⚙️ **Headcount Override:** Set active **{species}** count exactly to `{count}`.")
    else:
        await interaction.response.send_message("❌ Error: Species name not recognized by the counting layout engine.")

# --- LIVE REMOTE INTERACTIVE MODERATION SUITE ---
@bot.tree.command(name="admin_slay", description="Moderation: Remotely terminates the specified player's active dinosaur.")
@app_commands.checks.has_permissions(administrator=True)
async def admin_slay(interaction: discord.Interaction, member: discord.Member = None, steam_id: str = None):
    target = resolve_steam_target(member, steam_id)
    if not target:
        await interaction.response.send_message("❌ Error: Profile missing.", ephemeral=True)
        return
    await interaction.response.defer()
    result = await send_game_command(f"slay {target}")
    await interaction.followup.send(f"⚔️ **Admin Slay Dispatched** for Target `{target}`. Server response: `{result}`.")

@bot.tree.command(name="admin_kick", description="Moderation: Instantly disconnects a player from the live game server.")
@app_commands.checks.has_permissions(administrator=True)
async def admin_kick(interaction: discord.Interaction, reason: str, member: discord.Member = None, steam_id: str = None):
    target = resolve_steam_target(member, steam_id)
    if not target:
        await interaction.response.send_message("❌ Error: Identity not resolved.", ephemeral=True)
        return
    await interaction.response.defer()
    result = await send_game_command(f"kick {target} {reason}")
    await interaction.followup.send(f"🥾 **Admin Kick Dispatched** for Target `{target}`. Reason: `{reason}`. Response: `{result}`.")

@bot.tree.command(name="admin_ban", description="Moderation: Remotely locks a player out of the server.")
@app_commands.checks.has_permissions(ban_members=True)
async def admin_ban(interaction: discord.Interaction, duration: str, reason: str, member: discord.Member = None, steam_id: str = None):
    target = resolve_steam_target(member, steam_id)
    if not target:
        await interaction.response.send_message("❌ Error: Profile layout mapping missing.", ephemeral=True)
        return
    await interaction.response.defer()
    result = await send_game_command(f"ban {target} {duration} {reason}")
    await interaction.followup.send(f"🚫 **Admin Ban Dispatched** for Target `{target}`. Duration: `{duration}`. Response: `{result}`.")

@bot.tree.command(name="admin_revive", description="Moderation: Restores a player's profile backup.")
@app_commands.checks.has_permissions(administrator=True)
async def admin_revive(interaction: discord.Interaction, member: discord.Member = None, steam_id: str = None):
    target = resolve_steam_target(member, steam_id)
    if not target:
        await interaction.response.send_message("❌ Error: Verification missing.", ephemeral=True)
        return
    await interaction.response.defer()
    result = await send_game_command(f"revive {target}")
    await interaction.followup.send(f"🛡️ **Admin Revive Signal Dispatched** for Target `{target}`. Response: `{result}`.")

@bot.tree.command(name="admin_broadcast", description="Moderation: Sends a server-wide system announcement instantly to all online players.")
@app_commands.checks.has_permissions(administrator=True)
async def admin_broadcast(interaction: discord.Interaction, message: str):
    await interaction.response.defer()
    result = await send_game_command(f"broadcast {message}")
    await interaction.followup.send(f"📢 **Global Server Announcement Broadcasted:** `{message}`. Response: `{result}`.")

@bot.tree.command(name="admin_setgroup", description="Ecosystem Control: Promotes a player to an in-game admin permissions group.")
@app_commands.checks.has_permissions(administrator=True)
async def admin_setgroup(interaction: discord.Interaction, group: str, member: discord.Member = None, steam_id: str = None):
    target = resolve_steam_target(member, steam_id)
    if not target:
        await interaction.response.send_message("❌ Error: Identity missing.", ephemeral=True)
        return
    await interaction.response.defer()
    result = await send_game_command(f"addadmin {target} {group}")
    await interaction.followup.send(f"👑 **In-Game Admin Group Update Dispatched** for Target `{target}`. Group: `{group}`. Response: `{result}`.")

@bot.tree.command(name="isle_players", description="Admin: Fetch the live Evrima player list over RCON.")
@app_commands.checks.has_permissions(administrator=True)
async def isle_players(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await send_game_command("playerlist")
    output = result if result else "Empty RCON response"
    if len(output) > 1800:
        output = output[:1800] + "..."
    await interaction.followup.send("Live Evrima player list:\n```\n" + output + "\n```", ephemeral=True)

@bot.tree.command(name="isle_rcon", description="Admin: Test Evrima RCON with serverdetails.")
@app_commands.checks.has_permissions(administrator=True)
async def isle_rcon(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await send_game_command("serverdetails")
    output = result if result else "Empty RCON response"
    if len(output) > 1800:
        output = output[:1800] + "..."
    await interaction.followup.send("Evrima RCON status:\n```\n" + output + "\n```", ephemeral=True)

@bot.tree.command(name="isle_recon", description="Admin: Test Evrima RCON with serverdetails.")
@app_commands.checks.has_permissions(administrator=True)
async def isle_recon(interaction: discord.Interaction):
    await isle_rcon(interaction)

GAME_HOST_UUID = "09914030"
GAME_HOST_URL = "https://bropanel.gamehostbros.com"
ISLE_CMD_FLAG = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/cmd.flag"
ISLE_PRIME_DIR = "/TheIsle/Binaries/Win64/ue4ss/Mods/PrimevalRedeem/Saved/prime"

def load_game_panel_api_key():
    env = (os.environ.get("GAME_PANEL_API_KEY") or "").strip()
    if env:
        return env
    try:
        with open("game_panel_api_key.txt", "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return ""

async def queue_isle_cmd_flag(verb, steam, extra=""):
    key = load_game_panel_api_key()
    if not key:
        return False, "missing game panel API key"
    url = GAME_HOST_URL + "/api/client/servers/" + GAME_HOST_UUID + "/files/write?file=" + quote(ISLE_CMD_FLAG)
    extra = str(extra or "").strip()
    body = f"{verb} {steam} {extra}\n" if extra else f"{verb} {steam}\n"
    hdrs = {"Authorization": "Bearer " + key, "Accept": "Application/vnd.pterodactyl.v1+json", "Content-Type": "text/plain"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=hdrs, data=body) as resp:
                if resp.status in (200, 204):
                    return True, "queued"
                return False, f"write {resp.status}"
    except Exception as e:
        return False, str(e)

async def read_isle_prime_json(steam):
    key = load_game_panel_api_key()
    if not key:
        return ""
    path = ISLE_PRIME_DIR + "/" + str(steam) + ".json"
    url = GAME_HOST_URL + "/api/client/servers/" + GAME_HOST_UUID + "/files/contents?file=" + quote(path)
    hdrs = {"Authorization": "Bearer " + key, "Accept": "Application/vnd.pterodactyl.v1+json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=hdrs) as resp:
                if resp.status != 200:
                    return ""
                return await resp.text()
    except Exception:
        return ""

# Run Bot
_DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or os.environ.get("BOT_TOKEN")
if not _DISCORD_TOKEN:
    raise SystemExit(
        "Missing bot token: set the DISCORD_TOKEN (or BOT_TOKEN) environment variable before running."
    )
bot.run(_DISCORD_TOKEN)
