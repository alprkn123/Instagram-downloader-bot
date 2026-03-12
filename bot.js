const { Telegraf } = require('telegraf');
const axios = require('axios');
const fs = require('fs');
const path = require('path');
const { exec } = require('child_process');
const util = require('util');
const execPromise = util.promisify(exec);

// Konfigurasi
const BOT_TOKEN = process.env.BOT_TOKEN;
const DOWNLOAD_DIR = './downloads';
const AUDIO_DIR = './audio';
const TRANSCRIPT_DIR = './transcripts';

// Buat folder
[DOWNLOAD_DIR, AUDIO_DIR, TRANSCRIPT_DIR].forEach(dir => {
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
});

const bot = new Telegraf(BOT_TOKEN);

// Rate limiting sederhana
const userLastRequest = new Map();

// Start command
bot.start((ctx) => {
    ctx.reply(
        '🎥 **Instagram Downloader + Transkrip Bot**\n\n' +
        'Kirim link Instagram (post/reel/foto) untuk:\n' +
        '✅ Download video/foto\n' +
        '✅ Transkrip otomatis (untuk video)\n' +
        '✅ Auto hapus file setelah dikirim\n\n' +
        'Contoh: https://www.instagram.com/p/Cxample/\n' +
        '        https://www.instagram.com/reel/Cxample/'
    );
});

// Fungsi download Instagram pake ytdl
async function downloadInstagram(url) {
    const files = [];
    const timestamp = Date.now();
    
    try {
        // Untuk foto/video Instagram
        const outputTemplate = path.join(DOWNLOAD_DIR, `%(title)s_${timestamp}.%(ext)s`);
        
        const ytdl = require('yt-dlp-exec');
        await ytdl(url, {
            output: outputTemplate,
            noPlaylist: true,
            restrictFilenames: true,
        });
        
        // Cek file yang terdownload
        const downloadedFiles = fs.readdirSync(DOWNLOAD_DIR)
            .filter(f => f.includes(timestamp))
            .map(f => path.join(DOWNLOAD_DIR, f));
        
        return downloadedFiles;
    } catch (error) {
        console.error('Download error:', error);
        return [];
    }
}

// Fungsi extract audio pake ffmpeg
async function extractAudio(videoPath) {
    try {
        const audioPath = videoPath
            .replace('.mp4', '.wav')
            .replace(DOWNLOAD_DIR, AUDIO_DIR);
        
        await execPromise(`ffmpeg -i "${videoPath}" -vn -acodec pcm_s16le -ar 16000 -ac 1 -y "${audioPath}"`);
        
        if (fs.existsSync(audioPath) && fs.statSync(audioPath).size > 0) {
            return audioPath;
        }
        return null;
    } catch (error) {
        console.error('Audio extraction error:', error);
        return null;
    }
}

// Fungsi transkrip pake Vosk (pake Python bridge atau API lokal)
async function transcribeAudio(audioPath) {
    try {
        const transcriptPath = audioPath
            .replace('.wav', '.txt')
            .replace(AUDIO_DIR, TRANSCRIPT_DIR);
        
        // Pake script Python buat transkrip (karena Vosk lebih mantep di Python)
        const pythonScript = `
import sys
import json
from vosk import Model, KaldiRecognizer
import wave

try:
    wf = wave.open("${audioPath}", "rb")
    model = Model("models/vosk-model-small-en-us-0.15")
    rec = KaldiRecognizer(model, wf.getframerate())
    
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
    
    with open("${transcriptPath}", "w") as f:
        f.write(' '.join(results))
    print("SUCCESS")
except Exception as e:
    print(f"ERROR: {e}")
`;
        
        const pythonFile = path.join(TRANSCRIPT_DIR, 'temp_transcribe.py');
        fs.writeFileSync(pythonFile, pythonScript);
        
        await execPromise(`python3 ${pythonFile}`);
        fs.unlinkSync(pythonFile);
        
        if (fs.existsSync(transcriptPath)) {
            return fs.readFileSync(transcriptPath, 'utf8');
        }
        return null;
    } catch (error) {
        console.error('Transcription error:', error);
        return null;
    }
}

