"""Bounded browser PCM uploads forwarded to the stock Whisper service, and
speech text preparation for the stock SpeechT5 service."""
import io
import re
import wave

MAX_AUDIO_BYTES = 10 * 1024 * 1024


def validate_audio(data: bytes) -> None:
    try:
        with wave.open(io.BytesIO(data), 'rb') as clip:
            if (clip.getnchannels() != 1 or clip.getsampwidth() != 2
                    or clip.getframerate() != 16000 or clip.getcomptype() != 'NONE'
                    or not 0 < clip.getnframes() <= 20 * 16000):
                raise ValueError('Use mono 16 kHz PCM audio, at most 20 seconds.')
            if len(clip.readframes(clip.getnframes())) != clip.getnframes() * 2:
                raise ValueError('Incomplete audio recording.')
    except (wave.Error, EOFError) as exc:
        raise ValueError('Invalid WAV recording.') from exc


def transcribe(services, url: str, data: bytes) -> str:
    response = services.client.post(url.rstrip('/') + '/v1/audio/transcriptions',
        files={'file': ('recording.wav', data, 'audio/wav')},
        data={'model': 'whisper-1', 'response_format': 'json'}, timeout=60)
    response.raise_for_status()
    text = response.json().get('text')
    if not isinstance(text, str):
        raise ValueError('Transcription service returned no text.')
    text = text.strip()
    if len(text) > 500:
        raise ValueError('Please ask a shorter question (up to 500 characters).')
    return text


def silent_wav(seconds: float = 0.5) -> bytes:
    """A valid recording of silence, used only to wake the Whisper device."""
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(16000)
        clip.writeframes(b'\0\0' * int(16000 * seconds))
    return buffer.getvalue()


# SpeechT5 reads "CPU" as one word ("C-U") and spaced capitals ("N P U") are
# misheard; phonetic letter names were the forms a Whisper round trip heard
# back correctly (CPU, GPU, NPU, LLM, RTSP, MQTT, API, RAM, GPUs).
LETTER_NAMES = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (
    "ay bee see dee ee ef jee aitch I jay kay ell em en oh pee cue arr ess tee you vee"
    " double-you ex why zee").split()))
ACRONYM = re.compile(r"(?<![A-Za-z])([A-Z]{2,})(s?)(?![a-z])")


def speakable(text: str) -> str:
    """Spell out acronyms: all-capital tokens of two or three letters, or longer
    ones with no vowel. Longer pronounceable capitals (NATO, JSON, AMBER) stay words."""
    def spell(match):
        letters, plural = match[1], match[2]
        if len(letters) > 3 and re.search(r"[AEIOU]", letters):
            return match[0]
        spoken = " ".join(LETTER_NAMES[ch] for ch in letters) + ("z" if plural else "")
        # "FP16": the stock normalizer only expands digits that stand alone.
        return spoken + " " if match.string[match.end():match.end() + 1].isdigit() else spoken
    return ACRONYM.sub(spell, text)
