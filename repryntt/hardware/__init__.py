"""
repryntt.hardware — Hardware peripheral interfaces.

Edge device I/O:
    - Voice: TTS via Piper neural TTS + STT via faster-whisper
    - Vision: Dual IMX219 CSI stereo cameras via GStreamer/Argus
    - ROS2: Wheelchair control and robotics (conditional on ROS2 runtime)

Migration source:
    - SAIGE/brain/voice_interface.py (~400 lines)
    - SAIGE/brain/ros2_tools.py (~300 lines)
    - SAIGE/vision/check_cameras.py (~100 lines)
    - SAIGE/vision/dual_imx219_stereo_stream.py (~200 lines)
"""
