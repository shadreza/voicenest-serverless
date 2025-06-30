import boto3
import json
import os
import tempfile
import uuid
import requests
import base64
import time
import logging
import email

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
    "arb": "Zeina", "ar-AE": "Hala", "nl-BE": "Lisa", "ca-ES": "Arlet",
    "cs-CZ": "Jitka", "yue-CN": "Hiujin", "cmn-CN": "Zhiyu", "da-DK": "Sofie",
    "nl-NL": "Lotte", "en-AU": "Olivia", "en-GB": "Amy", "en-IN": "Kajal",
    "en-IE": "Niamh", "en-NZ": "Aria", "en-SG": "Jasmine", "en-ZA": "Ayanda",
    "en-US": "Joanna", "en-GB-WLS": "Geraint", "fi-FI": "Suvi", "fr-FR": "Lea",
    "fr-BE": "Isabelle", "fr-CA": "Gabrielle", "de-DE": "Vicki", "de-AT": "Hannah",
    "de-CH": "Sabrina", "hi-IN": "Kajal", "is-IS": "Dora", "it-IT": "Bianca",
    "ja-JP": "Mizuki", "ko-KR": "Seoyeon", "nb-NO": "Ida", "pl-PL": "Maja",
    "pt-BR": "Vitoria", "pt-PT": "Ines", "ro-RO": "Carmen", "ru-RU": "Tatyana",
    "es-ES": "Lucia", "es-MX": "Mia", "es-US": "Lupe", "sv-SE": "Elin",
    "tr-TR": "Burcu", "cy-GB": "Gwyneth"
}

NEURAL_SUPPORTED_VOICES = {
    "Hala", "Zayd", "Lisa", "Arlet", "Jitka", "Hiujin", "Zhiyu", "Sofie",
    "Laura", "Olivia", "Amy", "Emma", "Brian", "Arthur", "Kajal", "Niamh",
    "Aria", "Jasmine", "Ayanda", "Danielle", "Gregory", "Ivy", "Joanna", 
    "Kendra", "Kimberly", "Salli", "Joey", "Justin", "Kevin", "Matthew", 
    "Ruth", "Stephen", "Suvi", "Isabelle", "Gabrielle", "Liam", "Léa", 
    "Rémi", "Vicki", "Daniel", "Hannah", "Sabrina", "Bianca", "Adriano", 
    "Takumi", "Kazuha", "Tomoko", "Seoyeon", "Jihye", "Ida", "Ola", "Camila",
    "Vitoria", "Vitória", "Thiago", "Ines", "Inês", "Lucia", "Sergio", 
    "Mia", "Andrés", "Lupe", "Pedro", "Elin", "Burcu", "Gwyneth"
}

SUPPORTED_TRANSLATE_LANGS = list(set([code.split("-")[0] for code in SUPPORTED_POLLY_LANGS.keys()] + ["en"]))

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
        if not transcript_text or not transcript_text.strip():
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

        # Find best Polly voice for detected language
        voice_id, spoken_lang_code = find_best_voice_match(lang_code)
        logger.info(f"Matched Polly voice: {voice_id} for language code: {spoken_lang_code}")

        final_reply = reply

        if voice_id:
            # Polly supports this language, translate reply back to original language
            if spoken_lang_code != "en":
                try:
                    back_translation = translate.translate_text(
                        Text=reply,
                        SourceLanguageCode="en",
                        TargetLanguageCode=spoken_lang_code
                    )
                    final_reply = back_translation["TranslatedText"]
                    logger.info(f"Translated reply back to {spoken_lang_code}: {final_reply}")
                except Exception as e:
                    logger.warning(f"Back translation to {spoken_lang_code} failed: {str(e)}")
                    final_reply = reply
        else:
            # No voice found, fallback to English Joanna voice
            logger.info(f"No Polly voice found for {lang_code}, falling back to English (Joanna)")
            voice_id = "Joanna"
            spoken_lang_code = "en"
            if lang_code != "en":
                try:
                    fallback_translation = translate.translate_text(
                        Text=reply,
                        SourceLanguageCode=lang_code,
                        TargetLanguageCode="en"
                    )
                    final_reply = fallback_translation["TranslatedText"]
                    logger.info(f"Translated fallback response to English: {final_reply}")
                except Exception as e:
                    logger.warning(f"Fallback translation to English failed: {str(e)}")
                    final_reply = reply

        # Then synthesize speech with polly using final_reply and voice_id
        try:
            polly_response = polly.synthesize_speech(
            Text=final_reply,
            OutputFormat="mp3",
            VoiceId=voice_id,
            Engine="neural" if voice_id in NEURAL_SUPPORTED_VOICES else "standard"
        )
            audio_stream = polly_response["AudioStream"].read()
            audio_base64 = base64.b64encode(audio_stream).decode()

            logger.info(f"Polly audio synthesis successful in {spoken_lang_code} with voice {voice_id}")
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
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Expose-Headers": "x-language",
                "x-language": spoken_lang_code
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

