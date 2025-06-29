import boto3
import json
import os
import tempfile
import uuid
import requests
import base64
import time
import logging
import struct
import email
from email.mime.multipart import MIMEMultipart
from io import BytesIO

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
s3 = boto3.client('s3')
transcribe = boto3.client('transcribe')
comprehend = boto3.client('comprehend')
translate = boto3.client('translate')
polly = boto3.client('polly')

COHERE_API_KEY = os.environ.get("PROD_COHERE_API_KEY")
TRANSCRIBE_BUCKET = os.environ.get("PROD_TRANSCRIBE_BUCKET")

SUPPORTED_POLLY_LANGS = {
    "en": "Joanna", "es": "Conchita", "fr": "Celine", "de": "Vicki", "it": "Carla",
    "pt": "Vitoria", "ja": "Mizuki", "ko": "Seoyeon", "zh": "Zhiyu", "ar": "Zeina",
    "hi": "Aditi", "nl": "Lotte", "sv": "Astrid", "ru": "Tatyana", "tr": "Filiz"
}

SUPPORTED_TRANSLATE_LANGS = list(SUPPORTED_POLLY_LANGS.keys())

def handler(event, context):
    tmp_audio_path = None
    try:
        logger.info(f"Event headers: {json.dumps(event.get('headers', {}), default=str)}")

        if not COHERE_API_KEY:
            logger.error("COHERE_API_KEY not found in environment variables")
            return _response(500, "Missing API configuration")

        if not TRANSCRIBE_BUCKET:
            logger.error("TRANSCRIBE_BUCKET not found in environment variables")
            return _response(500, "Missing bucket configuration")

        body = event.get("body")
        headers = event.get("headers", {})

        content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        if not body:
            logger.error("No body found in request")
            return _response(400, "Missing audio data")

        is_base64 = event.get("isBase64Encoded", False)
        logger.info(f"Content-Type: {content_type}")
        logger.info(f"Is base64 encoded: {is_base64}")

        try:
            if "multipart/form-data" in content_type:
                logger.info("Processing multipart/form-data")
                audio_bytes = parse_multipart_data(body, content_type)
            else:
                audio_bytes = base64.b64decode(body) if is_base64 else body.encode() if isinstance(body, str) else body
        except Exception as e:
            logger.error(f"Failed to decode audio data: {str(e)}")
            return _response(400, "Invalid audio data format")

        if not audio_bytes or len(audio_bytes) < 100:
            logger.error("Audio data appears to be invalid or too small")
            return _response(400, "Audio data appears to be invalid or too small")

        file_extension, media_format = _detect_audio_format(audio_bytes, content_type)
        logger.info(f"Detected format: {media_format}, using extension: {file_extension}")

        with tempfile.NamedTemporaryFile(suffix=file_extension, delete=False) as tmp_audio:
            tmp_audio.write(audio_bytes)
            tmp_audio_path = tmp_audio.name
        logger.info(f"Audio saved to temporary file: {tmp_audio_path}")

        job_name = f"voicenest-job-{uuid.uuid4()}"
        transcribe_uri = _upload_and_transcribe(tmp_audio_path, job_name, media_format)
        if not transcribe_uri:
            return _response(500, "Transcription failed")

        transcript_text = _get_transcribed_text(job_name)
        if not transcript_text.strip():
            return _response(400, "No speech detected in audio")
        logger.info(f"Transcript: {transcript_text}")

        try:
            lang_detection = comprehend.detect_dominant_language(Text=transcript_text)
            lang_code = lang_detection['Languages'][0]['LanguageCode']
            confidence = lang_detection['Languages'][0]['Score']
            logger.info(f"Detected language: {lang_code} (confidence: {confidence:.2f})")
        except Exception as e:
            logger.error(f"Language detection failed: {str(e)}")
            lang_code = "en"

        translated_text = transcript_text
        if lang_code != "en" and lang_code in SUPPORTED_TRANSLATE_LANGS:
            try:
                translation_result = translate.translate_text(
                    Text=transcript_text,
                    SourceLanguageCode=lang_code,
                    TargetLanguageCode="en"
                )
                translated_text = translation_result['TranslatedText']
                logger.info(f"Translated to English: {translated_text}")
            except Exception as e:
                logger.warning(f"Translation to English failed: {str(e)}")

        try:
            sentiment_result = comprehend.detect_sentiment(Text=translated_text, LanguageCode="en")
            sentiment = sentiment_result['Sentiment']
            logger.info(f"Sentiment: {sentiment}")
        except Exception as e:
            logger.error(f"Sentiment analysis failed: {str(e)}")
            sentiment = "NEUTRAL"

        reply = _cohere_generate_reply(translated_text, sentiment)
        logger.info(f"Cohere reply: {reply}")

        final_reply = reply
        if lang_code != "en" and lang_code in SUPPORTED_TRANSLATE_LANGS:
            try:
                back_translation = translate.translate_text(
                    Text=reply,
                    SourceLanguageCode="en",
                    TargetLanguageCode=lang_code
                )
                final_reply = back_translation['TranslatedText']
                logger.info(f"Back-translated response: {final_reply}")
            except Exception as e:
                logger.warning(f"Back-translation failed: {str(e)}")

        voice_id = SUPPORTED_POLLY_LANGS.get(lang_code, "Joanna")
        try:
            polly_response = polly.synthesize_speech(
                Text=final_reply,
                OutputFormat="mp3",
                VoiceId=voice_id,
                Engine="neural" if voice_id in ["Joanna", "Matthew", "Ruth", "Vicki", "Mizuki", "Seoyeon", "Zhiyu"] else "standard"
            )
            audio_stream = polly_response["AudioStream"].read()
            audio_base64 = base64.b64encode(audio_stream).decode()
            logger.info(f"Polly audio synthesis successful in {lang_code} with voice {voice_id}")
        except Exception as e:
            logger.error(f"Polly synthesis failed: {str(e)}")
            return _response(500, "Audio response generation failed")

        return {
            "statusCode": 200,
            "isBase64Encoded": True,
            "headers": {
                "Content-Type": "audio/mpeg",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Allow-Methods": "POST, OPTIONS"
            },
            "body": audio_base64
        }

    except Exception as e:
        logger.error(f"Unexpected error in handler: {str(e)}", exc_info=True)
        return _response(500, f"Internal server error: {str(e)}")

    finally:
        if tmp_audio_path and os.path.exists(tmp_audio_path):
            try:
                os.unlink(tmp_audio_path)
                logger.info("Temporary audio file cleaned up")
            except Exception as e:
                logger.warning(f"Failed to clean up temporary file: {str(e)}")

