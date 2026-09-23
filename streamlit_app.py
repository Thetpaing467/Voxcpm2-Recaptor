import streamlit as st
import os
import time
import ffmpeg
import shutil
import subprocess
from google import genai
from gradio_client import Client, handle_file

# ============================================================
# Config
# ============================================================
LANGUAGE = "Myanmar"
MODEL = "gemini-3.6-flash"
VOXCPM_SPACE = "openbmb/VoxCPM-Demo"
PASSWORD = "voxcpm2026"

# 🆕 Font Setup
FONT_FILE = "akkayar.ttf"
FONT_NAME = "Akkhayar"

# ============================================================
# Password Check
# ============================================================
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("🔐 Private App")
    st.write("Password ထည့်ပါ။")
    pwd = st.text_input("Password", type="password")
    if st.button("Login"):
        if pwd == PASSWORD:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("❌ Password မှားနေပါတယ်။")
    st.stop()

# ============================================================
# Key Manager
# ============================================================
class KeyManager:
    def __init__(self, keys):
        self.keys = [k.strip() for k in keys if k.strip()]
        self.current_index = 0
        self.exhausted = set()

    def get_client(self):
        if not self.keys:
            raise Exception("API Key မထည့်ရသေးပါ။")
        if len(self.exhausted) >= len(self.keys):
            self.exhausted.clear()
            self.current_index = 0
        return genai.Client(api_key=self.keys[self.current_index])

    def rotate(self):
        self.exhausted.add(self.current_index)
        for i in range(len(self.keys)):
            nxt = (self.current_index + 1 + i) % len(self.keys)
            if nxt not in self.exhausted:
                self.current_index = nxt
                return True
        return False

    def remaining(self):
        return len(self.keys) - len(self.exhausted)


def call_gemini(contents, km):
    attempts = 0
    max_total = len(km.keys) * 3 if km.keys else 3
    while attempts < max_total:
        if km.remaining() == 0:
            km.exhausted.clear()
            km.current_index = 0
        client = km.get_client()
        try:
            return client.models.generate_content(model=MODEL, contents=contents)
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                km.rotate()
                attempts += 1
            elif "503" in err or "UNAVAILABLE" in err:
                time.sleep(3)
                attempts += 1
            elif "401" in err or "403" in err or "400" in err:
                km.rotate()
                attempts += 1
            else:
                raise e
    raise Exception("Retry ကုန်ပါပြီ။ Key စစ်ပါ။")

# ============================================================
# Split Script
# ============================================================
def split_script(text, max_chars=400):
    sentences = text.replace("။", "။|").split("|")
    sentences = [s.strip() + "။" for s in sentences if s.strip()]

    chunks = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) <= max_chars:
            current += sentence
        else:
            if current:
                chunks.append(current)
            if len(sentence) > max_chars:
                for i in range(0, len(sentence), max_chars):
                    chunks.append(sentence[i:i + max_chars])
                current = ""
            else:
                current = sentence
    if current:
        chunks.append(current)
    return chunks

# ============================================================
# VoxCPM2 TTS
# ============================================================
def run_tts_chunked(text, output_path, ref_audio_path=None, progress_callback=None):
    client = Client(VOXCPM_SPACE)
    chunks = split_script(text, max_chars=400)
    audio_files = []
    ref_file = handle_file(ref_audio_path) if ref_audio_path else None

    for i, chunk in enumerate(chunks):
        if progress_callback:
            progress_callback(i, len(chunks), chunk)

        result = client.predict(
            text_input=chunk,
            control_instruction="A warm young woman, calm and expressive",
            reference_wav_path_input=ref_file,
            use_prompt_text=False,
            prompt_text_input="",
            cfg_value_input=2.0,
            do_normalize=True,
            denoise=False,
            api_name="/generate",
        )

        if isinstance(result, (tuple, list)):
            audio_path = result[0]
        else:
            audio_path = result

        chunk_path = f"chunk_{i}.wav"
        shutil.copy(audio_path, chunk_path)
        audio_files.append(chunk_path)

    with open("concat_list.txt", "w", encoding="utf-8") as f:
        for audio in audio_files:
            f.write(f"file '{audio}'\n")

    ffmpeg.input("concat_list.txt", format="concat", safe=0).output(
        output_path, acodec="libmp3lame", audio_bitrate="192k", ar=48000
    ).run(overwrite_output=True)

    return output_path

