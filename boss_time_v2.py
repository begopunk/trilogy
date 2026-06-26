import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, timezone
import re
import json
import os
import sys
import asyncio
import threading
import time
import logging
import subprocess
import difflib
import psutil
import random
from logging.handlers import RotatingFileHandler
import telebot  # Library Telegram Bot
import yt_dlp  # Engine Jukebox Pengunduh Musik YouTube
from dotenv import load_dotenv  # Library untuk membaca file .env

# 📦 IMPORT DATA BOSS DARI FILE TERPISAH
from boss_data import BOSS_DATA

# --- SETUP LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler("boss_bot.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Filter untuk menekan log "Break infinity polling" dari TeleBot library
class NoBreakInfinityPollingFilter(logging.Filter):
    def filter(self, record):
        return not (record.name == 'TeleBot' and 'Break infinity polling' in record.getMessage())
telebot_logger = logging.getLogger('TeleBot')
telebot_logger.addFilter(NoBreakInfinityPollingFilter())

# --- MUAT KONFIGURASI AMAN (.env) ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") # Token Telegram
if not DISCORD_TOKEN or not TELEGRAM_TOKEN:
    log.error("❌ DISCORD_TOKEN atau TELEGRAM_TOKEN tidak ditemukan di file .env!")
    sys.exit(1)

ADMIN_LOG_CHANNEL_ID = int(os.getenv("ADMIN_LOG_CHANNEL_ID")) if os.getenv("ADMIN_LOG_CHANNEL_ID") else None # Channel log admin
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID") # ID Spreadsheet untuk database

# --- INITIALIZE TELEGRAM BOT ---
tele_bot = telebot.TeleBot(TELEGRAM_TOKEN)
try:
    tele_bot.remove_webhook()
except Exception:
    pass
if hasattr(tele_bot, "skip_pending"):
    tele_bot.skip_pending = True

# --- KONFIGURASI UTAMA ---
TZ_GMT8 = timezone(timedelta(hours=8))

BOT_START_TIME = time.time()

intents = discord.Intents.default()
intents.message_content = True

# case_insensitive=True agar !Jadwal / !List dari HP tetap berjalan
bot = commands.Bot(command_prefix='!', intents=intents, case_insensitive=True)

# --- GLOBAL STATE VARIABLES ---
boss_status = {}
notif_sent = {}
data_loaded = False
boss_logs = {} 
user_timezones = {} 
target_channel_id = None
telegram_chat_id = None
last_db_mtime = 0
last_code_mtime = 0
db_lock = threading.RLock() # Re-entrant Lock untuk mencegah deadlock saat auto-reset database
telebot_started = False
telebot_active = True # Flag untuk mengontrol loop polling Telegram
pending_update_notice = False

# --- DISCORD JUKEBOX STATE ---
music_queue = []
current_track = None # Menyimpan data lagu aktif (title, duration, start_time, dll)

# --- KONFIGURASI OPTIMASI JUKEBOX STREAMING & DOWNLOADING ---
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}
YDL_DISCORD_OPTS = {
    'format': 'bestaudio/best',
    'noplaylist': False, # Diaktifkan untuk mendukung playlist
    'default_search': 'ytsearch1',
    'quiet': True
}
YDL_TELEGRAM_OPTS = {
    'format': 'bestaudio/best', 
    'outtmpl': 'music_downloads/%(title)s.%(ext)s',
    'noplaylist': True,
    'default_search': 'ytsearch1',
    'quiet': True,
    'socket_timeout': 30, 
    'retries': 3,
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'm4a',
        'preferredquality': '192',
    }]
}

# --- FUNGSI DATABASE JSON ---
DATA_FILE = "bot_data.json"
SERVICE_ACCOUNT_FILE = "service_account.json"
# Lock untuk mencegah akses file kredensial secara bersamaan dari thread berbeda
_creds_lock = threading.Lock()
# Cache global untuk kredensial agar tidak terjadi PermissionError karena pembacaan berulang
_cached_creds = None

def load_data():
    global boss_logs, boss_status, user_timezones, target_channel_id, telegram_chat_id, last_db_mtime, notif_sent
    
    data = None
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            log.info("📦 Database dimuat dari cadangan lokal (bot_data.json).")
        except Exception as e:
            log.error(f"⚠️ Gagal membaca database lokal: {e}")

    if data:
        with db_lock:
            try:
                boss_logs = data.get("boss_logs", {})
                user_timezones = data.get("user_timezones", {})
                target_channel_id = data.get("target_channel_id", None)
                telegram_chat_id = data.get("telegram_chat_id", None)
                notif_sent = data.get("notif_sent", {})
                
                saved_status = data.get("boss_status", {})
                boss_status.clear()
                for boss, time_str in saved_status.items():
                    dt = datetime.fromisoformat(time_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=TZ_GMT8)
                    else:
                        dt = dt.astimezone(TZ_GMT8)
                    boss_status[boss] = dt
            except Exception as e:
                log.error(f"⚠️ Gagal memproses data ke memori: {e}")

    if os.path.exists(DATA_FILE):
        last_db_mtime = os.path.getmtime(DATA_FILE)

def save_data():
    global last_db_mtime
    try:
        serializable_status = {boss: time.isoformat() for boss, time in boss_status.items()}
        data_to_save = {
            "boss_logs": boss_logs,
            "boss_status": serializable_status,
            "user_timezones": user_timezones,
            "target_channel_id": target_channel_id,
            "telegram_chat_id": telegram_chat_id,
            "notif_sent": notif_sent
        }
        json_string = json.dumps(data_to_save, indent=4, ensure_ascii=False)

        # Selalu simpan ke cadangan lokal untuk keamanan
        with db_lock:
            # Menggunakan temporary file untuk mencegah korupsi data saat crash
            temp_file = DATA_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(json_string)
            os.replace(temp_file, DATA_FILE)
            # Update tracker agar loop tidak memicu reload dari hasil simpanan bot sendiri
            last_db_mtime = os.path.getmtime(DATA_FILE)
    except Exception as e:
        log.error(f"⚠️ Gagal menyimpan data: {e}")
    else:
        log.info("💾 Database berhasil diperbarui dan disimpan.")

# --- PROSES INPUT KELOLA WAKTU ---
def process_kill_time(boss_name, jam, menit, offset_hours, tz_name, author_name):
    interval_jam = BOSS_DATA[boss_name]
    user_tz = timezone(timedelta(hours=offset_hours))
    
    now_user = datetime.now(user_tz)
    waktu_mati_user = now_user.replace(hour=jam, minute=menit, second=0, microsecond=0)
    
    if waktu_mati_user > now_user:
        waktu_mati_user -= timedelta(days=1)
        
    waktu_mati_gmt8 = waktu_mati_user.astimezone(TZ_GMT8)
    next_spawn = waktu_mati_gmt8 + timedelta(hours=interval_jam)
    
    with db_lock:
        boss_status[boss_name] = next_spawn
        notif_sent[boss_name] = {"5m": False, "1m": False, "spawn": False}
        
        waktu_log_str = waktu_mati_gmt8.strftime("%H:%M")
        boss_logs[boss_name] = {
            "user": author_name,
            "waktu_mati": f"{waktu_log_str} (Server) / {jam:02d}:{menit:02d} ({tz_name})",
            "waktu_input": datetime.now(TZ_GMT8).strftime("%d-%m %H:%M")
        }
        save_data()
    return next_spawn