def parse_multipart_data(body, content_type):
    """Parse multipart/form-data from API Gateway"""
    try:
        # Extract boundary from content-type
        boundary = None
        if 'boundary=' in content_type:
            boundary = content_type.split('boundary=')[1].strip()
        
        if not boundary:
            logger.error("No boundary found in content-type")
            return None
        
        # Parse the multipart data
        body_bytes = base64.b64decode(body) if isinstance(body, str) else body
        
        # Create a proper multipart message
        multipart_data = b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + body_bytes
        
        # Parse using email library
        msg = email.message_from_bytes(multipart_data)
        
        for part in msg.walk():
            if part.get_content_disposition() == 'form-data':
                if part.get_param('name', header='content-disposition') == 'audio':
                    return part.get_payload(decode=True)
        
        return None
    except Exception as e:
        logger.error(f"Failed to parse multipart data: {str(e)}")
        return None

def _detect_audio_format(audio_bytes, content_type):
    """Detect audio format and return appropriate extension and media format"""
    try:
        # Check file signature first
        if len(audio_bytes) >= 12:
            if audio_bytes[:4] == b'RIFF' and audio_bytes[8:12] == b'WAVE':
                logger.info("Detected WAV format from file signature")
                return '.wav', 'wav'
            elif audio_bytes[:4] == b'OggS':
                logger.info("Detected OGG format from file signature")
                return '.ogg', 'ogg'
            elif audio_bytes[:3] == b'ID3' or audio_bytes[:2] == b'\xff\xfb':
                logger.info("Detected MP3 format from file signature")
                return '.mp3', 'mp3'
        
        # Check WebM format (more complex, just look for content-type)
        if content_type and 'webm' in content_type.lower():
            logger.info("Detected WebM format from content-type")
            return '.webm', 'webm'
        
        # Default based on content-type
        if content_type:
            if 'wav' in content_type.lower():
                return '.wav', 'wav'
            elif 'ogg' in content_type.lower():
                return '.ogg', 'ogg'
            elif 'mp3' in content_type.lower():
                return '.mp3', 'mp3'
            elif 'webm' in content_type.lower():
                return '.webm', 'webm'
        
        # Default to webm (common for web recordings)
        logger.info("Using default WebM format")
        return '.webm', 'webm'
        
    except Exception as e:
        logger.warning(f"Error detecting audio format: {str(e)}")
        return '.webm', 'webm'