# ============================================================
# 🆕 Script → SRT (Millisecond)
# ============================================================
def script_to_srt(script, audio_path, srt_path):
    """Gemini Script + Audio duration → SRT (millisecond)"""
    audio_dur = float(ffmpeg.probe(audio_path)['format']['duration'])

    sentences = script.replace("။", "။|").split("|")
    sentences = [s.strip() + "။" for s in sentences if s.strip()]

    if not sentences:
        return None

    total_chars = sum(len(s) for s in sentences)
    durations = [(len(s) / total_chars) * audio_dur for s in sentences]

    def ts(seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds - int(seconds)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(srt_path, "w", encoding="utf-8") as f:
        current = 0.0
        for i, (sent, dur) in enumerate(zip(sentences, durations), 1):
            start = current
            end = current + dur
            f.write(f"{i}\n{ts(start)} --> {ts(end)}\n{sent}\n\n")
            current = end

    return srt_path

# ============================================================
# 🆕 Blur Box + Subtitle (subprocess — no map error)
# ============================================================
def burn_subtitle_on_blur(video_path, srt_path, out_path,
                           font_size=26, position="bottom",
                           blur_height=150):
    srt_esc = srt_path.replace("\\", "/").replace(":", "\\:")
    font_dir = os.path.dirname(os.path.abspath(FONT_FILE))

    probe = ffmpeg.probe(video_path)
    vs = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    W, H = int(vs['width']), int(vs['height'])

    box_y = {
        "bottom": H - blur_height,
        "center": (H - blur_height) // 2,
        "top": 0
    }[position]

    alignment = {"bottom": 2, "center": 5, "top": 8}[position]
    margin_v = {"bottom": 30, "center": 0, "top": 30}[position]

    style = (
        f"FontName={FONT_NAME},"
        f"FontSize={font_size},"
        f"PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,"
        f"BorderStyle=1,Outline=2,Shadow=1,"
        f"Alignment={alignment},MarginV={margin_v}"
    )

    filter_complex = (
        f"color=c=black@0.5:s={W}x{blur_height}:d=1[box];"
        f"[0:v][box]overlay=0:{box_y}[blurred];"
        f"[blurred]subtitles='{srt_esc}':force_style='{style}':fontsdir='{font_dir}'[outv]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "medium",
        "-c:a", "copy",
        out_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg error:\n{result.stderr}")

    return out_path

# ============================================================
# UI
# ============================================================
st.set_page_config(page_title="🎬 VoxCPM2 Movie Recap", page_icon="🎬")
st.title("🎬 VoxCPM2 Movie Recap")
st.write("ဗီဒီယို upload တင်ပြီး VoxCPM2 အသံနဲ့ Recap ဖန်တီးပါ")

# ===== Sidebar — Gemini Key =====
st.sidebar.header("🔑 Gemini API Key")
key_input = st.sidebar.text_area(
    "Gemini API Key(s) — comma နဲ့ ခြား",
    placeholder="AQ.Ab8RN6... , AQ.Ab8RN6...",
    height=100
)

if key_input.strip():
    API_KEYS = [k.strip() for k in key_input.split(",") if k.strip()]
else:
    API_KEYS = [k.strip() for k in st.secrets.get("GEMINI_API_KEYS", "").split(",") if k.strip()]

st.sidebar.write(f"🔑 Key: {len(API_KEYS)} ခု")

# ===== Sidebar — Voice Sample =====
st.sidebar.header("🎙️ Voice Sample")

if "ref_audio_path" not in st.session_state:
    st.session_state.ref_audio_path = None

ref_audio = st.sidebar.file_uploader(
    "Reference Audio (၁၀-၁၅ စက္ကန့်) — တစ်ခါပဲ တင်ပါ",
    type=["wav", "mp3", "m4a"]
)

if ref_audio is not None:
    ref_path = "reference_voice.wav"
    with open(ref_path, "wb") as f:
        f.write(ref_audio.read())
    st.session_state.ref_audio_path = ref_path
    st.sidebar.success("✅ အသံ သိမ်းပြီး")

if st.session_state.ref_audio_path:
    st.sidebar.info("🎙️ Clone အသံ ရှိပြီး ✅")
    if st.sidebar.button("🗑️ အသံ ဖျောက်"):
        st.session_state.ref_audio_path = None
        st.rerun()
else:
    st.sidebar.warning("⚠️ Clone လုပ်ချင်ရင် အသံ တင်ပါ")

# ===== 🆕 Sidebar — Subtitle + Blur =====
st.sidebar.header("📝 Subtitle + Blur")

use_subtitle = st.sidebar.toggle("Subtitle ထည့်", value=True)

if use_subtitle:
    sub_position = st.sidebar.selectbox(
        "Blur နေရာ",
        ["bottom", "center", "top"],
        format_func=lambda x: {
            "bottom": "⬇️ အောက်ခြေ",
            "center": "⬅️ အလယ်",
            "top": "⬆️ အပေါ်"
        }[x]
    )
    sub_font_size = st.sidebar.slider("Font Size", 16, 48, 26)
    blur_height = st.sidebar.slider("Blur Box အမြင့်", 80, 300, 150)
else:
    sub_position = "bottom"
    sub_font_size = 26
    blur_height = 150

# ===== Video Upload =====
video_file = st.file_uploader("📹 ဗီဒီယို Upload", type=["mp4", "mov", "avi", "mkv"])

# ===== Main =====
if video_file is not None:
    if st.button("🚀 Generate Recap", type="primary"):
        if len(API_KEYS) == 0:
            st.error("❌ Gemini API Key မထည့်ရသေးပါ။")
            st.stop()

        km = KeyManager(API_KEYS)

        # 1. Video check
        with st.spinner("📹 ဗီဒီယို စစ်ဆေးနေသည်..."):
            video_filename = "input_video.mp4"
            with open(video_filename, "wb") as f:
                f.write(video_file.read())
            probe = ffmpeg.probe(video_filename)
            video_duration = float(probe['format']['duration'])
            st.write(f"📹 အရှည်: {video_duration:.2f} စက္ကန့်")

        # 2. Gemini Script
        with st.spinner("✍️ Gemini → Script ရေးနေသည်..."):
            client = km.get_client()
            gfile = client.files.upload(file=video_filename)
            while gfile.state.name == "PROCESSING":
                time.sleep(3)
                gfile = client.files.get(name=gfile.name)
            prompt = (
                f"Watch this video carefully and write a clear, continuous movie recap script "
                f"in {LANGUAGE} language for audio narration that matches the length of the video. "
                f"Return plain speech text only without markdown titles."
            )
            response = call_gemini([gfile, prompt], km)
            script = response.text.strip()
            st.write(f"✅ Script ({len(script)} စာလုံး)")

        # 3. VoxCPM2 TTS
        st.write("🎙️ VoxCPM2 → အသံ ထုတ်နေသည်...")
        progress_bar = st.progress(0)
        status_text = st.empty()

        def update_progress(i, total, chunk):
            progress_bar.progress((i + 1) / total)
            status_text.write(f"🎙️ [{i+1}/{total}] ({len(chunk)} စာလုံး)")

        audio_path = "recap_voice.mp3"

        try:
            run_tts_chunked(
                script,
                audio_path,
                ref_audio_path=st.session_state.ref_audio_path,
                progress_callback=update_progress
            )
            st.write("✅ အသံ ထုတ်ပြီး")

            audio_dur = float(ffmpeg.probe(audio_path)['format']['duration'])
            tempo = max(0.8, min(1.2, audio_dur / video_duration))
            st.write(f"🎙️ အသံ ({audio_dur:.1f}s) | Tempo: {tempo:.2f}x")
        except Exception as e:
            st.error(f"❌ VoxCPM2 error: {e}")
            st.stop()

        # 4. Render — Blur + Subtitle
        with st.spinner("🎬 Recap Video Render..."):
            temp_path = "temp_recap.mp4"
            final_path = "final_recap.mp4"

            # 4a. Video + Audio
            input_video = ffmpeg.input(video_filename)
            input_audio = ffmpeg.input(audio_path).audio.filter('atempo', tempo)
            stream = ffmpeg.output(
                input_video.video, input_audio, temp_path,
                vcodec='libx264',
                crf=18,
                preset='medium',
                acodec='aac',
                audio_bitrate='192k'
            )
            ffmpeg.run(stream, overwrite_output=True)

            # 4b. SRT + Blur + Subtitle
            if use_subtitle:
                with st.spinner("📝 Script → SRT..."):
                    srt_path = "recap.srt"
                    script_to_srt(script, audio_path, srt_path)

                with st.spinner("📝 Blur + Subtitle ထည့်နေသည်..."):
                    burn_subtitle_on_blur(
                        temp_path, srt_path, final_path,
                        font_size=sub_font_size,
                        position=sub_position,
                        blur_height=blur_height
                    )
            else:
                shutil.copy(temp_path, final_path)

        # 5. Output
        st.success("✅ ပြီးပါပြီ!")
        st.video(final_path)

        with open(final_path, "rb") as f:
            st.download_button("📥 Recap Video Download", f, file_name="final_recap.mp4")

        with st.expander("📝 Script"):
            st.text(script)

        if use_subtitle:
            with st.expander("📝 SRT File"):
                with open("recap.srt", "r", encoding="utf-8") as f:
                    st.text(f.read())