# --- TOMBOL PILIHAN TIMEZONE ---
class TimezoneSelectView(discord.ui.View):
    def __init__(self, boss_name: str, jam: int, menit: int, author: discord.User):
        super().__init__(timeout=60.0)
        self.boss_name = boss_name
        self.jam = jam
        self.menit = menit
        self.author = author

    async def calculate_and_send(self, interaction: discord.Interaction, offset_hours: int, tz_name: str):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("❌ Hanya pengguna yang menginput data yang bisa memilih zona waktu.", ephemeral=True)
            
        await interaction.response.defer()
        next_spawn = await asyncio.to_thread(process_kill_time, self.boss_name, self.jam, self.menit, offset_hours, tz_name, self.author.display_name)
        
        waktu_wita = next_spawn.strftime("%d-%m-%Y %H:%M:%S")
        waktu_wib = (next_spawn - timedelta(hours=1)).strftime("%d-%m-%Y %H:%M:%S")
        
        try: await interaction.message.delete()
        except: pass
        
        embed = discord.Embed(title="✅ TIMEZONE DIKONVERSI", color=0x00ffcc)
        embed.description = (
            f"⚔️ Boss **{self.boss_name.upper()}** dilaporkan mati pada jam `{self.jam:02d}:{self.menit:02d}` ({tz_name})\n\n"
            f"⏳ **Perkiraan Spawn Selanjutnya:**\n"
            f"🗓️ WITA: `{waktu_wita} (GMT+8)`\n"
            f"🗓️ WIB: `{waktu_wib} (GMT+7)`"
        )
        embed.set_footer(text=f"Diinput oleh: {self.author.display_name}")
        await interaction.followup.send(embed=embed)

        # 📢 [CROSS-POST] Kirim info ke Telegram
        if telegram_chat_id:
            tele_msg = (
                f"📝 <b>[DISCORD → TELEGRAM]</b>\n"
                f"👤 <b>{self.author.display_name}</b> menginput via Discord\n"
                f"👹 Boss: <code>{self.boss_name.upper()}</code>\n"
                f"☠️ Jam Mati: <code>{self.jam:02d}:{self.menit:02d} ({tz_name})</code>\n"
                f"⏳ Next WITA: <code>{waktu_wita}</code>"
            )
            bot.loop.run_in_executor(None, tele_bot.send_message, telegram_chat_id, tele_msg, "HTML")
        self.stop() # Hentikan view setelah selesai

    @discord.ui.button(label="🇮🇩 Jam WIB (GMT+7)", style=discord.ButtonStyle.primary)
    async def wib_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.calculate_and_send(interaction, 7, "WIB")

    @discord.ui.button(label="🇲🇾/🇵🇭 Jam WITA/Server (GMT+8)", style=discord.ButtonStyle.success)
    async def wita_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.calculate_and_send(interaction, 8, "GMT+8")

