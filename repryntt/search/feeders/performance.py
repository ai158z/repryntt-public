#!/usr/bin/env python3
"""
Performance Feeder - SAIGE System Monitoring Pipeline
Monitors system performance, learning metrics, and hardware status
Real implementation with hardware monitoring and optimization stimulus
"""

import json
import os
import time
import logging
import psutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, asdict
import numpy as np
from collections import deque, defaultdict
import threading
import queue

# GPU monitoring (if available)
try:
    import GPUtil
    GPU_AVAILABLE = True
except ImportError:
    GPU_AVAILABLE = False
    logging.warning("GPUtil not available - GPU monitoring disabled")

# Jetson-specific monitoring
try:
    import jtop
    JETSON_AVAILABLE = True
except ImportError:
    JETSON_AVAILABLE = False
    logging.info("jtop not available - running on non-Jetson hardware")

logger = logging.getLogger(__name__)

@dataclass
class SystemMetrics:
    timestamp: float
    cpu_usage: float  # Percentage
    memory_usage: float  # Percentage
    memory_available: float  # GB
    disk_usage: float  # Percentage
    disk_free: float  # GB
    temperature: Optional[float]  # Celsius
    power_consumption: Optional[float]  # Watts
    gpu_usage: Optional[float]  # Percentage
    gpu_memory: Optional[float]  # Percentage
    gpu_temperature: Optional[float]  # Celsius
    network_sent: float  # MB
    network_recv: float  # MB
    process_count: int
    load_average: List[float]  # 1, 5, 15 minute load averages

@dataclass
class LearningMetrics:
    timestamp: float
    training_cycles_completed: int
    average_loss: Optional[float]
    learning_rate: Optional[float]
    model_accuracy: Optional[float]
    inference_time: Optional[float]  # milliseconds
    memory_efficiency: float  # How well memory is being used
    convergence_rate: Optional[float]  # Learning improvement per cycle
    knowledge_retention: Optional[float]  # How much knowledge is retained
    adaptation_speed: Optional[float]  # How quickly system adapts to new data
    curiosity_satisfaction: float  # How well curiosity is being satisfied
    stimulus_response_rate: float  # How responsive system is to stimulus

@dataclass
class PerformanceAlert:
    timestamp: float
    alert_type: str  # 'warning', 'critical', 'info'
    category: str  # 'hardware', 'learning', 'efficiency'
    message: str
    metric_value: float
    threshold: float
    impact_level: float  # 0-1, how much this affects SAIGE
    auto_correctable: bool