def _upload_and_transcribe(audio_path, job_name, media_format):
    try:
        bucket = TRANSCRIBE_BUCKET
        key = f"uploads/{job_name}{os.path.splitext(audio_path)[1]}"
        
        logger.info(f"Uploading to S3: s3://{bucket}/{key}")
        s3.upload_file(audio_path, bucket, key)

        job_uri = f"s3://{bucket}/{key}"
        logger.info(f"Starting transcription job with URI: {job_uri}")
        
        job_config = {
            'TranscriptionJobName': job_name,
            'Media': {'MediaFileUri': job_uri},
            'MediaFormat': media_format,
            'IdentifyLanguage': True
        }
        
        transcribe.start_transcription_job(**job_config)
        return job_uri
        
    except Exception as e:
        logger.error(f"Upload and transcribe failed: {str(e)}", exc_info=True)
        return None

def _get_transcribed_text(job_name):
    max_wait_time = 300  # 5 minutes max wait
    start_time = time.time()
    
    try:
        while True:
            if time.time() - start_time > max_wait_time:
                logger.error(f"Transcription job {job_name} timed out after {max_wait_time} seconds")
                return None
                
            status = transcribe.get_transcription_job(TranscriptionJobName=job_name)
            job_status = status["TranscriptionJob"]["TranscriptionJobStatus"]
            logger.info(f"Transcription job status: {job_status}")
            
            if job_status in ["COMPLETED", "FAILED"]:
                break
            time.sleep(5)  # Wait 5 seconds between checks

        if job_status == "FAILED":
            failure_reason = status["TranscriptionJob"].get("FailureReason", "Unknown")
            logger.error(f"Transcription job failed: {failure_reason}")
            return None

        transcript_url = status["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
        logger.info(f"Fetching transcript from: {transcript_url}")
        
        response = requests.get(transcript_url, timeout=30)
        response.raise_for_status()
        
        transcript_data = response.json()
        transcript_text = transcript_data["results"]["transcripts"][0]["transcript"]
        
        return transcript_text
        
    except Exception as e:
        logger.error(f"Get transcribed text failed: {str(e)}", exc_info=True)
        return None

def _cohere_generate_reply(text, sentiment):
    try:
        payload = {
            "model": "command-r-plus",
            "prompt": f"You are a compassionate listener. The user said: \"{text}\" (Sentiment: {sentiment}). Respond with empathy and keep your response under 100 words.",
            "max_tokens": 150,
            "temperature": 0.7
        }
        headers = {
            "Authorization": f"Bearer {COHERE_API_KEY}",
            "Content-Type": "application/json"
        }

        cohere_url = "https://api.cohere.ai/v1/generate"
        logger.info("Sending request to Cohere API...")
        
        response = requests.post(cohere_url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        reply_text = result["generations"][0]["text"].strip()
        return reply_text
        
    except Exception as e:
        logger.error(f"Cohere API call failed: {str(e)}", exc_info=True)
        return "I understand you're sharing something important with me. Thank you for trusting me with your thoughts."

def _response(status, message):
    return {
        "statusCode": status,
        "body": json.dumps({"message": message}),
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST, OPTIONS"
        }
    }