# --- POP-UP JENDELA MODAL INPUT JAM MATI BOSS ---
class KillTimeModal(discord.ui.Modal):
    def __init__(self, boss_name: str, parent_view: discord.ui.View):
        super().__init__(title=f"Input Waktu Mati: {boss_name.upper()}")
        self.boss_name = boss_name
        self.parent_view = parent_view

        self.time_input = discord.ui.TextInput(
            label="Jam Kematian Boss",
            placeholder="Contoh: 05:30 atau 14.15",
            min_length=4,
            max_length=5,
            required=True
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        input_value = self.time_input.value.strip()
        match = re.match(r"(\d{1,2})[:.](\d{2})", input_value)
        
        if not match:
            return await interaction.response.send_message("❌ Format waktu salah! Gunakan format seperti `05:04` atau `13.15`", ephemeral=True)
            
        jam = int(match.group(1))
        menit = int(match.group(2))
        
        if jam < 0 or jam > 23 or menit < 0 or menit > 59:
            return await interaction.response.send_message("❌ Format waktu tidak valid.", ephemeral=True)

        user_id = str(interaction.user.id)
        for child in self.parent_view.children: child.disabled = True
        try: await self.parent_view.message.edit(view=self.parent_view)
        except: pass
        self.parent_view.stop()

        if user_id in user_timezones:
            await interaction.response.defer()
            utz = user_timezones[user_id]
            next_spawn = await asyncio.to_thread(process_kill_time, self.boss_name, jam, menit, utz["offset"], utz["name"], interaction.user.display_name)
            waktu_wita = next_spawn.strftime("%d-%m-%Y %H:%M:%S")
            waktu_wib = (next_spawn - timedelta(hours=1)).strftime("%d-%m-%Y %H:%M:%S")
            
            embed = discord.Embed(title="✅ JADWAL DICATAT (AUTO-TZ)", color=0x00ffcc)
            embed.description = (
                f"⚔️ Boss **{self.boss_name.upper()}** dilaporkan mati jam `{input_value}` ({utz['name']})\n\n"
                f"⏳ **Spawn Selanjutnya:**\n"
                f"🗓️ WITA: `{waktu_wita} (GMT+8)`\n"
                f"🗓️ WIB: `{waktu_wib} (GMT+7)`"
            )
            await interaction.followup.send(embed=embed)

            # 📢 [CROSS-POST] Kirim info ke Telegram
            if telegram_chat_id:
                tele_msg = (
                    f"📝 <b>[DISCORD → TELEGRAM]</b>\n"
                    f"👤 <b>{interaction.user.display_name}</b> menginput via Modal Discord\n"
                    f"👹 Boss: <code>{self.boss_name.upper()}</code>\n"
                    f"☠️ Jam Mati: <code>{input_value} ({utz['name']})</code>\n"
                    f"⏳ Next WITA: <code>{waktu_wita}</code>"
                )
                bot.loop.run_in_executor(None, tele_bot.send_message, telegram_chat_id, tele_msg, "HTML")
        else:
            view = TimezoneSelectView(self.boss_name, jam, menit, interaction.user)
            await interaction.response.send_message(f"🤔 Jam **{input_value}** yang dimasukkan {interaction.user.mention} itu berdasarkan zona waktu mana?", view=view)

# --- CLASS VIEW UTAMA TOMBOL SPAWN ---
class BossKillView(discord.ui.View):
    def __init__(self, boss_name: str):
        super().__init__(timeout=None)
        self.boss_name = boss_name
        self.message = None

    @discord.ui.button(label="⚔️ Record Kill (Now)", style=discord.ButtonStyle.danger)
    async def record_kill_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        interval_jam = BOSS_DATA[self.boss_name]
        sekarang_gmt8 = datetime.now(TZ_GMT8)
        next_spawn = sekarang_gmt8 + timedelta(hours=interval_jam)
        
        boss_status[self.boss_name] = next_spawn
        notif_sent[self.boss_name] = {"5m": False, "1m": False, "spawn": False}
        boss_logs[self.boss_name] = {
            "user": interaction.user.display_name,
            "waktu_mati": sekarang_gmt8.strftime("%H:%M (Real-time Tombol)"),
            "waktu_input": sekarang_gmt8.strftime("%d-%m %H:%M")
        }
        await asyncio.to_thread(save_data) # Pastikan save_data berjalan di thread terpisah
        
        waktu_wita = next_spawn.strftime("%d-%m-%Y %H:%M:%S")
        waktu_wib = (next_spawn - timedelta(hours=1)).strftime("%d-%m-%Y %H:%M:%S")
        
        for child in self.children: child.disabled = True
        await interaction.message.edit(view=self)
        
        embed = discord.Embed(title="⚔️ BOSS KILLED (VIA TOMBOL)", color=0xff4d4d)
        embed.description = (
            f"Boss **{self.boss_name.upper()}** telah berhasil ditumbangkan oleh {interaction.user.mention}!\n\n"
            f"⏳ **Spawn Selanjutnya pada:**\n"
            f"🗓️ WITA: `{waktu_wita} (GMT+8)`\n"
            f"🗓️ WIB: `{waktu_wib} (GMT+7)`"
        )
        await interaction.followup.send(embed=embed)

        # 📢 [CROSS-POST] Kirim info ke Telegram
        if telegram_chat_id:
            tele_msg = (
                f"⚔️ <b>[DISCORD → TELEGRAM]</b>\n"
                f"👤 <b>{interaction.user.display_name}</b> menekan tombol <b>Record Kill</b> di Discord!\n"
                f"👹 Boss: <code>{self.boss_name.upper()}</code> telah mati.\n"
                f"⏳ Next WITA: <code>{waktu_wita}</code>"
            )
            bot.loop.run_in_executor(None, tele_bot.send_message, telegram_chat_id, tele_msg, "HTML")
        self.stop() # Hentikan view setelah selesai

    @discord.ui.button(label="✏️ Input Kill Time", style=discord.ButtonStyle.primary)
    async def input_kill_time_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = KillTimeModal(self.boss_name, self)
        await interaction.response.send_modal(modal)

# --- VIEW UNTUK REFRESH JADWAL ---
class RefreshJadwalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # View persisten (tidak akan basi)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, custom_id="refresh_jadwal")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        with db_lock:
            if not boss_status:
                has_bosses = False
            else:
                has_bosses = True
                sorted_bosses = sorted(boss_status.items(), key=lambda x: x[1])

        if not has_bosses:
            return await interaction.response.edit_message(content="📋 Belum ada data jadwal boss yang aktif.", embed=None, view=None)

        fields_data = []
        for boss, spawn_time in sorted_bosses:
            sisa_waktu = spawn_time - datetime.now(TZ_GMT8)
            wita_str = spawn_time.strftime('%H:%M')
            wib_str = (spawn_time - timedelta(hours=1)).strftime('%H:%M')

            if sisa_waktu.total_seconds() <= 0: # Gunakan <= 0 untuk mencakup waktu spawn yang tepat
                status = f"🟢 **SUDAH SPAWN NOW!**\n└ ⏰ WITA: `{wita_str}` | WIB: `{wib_str}`"
            else:
                jam, sisa = divmod(int(sisa_waktu.total_seconds()), 3600)
                menit, _ = divmod(sisa, 60)
                status = f"⏳ `{jam}j {menit}m lagi`\n└ ⏰ WITA: `{wita_str}` | WIB: `{wib_str}`"
            fields_data.append({"name": f"🔹 {boss.upper()}", "value": status})

        total_pages = (len(fields_data) + 19) // 20
        # Ambil data untuk halaman terakhir agar sinkron dengan posisi tombol
        start_idx = max(0, (total_pages - 1) * 20)
        page = fields_data[start_idx:]
        
        embed = discord.Embed(title=f"🗓️ JADWAL SPAWN BOSS L2M (Halaman {total_pages}/{total_pages})", color=0x5865f2)
        for field in page:
            embed.add_field(name=field["name"], value=field["value"], inline=False)
        embed.set_footer(text=f"Terakhir diperbarui: {datetime.now(TZ_GMT8).strftime('%H:%M:%S')} WITA")
        
        await interaction.response.edit_message(embed=embed, view=self)

# --- JUKEBOX HELPERS & UI ---
def format_time(seconds):
    """Konversi detik ke format MM:SS"""
    if seconds is None: return "00:00"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"

def get_progress_bar(elapsed, duration):
    """Membuat visual progress bar"""
    length = 15
    if not duration: return f"00:00 [{'▬' * length}] 00:00"
    progress = min(max(elapsed / duration, 0), 1)
    filled = int(length * progress)
    bar = "▬" * filled + "🔘" + "▬" * max(0, length - filled - 1)
    return f"{format_time(elapsed)} {bar} {format_time(duration)}"

class MusicControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) # Tombol permanen

    @discord.ui.button(label="⏯️ Play/Pause", style=discord.ButtonStyle.primary, custom_id="music_pp")
    async def play_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc: return await interaction.response.send_message("Bot tidak di Voice Channel.", ephemeral=True)
        global current_track
        
        if vc.is_playing():
            vc.pause()
            if current_track: current_track['pause_time'] = time.time()
            await interaction.response.send_message("⏸️ Musik di-pause.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            if current_track and current_track.get('pause_time'):
                current_track['total_paused'] += (time.time() - current_track['pause_time'])
                current_track['pause_time'] = None
            await interaction.response.send_message("▶️ Musik dilanjutkan.", ephemeral=True)
        else:
            await interaction.response.send_message("Tidak ada musik yang aktif.", ephemeral=True)

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_music(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc:
            global music_queue, current_track
            music_queue.clear()
            current_track = None
            await vc.disconnect()
            await interaction.response.edit_message(content="⏹️ Musik dihentikan & antrean dikosongkan.", embed=None, view=None)
        else:
            await interaction.response.send_message("Bot tidak di Voice Channel.", ephemeral=True)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="music_skip")
    async def skip_music(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop() # Ini akan memicu 'after' untuk memutar lagu berikutnya
            await interaction.response.send_message("⏭️ Melewati lagu...", ephemeral=True)

@tasks.loop(seconds=10)
async def update_music_display():
    """Update Progress Bar pada pesan embed setiap 10 detik"""
    global current_track
    if not current_track or not current_track.get('message'): return
    
    guild = bot.get_guild(current_track['message'].guild.id)
    if not guild or not guild.voice_client: return
    vc = guild.voice_client

    elapsed = 0
    if current_track.get('start_time'):
        if vc.is_paused():
            elapsed = current_track['pause_time'] - current_track['start_time'] - current_track['total_paused']
        else:
            elapsed = time.time() - current_track['start_time'] - current_track['total_paused']
    
    bar = get_progress_bar(elapsed, current_track['duration'])
    embed = current_track['message'].embeds[0]
    embed.set_field_at(0, name="Progres Durasi", value=f"`{bar}`", inline=False)
    
    try: await current_track['message'].edit(embed=embed)
    except: pass

async def play_next(ctx):
    """Fungsi internal untuk memutar lagu berikutnya dalam antrean"""
    global current_track, music_queue
    vc = ctx.voice_client
    if not vc: return

    if not music_queue:
        current_track = None
        return await ctx.send("✅ Antrean selesai. Gunakan `!play` untuk memutar lagi.")

    track_info = music_queue.pop(0)
    title = track_info.get('title', 'Unknown Title')
    duration = track_info.get('duration', 0)
    webpage_url = track_info.get('webpage_url') or track_info.get('url')

    extract_msg = await ctx.send(f"🔄 Menyiapkan pemutaran: **{title}**...")
    try:
        with yt_dlp.YoutubeDL(YDL_DISCORD_OPTS) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, webpage_url, download=False)
            url = info.get('url')
    except Exception as e:
        log.error(f"Gagal mengekstrak URL streaming untuk {title}: {e}")
        await extract_msg.edit(content=f"⚠️ Gagal memutar **{title}** (Error Ekstraksi Link). Melanjutkan...")
        return await play_next(ctx)

    try: await extract_msg.delete()
    except: pass

    if vc.is_playing(): vc.stop()
    
    def after_playing(error):
        if error: log.error(f"Music error: {error}")
        bot.loop.create_task(play_next(ctx))

    vc.play(discord.FFmpegPCMAudio(url, **FFMPEG_OPTIONS), after=after_playing)
    
    embed = discord.Embed(title="🎵 Now Playing", description=f"**{title}**", color=0x1DB954)
    embed.add_field(name="Progres Durasi", value=f"`{get_progress_bar(0, duration)}`", inline=False)
    if music_queue: embed.set_footer(text=f"Antrean: {len(music_queue)} lagu tersisa")

    msg = await ctx.send(embed=embed, view=MusicControlView())
    current_track = {'title': title, 'duration': duration, 'start_time': time.time(), 'pause_time': None, 'total_paused': 0, 'message': msg}


# ==============================================================================
# 🎮 DISCORD HYBRID COMMANDS
# ==============================================================================

@bot.hybrid_command(name="pler", description="Easter egg: PAMAN EKO PLENGER!")
async def pler(ctx):
    # Daftar kata-kata lucu random untuk Paman Eko
    pler_quotes = [
        "Paman Eko lagi nyari orbital, eh malah dapet sandal jepit!",
        "Enchant gagal terus? Coba tumbalin temen clan sebelah.",
        "Lagi war malah kebelet, akhirnya mokad di semak-semak.",
        "Status: Sultan di game, Pas-pasan di real life. Sedih.",
        "Paman Eko pusing lihat harga item merah di market, mending turu.",
        "Skill naga, hoki cacing tanah. Semangat ya ampas!",
        "Jangan lari-lari pas war, nanti dikira maling jemuran Paman Eko.",
        "Looting wangi cuma mitos, ampas adalah kenyataan pahit.",
        "Paman Eko bersabda: 'Gagal enchant itu biasa, yang luar biasa itu kalau berhasil.'"
    ]
    random_pler = random.choice(pler_quotes)

    msg = await ctx.send("🚨 *Mendeteksi energi aneh...* 🚨\n`[|         ] 10%` Menyiapkan Efek...")
    await asyncio.sleep(0.5)
    await msg.edit(content="🌀 *Kepala mulai berputar-putar...* 🌀\n`[|||||    ] 50%` Nyaris Tumbang...")
    await asyncio.sleep(0.5)
    
    embed = discord.Embed(
        title="🤪 EASTER EGG UNLOCKED 🤪",
        description=(
            "# 💥 PAMAN EKO PLENGER 💥\n\n"
            f"### 📣 *\"{random_pler}\"*\n\n"
            "😵💫 *Paman Eko pusing tujuh keliling sampai kayang dan jungkir balik!* 💫😵\n\n"
            "┗(👁️皿👁️)┛  ➡️  (🚫_👁️)  ➡️  ⚖️ 🪵 *KO!*"
        ),
        color=0xffcc00
    )
    embed.set_image(url="https://media.giphy.com/media/26hxX7M5cXUK6xm0w/giphy.gif")
    embed.set_footer(text=f"Dipicu oleh: {ctx.author.display_name}")
    await msg.edit(content=None, embed=embed)

@bot.hybrid_command(name="nasib", description="Cek ramalan keberuntungan bossing kamu hari ini!")
async def nasib(ctx):
    """Easter egg: Memberikan kutipan lucu seputar nasib pemain saat hunting boss."""
    quotes = [
        "Ramalan hari ini: Looting wangi, tapi HP lowbat pas boss sisa 1%. Sabar ya!",
        "Tips: Pukul boss pake perasaan, jangan pake emosi. Biar dropnya cinta, bukan ampas.",
        "Kata Paman Eko: 'Jangan kebanyakan lari pas war, nanti disangka maling jemuran.'",
        "Status: Skill Naga, Looting Cacing. Semangat ya pejuang ampas!",
        "Hari ini kamu bakal dapet loot merah... tapi punya temen sebelah. Wkwk!",
        "Peringatan: Terlalu sering nungguin boss bisa menyebabkan mata panda dan dompet kering.",
        "Motivasi: Loot itu kayak jodoh, ditungguin nggak dateng, ditinggal malah spawn.",
        "Prediksi: Kamu bakal dapet Orb merah, tapi pas dipake malah 'Gagal Enchant'. Sakitnya tuh di sini!"
    ]
    
    embed = discord.Embed(
        title="🔮 RAMALAN DUKUN L2M",
        description=f"### \"{random.choice(quotes)}\"",
        color=0xffd700
    )
    embed.set_footer(text=f"Diramal khusus untuk: {ctx.author.display_name}")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="pantun", description="Easter egg: Mengeluarkan pantun lucu pejuang L2M.")
