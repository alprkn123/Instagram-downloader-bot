import os
import asyncio
import logging
import shutil
import time
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import instaloader
import aiohttp
import yt_dlp

# Konfigurasi logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Konfigurasi bot
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
DOWNLOAD_DIR = "downloads"
AUDIO_DIR = "audio"
TRANSCRIPT_DIR = "transcripts"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
MAX_CONCURRENT_DOWNLOADS = 1  # Kurangi untuk hemat RAM
CLEANUP_INTERVAL = 300  # 5 menit auto cleanup

# Inisialisasi folder
for dir_path in [DOWNLOAD_DIR, AUDIO_DIR, TRANSCRIPT_DIR]:
    Path(dir_path).mkdir(exist_ok=True)

# Rate limiting
user_last_request = {}

# Inisialisasi instaloader dengan konfigurasi hemat memori
L = instaloader.Instaloader(
    download_videos=True,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
    max_connection_attempts=2,
    post_metadata_txt_pattern="",
    max_managed_parallel_jobs=1
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk perintah /start"""
    await update.message.reply_text(
        "🎥 **Instagram Downloader + Transkrip Bot**\n\n"
        "Kirim link Instagram (post/reel/foto) untuk:\n"
        "✅ Download video/foto\n"
        "✅ Transkrip otomatis (untuk video)\n"
        "✅ Auto hapus file setelah dikirim\n\n"
        "Contoh: https://www.instagram.com/p/Cxample/\n"
        "        https://www.instagram.com/reel/Cxample/"
    )

async def extract_audio(video_path: str) -> Optional[str]:
    """Ekstrak audio dari video menggunakan ffmpeg"""
    try:
        audio_path = video_path.replace('.mp4', '.wav').replace(DOWNLOAD_DIR, AUDIO_DIR)
        os.makedirs(os.path.dirname(audio_path), exist_ok=True)
        
        # Gunakan ffmpeg untuk ekstrak audio
        cmd = [
            'ffmpeg', '-i', video_path,
            '-vn',  # No video
            '-acodec', 'pcm_s16le',  # Format WAV
            '-ar', '16000',  # Sample rate 16kHz
            '-ac', '1',  # Mono channel
            '-y',  # Overwrite
            audio_path
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
            return audio_path
        return None
        
    except Exception as e:
        logger.error(f"Audio extraction error: {e}")
        return None

async def transcribe_audio(audio_path: str) -> Optional[str]:
    """Transkrip audio menggunakan Vosk (offline, hemat RAM)"""
    try:
        transcript_path = audio_path.replace('.wav', '.txt').replace(AUDIO_DIR, TRANSCRIPT_DIR)
        os.makedirs(os.path.dirname(transcript_path), exist_ok=True)
        
        # Gunakan Vosk via subprocess (lebih hemat RAM daripada load model di memory utama)
        cmd = [
            'python3', '-c', f"""
import json
import sys
from vosk import Model, KaldiRecognizer
import wave

try:
    # Load model small (40MB) untuk hemat RAM
    model = Model("models/vosk-model-small-en-us-0.15")
    wf = wave.open("{audio_path}", "rb")
    rec = KaldiRecognizer(model, wf.getframerate())
    rec.SetWords(True)
    
    results = []
    while True:
        data = wf.readframes(4000)
        if len(data) == 0:
            break
        if rec.AcceptWaveform(data):
            result = json.loads(rec.Result())
            if 'text' in result:
                results.append(result['text'])
    
    final = json.loads(rec.FinalResult())
    if 'text' in final:
        results.append(final['text'])
    
    with open("{transcript_path}", "w") as f:
        f.write(' '.join(results))
    print("SUCCESS")
except Exception as e:
    print(f"ERROR: {{e}}")
"""
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if "SUCCESS" in stdout.decode() and os.path.exists(transcript_path):
            with open(transcript_path, 'r') as f:
                transcript = f.read().strip()
            return transcript if transcript else None
            
    except Exception as e:
        logger.error(f"Transcription error: {e}")
    
    return None

async def download_instagram_media(post_url: str) -> List[str]:
    """Download konten Instagram"""
    downloaded_files = []
    
    try:
        # Ekstrak shortcode
        if "/p/" in post_url:
            shortcode = post_url.split("/p/")[-1].split("/")[0]
        elif "/reel/" in post_url:
            shortcode = post_url.split("/reel/")[-1].split("/")[0]
        else:
            return []
        
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        if post.typename == "GraphImage":
            # Single image
            filename = f"{DOWNLOAD_DIR}/{shortcode}.jpg"
            L.download_pic(filename=filename, url=post.url, mtime=None)
            downloaded_files.append(filename)
            
        elif post.typename == "GraphVideo":
            # Video
            filename = f"{DOWNLOAD_DIR}/{shortcode}.mp4"
            async with aiohttp.ClientSession() as session:
                async with session.get(post.video_url) as resp:
                    if resp.status == 200:
                        with open(filename, 'wb') as f:
                            async for chunk in resp.content.iter_chunked(8192):
                                f.write(chunk)
                        downloaded_files.append(filename)
                        
        elif post.typename == "GraphSidecar":
            # Multiple items
            for idx, node in enumerate(post.get_sidecar_nodes()):
                if node.is_video:
                    filename = f"{DOWNLOAD_DIR}/{shortcode}_{idx}.mp4"
                    async with aiohttp.ClientSession() as session:
                        async with session.get(node.video_url) as resp:
                            if resp.status == 200:
                                with open(filename, 'wb') as f:
                                    async for chunk in resp.content.iter_chunked(8192):
                                        f.write(chunk)
                                    downloaded_files.append(filename)
                else:
                    filename = f"{DOWNLOAD_DIR}/{shortcode}_{idx}.jpg"
                    L.download_pic(filename=filename, url=node.display_url, mtime=None)
                    downloaded_files.append(filename)
        
        return downloaded_files
        
    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        return []

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk pesan masuk"""
    user_id = update.effective_user.id
    url = update.message.text.strip()
    
    # Rate limiting
    current_time = time.time()
    if user_id in user_last_request:
        if current_time - user_last_request[user_id] < 15:  # 15 detik cooldown
            await update.message.reply_text("⏳ Tunggu 15 detik sebelum request lagi")
            return
    
    user_last_request[user_id] = current_time
    
    if not ("instagram.com" in url):
        await update.message.reply_text("❌ Kirim link Instagram yang valid!")
        return
    
    status_msg = await update.message.reply_text("⏬ Mendownload...")
    
    try:
        # Download media
        files = await download_instagram_media(url)
        
        if not files:
            await status_msg.edit_text("❌ Gagal download. Coba link lain.")
            return
        
        # Proses setiap file
        transcript_texts = []
        
        for file_path in files:
            if not os.path.exists(file_path):
                continue
                
            file_size = os.path.getsize(file_path)
            is_video = file_path.endswith('.mp4')
            
            # Cek size limit
            if file_size > MAX_FILE_SIZE:
                await update.message.reply_text(f"⚠️ File terlalu besar: {file_size//1024//1024}MB")
                os.remove(file_path)
                continue
            
            # Untuk video, extract audio dan transkrip
            transcript = None
            if is_video:
                await status_msg.edit_text("🎵 Mengekstrak audio...")
                audio_path = await extract_audio(file_path)
                
                if audio_path:
                    await status_msg.edit_text("📝 Mentranskrip audio...")
                    transcript = await transcribe_audio(audio_path)
                    if transcript:
                        transcript_texts.append(f"**Transkrip:**\n{transcript[:1000]}{'...' if len(transcript) > 1000 else ''}")
                    
                    # Hapus file audio setelah diproses
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
            
            # Kirim file ke user
            await status_msg.edit_text("📤 Mengirim ke Telegram...")
            
            with open(file_path, 'rb') as f:
                if is_video:
                    await update.message.reply_video(
                        video=f,
                        caption="✅ Video Instagram",
                        supports_streaming=True,
                        read_timeout=60,
                        write_timeout=60
                    )
                else:
                    await update.message.reply_photo(
                        photo=f,
                        caption="✅ Foto Instagram",
                        read_timeout=60,
                        write_timeout=60
                    )
            
            # Hapus file video/foto setelah dikirim
            os.remove(file_path)
        
        # Kirim transkrip jika ada
        if transcript_texts:
            for transcript in transcript_texts:
                await update.message.reply_text(
                    transcript,
                    parse_mode='Markdown'
                )
        
        await status_msg.delete()
        
    except Exception as e:
        logger.error(f"Error: {str(e)}")
        await status_msg.edit_text(f"❌ Error: {str(e)[:50]}")
        
    finally:
        # Cleanup semua file tersisa
        for dir_path in [DOWNLOAD_DIR, AUDIO_DIR, TRANSCRIPT_DIR]:
            for file in Path(dir_path).glob("*"):
                if file.is_file():
                    try:
                        file.unlink()
                    except:
                        pass

async def cleanup_task():
    """Periodic cleanup setiap 5 menit"""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL)
            deleted = 0
            
            for dir_path in [DOWNLOAD_DIR, AUDIO_DIR, TRANSCRIPT_DIR]:
                for file in Path(dir_path).glob("*"):
                    if file.is_file():
                        try:
                            file.unlink()
                            deleted += 1
                        except:
                            pass
            
            if deleted > 0:
                logger.info(f"Cleaned {deleted} files")
                
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

