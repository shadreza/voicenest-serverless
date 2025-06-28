import boto3
import json
import os
import tempfile
import uuid
import requests
import base64
import time

s3 = boto3.client('s3')
transcribe = boto3.client('transcribe')
comprehend = boto3.client('comprehend')
translate = boto3.client('translate')
polly = boto3.client('polly')

COHERE_API_KEY = os.environ.get("PROD_COHERE_API_KEY")  # stored as Lambda env variable

def handler(event, context):
    # Parse and save incoming audio
    print("Event Received:", event)
    body = event.get("body")
    if not body:
        return _response(400, "Missing audio data")
    
    is_base64 = event.get("isBase64Encoded", False)
    audio_bytes = base64.b64decode(body) if is_base64 else body.encode()
    
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp_audio:
        tmp_audio.write(audio_bytes)
        tmp_audio_path = tmp_audio.name

    job_name = f"voicenest-job-{uuid.uuid4()}"
    transcribe_uri = _upload_and_transcribe(tmp_audio_path, job_name)

    if not transcribe_uri:
        return _response(500, "Transcription failed")

    transcript_text = _get_transcribed_text(job_name)
    if not transcript_text:
        return _response(500, "Could not retrieve transcription")

    # Detect dominant language
    lang_code = comprehend.detect_dominant_language(Text=transcript_text)['Languages'][0]['LanguageCode']
    
    # Translate to English (if needed)
    if lang_code != "en":
        translated_text = translate.translate_text(
            Text=transcript_text,
            SourceLanguageCode=lang_code,
            TargetLanguageCode="en"
        )['TranslatedText']
    else:
        translated_text = transcript_text

    # Analyze sentiment
    sentiment = comprehend.detect_sentiment(Text=translated_text, LanguageCode="en")['Sentiment']

    # Get reply from Cohere
    reply = _cohere_generate_reply(translated_text, sentiment)

    # Convert to audio via Polly
    polly_audio = polly.synthesize_speech(
        Text=reply,
        OutputFormat="mp3",
        VoiceId="Joanna"
    )

    audio_stream = polly_audio["AudioStream"].read()
    audio_base64 = base64.b64encode(audio_stream).decode()

    return {
        "statusCode": 200,
        "isBase64Encoded": True,
        "headers": {"Content-Type": "audio/mpeg"},
        "body": audio_base64
    }

def _upload_and_transcribe(audio_path, job_name):
    bucket = os.environ.get("PROD_TRANSCRIBE_BUCKET")
    key = f"uploads/{uuid.uuid4()}.webm"
    s3.upload_file(audio_path, bucket, key)

    job_uri = f"s3://{bucket}/{key}"
    transcribe.start_transcription_job(
        TranscriptionJobName=job_name,
        Media={"MediaFileUri": job_uri},
        MediaFormat="webm",
        LanguageCode="en-US",
        OutputBucketName=bucket
    )

    return job_uri

def _get_transcribed_text(job_name):
    while True:
        status = transcribe.get_transcription_job(TranscriptionJobName=job_name)
        job_status = status["TranscriptionJob"]["TranscriptionJobStatus"]
        if job_status in ["COMPLETED", "FAILED"]:
            break
        time.sleep(3)

    if job_status == "FAILED":
        return None

    transcript_url = status["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
    response = requests.get(transcript_url)
    return response.json()["results"]["transcripts"][0]["transcript"]

def _cohere_generate_reply(text, sentiment):
    payload = {
        "model": "command-r-plus",
        "prompt": f"You are a compassionate listener. The user said: \"{text}\" (Sentiment: {sentiment}). Respond with empathy.",
        "max_tokens": 150,
        "temperature": 0.7
    }
    headers = {
        "Authorization": f"Bearer {COHERE_API_KEY}",
        "Content-Type": "application/json"
    }

    cohere_url = "https://api.cohere.ai/v1/generate"
    response = requests.post(cohere_url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()["generations"][0]["text"]

def _response(status, message):
    return {
        "statusCode": status,
        "body": json.dumps({"message": message}),
        "headers": {"Content-Type": "application/json"}
    }
