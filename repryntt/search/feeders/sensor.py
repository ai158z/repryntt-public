#!/usr/bin/env python3
"""
Sensor Feeder - SAIGE Perception Pipeline
Processes camera, environmental, and sensor data into learning stimulus
Real computer vision and sensor fusion implementation
"""

import json
import os
import time
import logging
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, asdict
import threading
import queue
from collections import deque, defaultdict

# Computer Vision
import cv2
from PIL import Image, ImageEnhance
import torch
import torchvision.transforms as transforms
from torchvision.models import resnet50, ResNet50_Weights

# Image processing
from sklearn.cluster import KMeans
from skimage import feature, measure, filters
from skimage.segmentation import slic
from skimage.color import rgb2hsv, rgb2gray
import matplotlib.pyplot as plt

# Audio processing
import pyaudio
import wave
import librosa
import scipy.signal

# Environmental sensors (simulated GPIO for Jetson)
try:
    import busio
    import board
    import adafruit_dht
    import adafruit_bmp280
    SENSORS_AVAILABLE = True
except ImportError:
    SENSORS_AVAILABLE = False
    logging.warning("Hardware sensor libraries not available - using simulation mode")

logger = logging.getLogger(__name__)

@dataclass
class SensorReading:
    timestamp: float
    sensor_type: str
    value: Any
    unit: str
    location: str
    quality: float  # 0-1, confidence in reading
    
@dataclass
class VisualObservation:
    timestamp: float
    image_path: str
    detected_objects: List[Dict[str, Any]]
    scene_description: str
    dominant_colors: List[Tuple[int, int, int]]
    brightness: float
    motion_detected: bool
    faces_detected: int
    novelty_score: float  # How different from previous observations
    emotional_valence: float  # -1 (negative) to 1 (positive)
    complexity_score: float  # Visual complexity measure

@dataclass
class AudioObservation:
    timestamp: float
    audio_path: str
    amplitude_mean: float
    frequency_profile: List[float]
    speech_detected: bool
    music_detected: bool
    noise_level: float
    emotional_tone: str  # 'calm', 'excited', 'distressed', etc.
    novelty_score: float

