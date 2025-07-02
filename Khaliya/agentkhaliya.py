import os
import base64
import json
from flask import Flask, request
from flask_sockets import Sockets
from twilio.rest import Client
from google.cloud import speech
from google.cloud import texttospeech
import google.generativeai as genai
from dotenv import load_dotenv
from gevent import pywsgi
from geventwebsocket.handler import WebSocketHandler

# This line loads the environment variables from your .env file for local development.
# On Render, you will set these variables in the dashboard.
load_dotenv()

# --- ⚙️ CONFIGURATION ---
# The script securely fetches all credentials from environment variables.
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
RENDER_APP_URL = os.getenv("RENDER_APP_URL") # Example: "agent-khaliya.onrender.com"

# --- INITIALIZE SERVICES ---
app = Flask(__name__)
sockets = Sockets(app)
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
speech_client = speech.SpeechClient()
tts_client = texttospeech.TextToSpeechClient()

# Configure and initialize the Gemini model
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

# --- 🧠 AI & CONVERSATION LOGIC ---

def generate_ai_response(transcript, call_sid):
    """Sends transcript to Gemini and gets a response."""
    print(f"[{call_sid}] Sending to Gemini: '{transcript}'")
    prompt = f"""
    You are 'Khaliya', an AI cold calling agent. Your personality is polite, professional, and concise. 
    You must respond only in natural, conversational Malayalam. Do not use English.
    Your goal is to introduce a service and determine if there is interest.
    Keep your responses short.

    A person just said: "{transcript}"

    Your Malayalam response:
    """
    try:
        response = gemini_model.generate_content(prompt)
        ai_text_response = response.text.strip()
        print(f"[{call_sid}] Received from Gemini: '{ai_text_response}'")
        return ai_text_response
    except Exception as e:
        print(f"[{call_sid}] Gemini Error: {e}")
        return "ക്ഷമിക്കണം, എനിക്ക് ഒരു പിശക് സംഭവിച്ചു." # "Sorry, I encountered an error."

def text_to_speech_google(text, call_sid):
    """Converts text to speech and returns the public URL of the audio file."""
    print(f"[{call_sid}] Converting to speech: '{text}'")
    try:
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(language_code="ml-IN", name="ml-IN-Standard-A")
        audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
        response = tts_client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
        
        audio_filename = f"response_{call_sid}.mp3"
        with open(f"static/{audio_filename}", "wb") as f:
            f.write(response.audio_content)
        
        audio_url = f"https://{RENDER_APP_URL}/static/{audio_filename}"
        print(f"[{call_sid}] Audio file created: {audio_url}")
        return audio_url
    except Exception as e:
        print(f"[{call_sid}] Google TTS Error: {e}")
        return None

def play_audio_in_call(call_sid, audio_url):
    """Uses Twilio to interrupt the call and play the generated audio."""
    print(f"[{call_sid}] Playing audio in call: {audio_url}")
    try:
        twiml = f'<Response><Play>{audio_url}</Play><Pause length="15"/></Response>'
        twilio_client.calls(call_sid).update(twiml=twiml)
    except Exception as e:
        if "not in-progress" not in str(e):
            print(f"[{call_sid}] Twilio Error updating call: {e}")

# --- 📞 REAL-TIME STREAMING HANDLER ---

@sockets.route('/stream')
def stream_from_twilio(ws):
    """Handles the real-time audio stream from Twilio and Google Speech-to-Text."""
    call_sid = None
    
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
        sample_rate_hertz=8000,
        language_code="ml-IN",
        model="telephony", 
    )
    streaming_config = speech.StreamingRecognitionConfig(config=config, interim_results=False)

    def audio_generator():
        nonlocal call_sid
        while not ws.closed:
            message = ws.receive()
            if message is None: return
            data = json.loads(message)
            if data['event'] == 'start':
                call_sid = data['start']['callSid']
                print(f"[{call_sid}] Stream started.")
            if data['event'] == 'stop':
                print(f"[{call_sid}] Stream stopped.")
                break
            if data['event'] == 'media':
                yield base64.b64decode(data['media']['payload'])

    try:
        requests = (speech.StreamingRecognizeRequest(audio_content=chunk) for chunk in audio_generator())
        responses = speech_client.streaming_recognize(streaming_config, requests)

        for response in responses:
            if not response.results or not response.results[0].alternatives: continue
            transcript = response.results[0].alternatives[0].transcript
            print(f"[{call_sid}] Transcript: '{transcript}'")
            
            ai_response_text = generate_ai_response(transcript, call_sid)
            if ai_response_text:
                audio_url = text_to_speech_google(ai_response_text, call_sid)
                if audio_url:
                    play_audio_in_call(call_sid, audio_url)
    except Exception as e:
        print(f"Error during streaming: {e}")

# --- HTTP ROUTES ---

@app.route("/make_call", methods=['POST'])
def make_call():
    """Endpoint to programmatically initiate a call."""
    number_to_call = request.json.get('number')
    if not number_to_call:
        return "Error: 'number' not provided.", 400

    stream_url = f"wss://{RENDER_APP_URL}/stream"
    twiml = f"""
    <Response>
        <Start>
            <Stream url="{stream_url}" />
        </Start>
        <Say voice="ml-IN-Standard-A">നമസ്കാരം, ഞാൻ ഖാലിയ സംസാരിക്കുന്നു.</Say>
        <Pause length="15"/>
    </Response>
    """
    try:
        call = twilio_client.calls.create(twiml=twiml, to=number_to_call, from_=TWILIO_PHONE_NUMBER)
        print(f"Call initiated to {number_to_call} with SID: {call.sid}")
        return f"Call initiated with SID: {call.sid}", 200
    except Exception as e:
        print(f"Error initiating call: {e}")
        return f"Error initiating call: {e}", 500

@app.route('/static/<path:path>')
def send_static(path):
    """Serves the generated audio files."""
    return app.send_static_file(path)

# --- SERVER STARTUP ---

if __name__ == "__main__":
    if not os.path.exists('static'):
        os.makedirs('static')
        
    # Render provides the port to use in an environment variable; default to 5000 for local dev
    port = int(os.getenv("PORT", 5000))
    
    print(f"Starting server on port {port}...")
    server = pywsgi.WSGIServer(('', port), app, handler_class=WebSocketHandler)
    server.serve_forever()