async def pantun(ctx):
    """Easter egg: Memberikan pantun lucu seputar nasib pemain Lineage 2M secara acak."""
    list_pantun = [
        "Ke pasar beli ikan patin,\nJangan lupa beli terasi.\nLooting boss jangan prihatin,\nDapet ampas itu tradisi.",
        "Jalan-jalan ke kota Medan,\nSinggah sebentar di rumah makan.\nL2M emang bikin kecanduan,\nSampai lupa cari makan.",
        "Paman Eko makan ketupat,\nMakannya sambil lari-lari.\nOrb merah pengen didapat,\nEh malah zonk setiap hari.",
        "Beli baju di pasar loak,\nBaju baru warnanya biru.\nPas war bukannya nembak,\nMalah lari ketemu musuh baru.",
        "Siang-siang minum es kelapa,\nDiminumnya di bawah pohon waru.\nLupa makan itu biasa,\nLupa jam boss itu kiamat baru!"
    ]
    
    pantun_pilihan = random.choice(list_pantun)
    
    embed = discord.Embed(
        title="📜 PANTUN PEJUANG CLAN",
        description=f"```\n{pantun_pilihan}\n```",
        color=0x00ff00
    )
    embed.set_thumbnail(url="https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExNGI5Njg0YjUyZDYyYjRiZDUyYjRiZDUyYjRiZDUyYjRiZDUyYjRiZDUmZXA9djFfaW50ZXJuYWxfZ2lmX2J5X2lkJmN0PWc/3o7TKVUn7iM8FMEU24/giphy.gif")
    embed.set_footer(text=f"Pantun khusus untuk: {ctx.author.display_name}")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="ping", description="🏓 Cek latensi respon bot.")
async def ping(ctx):
    """Melihat kecepatan respon bot (latency)."""
    start_time = time.time()
    msg = await ctx.send("🏓 Pinging...")
    end_time = time.time()
    
    discord_latency = round(bot.latency * 1000)
    api_latency = round((end_time - start_time) * 1000)
    
    embed = discord.Embed(title="🏓 PONG!", color=0x2ecc71)
    embed.add_field(name="🌐 Discord Latency", value=f"`{discord_latency}ms`", inline=True)
    embed.add_field(name="🚀 API Response", value=f"`{api_latency}ms`", inline=True)
    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
    
    await msg.edit(content=None, embed=embed)

@bot.hybrid_command(name="curhat", description="Easter egg: Solusi ngawur saat gagal enchant.")
async def curhat(ctx):
    """Easter egg: Memberikan solusi ngawur saat gagal enchant."""
    curhat_solutions = [
        "Coba enchantnya sambil salto, siapa tahu hoki.",
        "Mungkin kurang sesajen di altar enchant. Coba pakai bunga 7 rupa.",
        "Enchant gagal? Itu tandanya kamu harus ganti HP/PC. Pasti berhasil!",
        "Solusi: Pensiun. Dijamin tidak akan gagal enchant lagi.",
        "Coba bisikin itemnya 'jangan gagal ya, aku sayang kamu'.",
        "Mungkin kamu kurang mandi. Mandi dulu, baru enchant lagi.",
        "Gagal enchant itu bukan salah itemnya, tapi salah jarinya. Coba pakai jari kaki.",
        "Jangan enchant di tempat yang sama. Cari spot hoki di pojokan map.",
        "Solusi paling ampuh: Beli yang sudah jadi di market. Beres!"
    ]
    
    embed = discord.Embed(
        title="💔 SOLUSI GAGAL ENCHANT ALA DUKUN L2M 💔",
        description=f"### \"{random.choice(curhat_solutions)}\"",
        color=0x8b0000 # Warna merah gelap untuk kesan dramatis
    )
    embed.set_footer(text=f"Curhatan dari: {ctx.author.display_name}")
    await ctx.send(embed=embed)

@bot.hybrid_command()
@commands.has_permissions(administrator=True)
async def restart_bot(ctx):
    global telebot_active
    await ctx.send("🔄 **Sedang me-restart bot...**")
    save_data() 
    telebot_active = False
    if telebot_started:
        await asyncio.to_thread(tele_bot.stop_polling)
    await bot.close()
    await asyncio.to_thread(os.execv, sys.executable, ['python'] + sys.argv) # Pastikan ini juga di thread terpisah

@bot.hybrid_command()
async def settz(ctx, tz_choice: str):
    user_id = str(ctx.author.id)
    tz_choice = tz_choice.lower()
    msg = ""
    with db_lock:
        if tz_choice in ["wib", "gmt+7", "gmt7"]:
            user_timezones[user_id] = {"offset": 7, "name": "WIB"}
            msg = f"✅ {ctx.author.mention}, default zona waktu diatur ke **WIB**."
        elif tz_choice in ["wita", "gmt+8", "gmt8", "server"]:
            user_timezones[user_id] = {"offset": 8, "name": "GMT+8 / WITA"}
            msg = f"✅ {ctx.author.mention}, default zona waktu diatur ke **WITA/Server**."
        if msg:
            save_data()
    if msg:
        await ctx.send(msg)

