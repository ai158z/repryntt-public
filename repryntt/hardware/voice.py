"""
Voice Interface for Human-AI Interaction
Enables Text-to-Speech (AI speaks) and Speech-to-Text (Human speaks)
"""

import os
import logging
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)

class VoiceInterface:
    """
    Handles voice operations for AI-Human interaction.
    Makes AI feel like a movie robot that can speak and listen.
    """
    
    def __init__(self, brain_system):
        self.brain = brain_system
        self.audio_dir = Path(brain_system.brain_path) / "voices" / "audio_cache"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        
        self.tts_engine = None
        self.stt_recognizer = None
        self.voice_enabled = False
        
        # Try to initialize TTS and STT
        self._initialize_tts()
        self._initialize_stt()
        
        logger.info("🎤 Voice Interface initialized")
    
    def _initialize_tts(self):
        """Initialize Text-to-Speech engine"""
        try:
            # Try pyttsx3 (offline, fast)
            import pyttsx3
            self.tts_engine = pyttsx3.init()
            
            # Configure voice properties
            self.tts_engine.setProperty('rate', 175)  # Speed
            self.tts_engine.setProperty('volume', 0.9)  # Volume
            
            # Try to find a good voice
            voices = self.tts_engine.getProperty('voices')
            for voice in voices:
                # Prefer English voices
                if 'english' in voice.name.lower():
                    self.tts_engine.setProperty('voice', voice.id)
                    break
            
            logger.info("✅ TTS initialized (pyttsx3)")
            return True
            
        except ImportError:
            logger.warning("⚠️ pyttsx3 not installed. Voice output disabled.")
            logger.info("   Install with: pip install pyttsx3")
        except Exception as e:
            logger.error(f"❌ Error initializing TTS: {e}")
        
        return False
    
    def _initialize_stt(self):
        """Initialize Speech-to-Text recognizer"""
        try:
            import speech_recognition as sr
            self.stt_recognizer = sr.Recognizer()
            
            # Adjust for ambient noise
            with sr.Microphone() as source:
                logger.info("🎙️ Adjusting for ambient noise... (please wait)")
                self.stt_recognizer.adjust_for_ambient_noise(source, duration=1)
            
            logger.info("✅ STT initialized (SpeechRecognition)")
            return True
            
        except ImportError:
            logger.warning("⚠️ speech_recognition not installed. Voice input disabled.")
            logger.info("   Install with: pip install SpeechRecognition pyaudio")
        except Exception as e:
            logger.error(f"❌ Error initializing STT: {e}")
        
        return False
    
    def speak(self, text: str, save_audio: bool = False) -> Optional[str]:
        """
        AI speaks the given text.
        
        Args:
            text: What AI should say
            save_audio: Whether to save audio file
            
        Returns:
            Path to audio file if saved, None otherwise
        """
        if not self.tts_engine:
            logger.warning("TTS not available - text only mode")
            return None
        
        try:
            # Speak the text
            self.tts_engine.say(text)
            self.tts_engine.runAndWait()
            
            # Optionally save to file
            if save_audio:
                import time
                audio_filename = f"ai_speech_{int(time.time())}.wav"
                audio_path = self.audio_dir / audio_filename
                
                self.tts_engine.save_to_file(text, str(audio_path))
                self.tts_engine.runAndWait()
                
                logger.debug(f"💾 Saved audio: {audio_filename}")
                return str(audio_path)
            
            return None
            
        except Exception as e:
            logger.error(f"Error in TTS: {e}")
            return None
    
    def listen(self, timeout: int = 5, phrase_time_limit: int = 10) -> Optional[str]:
        """
        Listen for human speech and convert to text.
        
        Args:
            timeout: How long to wait for speech to start
            phrase_time_limit: Maximum duration of speech
            
        Returns:
            Transcribed text or None
        """
        if not self.stt_recognizer:
            logger.warning("STT not available")
            return None
        
        try:
            import speech_recognition as sr
            
            with sr.Microphone() as source:
                logger.info("🎙️ Listening...")
                
                try:
                    # Listen for audio
                    audio = self.stt_recognizer.listen(
                        source,
                        timeout=timeout,
                        phrase_time_limit=phrase_time_limit
                    )
                    
                    # Convert to text using Google Speech Recognition
                    logger.info("🔄 Processing speech...")
                    text = self.stt_recognizer.recognize_google(audio)
                    
                    logger.info(f"📝 Heard: {text}")
                    return text
                    
                except sr.WaitTimeoutError:
                    logger.info("⏱️ No speech detected (timeout)")
                    return None
                except sr.UnknownValueError:
                    logger.warning("❓ Could not understand audio")
                    return None
                
        except Exception as e:
            logger.error(f"Error in STT: {e}")
            return None
    
    def listen_for_wake_word(self, wake_word: str = "hey saige") -> bool:
        """
        Listen continuously for wake word.
        Returns True when wake word is detected.
        """
        if not self.stt_recognizer:
            return False
        
        try:
            import speech_recognition as sr
            
            with sr.Microphone() as source:
                logger.debug("👂 Listening for wake word...")
                
                audio = self.stt_recognizer.listen(source, timeout=1, phrase_time_limit=2)
                
                try:
                    text = self.stt_recognizer.recognize_google(audio).lower()
                    
                    if wake_word.lower() in text:
                        logger.info(f"✨ Wake word detected: '{wake_word}'")
                        return True
                        
                except sr.UnknownValueError:
                    pass
                except sr.RequestError:
                    pass
                
        except Exception as e:
            # Silently fail - this runs in a loop
            pass
        
        return False
    
    def set_voice_enabled(self, enabled: bool):
        """Enable or disable voice mode"""
        self.voice_enabled = enabled
        if enabled:
            logger.info("🔊 Voice mode ENABLED")
            if self.tts_engine:
                self.speak("Voice mode activated.")
        else:
            logger.info("🔇 Voice mode DISABLED")
    
    def is_voice_available(self) -> bool:
        """Check if voice capabilities are available"""
        return self.tts_engine is not None or self.stt_recognizer is not None
    
    def get_voice_status(self) -> dict:
        """Get status of voice capabilities"""
        return {
            "voice_enabled": self.voice_enabled,
            "tts_available": self.tts_engine is not None,
            "stt_available": self.stt_recognizer is not None
        }
    
    def change_voice_settings(self, rate: int = None, volume: float = None):
        """Change TTS voice settings"""
        if not self.tts_engine:
            return
        
        try:
            if rate:
                self.tts_engine.setProperty('rate', rate)
                logger.info(f"🎚️ Speech rate set to {rate}")
            
            if volume:
                self.tts_engine.setProperty('volume', volume)
                logger.info(f"🔊 Volume set to {volume}")
                
        except Exception as e:
            logger.error(f"Error changing voice settings: {e}")