async def post_init(application: Application):
    """Jalankan background tasks"""
    asyncio.create_task(cleanup_task())

def setup_vosk_models():
    """Setup Vosk models untuk transkripsi (jalankan sekali)"""
    import subprocess
    import os
    
    models_dir = "models"
    os.makedirs(models_dir, exist_ok=True)
    
    # Download small model (40MB) - cukup untuk 1525MB RAM
    model_url = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
    model_zip = f"{models_dir}/vosk-model-small-en-us-0.15.zip"
    model_dir = f"{models_dir}/vosk-model-small-en-us-0.15"
    
    if not os.path.exists(model_dir):
        print("📥 Downloading Vosk model (40MB)...")
        subprocess.run(f"wget -O {model_zip} {model_url}", shell=True)
        subprocess.run(f"unzip {model_zip} -d {models_dir}", shell=True)
        subprocess.run(f"rm {model_zip}", shell=True)
        print("✅ Vosk model ready")

def main():
    """Main function"""
    # Setup Vosk models
    setup_vosk_models()
    
    # Buat aplikasi
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # Tambah handler
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Bot Instagram + Transkrip siap")
    print(f"RAM: 1525MB | DISK: 1525MB | CPU: 60%")
    print("Fitur: Download + Transkrip + Auto Delete")
    
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        pool_timeout=30
    )

if __name__ == "__main__":
    main()