@bot.hybrid_command()
async def kill(ctx, boss_name: str, waktu_mati: str = None):
    global target_channel_id
    target_channel_id = ctx.channel.id
    boss_name = boss_name.lower()
    user_id = str(ctx.author.id)
    
    if boss_name not in BOSS_DATA:
        return await ctx.send(f'❌ Boss **{boss_name}** tidak ditemukan.')

    if waktu_mati is None:
        interval_jam = BOSS_DATA[boss_name]
        sekarang_gmt8 = datetime.now(TZ_GMT8)
        next_spawn = sekarang_gmt8 + timedelta(hours=interval_jam)
        
        with db_lock:
            boss_status[boss_name] = next_spawn
            notif_sent[boss_name] = {"5m": False, "1m": False, "spawn": False}
            boss_logs[boss_name] = {
                "user": ctx.author.display_name,
                "waktu_mati": sekarang_gmt8.strftime("%H:%M (Real-time !kill)"),
                "waktu_input": sekarang_gmt8.strftime("%d-%m %H:%M")
            }
            save_data()
        
        waktu_wita = next_spawn.strftime("%d-%m-%Y %H:%M:%S")
        waktu_wib = (next_spawn - timedelta(hours=1)).strftime("%d-%m-%Y %H:%M:%S")
        
        embed = discord.Embed(title="⚔️ BOSS RECORDED", color=0x33ff33)
        embed.description = (
            f"Boss **{boss_name.upper()}** berhasil dicatat oleh {ctx.author.mention}.\n\n"
            f"⏳ **Spawn Selanjutnya:**\n"
            f"🗓️ WITA: `{waktu_wita} (GMT+8)`\n"
            f"🗓️ WIB: `{waktu_wib} (GMT+7)`"
        )
        await ctx.send(embed=embed)

        # 📢 [CROSS-POST] Kirim info ke Telegram
        if telegram_chat_id:
            tele_msg = (
                f"⚔️ <b>[DISCORD → TELEGRAM]</b>\n"
                f"👤 <b>{ctx.author.display_name}</b> mengetik <code>!kill</code> di Discord\n"
                f"👹 Boss: <code>{boss_name.upper()}</code> (Mati Sekarang)\n"
                f"⏳ Next WITA: <code>{waktu_wita}</code>"
            )
            bot.loop.run_in_executor(None, tele_bot.send_message, telegram_chat_id, tele_msg, "HTML")
        return

    match = re.match(r"(\d{1,2})[:.](\d{2})", waktu_mati)
    if match:
        jam = int(match.group(1))
        menit = int(match.group(2))
        with db_lock:
            utz = user_timezones.get(user_id)
        if utz:
            next_spawn = await asyncio.to_thread(process_kill_time, boss_name, jam, menit, utz["offset"], utz["name"], ctx.author.display_name)
            waktu_wita = next_spawn.strftime("%d-%m-%Y %H:%M:%S")
            waktu_wib = (next_spawn - timedelta(hours=1)).strftime("%d-%m-%Y %H:%M:%S")
            
            embed = discord.Embed(title="✅ JADWAL DICATAT (AUTO-TZ)", color=0x00ffcc)
            embed.description = (
                f"⚔️ Boss **{boss_name.upper()}** dilaporkan mati jam `{waktu_mati}` ({utz['name']})\n\n"
                f"⏳ **Spawn Selanjutnya:**\n"
                f"🗓️ WITA: `{waktu_wita} (GMT+8)`\n"
                f"🗓️ WIB: `{waktu_wib} (GMT+7)`"
            )
            await ctx.send(embed=embed)

            # 📢 [CROSS-POST] Kirim info ke Telegram
            if telegram_chat_id:
                tele_msg = (
                    f"📝 <b>[DISCORD → TELEGRAM]</b>\n"
                    f"👤 <b>{ctx.author.display_name}</b> menginput lewat teks Discord\n"
                    f"👹 Boss: <code>{boss_name.upper()}</code>\n"
                    f"☠️ Jam Mati: <code>{waktu_mati} ({utz['name']})</code>\n"
                    f"⏳ Next WITA: <code>{waktu_wita}</code>"
                )
                bot.loop.run_in_executor(None, tele_bot.send_message, telegram_chat_id, tele_msg, "HTML")
        else:
            view = TimezoneSelectView(boss_name, jam, menit, ctx.author)
            await ctx.send(f"🤔 Jam **{waktu_mati}** yang dimasukkan {ctx.author.mention} itu berdasarkan zona waktu mana?", view=view)
    else:
        await ctx.send("❌ Format waktu salah. Gunakan format jam seperti `05:04` atau `13.15`")

@bot.hybrid_command(name="jadwal", aliases=['jadwall', 'list', 'daftar'])
async def jadwal(ctx):
    global target_channel_id
    target_channel_id = ctx.channel.id
    with db_lock:
        if not boss_status:
            has_bosses = False
        else:
            has_bosses = True
            sorted_bosses = sorted(boss_status.items(), key=lambda x: x[1])

    if not has_bosses:
        return await ctx.send("📋 Belum ada data jadwal boss yang aktif.")

    fields_data = []
    for boss, spawn_time in sorted_bosses:
        sisa_waktu = spawn_time - datetime.now(TZ_GMT8)
        wita_str = spawn_time.strftime('%H:%M')
        wib_str = (spawn_time - timedelta(hours=1)).strftime('%H:%M')

        if sisa_waktu.total_seconds() <= 0: # Gunakan <= 0 untuk mencakup waktu spawn yang tepat
            status = f"🟢 **SUDAH SPAWN NOW!**\n└ ⏰ WITA: `{wita_str}` | WIB: `{wib_str}`"
        else:
            jam, sisa = divmod(int(sisa_waktu.total_seconds()), 3600)
            menit, _ = divmod(sisa, 60)
            status = f"⏳ `{jam}j {menit}m lagi`\n└ ⏰ WITA: `{wita_str}` | WIB: `{wib_str}`"
        fields_data.append({"name": f"🔹 {boss.upper()}", "value": status})

    pages = [fields_data[i:i + 20] for i in range(0, len(fields_data), 20)]
    for index, page in enumerate(pages):
        embed = discord.Embed(title=f"🗓️ JADWAL SPAWN BOSS L2M (Halaman {index + 1}/{len(pages)})", color=0x5865f2)
        for field in page:
            embed.add_field(name=field["name"], value=field["value"], inline=False)
        embed.set_footer(text="Zona Waktu: WITA (GMT+8) & WIB (GMT+7)")
        if index == len(pages) - 1:
            await ctx.send(embed=embed, view=RefreshJadwalView())
        else:
            await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    """Menyingkronkan Slash Commands secara manual (Cegah Rate Limit)"""
    synced = await bot.tree.sync()
    await ctx.send(f"🔄 Berhasil menyinkronkan {len(synced)} Slash Commands ke Discord.")

@bot.hybrid_command()
async def logs(ctx):
    with db_lock:
        if not boss_logs:
            has_logs = False
        else:
            has_logs = True
            logs_data = list(boss_logs.items())

    if not has_logs:
        return await ctx.send("📋 Belum ada riwayat pelaporan boss hari ini.")
    pages = [logs_data[i:i + 20] for i in range(0, len(logs_data), 20)]
    for index, page in enumerate(pages):
        embed = discord.Embed(title=f"📜 RIWAYAT INPUT / RECORD BOSS CLAN ({index+1}/{len(pages)})", color=0xffa500)
        for boss, data in page:
            info = f"👤 **Oleh:** {data['user']}\n☠️ **Jam Mati:** `{data['waktu_mati']}`\n📅 **Waktu Input:** `{data['waktu_input']}`"
            embed.add_field(name=f"⚔️ {boss.upper()}", value=info, inline=False)
        embed.set_footer(text="Database Aman di Lokal JSON.")
        await ctx.send(embed=embed)

@bot.hybrid_command(name="resetjadwal", aliases=['reset'])
@commands.has_permissions(administrator=True)
async def resetjadwal(ctx, boss_name: str = None):
    if boss_name is None:
        return await ctx.send("❌ Masukkan nama boss atau `!reset all`.")
    boss_name = boss_name.lower()
    if boss_name == "all":
        with db_lock:
            boss_status.clear()
            notif_sent.clear()
            boss_logs.clear()
            save_data()
        return await ctx.send(f'🗑️ **[RESET TOTAL]** Semua jadwal dibersihkan oleh {ctx.author.mention}!')

    with db_lock:
        found = boss_name in boss_status
        if found:
            del boss_status[boss_name]
            if boss_name in notif_sent: del notif_sent[boss_name]
            if boss_name in boss_logs: del boss_logs[boss_name]
            save_data()

    if found:
        await ctx.send(f'🗑️ Jadwal boss **{boss_name.upper()}** dihapus oleh {ctx.author.mention}.')
    else:
        await ctx.send(f'❌ Boss **{boss_name.upper()}** tidak ditemukan.')

# --- ERROR HANDLER UNTUK RESETJADWAL ---
@resetjadwal.error
async def resetjadwal_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Maaf, hanya **Administrator** yang diizinkan mereset jadwal boss.", ephemeral=True)

@bot.hybrid_command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int = 5):
    await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f'🧹 **{amount}** pesan telah dibersihkan.', delete_after=3)

