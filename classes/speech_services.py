import os
import azure.cognitiveservices.speech as speechsdk
from agent_framework import tool


class AzureSpeechService:
    def __init__(self, speech_client):
        self.speech_client = speech_client

    def transcribe_audio(self, audio_path: str) -> str:
        if not self.speech_client:
            return "ERROR: Speech service is not configured."

        print(f"[SPEECH STT] Transcribing audio file: '{audio_path}'")

        if not os.path.exists(audio_path):
            return f"ERROR: Audio file not found at '{audio_path}'"

        try:
            audio_config = speechsdk.audio.AudioConfig(filename=audio_path)
            speech_recognizer = speechsdk.SpeechRecognizer(
                speech_config=self.speech_client, audio_config=audio_config
            )

            result = speech_recognizer.recognize_once()

            if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                print("[SPEECH STT] Successfully transcribed audio.")
                return f"Transcription Result:\n{result.text}"
            elif result.reason == speechsdk.ResultReason.NoMatch:
                return "ERROR: No speech could be recognized from the audio file."
            elif result.reason == speechsdk.ResultReason.Canceled:
                cancellation = result.cancellation_details
                error_msg = (
                    f"Speech recognition canceled. Reason: {cancellation.reason}"
                )
                if cancellation.reason == speechsdk.CancellationReason.Error:
                    error_msg += f" | Error Details: {cancellation.error_details}"
                return f"ERROR: {error_msg}"

        except Exception as error:
            print(f"[SPEECH STT] Error: {error}")
            return f"ERROR processing audio transcription: {error}"

    def synthesize_speech(self, text: str, voice_name: str = None) -> str:
        if not self.speech_client:
            return "ERROR: Speech service is not configured."

        output_audio_path = "outputs/audio_file.wav"
        print(f"[SPEECH TTS] Synthesizing text to file: '{output_audio_path}'")

        try:
            # outputs/ is git-ignored, so it does not exist in a fresh clone
            os.makedirs(os.path.dirname(output_audio_path), exist_ok=True)

            # Set a specific neural voice if provided
            if voice_name:
                self.speech_client.speech_synthesis_voice_name = voice_name

            # Configure output destination to a local audio file
            file_config = speechsdk.audio.AudioOutputConfig(filename=output_audio_path)
            speech_synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=self.speech_client, audio_config=file_config
            )

            result = speech_synthesizer.speak_text_async(text).get()

            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                print(
                    f"[SPEECH TTS] Successfully synthesized audio to {output_audio_path}"
                )
                return f"Success: Audio successfully synthesized and saved to '{output_audio_path}'."
            elif result.reason == speechsdk.ResultReason.Canceled:
                cancellation = result.cancellation_details
                error_msg = f"Speech synthesis canceled. Reason: {cancellation.reason}"
                if cancellation.reason == speechsdk.CancellationReason.Error:
                    error_msg += f" | Error Details: {cancellation.error_details}"
                return f"ERROR: {error_msg}"

        except Exception as error:
            print(f"[SPEECH TTS] Error: {error}")
            return f"ERROR synthesizing speech: {error}"


def text_to_speech(
    text: str,
    voice_name: str = "en-US-AvaMultilingualNeural",
) -> str:
    print(f"\n🛠️ [LOCAL TOOL EXECUTION] Dispatching TTS to AzureSpeechService...")

    resolved_key = os.getenv("SPEECH_KEY")
    resolved_region = os.getenv("SPEECH_REGION")

    speech_sdk_config = speechsdk.SpeechConfig(
        subscription=resolved_key, region=resolved_region
    )

    speech_service = AzureSpeechService(speech_client=speech_sdk_config)
    return speech_service.synthesize_speech(text, voice_name)

# speech functions as tools for the agent
# @tool
# def speech_to_text(audio_path: str) -> str:
#     print(f"\n🛠️ [LOCAL TOOL EXECUTION] Dispatching STT to AzureSpeechService...")

#     resolved_key = os.getenv("SPEECH_KEY")
#     resolved_region = os.getenv("SPEECH_REGION")

#     speech_sdk_config = speechsdk.SpeechConfig(
#         subscription=resolved_key, region=resolved_region
#     )

#     speech_service = AzureSpeechService(speech_client=speech_sdk_config)
#     return speech_service.transcribe_audio(audio_path)


# @tool
# def text_to_speech(
#     text: str,
#     voice_name: str = "en-US-AvaMultilingualNeural",
# ) -> str:
#     print(f"\n🛠️ [LOCAL TOOL EXECUTION] Dispatching TTS to AzureSpeechService...")

#     resolved_key = os.getenv("SPEECH_KEY")
#     resolved_region = os.getenv("SPEECH_REGION")

#     speech_sdk_config = speechsdk.SpeechConfig(
#         subscription=resolved_key, region=resolved_region
#     )

#     speech_service = AzureSpeechService(speech_client=speech_sdk_config)
#     return speech_service.synthesize_speech(text, voice_name)