def install_voice_dependencies():
    """
    Helper function to install voice dependencies.
    Run this if voice is not available.
    """
    import subprocess
    import sys
    
    print("Installing voice dependencies...")
    
    try:
        # Install pyttsx3 for TTS
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'pyttsx3'])
        print("✅ Installed pyttsx3 (Text-to-Speech)")
        
        # Install SpeechRecognition for STT
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'SpeechRecognition'])
        print("✅ Installed SpeechRecognition (Speech-to-Text)")
        
        # Install PyAudio for microphone access
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'pyaudio'])
        print("✅ Installed PyAudio (Microphone)")
        
        print("\n🎉 Voice dependencies installed successfully!")
        print("Please restart the chat interface to use voice features.")
        
    except Exception as e:
        print(f"❌ Error installing dependencies: {e}")
        print("\nManual installation:")
        print("  pip install pyttsx3 SpeechRecognition pyaudio")


if __name__ == "__main__":
    # Test voice interface
    print("Testing Voice Interface...")
    
    import tempfile
    class DummyBrain:
        brain_path = os.path.join(tempfile.gettempdir(), "test_voice")
    
    voice = VoiceInterface(DummyBrain())
    
    if voice.tts_engine:
        print("Testing speech...")
        voice.speak("Hello, I am SAIGE. Voice interface is working.")
    else:
        print("TTS not available")
    
    if voice.stt_recognizer:
        print("Testing listening... (say something)")
        text = voice.listen()
        if text:
            print(f"You said: {text}")
    else:
        print("STT not available")