class PerformanceFeeder:
    """
    Monitors SAIGE's performance and generates optimization stimulus
    """
    
    def __init__(self, config_path: str = "config/performance_feeder.json"):
        self.config = self._load_config(config_path)
        
        # Performance history
        self.system_metrics_history = deque(maxlen=1000)
        self.learning_metrics_history = deque(maxlen=500)
        self.alerts_history = deque(maxlen=200)
        
        # Baseline metrics for comparison
        self.baseline_metrics = None
        self.performance_trends = {}
        
        # Jetson monitoring
        if JETSON_AVAILABLE:
            try:
                self.jetson = jtop.jtop()
                self.jetson.start()
            except Exception as e:
                logger.error(f"Failed to initialize Jetson monitoring: {e}")
                self.jetson = None
        else:
            self.jetson = None
        
        # Previous network stats for delta calculation
        self.prev_network_stats = None
        
        # Learning state tracking
        self.learning_state = {
            'total_training_cycles': 0,
            'successful_adaptations': 0,
            'failed_adaptations': 0,
            'last_model_update': None,
            'knowledge_base_size': 0,
            'stimulus_processing_times': deque(maxlen=100)
        }
    
    def _load_config(self, config_path: str) -> Dict:
        """Load configuration or create default"""
        default_config = {
            "monitoring": {
                "system_check_interval": 30,  # seconds
                "learning_check_interval": 60,  # seconds
                "alert_check_interval": 120,  # seconds
                "baseline_establishment_cycles": 20
            },
            "thresholds": {
                "cpu_usage": {
                    "warning": 80.0,
                    "critical": 95.0
                },
                "memory_usage": {
                    "warning": 85.0,
                    "critical": 95.0
                },
                "temperature": {
                    "warning": 70.0,  # Celsius
                    "critical": 85.0
                },
                "gpu_usage": {
                    "warning": 90.0,
                    "critical": 98.0
                },
                "disk_usage": {
                    "warning": 80.0,
                    "critical": 90.0
                },
                "inference_time": {
                    "warning": 1000.0,  # milliseconds
                    "critical": 5000.0
                },
                "learning_efficiency": {
                    "warning": 0.3,  # Below 30% efficiency
                    "critical": 0.1   # Below 10% efficiency
                }
            },
            "optimization": {
                "auto_throttling": True,
                "memory_cleanup": True,
                "adaptive_learning_rate": True,
                "thermal_management": True
            },
            "learning_analysis": {
                "track_convergence": True,
                "measure_retention": True,
                "analyze_adaptation": True,
                "monitor_curiosity": True
            },
            "output": {
                "system_metrics": "data/system_metrics.json",
                "learning_metrics": "data/learning_metrics.json",
                "performance_alerts": "data/performance_alerts.json",
                "stimulus_output": "data/performance_stimulus.json"
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
    
    def collect_system_metrics(self) -> SystemMetrics:
        """Collect comprehensive system performance metrics"""
        try:
            timestamp = time.time()
            
            # CPU metrics
            cpu_usage = psutil.cpu_percent(interval=1)
            
            # Memory metrics
            memory = psutil.virtual_memory()
            memory_usage = memory.percent
            memory_available = memory.available / (1024**3)  # GB
            
            # Disk metrics
            disk = psutil.disk_usage('/')
            disk_usage = (disk.used / disk.total) * 100
            disk_free = disk.free / (1024**3)  # GB
            
            # Temperature (attempt multiple sources)
            temperature = self._get_system_temperature()
            
            # Power consumption (Jetson specific)
            power_consumption = self._get_power_consumption()
            
            # GPU metrics
            gpu_usage, gpu_memory, gpu_temperature = self._get_gpu_metrics()
            
            # Network metrics
            current_network = psutil.net_io_counters()
            if self.prev_network_stats:
                network_sent = (current_network.bytes_sent - self.prev_network_stats.bytes_sent) / (1024**2)  # MB
                network_recv = (current_network.bytes_recv - self.prev_network_stats.bytes_recv) / (1024**2)  # MB
            else:
                network_sent = network_recv = 0.0
            self.prev_network_stats = current_network
            
            # Process metrics
            process_count = len(psutil.pids())
            
            # Load average
            load_average = list(os.getloadavg()) if hasattr(os, 'getloadavg') else [0.0, 0.0, 0.0]
            
            metrics = SystemMetrics(
                timestamp=timestamp,
                cpu_usage=cpu_usage,
                memory_usage=memory_usage,
                memory_available=memory_available,
                disk_usage=disk_usage,
                disk_free=disk_free,
                temperature=temperature,
                power_consumption=power_consumption,
                gpu_usage=gpu_usage,
                gpu_memory=gpu_memory,
                gpu_temperature=gpu_temperature,
                network_sent=network_sent,
                network_recv=network_recv,
                process_count=process_count,
                load_average=load_average
            )
            
            self.system_metrics_history.append(metrics)
            return metrics
            
        except Exception as e:
            logger.error(f"Error collecting system metrics: {e}")
            return None
    
    def _get_system_temperature(self) -> Optional[float]:
        """Get system temperature from various sources"""
        try:
            # Jetson temperature
            if self.jetson and self.jetson.ok:
                try:
                    stats = self.jetson.stats
                    if 'Temp CPU' in stats:
                        return float(stats['Temp CPU'])
                    elif 'temperature' in stats:
                        return float(stats['temperature'])
                except:
                    pass
            
            # Linux thermal zones
            try:
                thermal_zones = Path('/sys/class/thermal').glob('thermal_zone*')
                for zone in thermal_zones:
                    temp_file = zone / 'temp'
                    if temp_file.exists():
                        temp = int(temp_file.read_text().strip()) / 1000.0  # milli-celsius to celsius
                        if 30 <= temp <= 100:  # Reasonable temperature range
                            return temp
            except:
                pass
            
            # psutil sensors (if available)
            try:
                if hasattr(psutil, 'sensors_temperatures'):
                    temps = psutil.sensors_temperatures()
                    for name, entries in temps.items():
                        if entries and entries[0].current:
                            return entries[0].current
            except:
                pass
            
            return None
            
        except Exception as e:
            logger.debug(f"Error getting temperature: {e}")
            return None
    
    def _get_power_consumption(self) -> Optional[float]:
        """Get power consumption (Jetson specific)"""
        try:
            if self.jetson and self.jetson.ok:
                stats = self.jetson.stats
                if 'Power TOT' in stats:
                    return float(stats['Power TOT'])
            
            # Alternative: read from power supply files
            power_files = [
                '/sys/class/power_supply/BAT0/power_now',
                '/sys/class/power_supply/BAT1/power_now'
            ]
            
            for power_file in power_files:
                try:
                    if os.path.exists(power_file):
                        power_uw = int(Path(power_file).read_text().strip())
                        return power_uw / 1000000.0  # micro-watts to watts
                except:
                    continue
            
            return None
            
        except Exception as e:
            logger.debug(f"Error getting power consumption: {e}")
            return None
    
    def _get_gpu_metrics(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Get GPU utilization, memory, and temperature"""
        try:
            # Jetson GPU
            if self.jetson and self.jetson.ok:
                stats = self.jetson.stats
                gpu_usage = stats.get('GPU', None)
                gpu_memory = None  # Jetson uses unified memory
                gpu_temp = stats.get('Temp GPU', None)
                
                if gpu_usage is not None:
                    gpu_usage = float(gpu_usage)
                if gpu_temp is not None:
                    gpu_temp = float(gpu_temp)
                
                return gpu_usage, gpu_memory, gpu_temp
            
            # NVIDIA GPU via GPUtil
            if GPU_AVAILABLE:
                gpus = GPUtil.getGPUs()
                if gpus:
                    gpu = gpus[0]  # First GPU
                    return gpu.load * 100, gpu.memoryUtil * 100, gpu.temperature
            
            return None, None, None
            
        except Exception as e:
            logger.debug(f"Error getting GPU metrics: {e}")
            return None, None, None
    
    def collect_learning_metrics(self) -> LearningMetrics:
        """Collect learning performance and effectiveness metrics"""
        try:
            timestamp = time.time()
            
            # Read learning state from SAIGE files
            training_cycles = self._get_training_cycles()
            average_loss = self._get_average_loss()
            learning_rate = self._get_learning_rate()
            model_accuracy = self._get_model_accuracy()
            inference_time = self._get_inference_time()
            
            # Calculate derived metrics
            memory_efficiency = self._calculate_memory_efficiency()
            convergence_rate = self._calculate_convergence_rate()
            knowledge_retention = self._calculate_knowledge_retention()
            adaptation_speed = self._calculate_adaptation_speed()
            curiosity_satisfaction = self._calculate_curiosity_satisfaction()
            stimulus_response_rate = self._calculate_stimulus_response_rate()
            
            metrics = LearningMetrics(
                timestamp=timestamp,
                training_cycles_completed=training_cycles,
                average_loss=average_loss,
                learning_rate=learning_rate,
                model_accuracy=model_accuracy,
                inference_time=inference_time,
                memory_efficiency=memory_efficiency,
                convergence_rate=convergence_rate,
                knowledge_retention=knowledge_retention,
                adaptation_speed=adaptation_speed,
                curiosity_satisfaction=curiosity_satisfaction,
                stimulus_response_rate=stimulus_response_rate
            )
            
            self.learning_metrics_history.append(metrics)
            return metrics
            
        except Exception as e:
            logger.error(f"Error collecting learning metrics: {e}")
            return None
    
    def _get_training_cycles(self) -> int:
        """Get number of completed training cycles"""
        try:
            # Check for evolution loop state file
            state_files = [
                "data/evolution_state.json",
                "logs/training_log.json",
                "brain/learning_progress.json"
            ]
            
            for state_file in state_files:
                if os.path.exists(state_file):
                    with open(state_file, 'r') as f:
                        data = json.load(f)
                        if 'training_cycles' in data:
                            return data['training_cycles']
                        elif 'cycles_completed' in data:
                            return data['cycles_completed']
            
            return self.learning_state['total_training_cycles']
            
        except Exception as e:
            logger.debug(f"Error getting training cycles: {e}")
            return 0
    
    def _get_average_loss(self) -> Optional[float]:
        """Get current average training loss"""
        try:
            loss_files = [
                "data/training_loss.json",
                "logs/loss_history.json",
                "brain/learning_metrics.json"
            ]
            
            for loss_file in loss_files:
                if os.path.exists(loss_file):
                    with open(loss_file, 'r') as f:
                        data = json.load(f)
                        if 'current_loss' in data:
                            return float(data['current_loss'])
                        elif 'average_loss' in data:
                            return float(data['average_loss'])
                        elif 'loss_history' in data and data['loss_history']:
                            return float(np.mean(data['loss_history'][-10:]))  # Last 10 values
            
            return None
            
        except Exception as e:
            logger.debug(f"Error getting average loss: {e}")
            return None
    
    def _get_learning_rate(self) -> Optional[float]:
        """Get current learning rate"""
        try:
            lr_files = [
                "data/learning_config.json",
                "brain/hyperparameters.json",
                "config/model_config.json"
            ]
            
            for lr_file in lr_files:
                if os.path.exists(lr_file):
                    with open(lr_file, 'r') as f:
                        data = json.load(f)
                        if 'learning_rate' in data:
                            return float(data['learning_rate'])
                        elif 'lr' in data:
                            return float(data['lr'])
            
            return None
            
        except Exception as e:
            logger.debug(f"Error getting learning rate: {e}")
            return None
    
    def _get_model_accuracy(self) -> Optional[float]:
        """Get current model accuracy/performance"""
        try:
            accuracy_files = [
                "data/model_performance.json",
                "brain/evaluation_results.json",
                "logs/accuracy_log.json"
            ]
            
            for acc_file in accuracy_files:
                if os.path.exists(acc_file):
                    with open(acc_file, 'r') as f:
                        data = json.load(f)
                        if 'accuracy' in data:
                            return float(data['accuracy'])
                        elif 'performance_score' in data:
                            return float(data['performance_score'])
            
            return None
            
        except Exception as e:
            logger.debug(f"Error getting model accuracy: {e}")
            return None
    
    def _get_inference_time(self) -> Optional[float]:
        """Get average inference time in milliseconds"""
        try:
            timing_files = [
                "data/inference_timing.json",
                "logs/performance_log.json"
            ]
            
            for timing_file in timing_files:
                if os.path.exists(timing_file):
                    with open(timing_file, 'r') as f:
                        data = json.load(f)
                        if 'average_inference_time' in data:
                            return float(data['average_inference_time'])
                        elif 'inference_times' in data and data['inference_times']:
                            return float(np.mean(data['inference_times'][-50:]))  # Last 50 inferences
            
            # Calculate from stimulus processing times
            if self.learning_state['stimulus_processing_times']:
                return float(np.mean(self.learning_state['stimulus_processing_times']))
            
            return None
            
        except Exception as e:
            logger.debug(f"Error getting inference time: {e}")
            return None
    
    def _calculate_memory_efficiency(self) -> float:
        """Calculate how efficiently memory is being used"""
        try:
            if not self.system_metrics_history:
                return 0.5  # Default efficiency
            
            recent_metrics = list(self.system_metrics_history)[-10:]  # Last 10 readings
            
            # Efficiency is inverse of wasted memory
            avg_memory_usage = np.mean([m.memory_usage for m in recent_metrics])
            
            # Consider memory usage pattern stability
            memory_variance = np.var([m.memory_usage for m in recent_metrics])
            stability_factor = 1.0 / (1.0 + memory_variance / 100.0)
            
            # Optimal memory usage is around 60-80%
            if 60 <= avg_memory_usage <= 80:
                usage_efficiency = 1.0
            elif avg_memory_usage < 60:
                usage_efficiency = avg_memory_usage / 60.0  # Underutilization penalty
            else:
                usage_efficiency = max(0.1, (100 - avg_memory_usage) / 20.0)  # Overutilization penalty
            
            return usage_efficiency * stability_factor
            
        except Exception as e:
            logger.debug(f"Error calculating memory efficiency: {e}")
            return 0.5
    
    def _calculate_convergence_rate(self) -> Optional[float]:
        """Calculate rate of learning convergence"""
        try:
            if len(self.learning_metrics_history) < 5:
                return None
            
            recent_metrics = list(self.learning_metrics_history)[-10:]
            
            # Look at loss improvement over time
            if all(m.average_loss is not None for m in recent_metrics):
                losses = [m.average_loss for m in recent_metrics]
                timestamps = [m.timestamp for m in recent_metrics]
                
                # Calculate slope of loss reduction
                if len(losses) >= 2:
                    time_span = timestamps[-1] - timestamps[0]
                    loss_improvement = losses[0] - losses[-1]  # Positive = improvement
                    
                    if time_span > 0:
                        convergence_rate = loss_improvement / time_span  # Improvement per second
                        return max(0.0, convergence_rate)
            
            return None
            
        except Exception as e:
            logger.debug(f"Error calculating convergence rate: {e}")
            return None
    
    def _calculate_knowledge_retention(self) -> Optional[float]:
        """Calculate how well knowledge is being retained"""
        try:
            # This would ideally test the model on previous knowledge
            # For now, use heuristics based on learning stability
            
            if len(self.learning_metrics_history) < 5:
                return None
            
            recent_metrics = list(self.learning_metrics_history)[-10:]
            
            # Stable accuracy suggests good retention
            if all(m.model_accuracy is not None for m in recent_metrics):
                accuracies = [m.model_accuracy for m in recent_metrics]
                
                # Low variance in accuracy = good retention
                accuracy_variance = np.var(accuracies)
                avg_accuracy = np.mean(accuracies)
                
                # Normalize variance relative to accuracy level
                if avg_accuracy > 0:
                    stability_score = 1.0 / (1.0 + accuracy_variance / avg_accuracy)
                    return min(1.0, stability_score * avg_accuracy)
            
            return None
            
        except Exception as e:
            logger.debug(f"Error calculating knowledge retention: {e}")
            return None
    
    def _calculate_adaptation_speed(self) -> Optional[float]:
        """Calculate how quickly the system adapts to new information"""
        try:
            # Measure how quickly performance improves after new stimulus
            if len(self.learning_metrics_history) < 3:
                return None
            
            recent_metrics = list(self.learning_metrics_history)[-5:]
            
            # Look for improvement trends
            improvements = 0
            total_comparisons = 0
            
            for i in range(1, len(recent_metrics)):
                prev_metric = recent_metrics[i-1]
                curr_metric = recent_metrics[i]
                
                # Check various improvement indicators
                if prev_metric.average_loss and curr_metric.average_loss:
                    if curr_metric.average_loss < prev_metric.average_loss:
                        improvements += 1
                    total_comparisons += 1
                
                if prev_metric.model_accuracy and curr_metric.model_accuracy:
                    if curr_metric.model_accuracy > prev_metric.model_accuracy:
                        improvements += 1
                    total_comparisons += 1
            
            if total_comparisons > 0:
                return improvements / total_comparisons
            
            return None
            
        except Exception as e:
            logger.debug(f"Error calculating adaptation speed: {e}")
            return None
    
    def _calculate_curiosity_satisfaction(self) -> float:
        """Calculate how well the system's curiosity is being satisfied"""
        try:
            # Check stimulus files for curiosity-driven activities
            stimulus_files = [
                "data/web_research_stimulus.json",
                "data/sensor_stimulus.json",
                "data/news_stimulus.json",
                "data/conversation_stimulus.json"
            ]
            
            total_curiosity = 0.0
            active_feeders = 0
            
            for stimulus_file in stimulus_files:
                if os.path.exists(stimulus_file):
                    try:
                        with open(stimulus_file, 'r') as f:
                            data = json.load(f)
                            if 'stimulus' in data and 'adrenaline' in data['stimulus']:
                                # Adrenaline represents curiosity/exploration
                                total_curiosity += data['stimulus']['adrenaline']
                                active_feeders += 1
                    except:
                        continue
            
            if active_feeders > 0:
                avg_curiosity = total_curiosity / active_feeders
                return min(1.0, avg_curiosity)
            
            return 0.3  # Default moderate curiosity
            
        except Exception as e:
            logger.debug(f"Error calculating curiosity satisfaction: {e}")
            return 0.3
    
    def _calculate_stimulus_response_rate(self) -> float:
        """Calculate how responsive the system is to stimulus"""
        try:
            # Check for recent stimulus processing activity
            current_time = time.time()
            recent_threshold = 3600  # 1 hour
            
            stimulus_files = [
                "data/web_research_stimulus.json",
                "data/sensor_stimulus.json", 
                "data/news_stimulus.json",
                "data/conversation_stimulus.json"
            ]
            
            recent_stimuli = 0
            total_files = len(stimulus_files)
            
            for stimulus_file in stimulus_files:
                if os.path.exists(stimulus_file):
                    try:
                        stat = os.stat(stimulus_file)
                        if current_time - stat.st_mtime < recent_threshold:
                            recent_stimuli += 1
                    except:
                        continue
            
            if total_files > 0:
                return recent_stimuli / total_files
            
            return 0.0
            
        except Exception as e:
            logger.debug(f"Error calculating stimulus response rate: {e}")
            return 0.0
    
    def analyze_performance_trends(self) -> Dict[str, Any]:
        """Analyze performance trends over time"""
        try:
            if len(self.system_metrics_history) < 10:
                return {"status": "insufficient_data"}
            
            recent_system = list(self.system_metrics_history)[-20:]  # Last 20 readings
            recent_learning = list(self.learning_metrics_history)[-10:] if self.learning_metrics_history else []
            
            trends = {}
            
            # System performance trends
            cpu_values = [m.cpu_usage for m in recent_system]
            memory_values = [m.memory_usage for m in recent_system]
            temp_values = [m.temperature for m in recent_system if m.temperature is not None]
            
            trends['cpu_trend'] = self._calculate_trend(cpu_values)
            trends['memory_trend'] = self._calculate_trend(memory_values)
            trends['temperature_trend'] = self._calculate_trend(temp_values) if temp_values else 0.0
            
            # Learning performance trends
            if recent_learning:
                if all(m.memory_efficiency is not None for m in recent_learning):
                    efficiency_values = [m.memory_efficiency for m in recent_learning]
                    trends['efficiency_trend'] = self._calculate_trend(efficiency_values)
                
                if all(m.stimulus_response_rate is not None for m in recent_learning):
                    response_values = [m.stimulus_response_rate for m in recent_learning]
                    trends['responsiveness_trend'] = self._calculate_trend(response_values)
            
            # Overall system health
            current_system = recent_system[-1]
            health_score = self._calculate_health_score(current_system)
            trends['overall_health'] = health_score
            
            return trends
            
        except Exception as e:
            logger.error(f"Error analyzing performance trends: {e}")
            return {"status": "error", "message": str(e)}
    
    def _calculate_trend(self, values: List[float]) -> float:
        """Calculate trend direction (-1 to 1, negative=declining, positive=improving)"""
        if len(values) < 2:
            return 0.0
        
        # Simple linear regression slope
        n = len(values)
        x = list(range(n))
        
        x_mean = np.mean(x)
        y_mean = np.mean(values)
        
        numerator = sum((x[i] - x_mean) * (values[i] - y_mean) for i in range(n))
        denominator = sum((x[i] - x_mean) ** 2 for i in range(n))
        
        if denominator == 0:
            return 0.0
        
        slope = numerator / denominator
        
        # Normalize slope to [-1, 1] range based on value scale
        value_range = max(values) - min(values)
        if value_range > 0:
            normalized_slope = slope / (value_range / n)
            return max(-1.0, min(1.0, normalized_slope))
        
        return 0.0
    
    def _calculate_health_score(self, metrics: SystemMetrics) -> float:
        """Calculate overall system health score (0-1)"""
        try:
            health_factors = []
            
            # CPU health (inverse of usage)
            cpu_health = max(0.0, (100 - metrics.cpu_usage) / 100.0)
            health_factors.append(cpu_health * 0.25)
            
            # Memory health
            memory_health = max(0.0, (100 - metrics.memory_usage) / 100.0)
            health_factors.append(memory_health * 0.25)
            
            # Temperature health
            if metrics.temperature is not None:
                if metrics.temperature < 60:
                    temp_health = 1.0
                elif metrics.temperature < 80:
                    temp_health = (80 - metrics.temperature) / 20.0
                else:
                    temp_health = 0.0
                health_factors.append(temp_health * 0.2)
            
            # Disk health
            disk_health = max(0.0, (100 - metrics.disk_usage) / 100.0)
            health_factors.append(disk_health * 0.15)
            
            # Load average health
            if metrics.load_average and len(metrics.load_average) > 0:
                # Assume good performance with load < number of CPU cores
                cpu_count = psutil.cpu_count()
                load_health = max(0.0, min(1.0, (cpu_count - metrics.load_average[0]) / cpu_count))
                health_factors.append(load_health * 0.15)
            
            return sum(health_factors)
            
        except Exception as e:
            logger.error(f"Error calculating health score: {e}")
            return 0.5
    
    def generate_alerts(self, system_metrics: SystemMetrics, 
                       learning_metrics: Optional[LearningMetrics] = None) -> List[PerformanceAlert]:
        """Generate performance alerts based on thresholds"""
        alerts = []
        current_time = time.time()
        
        try:
            # System alerts
            if system_metrics.cpu_usage >= self.config["thresholds"]["cpu_usage"]["critical"]:
                alerts.append(PerformanceAlert(
                    timestamp=current_time,
                    alert_type="critical",
                    category="hardware",
                    message=f"Critical CPU usage: {system_metrics.cpu_usage:.1f}%",
                    metric_value=system_metrics.cpu_usage,
                    threshold=self.config["thresholds"]["cpu_usage"]["critical"],
                    impact_level=0.9,
                    auto_correctable=True
                ))
            elif system_metrics.cpu_usage >= self.config["thresholds"]["cpu_usage"]["warning"]:
                alerts.append(PerformanceAlert(
                    timestamp=current_time,
                    alert_type="warning",
                    category="hardware",
                    message=f"High CPU usage: {system_metrics.cpu_usage:.1f}%",
                    metric_value=system_metrics.cpu_usage,
                    threshold=self.config["thresholds"]["cpu_usage"]["warning"],
                    impact_level=0.6,
                    auto_correctable=True
                ))
            
            # Memory alerts
            if system_metrics.memory_usage >= self.config["thresholds"]["memory_usage"]["critical"]:
                alerts.append(PerformanceAlert(
                    timestamp=current_time,
                    alert_type="critical",
                    category="hardware",
                    message=f"Critical memory usage: {system_metrics.memory_usage:.1f}%",
                    metric_value=system_metrics.memory_usage,
                    threshold=self.config["thresholds"]["memory_usage"]["critical"],
                    impact_level=0.95,
                    auto_correctable=True
                ))
            
            # Temperature alerts
            if (system_metrics.temperature and 
                system_metrics.temperature >= self.config["thresholds"]["temperature"]["critical"]):
                alerts.append(PerformanceAlert(
                    timestamp=current_time,
                    alert_type="critical",
                    category="hardware",
                    message=f"Critical temperature: {system_metrics.temperature:.1f}°C",
                    metric_value=system_metrics.temperature,
                    threshold=self.config["thresholds"]["temperature"]["critical"],
                    impact_level=0.8,
                    auto_correctable=True
                ))
            
            # Learning performance alerts
            if learning_metrics:
                if (learning_metrics.inference_time and 
                    learning_metrics.inference_time >= self.config["thresholds"]["inference_time"]["warning"]):
                    alerts.append(PerformanceAlert(
                        timestamp=current_time,
                        alert_type="warning",
                        category="learning",
                        message=f"Slow inference time: {learning_metrics.inference_time:.1f}ms",
                        metric_value=learning_metrics.inference_time,
                        threshold=self.config["thresholds"]["inference_time"]["warning"],
                        impact_level=0.4,
                        auto_correctable=False
                    ))
                
                if learning_metrics.memory_efficiency < self.config["thresholds"]["learning_efficiency"]["warning"]:
                    alerts.append(PerformanceAlert(
                        timestamp=current_time,
                        alert_type="warning",
                        category="efficiency",
                        message=f"Low learning efficiency: {learning_metrics.memory_efficiency:.2f}",
                        metric_value=learning_metrics.memory_efficiency,
                        threshold=self.config["thresholds"]["learning_efficiency"]["warning"],
                        impact_level=0.5,
                        auto_correctable=True
                    ))
            
            # Store alerts
            for alert in alerts:
                self.alerts_history.append(alert)
            
            return alerts
            
        except Exception as e:
            logger.error(f"Error generating alerts: {e}")
            return []
    
    def generate_stimulus(self, system_metrics: SystemMetrics, 
                         learning_metrics: Optional[LearningMetrics],
                         alerts: List[PerformanceAlert],
                         trends: Dict[str, Any]) -> Dict[str, float]:
        """Generate hormone stimulus based on performance analysis"""
        
        stimulus = {
            'adrenaline': 0.0,   # System alerts, performance issues
            'serotonin': 0.0,    # Good performance, efficiency
            'dopamine': 0.0,     # Learning progress, optimization success
            'cortisol': 0.0,     # Performance problems, resource constraints
            'oxytocin': 0.0      # System harmony, balanced resource usage
        }
        
        try:
            # Alert-based stimulus
            for alert in alerts:
                if alert.alert_type == "critical":
                    stimulus['adrenaline'] += 0.4 * alert.impact_level
                    stimulus['cortisol'] += 0.5 * alert.impact_level
                elif alert.alert_type == "warning":
                    stimulus['adrenaline'] += 0.2 * alert.impact_level
                    stimulus['cortisol'] += 0.3 * alert.impact_level
            
            # System performance stimulus
            health_score = self._calculate_health_score(system_metrics)
            
            if health_score > 0.8:
                stimulus['serotonin'] += 0.3  # Good system health
                stimulus['oxytocin'] += 0.2   # System harmony
            elif health_score < 0.4:
                stimulus['cortisol'] += 0.4   # Poor system health
                stimulus['adrenaline'] += 0.2  # Need attention
            
            # Resource utilization stimulus
            if system_metrics.cpu_usage < 50:
                stimulus['serotonin'] += 0.1  # Comfortable CPU usage
            elif system_metrics.cpu_usage > 90:
                stimulus['cortisol'] += 0.3   # High CPU stress
            
            if 60 <= system_metrics.memory_usage <= 80:
                stimulus['oxytocin'] += 0.2   # Optimal memory usage
            elif system_metrics.memory_usage > 90:
                stimulus['cortisol'] += 0.4   # Memory pressure
            
            # Learning performance stimulus
            if learning_metrics:
                if learning_metrics.memory_efficiency > 0.8:
                    stimulus['dopamine'] += 0.3  # Efficient learning
                    stimulus['serotonin'] += 0.2
                
                if learning_metrics.convergence_rate and learning_metrics.convergence_rate > 0:
                    stimulus['dopamine'] += 0.2  # Learning progress
                
                if learning_metrics.curiosity_satisfaction > 0.7:
                    stimulus['serotonin'] += 0.2  # Curiosity satisfied
                
                if learning_metrics.stimulus_response_rate > 0.8:
                    stimulus['dopamine'] += 0.1  # Responsive system
                elif learning_metrics.stimulus_response_rate < 0.3:
                    stimulus['cortisol'] += 0.2  # Unresponsive system
            
            # Trend-based stimulus
            if trends and trends.get("status") != "insufficient_data":
                if trends.get('efficiency_trend', 0) > 0.1:
                    stimulus['dopamine'] += 0.2  # Improving efficiency
                elif trends.get('efficiency_trend', 0) < -0.1:
                    stimulus['cortisol'] += 0.2  # Declining efficiency
                
                if trends.get('overall_health', 0.5) > 0.8:
                    stimulus['serotonin'] += 0.1  # Good overall health
            
            # Temperature-based stress
            if system_metrics.temperature:
                if system_metrics.temperature > 75:
                    temp_stress = min((system_metrics.temperature - 75) / 15, 1.0)
                    stimulus['cortisol'] += temp_stress * 0.3
                elif system_metrics.temperature < 50:
                    stimulus['serotonin'] += 0.1  # Cool and stable
            
            # Power consumption awareness (if available)
            if system_metrics.power_consumption:
                if system_metrics.power_consumption > 20:  # High power usage
                    stimulus['cortisol'] += 0.1
                elif system_metrics.power_consumption < 10:  # Efficient power usage
                    stimulus['serotonin'] += 0.1
            
            # Normalize all stimulus values
            for hormone in stimulus:
                stimulus[hormone] = max(0.0, min(stimulus[hormone], 1.0))
            
            return stimulus
            
        except Exception as e:
            logger.error(f"Error generating performance stimulus: {e}")
            return self._default_stimulus()
    
    def _default_stimulus(self) -> Dict[str, float]:
        """Default stimulus when performance analysis fails"""
        return {
            'adrenaline': 0.2,  # Moderate alertness
            'serotonin': 0.3,   # Neutral mood
            'dopamine': 0.1,    # Minimal satisfaction
            'cortisol': 0.2,    # Some baseline stress
            'oxytocin': 0.2     # Neutral harmony
        }
    
    def save_data(self, system_metrics: SystemMetrics, 
                  learning_metrics: Optional[LearningMetrics],
                  alerts: List[PerformanceAlert]):
        """Save performance data to files"""
        try:
            # Save system metrics
            if system_metrics:
                system_data = {
                    'current_metrics': asdict(system_metrics),
                    'metrics_history': [asdict(m) for m in list(self.system_metrics_history)[-100:]],  # Last 100
                    'last_updated': time.time()
                }
                
                os.makedirs(os.path.dirname(self.config["output"]["system_metrics"]), exist_ok=True)
                with open(self.config["output"]["system_metrics"], 'w') as f:
                    json.dump(system_data, f, indent=2)
            
            # Save learning metrics
            if learning_metrics:
                learning_data = {
                    'current_metrics': asdict(learning_metrics),
                    'metrics_history': [asdict(m) for m in list(self.learning_metrics_history)[-50:]],  # Last 50
                    'last_updated': time.time()
                }
                
                with open(self.config["output"]["learning_metrics"], 'w') as f:
                    json.dump(learning_data, f, indent=2)
            
            # Save alerts
            if alerts:
                alerts_data = {
                    'current_alerts': [asdict(alert) for alert in alerts],
                    'alerts_history': [asdict(alert) for alert in list(self.alerts_history)[-100:]],  # Last 100
                    'last_updated': time.time()
                }
                
                with open(self.config["output"]["performance_alerts"], 'w') as f:
                    json.dump(alerts_data, f, indent=2)
            
            logger.info("Performance data saved successfully")
            
        except Exception as e:
            logger.error(f"Error saving performance data: {e}")
    
    def run_performance_cycle(self) -> Dict[str, float]:
        """Run one complete performance monitoring cycle"""
        logger.info("Starting performance monitoring cycle...")
        
        # Collect metrics
        system_metrics = self.collect_system_metrics()
        learning_metrics = self.collect_learning_metrics()
        
        # Analyze trends
        trends = self.analyze_performance_trends()
        
        # Generate alerts
        alerts = []
        if system_metrics:
            alerts = self.generate_alerts(system_metrics, learning_metrics)
        
        # Generate stimulus
        stimulus = self.generate_stimulus(system_metrics, learning_metrics, alerts, trends)
        
        # Save data
        self.save_data(system_metrics, learning_metrics, alerts)
        
        # Save stimulus
        stimulus_data = {
            "timestamp": time.time(),
            "source": "performance_feeder",
            "stimulus": stimulus,
            "metadata": {
                "system_health": self._calculate_health_score(system_metrics) if system_metrics else 0.5,
                "active_alerts": len(alerts),
                "critical_alerts": len([a for a in alerts if a.alert_type == "critical"]),
                "cpu_usage": system_metrics.cpu_usage if system_metrics else 0,
                "memory_usage": system_metrics.memory_usage if system_metrics else 0,
                "temperature": system_metrics.temperature if system_metrics else None,
                "learning_efficiency": learning_metrics.memory_efficiency if learning_metrics else None
            }
        }
        
        os.makedirs(os.path.dirname(self.config["output"]["stimulus_output"]), exist_ok=True)
        with open(self.config["output"]["stimulus_output"], 'w') as f:
            json.dump(stimulus_data, f, indent=2)
        
        logger.info(f"Performance monitoring complete, stimulus: {stimulus}")
        
        return stimulus
    
    def run_continuous(self):
        """Run continuous performance monitoring"""
        logger.info("Starting continuous performance monitoring...")
        
        try:
            while True:
                # System metrics collection
                system_start = time.time()
                system_metrics = self.collect_system_metrics()
                
                # Learning metrics collection (less frequent)
                learning_metrics = None
                if time.time() - getattr(self, '_last_learning_check', 0) > self.config["monitoring"]["learning_check_interval"]:
                    learning_metrics = self.collect_learning_metrics()
                    self._last_learning_check = time.time()
                
                # Alert checking (even less frequent)
                alerts = []
                if time.time() - getattr(self, '_last_alert_check', 0) > self.config["monitoring"]["alert_check_interval"]:
                    if system_metrics:
                        alerts = self.generate_alerts(system_metrics, learning_metrics)
                    self._last_alert_check = time.time()
                
                # Generate stimulus
                trends = self.analyze_performance_trends()
                stimulus = self.generate_stimulus(system_metrics, learning_metrics, alerts, trends)
                
                # Save data periodically
                if time.time() - getattr(self, '_last_save', 0) > 300:  # Every 5 minutes
                    self.save_data(system_metrics, learning_metrics, alerts)
                    self._last_save = time.time()
                
                # Save stimulus
                stimulus_data = {
                    "timestamp": time.time(),
                    "source": "performance_feeder",
                    "stimulus": stimulus,
                    "metadata": {
                        "system_health": self._calculate_health_score(system_metrics) if system_metrics else 0.5,
                        "active_alerts": len(alerts),
                        "cpu_usage": system_metrics.cpu_usage if system_metrics else 0,
                        "memory_usage": system_metrics.memory_usage if system_metrics else 0
                    }
                }
                
                os.makedirs(os.path.dirname(self.config["output"]["stimulus_output"]), exist_ok=True)
                with open(self.config["output"]["stimulus_output"], 'w') as f:
                    json.dump(stimulus_data, f, indent=2)
                
                # Wait for next cycle
                elapsed = time.time() - system_start
                sleep_time = max(0, self.config["monitoring"]["system_check_interval"] - elapsed)
                time.sleep(sleep_time)
                
        except KeyboardInterrupt:
            logger.info("Performance monitoring stopped by user")
        except Exception as e:
            logger.error(f"Error in performance monitoring: {e}")
        finally:
            # Cleanup
            if self.jetson:
                try:
                    self.jetson.close()
                except:
                    pass

def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    feeder = PerformanceFeeder()
    
    # Test mode: run single performance cycle
    if len(os.sys.argv) > 1 and os.sys.argv[1] == '--test':
        stimulus = feeder.run_performance_cycle()
        print(f"Generated performance stimulus: {stimulus}")
    else:
        # Continuous monitoring mode
        feeder.run_continuous()

if __name__ == "__main__":
    main()