# --- 🎵 DISCORD JUKEBOX HYBRID COMMANDS ---
@bot.hybrid_command(name="play", description="🎵 Putar musik dari YouTube di Voice Channel")
async def play(ctx, *, search: str):
    global music_queue
    if not ctx.author.voice:
        return await ctx.send("❌ Kamu harus masuk ke Voice Channel terlebih dahulu!")

    vc = ctx.voice_client or await ctx.author.voice.channel.connect()
    if vc.channel != ctx.author.voice.channel: # Pindahkan bot jika sudah di VC lain
        await vc.move_to(ctx.author.voice.channel)

    msg = await ctx.send(f"🔍 Mencari `{search}` di YouTube...")
    try:
        with yt_dlp.YoutubeDL(YDL_DISCORD_OPTS) as ydl:
            info = await asyncio.to_thread(ydl.extract_info, search, download=False)
            
            if 'entries' in info: # Jika playlist
                # Jika playlist, masukkan semua entri
                entries = [e for e in info['entries'] if e]
                if len(entries) > 1:
                    music_queue.extend(entries)
                    await msg.edit(content=f"✅ Menambahkan **{len(entries)}** lagu dari playlist ke antrean.")
                else:
                    music_queue.append(entries[0])
                    await msg.edit(content=f"📝 Menambahkan ke antrean: `{entries[0]['title']}`")
            else:
                music_queue.append(info)
                await msg.edit(content=f"📝 Menambahkan ke antrean: `{info['title']}`")

        if not vc.is_playing() and not vc.is_paused(): # Mulai putar jika bot sedang diam
            # Mulai putar jika bot sedang diam
            if msg: await msg.delete()
            await play_next(ctx)

    except Exception as e:
        await msg.edit(content=f"⚠️ Gagal memutar lagu: {e}")

@bot.hybrid_command(name="stop", description="⏹️ Hentikan musik dan keluarkan bot dari VC")
async def stop(ctx):
    global music_queue, current_track
    if ctx.voice_client:
        music_queue.clear()
        current_track = None
        await ctx.voice_client.disconnect()
        await ctx.send("⏹️ Musik dihentikan & antrean dibersihkan, bot keluar dari Voice Channel.", ephemeral=True)
    else:
        await ctx.send("❌ Bot tidak berada di Voice Channel.")

@bot.hybrid_command(name="queue", description="📜 Lihat daftar lagu dalam antrean")
async def queue(ctx):
    """Lihat daftar lagu yang sedang diputar dan 10 lagu berikutnya dalam antrean"""
    global music_queue, current_track
    
    if not current_track and not music_queue:
        return await ctx.send("📭 Antrean musik kosong.")

    embed = discord.Embed(title="🎶 Antrean Jukebox Clan", color=0x1DB954)
    
    # 1. Menampilkan lagu yang sedang diputar
    if current_track:
        elapsed = 0
        vc = ctx.voice_client
        # Hitung waktu berjalan saat ini untuk tampilan antrean
        if vc and current_track.get('start_time'):
            if vc.is_paused():
                elapsed = current_track['pause_time'] - current_track['start_time'] - current_track['total_paused']
            else:
                elapsed = time.time() - current_track['start_time'] - current_track['total_paused']
        
        bar = get_progress_bar(elapsed, current_track['duration'])
        embed.add_field(name="▶️ Sedang Diputar", value=f"**{current_track['title']}**\n`{bar}`", inline=False)

    # 2. Menampilkan 10 lagu berikutnya dalam list
    if music_queue:
        queue_list = ""
        for i, track in enumerate(music_queue[:10], 1):
            title = track.get('title', 'Judul Tidak Diketahui')
            duration = format_time(track.get('duration', 0)) # Format durasi
            queue_list += f"`{i}.` {title} (`{duration}`)\n"
        
        embed.add_field(name=f"📋 Antrean Selanjutnya ({len(music_queue)} lagu)", value=queue_list, inline=False)
        if len(music_queue) > 10:
            embed.set_footer(text=f"Menampilkan 10 dari total {len(music_queue)} lagu.")
    else:
        if current_track:
            embed.add_field(name="📋 Antrean Selanjutnya", value="Tidak ada lagu lagi di antrean.", inline=False)

    await ctx.send(embed=embed)


# ==============================================================================
# ✈️ TELEGRAM COMMANDS & AUDIO JUKEBOX SYSTEM (PREMIUM HTML LAYOUT)
# ==============================================================================

@tele_bot.message_handler(commands=['start', 'help'])
def telegram_help(message):
    global telegram_chat_id
    telegram_chat_id = message.chat.id # Simpan ID chat untuk notifikasi
    save_data()
    help_text = (
        "🤖 <b>SISTEM DIKENDALIKAN (CROSS-PLATFORM)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 <b>Daftar Perintah Telegram:</b>\n"
        " ├ <code>/boss</code> atau <code>/jadwal</code> - Cek estimasi jadwal\n"
        " ├ <code>/kill [nama_boss]</code> - Record mati sekarang\n"
        " ├ <code>/kill [nama_boss] [jam]</code> - Record jam kustom (Contoh: <code>/kill medusa 09:45</code>)\n"
        " ├ <code>/play [judul lagu]</code> - Unduh & Kirim Musik dari YT\n"
        " ├ <code>/stats</code> atau <code>!stats</code> - Statistik performa bot\n"
        " ├ <code>/ping</code> - Cek koneksi bot\n"
        " └ <code>/backup</code> atau <code>!backup</code> - Backup manual ke Drive\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📡 <i>ID Chat terdaftar sebagai penerima broadcast notifikasi.</i>"
    )
    tele_bot.reply_to(message, help_text, parse_mode="HTML")

@tele_bot.message_handler(commands=['boss', 'list', 'jadwal', 'daftar'])
def telegram_list(message):
    with db_lock:
        if not boss_status:
            has_bosses = False
        else:
            has_bosses = True
            sorted_bosses = sorted(boss_status.items(), key=lambda x: x[1])

    if not has_bosses:
        no_data_text = ( # Pesan jika tidak ada data boss
            "🦅 <b>MONITORING JADWAL BOSS L2M</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📭 <i>Belum ada data jadwal boss yang aktif saat ini.</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>Ketik <code>/kill [nama_boss]</code> untuk merekam data kematian boss baru.</i>"
        )
        tele_bot.reply_to(message, no_data_text, parse_mode="HTML")
        return

    spawned_bosses = []
    upcoming_bosses = []
    now = datetime.now(TZ_GMT8)

    # Kelompokkan boss secara otomatis
    for boss, spawn_time in sorted_bosses:
        sisa_waktu = spawn_time - now
        wita_str = spawn_time.strftime('%H:%M') # Format waktu WITA
        wib_str = (spawn_time - timedelta(hours=1)).strftime('%H:%M') # Format waktu WIB
        
        if sisa_waktu.total_seconds() <= 0:
            # Grup 1: Sudah Lewat / Sedang Spawn (warna hijau)
            line = f"⚔️ <b>{boss.upper()}</b>\n└ ⏱️ WITA: <code>{wita_str}</code>  |  WIB: <code>{wib_str}</code>"
            spawned_bosses.append(line)
        else:
            # Grup 2: Menunggu Spawn (Hitungan Mundur)
            jam, sisa = divmod(int(sisa_waktu.total_seconds()), 3600)
            menit, _ = divmod(sisa, 60)
            line = f"👹 <b>{boss.upper()}</b>\n├ ⏳ Sisa: <code>{jam:02d}j {menit:02d}m lagi</code>\n└ ⏱️ WITA: <code>{wita_str}</code>  |  WIB: <code>{wib_str}</code>"
            upcoming_bosses.append(line)

    # Merakit struktur template pesan agar elegan
    text_lines = []
    text_lines.append("🦅 <b>MONITORING SPAWN BOSS L2M</b>") # Header pesan
    text_lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    if spawned_bosses:
        text_lines.append("🟢 <b>LIVE / SPAWN NOW:</b>")
        text_lines.append("\n\n".join(spawned_bosses))
        text_lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    if upcoming_bosses:
        text_lines.append("⏳ <b>UPCOMING SPAWN:</b>")
        text_lines.append("\n\n".join(upcoming_bosses))
        text_lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    text_lines.append("💬 <i>Gunakan /kill untuk update data kematian.</i>")

    final_text = "\n".join(text_lines)
    tele_bot.reply_to(message, final_text, parse_mode="HTML")

