import streamlit as st
import os
import time
import ffmpeg
import shutil
import requests
from google import genai
from gradio_client import Client, handle_file

LANGUAGE = "Myanmar"
MODEL = "gemini-3.6-flash"
VOXCPM_DEMO = "openbmb/VoxCPM-Demo"

# ===== Secrets ကနေ URL ယူ =====
VOXCPM_SERVER = st.secrets.get("VOXCPM_SERVER", "")

# ===== Password Check =====
PASSWORD = "voxcpm2026"

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


# ===== Demo အား/မအား စစ် =====
def check_demo_available():
    try:
        client = Client(VOXCPM_DEMO)
        result = client.predict(
            text_input="test",
            control_instruction="",
            reference_wav_path_input=None,
            use_prompt_text=False,
            prompt_text_input="",
            cfg_value_input=2.0,
            do_normalize=True,
            denoise=False,
            api_name="/generate",
        )
        return True
    except Exception:
        return False


# ===== Demo Space သုံး =====
def run_tts_demo(text, output_path, ref_audio_path=None, progress_callback=None):
    client = Client(VOXCPM_DEMO)
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


# ===== Colab Server သုံး =====
def run_tts_server(text, output_path, ref_audio_path=None, progress_callback=None):
    files = {}
    if ref_audio_path:
        files["ref_audio"] = open(ref_audio_path, "rb")

    if progress_callback:
        progress_callback(0, 1, text[:50])

    response = requests.post(
        f"{VOXCPM_SERVER}/tts",
        params={"text": text},
        files=files if files else None,
        timeout=600
    )
    if response.status_code == 200:
        with open(output_path, "wb") as f:
            f.write(response.content)
        if progress_callback:
            progress_callback(1, 1, "done")
        return output_path
    else:
        raise Exception(f"Server error: {response.status_code}")


# ===== Auto Fallback =====
def run_tts_chunked(text, output_path, ref_audio_path=None, progress_callback=None):
    with st.spinner("🔍 Demo Space စစ်ဆေးနေသည်..."):
        demo_ok = check_demo_available()

    if demo_ok:
        st.success("✅ Demo Space — အား — သုံးမယ်")
        return run_tts_demo(text, output_path, ref_audio_path, progress_callback)
    else:
        if not VOXCPM_SERVER:
            raise Exception("❌ Demo Busy + Server URL မထည့်ရသေးပါ။")
        st.warning("⚠️ Demo Busy — Colab Server ကို ပြောင်း")
        return run_tts_server(text, output_path, ref_audio_path, progress_callback)


st.set_page_config(page_title="🎬 VoxCPM2 Movie Recap", page_icon="🎬")
st.title("🎬 VoxCPM2 Movie Recap")
st.write("ဗီဒီယို upload တင်ပြီး VoxCPM2 အသံနဲ့ Recap ဖန်တီးပါ")

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

video_file = st.file_uploader("📹 ဗီဒီယို Upload", type=["mp4", "mov", "avi", "mkv"])


if video_file is not None:
    if st.button("🚀 Generate Recap", type="primary"):
        if len(API_KEYS) == 0:
            st.error("❌ Gemini API Key မထည့်ရသေးပါ။")
            st.stop()

        km = KeyManager(API_KEYS)

        with st.spinner("📹 ဗီဒီယို စစ်ဆေးနေသည်..."):
            video_filename = "input_video.mp4"
            with open(video_filename, "wb") as f:
                f.write(video_file.read())
            probe = ffmpeg.probe(video_filename)
            video_duration = float(probe['format']['duration'])
            st.write(f"📹 အရှည်: {video_duration:.2f} စက္ကန့်")

        # ===== Script Cache =====
        if "cached_video" not in st.session_state:
            st.session_state.cached_video = None
        if "cached_script" not in st.session_state:
            st.session_state.cached_script = None

        if st.session_state.cached_video == video_file.name and st.session_state.cached_script:
            script = st.session_state.cached_script
            st.write(f"✅ Script (Cache — {len(script)} စာလုံး)")
        else:
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
                st.session_state.cached_script = script
                st.session_state.cached_video = video_file.name
                st.write(f"✅ Script ({len(script)} စာလုံး)")

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

        with st.spinner("🎬 Recap Video Render..."):
            final_path = "final_recap.mp4"
            input_video = ffmpeg.input(video_filename)
            input_audio = ffmpeg.input(audio_path).audio.filter('atempo', tempo)
            stream = ffmpeg.output(
                input_video.video, input_audio, final_path,
                vcodec='libx264',
                crf=18,
                preset='medium',
                acodec='aac',
                audio_bitrate='192k'
            )
            ffmpeg.run(stream, overwrite_output=True)

        st.success("✅ ပြီးပါပြီ!")
        st.video(final_path)

        with open(final_path, "rb") as f:
            st.download_button("📥 Recap Video Download", f, file_name="final_recap.mp4")

        with st.expander("📝 Script"):
            st.text(script)