// Cleanup file lama
function cleanup() {
    const now = Date.now();
    const maxAge = 3600000; // 1 jam
    
    [DOWNLOAD_DIR, AUDIO_DIR, TRANSCRIPT_DIR].forEach(dir => {
        if (fs.existsSync(dir)) {
            fs.readdirSync(dir).forEach(file => {
                const filePath = path.join(dir, file);
                const stats = fs.statSync(filePath);
                if (now - stats.mtimeMs > maxAge) {
                    fs.unlinkSync(filePath);
                }
            });
        }
    });
}

// Handle pesan
bot.on('text', async (ctx) => {
    const userId = ctx.from.id;
    const url = ctx.message.text;
    
    // Rate limiting
    const now = Date.now();
    if (userLastRequest.has(userId)) {
        const lastTime = userLastRequest.get(userId);
        if (now - lastTime < 15000) { // 15 detik
            return ctx.reply('⏳ Tunggu 15 detik sebelum request lagi');
        }
    }
    userLastRequest.set(userId, now);
    
    if (!url.includes('instagram.com')) {
        return ctx.reply('❌ Kirim link Instagram yang valid!');
    }
    
    const statusMsg = await ctx.reply('⏬ Mendownload...');
    
    try {
        // Download
        const files = await downloadInstagram(url);
        
        if (files.length === 0) {
            return ctx.reply('❌ Gagal download. Coba link lain.');
        }
        
        // Proses tiap file
        for (const file of files) {
            const isVideo = file.endsWith('.mp4') || file.endsWith('.mov');
            const fileSize = fs.statSync(file).size;
            
            if (fileSize > 50 * 1024 * 1024) {
                await ctx.reply(`⚠️ File terlalu besar: ${Math.round(fileSize/1024/1024)}MB`);
                fs.unlinkSync(file);
                continue;
            }
            
            // Extract audio & transcribe kalo video
            let transcript = null;
            if (isVideo) {
                await ctx.telegram.editMessageText(
                    ctx.chat.id,
                    statusMsg.message_id,
                    null,
                    '🎵 Mengekstrak audio...'
                );
                
                const audioPath = await extractAudio(file);
                if (audioPath) {
                    await ctx.telegram.editMessageText(
                        ctx.chat.id,
                        statusMsg.message_id,
                        null,
                        '📝 Mentranskrip audio...'
                    );
                    
                    transcript = await transcribeAudio(audioPath);
                    if (fs.existsSync(audioPath)) fs.unlinkSync(audioPath);
                }
            }
            
            // Kirim file
            await ctx.telegram.editMessageText(
                ctx.chat.id,
                statusMsg.message_id,
                null,
                '📤 Mengirim ke Telegram...'
            );
            
            if (isVideo) {
                await ctx.replyWithVideo(
                    { source: file },
                    { caption: '✅ Video Instagram', supports_streaming: true }
                );
            } else {
                await ctx.replyWithPhoto(
                    { source: file },
                    { caption: '✅ Foto Instagram' }
                );
            }
            
            // Hapus file
            fs.unlinkSync(file);
        }
        
        // Hapus status message
        await ctx.telegram.deleteMessage(ctx.chat.id, statusMsg.message_id);
        
    } catch (error) {
        console.error('Error:', error);
        await ctx.reply(`❌ Error: ${error.message.substring(0, 50)}`);
    }
    
    // Cleanup berkala
    cleanup();
});

// Error handling
bot.catch((err, ctx) => {
    console.error('Bot error:', err);
});

// Jalankan bot
bot.launch().then(() => {
    console.log('🤖 Bot Instagram + Transkrip siap');
    console.log('RAM: 1525MB | DISK: 1525MB | CPU: 60%');
    console.log('Fitur: Download + Transkrip + Auto Delete');
});

// Graceful stop
process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));