@tele_bot.message_handler(commands=['addboss'])
def telegram_addboss(message):
    args = message.text.split()
    if len(args) != 3:
        tele_bot.reply_to(message, "⚠️ Format: <code>/addboss [nama_boss] [interval_jam]</code>\nContoh: <code>/addboss orfen 24</code>", parse_mode="HTML")
        return
    
    boss_name = args[1].lower()
    try:
        interval = int(args[2])
    except ValueError:
        tele_bot.reply_to(message, "⚠️ Interval harus berupa angka.", parse_mode="HTML")
        return

    # Update in memory
    BOSS_DATA[boss_name] = interval
    
    # Save to file
    try:
        with open("boss_data.py", "w", encoding="utf-8") as f:
            f.write("# boss_data.py\n# File ini HANYA berisi data boss. Dipisah agar bisa di-import\n# oleh bot.py maupun app.py tanpa konflik.\n\n")
            f.write("BOSS_DATA = " + repr(BOSS_DATA) + "\n")
        tele_bot.reply_to(message, f"✅ Boss <b>{boss_name}</b> dengan interval <b>{interval} jam</b> berhasil ditambahkan!", parse_mode="HTML")
    except Exception as e:
        tele_bot.reply_to(message, f"❌ Gagal menyimpan data boss: {str(e)}", parse_mode="HTML")

@tele_bot.message_handler(commands=['kill'])
def telegram_kill(message):
    global telegram_chat_id
    with db_lock:
        telegram_chat_id = message.chat.id # Simpan ID chat untuk notifikasi
        save_data()
    
    args = message.text.replace('/kill', '').strip().split()
    if not args:
        tele_bot.reply_to(message, "⚠️ Format: <code>/kill [nama_boss]</code> atau <code>/kill [nama_boss] [jam]</code>", parse_mode="HTML")
        return
        
    boss_name = args[0].lower()
    if boss_name not in BOSS_DATA:
        tele_bot.reply_to(message, f"❌ Boss <b>{boss_name}</b> tidak terdaftar dalam database.", parse_mode="HTML")
        return
        
    author_name = message.from_user.first_name or "User Telegram" # Nama pelapor
    
    if len(args) == 1:
        interval_jam = BOSS_DATA[boss_name]
        sekarang_gmt8 = datetime.now(TZ_GMT8)
        next_spawn = sekarang_gmt8 + timedelta(hours=interval_jam)
        
        with db_lock:
            boss_status[boss_name] = next_spawn
            notif_sent[boss_name] = {"5m": False, "1m": False, "spawn": False}
            boss_logs[boss_name] = {
                "user": f"{author_name} (Telegram)",
                "waktu_mati": sekarang_gmt8.strftime("%H:%M (Real-time Tele)"),
                "waktu_input": sekarang_gmt8.strftime("%d-%m %H:%M")
            }
            save_data()
        
        waktu_wita = next_spawn.strftime("%d-%m-%Y %H:%M:%S")
        waktu_wib = (next_spawn - timedelta(hours=1)).strftime("%d-%m-%Y %H:%M:%S")
        
        res = (
            f"🚨 <b>Laporan Kill Sukses via Telegram!</b>\n"
            f"👹 Boss: <code>{boss_name.upper()}</code>\n\n"
            f"⏳ <b>Estimasi Bangkit:</b>\n"
            f" ├ ⏳ WITA: <code>{waktu_wita}</code>\n"
            f" └ 🔹 WIB: <code>{waktu_wib}</code>"
        )
        tele_bot.reply_to(message, res, parse_mode="HTML")

        # 📢 [CROSS-POST] Kirim Info dari Telegram masuk ke Discord
        if target_channel_id:
            channel = bot.get_channel(target_channel_id)
            if channel:
                embed = discord.Embed(title="✈️ TELEGRAM → DISCORD REWRITE", color=0x33ff33)
                embed.description = (
                    f"👹 Boss **{boss_name.upper()}** berhasil dicatat oleh **{author_name}** via Telegram (Mati Sekarang).\n\n"
                    f"⏳ **Spawn Selanjutnya:**\n"
                    f"🗓️ WITA: `{waktu_wita}`\n"
                    f"🗓️ WIB: `{waktu_wib}`"
                )
                asyncio.run_coroutine_threadsafe(channel.send(embed=embed), bot.loop)
    else:
        waktu_mati = args[1]
        match = re.match(r"(\d{1,2})[:.](\d{2})", waktu_mati)
        if match:
            jam = int(match.group(1)) # Ambil jam dan menit
            menit = int(match.group(2))
            next_spawn = process_kill_time(boss_name, jam, menit, 8, "WITA/Server", f"{author_name} (Telegram)")
            waktu_wita = next_spawn.strftime("%d-%m-%Y %H:%M:%S")
            waktu_wib = (next_spawn - timedelta(hours=1)).strftime("%d-%m-%Y %H:%M:%S")
            
            res = (
                f"✅ <b>JADWAL DICATAT VIA TELEGRAM</b>\n"
                f"⚔️ Boss <b>{boss_name.upper()}</b> mati jam <code>{waktu_mati}</code> (Server/WITA)\n\n"
                f"⏳ <b>Spawn Selanjutnya:</b>\n"
                f" ├ ⏳ WITA: <code>{waktu_wita}</code>\n"
                f" └ 🔹 WIB: <code>{waktu_wib}</code>"
            )
            tele_bot.reply_to(message, res, parse_mode="HTML")

            # 📢 [CROSS-POST] Kirim Info dari Telegram masuk ke Discord
            if target_channel_id:
                channel = bot.get_channel(target_channel_id)
                if channel:
                    embed = discord.Embed(title="✈️ JADWAL DICATAT VIA TELEGRAM", color=0x00ffcc)
                    embed.description = (
                        f"⚔️ Boss **{boss_name.upper()}** dilaporkan mati jam `{waktu_mati}` (Server/WITA) oleh **{author_name}**.\n\n"
                        f"⏳ **Spawn Selanjutnya:**\n"
                        f"🗓️ WITA: `{waktu_wita}`\n"
                        f"🗓️ WIB: `{waktu_wib}`"
                    )
                    asyncio.run_coroutine_threadsafe(channel.send(embed=embed), bot.loop)
        else:
            tele_bot.reply_to(message, "❌ Format waktu salah. Gunakan <code>05:04</code> atau <code>13.15</code>", parse_mode="HTML")

@tele_bot.message_handler(commands=['play'])
def telegram_play(message):
    query = message.text.replace('/play', '').strip()
    if not query:
        tele_bot.reply_to(message, "⚠️ Format: <code>/play [nama lagu/link]</code>", parse_mode="HTML")
        return
        
    status_msg = tele_bot.reply_to(message, f"🔍 Mencari & mendownload `{query}` dari YouTube...") # Pesan status
    try:
        if not os.path.exists("music_downloads"):
            os.makedirs("music_downloads")
            
        with yt_dlp.YoutubeDL(YDL_TELEGRAM_OPTS) as ydl:
            info = ydl.extract_info(query, download=True)
            if 'entries' in info: info = info['entries'][0]
            raw_path = ydl.prepare_filename(info)
            file_path = f"{os.path.splitext(raw_path)[0]}.m4a"

        if os.path.exists(file_path):
            with open(file_path, 'rb') as audio:
                tele_bot.send_audio(message.chat.id, audio, caption=f"🎵 Berhasil diunduh!", timeout=300)
            os.remove(file_path)
            tele_bot.delete_message(message.chat.id, status_msg.message_id)
        else:
            tele_bot.edit_message_text("❌ File gagal diekstrak.", message.chat.id, status_msg.message_id)
    except Exception as e:
        tele_bot.edit_message_text(f"⚠️ Kesalahan sistem Jukebox: {e}", message.chat.id, status_msg.message_id)

@tele_bot.message_handler(commands=['backup'])
@tele_bot.message_handler(func=lambda m: m.text == '!backup')
def telegram_manual_backup(message):
    tele_bot.reply_to(message, 'ℹ️ <b>Fitur backup ke Google Drive dinonaktifkan.</b>', parse_mode='HTML')
