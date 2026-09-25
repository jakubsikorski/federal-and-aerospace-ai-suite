"""Bounded browser PCM uploads forwarded to the stock Whisper service."""
import io
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
