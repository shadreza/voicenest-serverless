import boto3
import json
import os
import tempfile
import uuid
import requests
import base64
import time
import logging

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

def handler(event, context):
    tmp_audio_path = None
    
    try:
        logger.info(f"Event received: {json.dumps(event, default=str)}")
        
        # Validate environment variables
        if not COHERE_API_KEY:
            logger.error("COHERE_API_KEY not found in environment variables")
            return _response(500, "Missing API configuration")
        
        if not TRANSCRIBE_BUCKET:
            logger.error("TRANSCRIBE_BUCKET not found in environment variables")
            return _response(500, "Missing bucket configuration")
        
        # Parse and save incoming audio
        logger.info("Parsing audio data...")
        body = event.get("body")
        if not body:
            logger.error("No body found in request")
            return _response(400, "Missing audio data")
        
        is_base64 = event.get("isBase64Encoded", False)
        logger.info(f"Audio is base64 encoded: {is_base64}")
        
        try:
            if is_base64:
                audio_bytes = base64.b64decode(body)
            else:
                # Handle case where body might already be bytes
                if isinstance(body, str):
                    audio_bytes = body.encode()
                else:
                    audio_bytes = body
        except Exception as e:
            logger.error(f"Failed to decode audio data: {str(e)}")
            return _response(400, "Invalid audio data format")
        
        logger.info(f"Audio data size: {len(audio_bytes)} bytes")
        
        # Save audio to temporary file
        try:
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp_audio:
                tmp_audio.write(audio_bytes)
                tmp_audio_path = tmp_audio.name
            logger.info(f"Audio saved to temporary file: {tmp_audio_path}")
        except Exception as e:
            logger.error(f"Failed to save audio file: {str(e)}")
            return _response(500, "Failed to process audio file")

        # Start transcription
        job_name = f"voicenest-job-{uuid.uuid4()}"
        logger.info(f"Starting transcription job: {job_name}")
        
        transcribe_uri = _upload_and_transcribe(tmp_audio_path, job_name)
        if not transcribe_uri:
            logger.error("Transcription upload failed")
            return _response(500, "Transcription failed")

        # Get transcription result
        logger.info("Waiting for transcription to complete...")
        transcript_text = _get_transcribed_text(job_name)
        if not transcript_text:
            logger.error("Could not retrieve transcription result")
            return _response(500, "Could not retrieve transcription")
        
        logger.info(f"Transcript: {transcript_text}")

        # Detect dominant language
        logger.info("Detecting language...")
        try:
            lang_detection = comprehend.detect_dominant_language(Text=transcript_text)
            lang_code = lang_detection['Languages'][0]['LanguageCode']
            logger.info(f"Detected language: {lang_code}")
        except Exception as e:
            logger.error(f"Language detection failed: {str(e)}")
            lang_code = "en"  # Default to English
        
        # Translate to English (if needed)
        translated_text = transcript_text
        if lang_code != "en":
            logger.info(f"Translating from {lang_code} to English...")
            try:
                translation_result = translate.translate_text(
                    Text=transcript_text,
                    SourceLanguageCode=lang_code,
                    TargetLanguageCode="en"
                )
                translated_text = translation_result['TranslatedText']
                logger.info(f"Translated text: {translated_text}")
            except Exception as e:
                logger.error(f"Translation failed: {str(e)}")
                # Continue with original text if translation fails

        # Analyze sentiment
        logger.info("Analyzing sentiment...")
        try:
            sentiment_result = comprehend.detect_sentiment(Text=translated_text, LanguageCode="en")
            sentiment = sentiment_result['Sentiment']
            logger.info(f"Detected sentiment: {sentiment}")
        except Exception as e:
            logger.error(f"Sentiment analysis failed: {str(e)}")
            sentiment = "NEUTRAL"  # Default sentiment

        # Get reply from Cohere
        logger.info("Generating response from Cohere...")
        reply = _cohere_generate_reply(translated_text, sentiment)
        if not reply:
            logger.error("Failed to generate Cohere response")
            return _response(500, "Failed to generate response")
        
        logger.info(f"Cohere response: {reply}")

        # Convert to audio via Polly
        logger.info("Converting response to audio...")
        try:
            polly_audio = polly.synthesize_speech(
                Text=reply,
                OutputFormat="mp3",
                VoiceId="Joanna"
            )
            audio_stream = polly_audio["AudioStream"].read()
            audio_base64 = base64.b64encode(audio_stream).decode()
            logger.info(f"Audio response generated, size: {len(audio_base64)} characters")
        except Exception as e:
            logger.error(f"Audio synthesis failed: {str(e)}")
            return _response(500, "Failed to generate audio response")

        return {
            "statusCode": 200,
            "isBase64Encoded": True,
            "headers": {"Content-Type": "audio/mpeg"},
            "body": audio_base64
        }

    except Exception as e:
        logger.error(f"Unexpected error in handler: {str(e)}", exc_info=True)
        return _response(500, f"Internal server error: {str(e)}")
    
    finally:
        # Clean up temporary file
        if tmp_audio_path and os.path.exists(tmp_audio_path):
            try:
                os.unlink(tmp_audio_path)
                logger.info("Temporary audio file cleaned up")
            except Exception as e:
                logger.warning(f"Failed to clean up temporary file: {str(e)}")

def _upload_and_transcribe(audio_path, job_name):
    try:
        bucket = TRANSCRIBE_BUCKET
        key = f"uploads/{uuid.uuid4()}.webm"
        
        logger.info(f"Uploading to S3: s3://{bucket}/{key}")
        s3.upload_file(audio_path, bucket, key)

        job_uri = f"s3://{bucket}/{key}"
        logger.info(f"Starting transcription job with URI: {job_uri}")
        
        transcribe.start_transcription_job(
            TranscriptionJobName=job_name,
            Media={"MediaFileUri": job_uri},
            MediaFormat="webm",
            LanguageCode="en-US",  # You might want to make this auto-detect
            OutputBucketName=bucket
        )

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
            "prompt": f"You are a compassionate listener. The user said: \"{text}\" (Sentiment: {sentiment}). Respond with empathy.",
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
        return None

def _response(status, message):
    return {
        "statusCode": status,
        "body": json.dumps({"message": message}),
        "headers": {"Content-Type": "application/json"}
    }