@tele_bot.message_handler(commands=['stats'])
@tele_bot.message_handler(func=lambda m: m.text == "!stats")
def telegram_stats(message):
    """Melihat statistik performa bot (Uptime & RAM).""" # Statistik bot
    process = psutil.Process(os.getpid())
    mem_info = process.memory_info().rss / (1024 * 1024)  # Konversi ke MB
    
    uptime_seconds = int(time.time() - BOT_START_TIME)
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    uptime_str = f"{days} hari, {hours} jam, {minutes} menit"
    if days == 0:
        uptime_str = f"{hours}j {minutes}m {seconds}s"

    res = (
        "📊 <b>STATISTIK PERFORMA BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ <b>Uptime:</b> <code>{uptime_str}</code>\n"
        f"💾 <b>RAM Usage:</b> <code>{mem_info:.2f} MB</code>\n"
        f"🧵 <b>Threads:</b> <code>{threading.active_count()}</code>\n"
        f"👹 <b>Boss Aktif:</b> <code>{len(boss_status)}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Status System:</b> 🟢 <b>ONLINE</b>"
    )
    tele_bot.reply_to(message, res, parse_mode="HTML")

@tele_bot.message_handler(commands=['ping'])
def telegram_ping(message):
    """Cek latensi Telegram."""
    start_time = time.time()
    msg = tele_bot.reply_to(message, "🏓 <b>Pinging...</b>", parse_mode="HTML")
    end_time = time.time()
    
    diff = round((end_time - start_time) * 1000)
    tele_bot.edit_message_text(f"🏓 <b>PONG!</b>\n⏱ Latensi: <code>{diff}ms</code>", 
                             message.chat.id, msg.message_id, parse_mode="HTML")

def run_telegram_polling():
    print("🤖 Polling Telegram Aktif Terkendali...")
    while telebot_active:
        try:
            try:
                tele_bot.remove_webhook()
            except Exception:
                pass

            if hasattr(tele_bot, "skip_pending"):
                tele_bot.skip_pending = True

            tele_bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except telebot.apihelper.ApiTelegramException as e:
            if not telebot_active:
                break
            error_code = None
            try:
                error_code = e.result_json.get("error_code") if hasattr(e, "result_json") and e.result_json else None
            except Exception:
                error_code = None
            if error_code == 409 or "Conflict" in str(e):
                log.info("ℹ️ Telegram: Konflik getUpdates terdeteksi, menunggu sebelum mencoba ulang...")
                time.sleep(10)
                continue
            log.warning(f"⚠️ Telegram polling terhenti, mencoba ulang: {e}")
            time.sleep(5)
        except Exception as e:
            if not telebot_active:
                break
            log.warning(f"⚠️ Telegram polling terhenti, mencoba ulang: {e}")
            time.sleep(5)

# ==============================================================================
# 🕒 BACKGROUND TASK LOOP SYSTEM (BROADCAST TEXT ONLY)
# ==============================================================================

@tasks.loop(seconds=1.0)
async def check_boss_timer():
    global target_channel_id, last_db_mtime
    
    # 🔄 Sinkronisasi otomatis jika file bot_data.json diubah oleh proses lain
    if os.path.exists(DATA_FILE):
        try: # Pengecekan modifikasi file
            current_mtime = os.path.getmtime(DATA_FILE)
            if current_mtime > last_db_mtime:
                log.info(f"🔄 Sinkronisasi: Perubahan eksternal terdeteksi pada {DATA_FILE}. Memuat ulang data...")
                await asyncio.to_thread(load_data)
        except Exception as e:
            log.error(f"⚠️ Error saat pengecekan modifikasi file: {e}")

    if target_channel_id is None: return

    channel = bot.get_channel(target_channel_id)
    if channel is None:
        try: channel = await bot.fetch_channel(target_channel_id)
        except: return

    now = datetime.now(TZ_GMT8)
    loop = asyncio.get_running_loop()
    
    with db_lock:
        boss_status_copy = list(boss_status.items())
    
    for boss, spawn_time in boss_status_copy:
        sisa_detik = (spawn_time - now).total_seconds()

        with db_lock:
            if boss not in notif_sent:
                sudah_lewat = sisa_detik <= 0
                notif_sent[boss] = {"5m": sudah_lewat, "1m": sudah_lewat, "spawn": sudah_lewat}
            is_5m_sent = notif_sent[boss]["5m"]
            is_1m_sent = notif_sent[boss]["1m"]
            is_spawn_sent = notif_sent[boss]["spawn"]

        # ⚠️ Pengingat Teks 5 Menit
        if 240 < sisa_detik <= 300 and not is_5m_sent:
            with db_lock:
                notif_sent[boss]["5m"] = True
                save_data()
            await channel.send(f'⚠️ **[5 MENIT]** **{boss.upper()}** akan muncul!')
            if telegram_chat_id:
                tele_msg = f"⚠️ <b>[5 MENIT]</b> Boss <b>{boss.upper()}</b> akan segera muncul!"
                loop.run_in_executor(None, tele_bot.send_message, telegram_chat_id, tele_msg, "HTML")

        # 🚨 Pengingat Teks 1 Menit (Menggunakan rentang yang lebih aman)
        elif 0 < sisa_detik <= 60 and not is_1m_sent:
            with db_lock:
                notif_sent[boss]["1m"] = True
                save_data()
            await channel.send(f'🚨 **[1 MENIT]** **{boss.upper()}** merapat!')
            if telegram_chat_id:
                tele_msg = f"🚨 <b>[1 MENIT]</b> Boss <b>{boss.upper()}</b> merapat!"
                loop.run_in_executor(None, tele_bot.send_message, telegram_chat_id, tele_msg, "HTML")

        # 🟢 Pengingat Saat Spawn Teks (0 Detik)
        elif sisa_detik <= 0 and not is_spawn_sent:
            with db_lock:
                notif_sent[boss]["spawn"] = True
                save_data()
            view = BossKillView(boss)
            
            try:
                msg = await channel.send(f'🟢 **{boss.upper()} SPAWN!** @everyone', view=view)
                view.message = msg
            except discord.HTTPException as e:
                print(f"Gagal mengirim pesan spawn: {e}")
            
            if telegram_chat_id:
                tele_msg = f"🟢 <b>{boss.upper()} SPAWN NOW!</b> 🔥"
                loop.run_in_executor(None, lambda: tele_bot.send_message(telegram_chat_id, tele_msg, parse_mode="HTML"))

@tasks.loop(hours=6.0)
async def system_heartbeat():
    """Task rutin untuk memastikan bot masih hidup dan terpantau di Telegram"""
    if telegram_chat_id:
        # Cek Sisa Disk secara cross-platform
        import platform
        disk_path = 'E:' if platform.system() == 'Windows' else '/'
        try:
            disk = psutil.disk_usage(disk_path)
            disk_free = round(disk.free / (1024**3), 2) # GB
        except Exception as e:
            disk_free = "N/A"
            log.warning(f"Gagal mengambil info disk: {e}")
        
        waktu_sekarang = datetime.now(TZ_GMT8).strftime("%d-%m %H:%M:%S")
        with db_lock:
            boss_count = len(boss_status)
        msg = (
            f"💓 <b>SYSTEM HEARTBEAT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 Waktu: <code>{waktu_sekarang} WITA</code>\n"
            f"✅ Status: <b>Online & Monitoring</b>\n"
            f"💾 Database: <code>{boss_count} Boss Aktif</code>\n"
            f"💽 Sisa Disk: <code>{disk_free} GB</code>"
        )
        try:
            bot.loop.run_in_executor(None, lambda: tele_bot.send_message(telegram_chat_id, msg, parse_mode="HTML"))
        except: pass


# ==============================================================================

# ==============================================================================
# 🚀 CORE ENGINE GATEWAY LIFECYCLE
# ==============================================================================

@bot.event
async def on_ready():
    global telebot_started, data_loaded
    
    if not data_loaded:
        await asyncio.to_thread(load_data) # Load data di thread terpisah
        data_loaded = True

    bot.add_view(RefreshJadwalView())
    bot.add_view(MusicControlView())
    log.info(f'Bot {bot.user.name} online! Menunggu event dan sinkronisasi file...')
    
    # --- UPDATE DISCORD PRESENCE ---
    activity = discord.Game(name="Lineage 2M | !help", type=3)
    await bot.change_presence(status=discord.Status.online, activity=activity)

    # Sinkronisasi slash commands sekali saja saat start
    try: await bot.tree.sync()
    except: pass

    if not check_boss_timer.is_running():
        check_boss_timer.start()

    if not update_music_display.is_running():
        update_music_display.start()

    if not system_heartbeat.is_running():
        system_heartbeat.start()

    if not telebot_started:
        log.info("🤖 Memulai thread Telegram Polling...")
        t = threading.Thread(target=run_telegram_polling, daemon=True)
        t.start()
        telebot_started = True

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)