def find_best_voice_match(lang_code):
    """
    Attempt to find the best Polly voice match for the detected language code.
    Matching order:
    1. Exact match (e.g., 'hi-IN')
    2. Prefix match (e.g., detected 'hi' matches 'hi-IN')
    3. Loose containment match (e.g., detected 'hi' contained in 'hi-IN')
    """
    # Exact match
    if lang_code in SUPPORTED_POLLY_LANGS:
        return SUPPORTED_POLLY_LANGS[lang_code], lang_code

    # Try prefix match for codes like 'hi' to 'hi-IN'
    for full_code, voice_id in SUPPORTED_POLLY_LANGS.items():
        if full_code.startswith(lang_code + "-"):
            return voice_id, full_code

    # Loose containment match anywhere in the string (for cases like 'hi' in 'hi-IN')
    for full_code, voice_id in SUPPORTED_POLLY_LANGS.items():
        if lang_code in full_code:
            return voice_id, full_code

    # No match found
    return None, None

def parse_multipart_data(body, content_type):
    """Parse multipart/form-data from API Gateway"""
    try:
        boundary = None
        if 'boundary=' in content_type:
            boundary = content_type.split('boundary=')[1].strip()
        if not boundary:
            logger.error("No boundary found in content-type")
            return None

        body_bytes = base64.b64decode(body) if isinstance(body, str) else body
        multipart_data = b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + body_bytes

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
    try:
        if len(audio_bytes) >= 12:
            if audio_bytes[:4] == b'RIFF' and audio_bytes[8:12] == b'WAVE':
                return '.wav', 'wav'
            elif audio_bytes[:4] == b'OggS':
                return '.ogg', 'ogg'
            elif audio_bytes[:3] == b'ID3' or audio_bytes[:2] == b'\xff\xfb':
                return '.mp3', 'mp3'

        if content_type and 'webm' in content_type.lower():
            return '.webm', 'webm'

        if content_type:
            if 'wav' in content_type.lower():
                return '.wav', 'wav'
            elif 'ogg' in content_type.lower():
                return '.ogg', 'ogg'
            elif 'mp3' in content_type.lower():
                return '.mp3', 'mp3'
            elif 'webm' in content_type.lower():
                return '.webm', 'webm'

        return '.webm', 'webm'
    except Exception as e:
        logger.warning(f"Error detecting audio format: {str(e)}")
        return '.webm', 'webm'

def _upload_and_transcribe(audio_path, job_name, media_format):
    try:
        bucket = TRANSCRIBE_BUCKET
        key = f"uploads/{job_name}{os.path.splitext(audio_path)[1]}"

        s3.upload_file(audio_path, bucket, key)

        job_uri = f"s3://{bucket}/{key}"

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

            if job_status in ["COMPLETED", "FAILED"]:
                break
            time.sleep(5)

        if job_status == "FAILED":
            failure_reason = status["TranscriptionJob"].get("FailureReason", "Unknown")
            logger.error(f"Transcription job failed: {failure_reason}")
            return None

        transcript_url = status["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]

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