class SensorFeeder:
    """
    Processes multi-modal sensor data into learning experiences for SAIGE
    """
    
    def __init__(self, config_path: str = "config/sensor_feeder.json"):
        self.config = self._load_config(config_path)
        
        # Computer vision setup
        self.setup_vision_system()
        
        # Audio processing setup
        self.setup_audio_system()
        
        # Environmental sensors setup
        self.setup_environmental_sensors()
        
        # Data storage
        self.visual_memory = deque(maxlen=1000)
        self.audio_memory = deque(maxlen=500)
        self.sensor_memory = deque(maxlen=2000)
        
        # Processing queues
        self.visual_queue = queue.Queue(maxsize=10)
        self.audio_queue = queue.Queue(maxsize=20)
        self.sensor_queue = queue.Queue(maxsize=50)
        
        # Background processing threads
        self.processing_threads = []
        self.running = False
        
        # Novelty detection
        self.previous_visual_features = None
        self.previous_audio_features = None
        
    def _load_config(self, config_path: str) -> Dict:
        """Load configuration or create default"""
        default_config = {
            "cameras": {
                "primary": {
                    "device_id": 0,
                    "resolution": [640, 480],
                    "fps": 2,  # Low fps for continuous monitoring
                    "enabled": True
                },
                "stereo_left": {
                    "device_id": 1,
                    "resolution": [640, 480], 
                    "fps": 2,
                    "enabled": False
                },
                "stereo_right": {
                    "device_id": 2,
                    "resolution": [640, 480],
                    "fps": 2,
                    "enabled": False
                }
            },
            "audio": {
                "device_index": None,  # Default input device
                "sample_rate": 16000,
                "chunk_size": 1024,
                "channels": 1,
                "recording_duration": 5.0,  # seconds
                "enabled": True
            },
            "environmental_sensors": {
                "temperature_humidity": {
                    "pin": 18,
                    "type": "DHT22",
                    "enabled": SENSORS_AVAILABLE
                },
                "pressure": {
                    "i2c_address": 0x77,
                    "type": "BMP280",
                    "enabled": SENSORS_AVAILABLE
                }
            },
            "processing": {
                "visual_processing_interval": 10,  # seconds
                "audio_processing_interval": 30,   # seconds
                "sensor_reading_interval": 60,     # seconds
                "novelty_threshold": 0.3,
                "save_interesting_images": True,
                "max_stored_images": 100
            },
            "output": {
                "visual_data": "data/visual_observations.json",
                "audio_data": "data/audio_observations.json", 
                "sensor_data": "data/sensor_readings.json",
                "stimulus_output": "data/sensor_stimulus.json",
                "image_storage": "data/images/"
            }
        }
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return {**default_config, **json.load(f)}
        else:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, 'w') as f:
                json.dump(default_config, f, indent=2)
            return default_config
    
    def setup_vision_system(self):
        """Initialize computer vision components"""
        try:
            # Load pre-trained ResNet for object recognition
            self.vision_model = resnet50(weights=ResNet50_Weights.IMAGENET1K_V2)
            self.vision_model.eval()
            
            # Image preprocessing
            self.vision_transform = transforms.Compose([
                transforms.Resize(224),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406], 
                                   std=[0.229, 0.224, 0.225])
            ])
            
            # Camera handles are owned by repryntt.hardware.camera_broker.
            # We just remember which sensor_id maps to which logical name.
            self.cameras = {}
            for name, cam_config in self.config["cameras"].items():
                if cam_config["enabled"]:
                    self.cameras[name] = int(cam_config["device_id"])
                    logger.info(
                        "Registered camera %s (sensor %s) with broker",
                        name, cam_config["device_id"],
                    )
            
            # Face detection
            self.face_cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
            )
            
            logger.info("Vision system initialized")
            
        except Exception as e:
            logger.error(f"Error setting up vision system: {e}")
    
    def setup_audio_system(self):
        """Initialize audio processing components"""
        try:
            if not self.config["audio"]["enabled"]:
                return
            
            # Initialize PyAudio
            self.audio = pyaudio.PyAudio()
            
            # Find default input device if not specified
            if self.config["audio"]["device_index"] is None:
                self.config["audio"]["device_index"] = self.audio.get_default_input_device_info()["index"]
            
            logger.info("Audio system initialized")
            
        except Exception as e:
            logger.error(f"Error setting up audio system: {e}")
            self.config["audio"]["enabled"] = False
    
    def setup_environmental_sensors(self):
        """Initialize environmental sensors"""
        try:
            if not SENSORS_AVAILABLE:
                logger.info("Running in sensor simulation mode")
                return
            
            self.environmental_sensors = {}
            
            # Temperature/Humidity sensor
            if self.config["environmental_sensors"]["temperature_humidity"]["enabled"]:
                try:
                    self.environmental_sensors["dht"] = adafruit_dht.DHT22(
                        board.D18  # GPIO pin 18
                    )
                    logger.info("DHT22 temperature/humidity sensor initialized")
                except Exception as e:
                    logger.error(f"Error initializing DHT22: {e}")
            
            # Pressure sensor
            if self.config["environmental_sensors"]["pressure"]["enabled"]:
                try:
                    i2c = busio.I2C(board.SCL, board.SDA)
                    self.environmental_sensors["bmp"] = adafruit_bmp280.Adafruit_BMP280_I2C(i2c)
                    logger.info("BMP280 pressure sensor initialized")
                except Exception as e:
                    logger.error(f"Error initializing BMP280: {e}")
            
        except Exception as e:
            logger.error(f"Error setting up environmental sensors: {e}")
    
    def capture_visual_data(self) -> Optional[VisualObservation]:
        """Capture and process visual data from cameras"""
        try:
            if not self.cameras:
                return None

            # Use primary camera (sensor_id from broker registration above)
            primary_id = self.cameras.get("primary")
            if primary_id is None:
                return None

            try:
                from repryntt.hardware.camera_broker import broker
                frame, _ts = broker.get_latest(
                    int(primary_id), max_age_ms=1000, timeout_s=2.0,
                )
            except Exception as e:
                logger.warning(f"camera_broker fetch failed: {e}")
                return None
            if frame is None:
                logger.warning("Failed to capture frame from primary camera")
                return None
            
            timestamp = time.time()
            
            # Save image if interesting
            image_path = None
            if self.config["processing"]["save_interesting_images"]:
                image_dir = self.config["output"]["image_storage"]
                os.makedirs(image_dir, exist_ok=True)
                image_path = os.path.join(image_dir, f"frame_{int(timestamp)}.jpg")
                cv2.imwrite(image_path, frame)
            
            # Convert to RGB for processing
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            # Detect objects
            detected_objects = self._detect_objects(rgb_frame)
            
            # Scene analysis
            scene_description = self._analyze_scene(rgb_frame, detected_objects)
            
            # Color analysis
            dominant_colors = self._extract_dominant_colors(rgb_frame)
            
            # Brightness analysis
            brightness = self._calculate_brightness(rgb_frame)
            
            # Motion detection (compare with previous frame)
            motion_detected = self._detect_motion(frame)
            
            # Face detection
            faces_detected = self._detect_faces(frame)
            
            # Novelty assessment
            novelty_score = self._assess_visual_novelty(rgb_frame)
            
            # Emotional valence (based on colors, objects, faces)
            emotional_valence = self._assess_emotional_valence(rgb_frame, detected_objects, faces_detected)
            
            # Visual complexity
            complexity_score = self._calculate_visual_complexity(rgb_frame)
            
            observation = VisualObservation(
                timestamp=timestamp,
                image_path=image_path or "",
                detected_objects=detected_objects,
                scene_description=scene_description,
                dominant_colors=dominant_colors,
                brightness=brightness,
                motion_detected=motion_detected,
                faces_detected=faces_detected,
                novelty_score=novelty_score,
                emotional_valence=emotional_valence,
                complexity_score=complexity_score
            )
            
            self.visual_memory.append(observation)
            return observation
            
        except Exception as e:
            logger.error(f"Error capturing visual data: {e}")
            return None
    
    def _detect_objects(self, rgb_frame: np.ndarray) -> List[Dict[str, Any]]:
        """Detect objects in the frame using ResNet features"""
        try:
            # Convert to PIL Image
            pil_image = Image.fromarray(rgb_frame)
            
            # Preprocess for model
            input_tensor = self.vision_transform(pil_image).unsqueeze(0)
            
            # Get features (not full classification to save compute)
            with torch.no_grad():
                features = self.vision_model.conv1(input_tensor)
                features = torch.nn.functional.adaptive_avg_pool2d(features, (1, 1))
                features = features.flatten()
            
            # Simple object detection based on feature patterns
            # (This is a simplified approach - real deployment would use YOLO/SSD)
            objects = []
            
            # Analyze color distribution for basic object types
            hsv_image = rgb2hsv(rgb_frame)
            
            # Green detection (plants, grass)
            green_mask = (hsv_image[:,:,0] > 0.25) & (hsv_image[:,:,0] < 0.4) & (hsv_image[:,:,1] > 0.3)
            if np.sum(green_mask) > 1000:  # Significant green area
                objects.append({
                    "type": "vegetation",
                    "confidence": min(np.sum(green_mask) / (rgb_frame.shape[0] * rgb_frame.shape[1]) * 2, 1.0),
                    "area_percentage": np.sum(green_mask) / (rgb_frame.shape[0] * rgb_frame.shape[1])
                })
            
            # Blue detection (sky, water)
            blue_mask = (hsv_image[:,:,0] > 0.5) & (hsv_image[:,:,0] < 0.7) & (hsv_image[:,:,1] > 0.3)
            if np.sum(blue_mask) > 500:
                objects.append({
                    "type": "sky_or_water",
                    "confidence": min(np.sum(blue_mask) / (rgb_frame.shape[0] * rgb_frame.shape[1]) * 3, 1.0),
                    "area_percentage": np.sum(blue_mask) / (rgb_frame.shape[0] * rgb_frame.shape[1])
                })
            
            # Edge detection for structured objects
            gray = rgb2gray(rgb_frame)
            edges = feature.canny(gray, sigma=1.0)
            edge_density = np.sum(edges) / (rgb_frame.shape[0] * rgb_frame.shape[1])
            
            if edge_density > 0.05:  # High edge density suggests structured objects
                objects.append({
                    "type": "structured_objects",
                    "confidence": min(edge_density * 10, 1.0),
                    "edge_density": edge_density
                })
            
            return objects
            
        except Exception as e:
            logger.error(f"Error detecting objects: {e}")
            return []
    
    def _analyze_scene(self, rgb_frame: np.ndarray, detected_objects: List[Dict]) -> str:
        """Generate scene description"""
        try:
            descriptions = []
            
            # Brightness-based description
            brightness = np.mean(rgb_frame) / 255.0
            if brightness > 0.7:
                descriptions.append("bright")
            elif brightness < 0.3:
                descriptions.append("dark")
            else:
                descriptions.append("moderately lit")
            
            # Object-based description
            for obj in detected_objects:
                if obj["type"] == "vegetation" and obj["confidence"] > 0.3:
                    descriptions.append("outdoor/natural setting")
                elif obj["type"] == "sky_or_water" and obj["confidence"] > 0.2:
                    descriptions.append("open space")
                elif obj["type"] == "structured_objects" and obj["confidence"] > 0.5:
                    descriptions.append("indoor/urban environment")
            
            # Color diversity
            dominant_colors = self._extract_dominant_colors(rgb_frame)
            if len(dominant_colors) > 4:
                descriptions.append("colorful scene")
            else:
                descriptions.append("simple color palette")
            
            return ", ".join(descriptions) if descriptions else "unclear scene"
            
        except Exception as e:
            logger.error(f"Error analyzing scene: {e}")
            return "analysis failed"
    
    def _extract_dominant_colors(self, rgb_frame: np.ndarray, k: int = 5) -> List[Tuple[int, int, int]]:
        """Extract dominant colors using k-means clustering"""
        try:
            # Reshape image to list of pixels
            pixels = rgb_frame.reshape(-1, 3)
            
            # Sample pixels for faster processing
            if len(pixels) > 10000:
                indices = np.random.choice(len(pixels), 10000, replace=False)
                pixels = pixels[indices]
            
            # K-means clustering
            kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
            kmeans.fit(pixels)
            
            # Get cluster centers (dominant colors)
            colors = kmeans.cluster_centers_.astype(int)
            
            return [tuple(color) for color in colors]
            
        except Exception as e:
            logger.error(f"Error extracting dominant colors: {e}")
            return [(128, 128, 128)]  # Gray fallback
    
    def _calculate_brightness(self, rgb_frame: np.ndarray) -> float:
        """Calculate overall brightness (0-1)"""
        return np.mean(rgb_frame) / 255.0
    
    def _detect_motion(self, current_frame: np.ndarray) -> bool:
        """Detect motion by comparing with previous frame"""
        try:
            if not hasattr(self, '_previous_frame'):
                self._previous_frame = current_frame.copy()
                return False
            
            # Convert to grayscale
            current_gray = cv2.cvtColor(current_frame, cv2.COLOR_BGR2GRAY)
            previous_gray = cv2.cvtColor(self._previous_frame, cv2.COLOR_BGR2GRAY)
            
            # Calculate frame difference
            diff = cv2.absdiff(current_gray, previous_gray)
            
            # Threshold difference
            _, thresh = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)
            
            # Count non-zero pixels
            motion_pixels = cv2.countNonZero(thresh)
            motion_percentage = motion_pixels / (current_frame.shape[0] * current_frame.shape[1])
            
            # Update previous frame
            self._previous_frame = current_frame.copy()
            
            return motion_percentage > 0.01  # 1% threshold
            
        except Exception as e:
            logger.error(f"Error detecting motion: {e}")
            return False
    
    def _detect_faces(self, frame: np.ndarray) -> int:
        """Detect number of faces in frame"""
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = self.face_cascade.detectMultiScale(gray, 1.1, 4)
            return len(faces)
        except Exception as e:
            logger.error(f"Error detecting faces: {e}")
            return 0
    
    def _assess_visual_novelty(self, rgb_frame: np.ndarray) -> float:
        """Assess how novel/different this frame is from recent observations"""
        try:
            # Extract simple features for comparison
            features = self._extract_visual_features(rgb_frame)
            
            if self.previous_visual_features is None:
                self.previous_visual_features = features
                return 0.5  # Moderate novelty for first observation
            
            # Calculate similarity
            similarity = np.corrcoef(features, self.previous_visual_features)[0, 1]
            if np.isnan(similarity):
                similarity = 0.0
            
            novelty = 1.0 - abs(similarity)
            
            # Update previous features
            self.previous_visual_features = features
            
            return max(0.0, min(novelty, 1.0))
            
        except Exception as e:
            logger.error(f"Error assessing visual novelty: {e}")
            return 0.5
    
    def _extract_visual_features(self, rgb_frame: np.ndarray) -> np.ndarray:
        """Extract feature vector from image"""
        try:
            # Color histogram
            hist_r = np.histogram(rgb_frame[:,:,0], bins=8, range=(0, 256))[0]
            hist_g = np.histogram(rgb_frame[:,:,1], bins=8, range=(0, 256))[0]
            hist_b = np.histogram(rgb_frame[:,:,2], bins=8, range=(0, 256))[0]
            
            # Edge density
            gray = rgb2gray(rgb_frame)
            edges = feature.canny(gray, sigma=1.0)
            edge_density = np.sum(edges) / edges.size
            
            # Texture features (LBP)
            lbp = feature.local_binary_pattern(gray, 8, 1, method='uniform')
            lbp_hist = np.histogram(lbp, bins=10)[0]
            
            # Combine features
            features = np.concatenate([hist_r, hist_g, hist_b, [edge_density], lbp_hist])
            
            return features / (np.linalg.norm(features) + 1e-8)  # Normalize
            
        except Exception as e:
            logger.error(f"Error extracting visual features: {e}")
            return np.zeros(35)  # Default feature vector size
    
    def _assess_emotional_valence(self, rgb_frame: np.ndarray, objects: List[Dict], faces: int) -> float:
        """Assess emotional valence of the scene (-1 to 1)"""
        try:
            valence = 0.0
            
            # Color-based emotion
            dominant_colors = self._extract_dominant_colors(rgb_frame, k=3)
            for color in dominant_colors:
                r, g, b = color
                # Warm colors tend to be more positive
                warmth = (r + g * 0.5) / (b + 1)
                valence += (warmth - 1.0) * 0.1
            
            # Brightness contribution
            brightness = np.mean(rgb_frame) / 255.0
            valence += (brightness - 0.5) * 0.3  # Brighter = more positive
            
            # Object contribution
            for obj in objects:
                if obj["type"] == "vegetation":
                    valence += obj["confidence"] * 0.2  # Nature is positive
                elif obj["type"] == "sky_or_water":
                    valence += obj["confidence"] * 0.1  # Open space is positive
            
            # Face contribution
            if faces > 0:
                valence += 0.2  # Human presence is generally positive
            
            return max(-1.0, min(valence, 1.0))
            
        except Exception as e:
            logger.error(f"Error assessing emotional valence: {e}")
            return 0.0
    
    def _calculate_visual_complexity(self, rgb_frame: np.ndarray) -> float:
        """Calculate visual complexity (0-1)"""
        try:
            # Edge density
            gray = rgb2gray(rgb_frame)
            edges = feature.canny(gray, sigma=1.0)
            edge_complexity = np.sum(edges) / edges.size
            
            # Color diversity
            colors = self._extract_dominant_colors(rgb_frame, k=8)
            color_variance = np.var([np.linalg.norm(color) for color in colors])
            color_complexity = min(color_variance / 10000, 1.0)
            
            # Texture complexity
            lbp = feature.local_binary_pattern(gray, 8, 1, method='uniform')
            texture_complexity = len(np.unique(lbp)) / 256.0
            
            # Combine measures
            complexity = (edge_complexity * 0.4 + color_complexity * 0.3 + texture_complexity * 0.3)
            
            return max(0.0, min(complexity, 1.0))
            
        except Exception as e:
            logger.error(f"Error calculating visual complexity: {e}")
            return 0.5
    
    def capture_audio_data(self) -> Optional[AudioObservation]:
        """Capture and process audio data"""
        try:
            if not self.config["audio"]["enabled"]:
                return None
            
            timestamp = time.time()
            
            # Record audio
            audio_data = self._record_audio()
            if audio_data is None:
                return None
            
            # Save audio file
            audio_dir = os.path.dirname(self.config["output"]["audio_data"])
            os.makedirs(audio_dir, exist_ok=True)
            audio_path = os.path.join(audio_dir, f"audio_{int(timestamp)}.wav")
            
            # Save as WAV file
            with wave.open(audio_path, 'wb') as wf:
                wf.setnchannels(self.config["audio"]["channels"])
                wf.setsampwidth(self.audio.get_sample_size(pyaudio.paInt16))
                wf.setframerate(self.config["audio"]["sample_rate"])
                wf.writeframes(audio_data)
            
            # Process audio
            audio_float = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            
            # Amplitude analysis
            amplitude_mean = float(np.mean(np.abs(audio_float)))
            
            # Frequency analysis
            frequency_profile = self._analyze_frequency_content(audio_float)
            
            # Speech detection (basic)
            speech_detected = self._detect_speech(audio_float)
            
            # Music detection (basic)
            music_detected = self._detect_music(audio_float)
            
            # Noise level
            noise_level = float(np.std(audio_float))
            
            # Emotional tone
            emotional_tone = self._assess_audio_emotion(audio_float, frequency_profile)
            
            # Novelty assessment
            novelty_score = self._assess_audio_novelty(frequency_profile)
            
            observation = AudioObservation(
                timestamp=timestamp,
                audio_path=audio_path,
                amplitude_mean=amplitude_mean,
                frequency_profile=frequency_profile,
                speech_detected=speech_detected,
                music_detected=music_detected,
                noise_level=noise_level,
                emotional_tone=emotional_tone,
                novelty_score=novelty_score
            )
            
            self.audio_memory.append(observation)
            return observation
            
        except Exception as e:
            logger.error(f"Error capturing audio data: {e}")
            return None
    
    def _record_audio(self) -> Optional[bytes]:
        """Record audio sample"""
        try:
            stream = self.audio.open(
                format=pyaudio.paInt16,
                channels=self.config["audio"]["channels"],
                rate=self.config["audio"]["sample_rate"],
                input=True,
                input_device_index=self.config["audio"]["device_index"],
                frames_per_buffer=self.config["audio"]["chunk_size"]
            )
            
            frames = []
            num_chunks = int(self.config["audio"]["sample_rate"] * 
                           self.config["audio"]["recording_duration"] / 
                           self.config["audio"]["chunk_size"])
            
            for _ in range(num_chunks):
                data = stream.read(self.config["audio"]["chunk_size"])
                frames.append(data)
            
            stream.stop_stream()
            stream.close()
            
            return b''.join(frames)
            
        except Exception as e:
            logger.error(f"Error recording audio: {e}")
            return None
    
    def _analyze_frequency_content(self, audio_data: np.ndarray) -> List[float]:
        """Analyze frequency content of audio"""
        try:
            # FFT analysis
            fft = np.fft.fft(audio_data)
            magnitude = np.abs(fft[:len(fft)//2])
            
            # Divide into frequency bands
            bands = np.array_split(magnitude, 10)
            band_energies = [float(np.mean(band)) for band in bands]
            
            return band_energies
            
        except Exception as e:
            logger.error(f"Error analyzing frequency content: {e}")
            return [0.0] * 10
    
    def _detect_speech(self, audio_data: np.ndarray) -> bool:
        """Basic speech detection"""
        try:
            # Speech typically has energy in 300-3400 Hz range
            sample_rate = self.config["audio"]["sample_rate"]
            freqs, times, spectrogram = scipy.signal.spectrogram(audio_data, sample_rate)
            
            # Find indices for speech frequency range
            speech_freq_mask = (freqs >= 300) & (freqs <= 3400)
            speech_energy = np.mean(spectrogram[speech_freq_mask, :])
            
            # Compare to total energy
            total_energy = np.mean(spectrogram)
            
            return (speech_energy / (total_energy + 1e-8)) > 0.3
            
        except Exception as e:
            logger.error(f"Error detecting speech: {e}")
            return False
    
    def _detect_music(self, audio_data: np.ndarray) -> bool:
        """Basic music detection"""
        try:
            # Music typically has more harmonic structure
            # Simple heuristic: check for rhythmic patterns
            
            # Onset detection
            onset_frames = librosa.onset.onset_detect(
                y=audio_data, 
                sr=self.config["audio"]["sample_rate"],
                units='frames'
            )
            
            # If we have regular onsets, might be music
            return len(onset_frames) > 3
            
        except Exception as e:
            logger.error(f"Error detecting music: {e}")
            return False
    
    def _assess_audio_emotion(self, audio_data: np.ndarray, frequency_profile: List[float]) -> str:
        """Assess emotional tone of audio"""
        try:
            # Simple heuristics based on audio characteristics
            amplitude = np.mean(np.abs(audio_data))
            high_freq_energy = sum(frequency_profile[7:])  # High frequency bands
            low_freq_energy = sum(frequency_profile[:3])   # Low frequency bands
            
            if amplitude > 0.1:
                if high_freq_energy > low_freq_energy * 1.5:
                    return "excited"
                else:
                    return "active"
            else:
                if low_freq_energy > high_freq_energy * 2:
                    return "calm"
                else:
                    return "quiet"
                    
        except Exception as e:
            logger.error(f"Error assessing audio emotion: {e}")
            return "neutral"
    
    def _assess_audio_novelty(self, frequency_profile: List[float]) -> float:
        """Assess audio novelty compared to recent observations"""
        try:
            if self.previous_audio_features is None:
                self.previous_audio_features = frequency_profile
                return 0.5
            
            # Calculate similarity
            similarity = np.corrcoef(frequency_profile, self.previous_audio_features)[0, 1]
            if np.isnan(similarity):
                similarity = 0.0
            
            novelty = 1.0 - abs(similarity)
            
            # Update previous features
            self.previous_audio_features = frequency_profile
            
            return max(0.0, min(novelty, 1.0))
            
        except Exception as e:
            logger.error(f"Error assessing audio novelty: {e}")
            return 0.5
    
    def read_environmental_sensors(self) -> List[SensorReading]:
        """Read data from environmental sensors"""
        readings = []
        timestamp = time.time()
        
        if not SENSORS_AVAILABLE:
            # Simulate sensor readings
            readings.extend([
                SensorReading(timestamp, "temperature", 22.5 + np.random.normal(0, 1), "°C", "ambient", 0.9),
                SensorReading(timestamp, "humidity", 45.0 + np.random.normal(0, 5), "%", "ambient", 0.9),
                SensorReading(timestamp, "pressure", 1013.25 + np.random.normal(0, 2), "hPa", "ambient", 0.9)
            ])
            return readings
        
        # Real sensor readings
        try:
            # Temperature and humidity
            if "dht" in self.environmental_sensors:
                try:
                    temperature = self.environmental_sensors["dht"].temperature
                    humidity = self.environmental_sensors["dht"].humidity
                    
                    if temperature is not None:
                        readings.append(SensorReading(timestamp, "temperature", temperature, "°C", "ambient", 0.95))
                    if humidity is not None:
                        readings.append(SensorReading(timestamp, "humidity", humidity, "%", "ambient", 0.95))
                        
                except RuntimeError as e:
                    logger.warning(f"DHT sensor reading error: {e}")
            
            # Pressure
            if "bmp" in self.environmental_sensors:
                try:
                    pressure = self.environmental_sensors["bmp"].pressure
                    readings.append(SensorReading(timestamp, "pressure", pressure, "hPa", "ambient", 0.95))
                except Exception as e:
                    logger.warning(f"BMP sensor reading error: {e}")
            
        except Exception as e:
            logger.error(f"Error reading environmental sensors: {e}")
        
        # Store readings
        for reading in readings:
            self.sensor_memory.append(reading)
        
        return readings
    
    def generate_stimulus(self, visual_obs: Optional[VisualObservation] = None,
                         audio_obs: Optional[AudioObservation] = None, 
                         sensor_readings: Optional[List[SensorReading]] = None) -> Dict[str, float]:
        """Generate hormone stimulus based on sensor data"""
        
        stimulus = {
            'adrenaline': 0.0,   # Novelty, motion, complexity
            'serotonin': 0.0,    # Pleasant environment, faces
            'dopamine': 0.0,     # Interesting discoveries, positive valence
            'cortisol': 0.0,     # Sudden changes, loud noises, threats
            'oxytocin': 0.0      # Human presence, social cues
        }
        
        # Visual contributions
        if visual_obs:
            # Novelty drives curiosity (adrenaline)
            stimulus['adrenaline'] += visual_obs.novelty_score * 0.4
            
            # Complexity drives interest (adrenaline)
            stimulus['adrenaline'] += visual_obs.complexity_score * 0.3
            
            # Motion drives alertness (adrenaline)
            if visual_obs.motion_detected:
                stimulus['adrenaline'] += 0.2
            
            # Positive emotional valence (dopamine, serotonin)
            if visual_obs.emotional_valence > 0:
                stimulus['dopamine'] += visual_obs.emotional_valence * 0.4
                stimulus['serotonin'] += visual_obs.emotional_valence * 0.3
            
            # Faces promote social connection (oxytocin)
            if visual_obs.faces_detected > 0:
                stimulus['oxytocin'] += min(visual_obs.faces_detected * 0.3, 0.6)
            
            # Bright environments are pleasant (serotonin)
            stimulus['serotonin'] += visual_obs.brightness * 0.2
            
            # Very dark or very bright can cause stress (cortisol)
            if visual_obs.brightness < 0.1 or visual_obs.brightness > 0.9:
                stimulus['cortisol'] += 0.2
        
        # Audio contributions
        if audio_obs:
            # Audio novelty drives curiosity (adrenaline)
            stimulus['adrenaline'] += audio_obs.novelty_score * 0.3
            
            # Speech promotes social connection (oxytocin)
            if audio_obs.speech_detected:
                stimulus['oxytocin'] += 0.4
            
            # Music can be pleasant (serotonin, dopamine)
            if audio_obs.music_detected:
                stimulus['serotonin'] += 0.3
                stimulus['dopamine'] += 0.2
            
            # High noise levels cause stress (cortisol)
            if audio_obs.noise_level > 0.3:
                stimulus['cortisol'] += (audio_obs.noise_level - 0.3) * 0.5
            
            # Emotional tone contributions
            if audio_obs.emotional_tone == "excited":
                stimulus['adrenaline'] += 0.3
                stimulus['dopamine'] += 0.2
            elif audio_obs.emotional_tone == "calm":
                stimulus['serotonin'] += 0.4
            elif audio_obs.emotional_tone == "active":
                stimulus['adrenaline'] += 0.2
        
        # Environmental sensor contributions
        if sensor_readings:
            for reading in sensor_readings:
                if reading.sensor_type == "temperature":
                    # Comfortable temperature range (18-24°C)
                    if 18 <= reading.value <= 24:
                        stimulus['serotonin'] += 0.1
                    else:
                        # Temperature stress
                        temp_stress = min(abs(reading.value - 21) / 10, 0.3)
                        stimulus['cortisol'] += temp_stress
                
                elif reading.sensor_type == "humidity":
                    # Comfortable humidity range (40-60%)
                    if 40 <= reading.value <= 60:
                        stimulus['serotonin'] += 0.1
                    else:
                        # Humidity stress
                        humidity_stress = min(abs(reading.value - 50) / 20, 0.2)
                        stimulus['cortisol'] += humidity_stress
                
                elif reading.sensor_type == "pressure":
                    # Pressure changes can indicate weather changes (mild curiosity)
                    if hasattr(self, '_last_pressure'):
                        pressure_change = abs(reading.value - self._last_pressure)
                        if pressure_change > 5:  # Significant pressure change
                            stimulus['adrenaline'] += min(pressure_change / 20, 0.2)
                    self._last_pressure = reading.value
        
        # Normalize all stimulus values to [0, 1]
        for hormone in stimulus:
            stimulus[hormone] = max(0.0, min(stimulus[hormone], 1.0))
        
        return stimulus
    
    def save_observations(self, visual_obs: Optional[VisualObservation] = None,
                         audio_obs: Optional[AudioObservation] = None,
                         sensor_readings: Optional[List[SensorReading]] = None):
        """Save observations to files"""
        try:
            # Save visual observations
            if visual_obs:
                self._save_json_data(self.config["output"]["visual_data"], 
                                   asdict(visual_obs), "visual_observations")
            
            # Save audio observations  
            if audio_obs:
                self._save_json_data(self.config["output"]["audio_data"],
                                   asdict(audio_obs), "audio_observations")
            
            # Save sensor readings
            if sensor_readings:
                readings_data = [asdict(reading) for reading in sensor_readings]
                self._save_json_data(self.config["output"]["sensor_data"],
                                   readings_data, "sensor_readings")
            
        except Exception as e:
            logger.error(f"Error saving observations: {e}")
    
    def _save_json_data(self, file_path: str, data: Any, data_type: str):
        """Save data to JSON file with history"""
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            # Load existing data
            existing_data = []
            if os.path.exists(file_path):
                with open(file_path, 'r') as f:
                    file_content = json.load(f)
                    existing_data = file_content.get(data_type, [])
            
            # Add new data
            if isinstance(data, list):
                existing_data.extend(data)
            else:
                existing_data.append(data)
            
            # Keep only recent entries (last 500)
            existing_data = sorted(existing_data, key=lambda x: x.get('timestamp', 0), reverse=True)[:500]
            
            # Save
            output_data = {
                data_type: existing_data,
                'last_updated': time.time(),
                'total_entries': len(existing_data)
            }
            
            with open(file_path, 'w') as f:
                json.dump(output_data, f, indent=2)
                
        except Exception as e:
            logger.error(f"Error saving JSON data to {file_path}: {e}")
    
    def run_sensor_cycle(self) -> Dict[str, float]:
        """Run one complete sensor data collection cycle"""
        logger.info("Starting sensor data collection cycle...")
        
        # Collect data from all sensors
        visual_obs = self.capture_visual_data()
        audio_obs = self.capture_audio_data()
        sensor_readings = self.read_environmental_sensors()
        
        # Generate stimulus
        stimulus = self.generate_stimulus(visual_obs, audio_obs, sensor_readings)
        
        # Save observations
        self.save_observations(visual_obs, audio_obs, sensor_readings)
        
        # Save stimulus
        stimulus_data = {
            "timestamp": time.time(),
            "source": "sensor_feeder",
            "stimulus": stimulus,
            "metadata": {
                "visual_novelty": visual_obs.novelty_score if visual_obs else 0,
                "audio_novelty": audio_obs.novelty_score if audio_obs else 0,
                "faces_detected": visual_obs.faces_detected if visual_obs else 0,
                "motion_detected": visual_obs.motion_detected if visual_obs else False,
                "speech_detected": audio_obs.speech_detected if audio_obs else False,
                "sensor_count": len(sensor_readings) if sensor_readings else 0
            }
        }
        
        os.makedirs(os.path.dirname(self.config["output"]["stimulus_output"]), exist_ok=True)
        with open(self.config["output"]["stimulus_output"], 'w') as f:
            json.dump(stimulus_data, f, indent=2)
        
        logger.info(f"Generated sensor stimulus: {stimulus}")
        
        return stimulus
    
    def run_continuous(self):
        """Run continuous sensor monitoring"""
        logger.info("Starting continuous sensor monitoring...")
        self.running = True
        
        # Start processing threads
        self.processing_threads = [
            threading.Thread(target=self._visual_processing_loop, daemon=True),
            threading.Thread(target=self._audio_processing_loop, daemon=True),
            threading.Thread(target=self._sensor_processing_loop, daemon=True)
        ]
        
        for thread in self.processing_threads:
            thread.start()
        
        try:
            while self.running:
                time.sleep(1)  # Main thread sleep
        except KeyboardInterrupt:
            logger.info("Sensor feeder stopped by user")
            self.running = False
        
        # Wait for threads to finish
        for thread in self.processing_threads:
            thread.join(timeout=5)
        
        # Cleanup
        for camera in self.cameras.values():
            camera.release()
        
        if hasattr(self, 'audio'):
            self.audio.terminate()
    
    def _visual_processing_loop(self):
        """Background visual processing loop"""
        while self.running:
            try:
                visual_obs = self.capture_visual_data()
                if visual_obs:
                    self.visual_queue.put(visual_obs, timeout=1)
                
                time.sleep(self.config["processing"]["visual_processing_interval"])
                
            except Exception as e:
                logger.error(f"Error in visual processing loop: {e}")
                time.sleep(5)  # Brief pause on error
    
    def _audio_processing_loop(self):
        """Background audio processing loop"""
        while self.running:
            try:
                audio_obs = self.capture_audio_data()
                if audio_obs:
                    self.audio_queue.put(audio_obs, timeout=1)
                
                time.sleep(self.config["processing"]["audio_processing_interval"])
                
            except Exception as e:
                logger.error(f"Error in audio processing loop: {e}")
                time.sleep(5)  # Brief pause on error
    
    def _sensor_processing_loop(self):
        """Background sensor reading loop"""
        while self.running:
            try:
                sensor_readings = self.read_environmental_sensors()
                if sensor_readings:
                    for reading in sensor_readings:
                        self.sensor_queue.put(reading, timeout=1)
                
                time.sleep(self.config["processing"]["sensor_reading_interval"])
                
            except Exception as e:
                logger.error(f"Error in sensor processing loop: {e}")
                time.sleep(10)  # Longer pause for sensor errors

def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    feeder = SensorFeeder()
    
    # Test mode: run single sensor cycle
    if len(os.sys.argv) > 1 and os.sys.argv[1] == '--test':
        stimulus = feeder.run_sensor_cycle()
        print(f"Generated sensor stimulus: {stimulus}")
    else:
        # Continuous monitoring mode
        feeder.run_continuous()

if __name__ == "__main__":
    main()