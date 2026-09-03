# 技術參考與版本依據

查核日期：2026-09-03。此清單優先使用官方文件；版本、支援期、價格與供貨仍應在實作/採購時重新確認。

## ROS 2 與模擬

- [ROS 2 distributions](https://docs.ros.org/en/rolling/Releases.html)：各 distribution 的狀態與支援期。
- [ROS 2 Jazzy on Ubuntu](https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html)：Jazzy binary packages 的 Ubuntu 24.04 基線。
- [ROS 2 Lyrical Luth](https://docs.ros.org/en/lyrical/Releases/Release-Lyrical-Luth.html)：2026 LTS 與支援資訊。
- [Gazebo and ROS installation compatibility](https://gazebosim.org/docs/latest/ros_installation/)：Jazzy/Harmonic、Lyrical/Jetty 等推薦配對。
- [ROS 2 and Gazebo integration](https://gazebosim.org/docs/latest/ros2_integration/)：joint state、command、RViz/Gazebo 整合。
- [robot_state_publisher](https://docs.ros.org/en/jazzy/p/robot_state_publisher/)：由 joint angles 與 kinematic tree 發布 link poses。
- [Using URDF with robot_state_publisher](https://docs.ros.org/en/jazzy/Tutorials/Intermediate/URDF/Using-URDF-with-Robot-State-Publisher-py.html)。
- [MoveIt 2 concepts](https://moveit.picknik.ai/main/doc/concepts/concepts.html)：kinematics、motion planning、planning scene、trajectory。
- [MoveIt Setup Assistant](https://moveit.picknik.ai/main/doc/examples/setup_assistant/setup_assistant_tutorial.html)：self-collision、planning groups、end effector。

## Learning 與自有硬體

- [LeRobot overview](https://huggingface.co/docs/lerobot/index)：teleoperate → record → train → deploy 工作流。
- [LeRobot: Bring Your Own Hardware](https://huggingface.co/docs/lerobot/integrate_hardware)：以標準 Robot interface 串接 serial/CAN/TCP 裝置。
- [LeRobot imitation learning](https://huggingface.co/docs/lerobot/il_robots)：實機資料收集、policy training 與 evaluation。
- [HIL-SERL workflow](https://huggingface.co/docs/lerobot/hilserl)：人機迴路、bounds、監控與實機學習安全工具。

## 運算與感測硬體

- [Raspberry Pi 5 product page](https://www.raspberrypi.com/products/raspberry-pi-5/)：CPU、RAM、I/O 與 connectivity。
- [Raspberry Pi 5 documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)：電源與硬體介面細節。
- [Raspberry Pi 27W USB-C Power Supply](https://www.raspberrypi.com/products/27w-power-supply/)：5.1V/5A 官方電源規格。
- [Raspberry Pi Camera Module 3](https://www.raspberrypi.com/products/camera-module-3/)：12MP IMX708 與 autofocus 規格；本專案 v0.1 因 OS 基線不選作預設。
- [Ubuntu on Raspberry Pi：camera support](https://ubuntu.com/hardware/docs/boards/how-to/special_hardware/rpi-camera/)：Ubuntu/libcamera 支援狀態與相機 sensors。
- [Ubuntu Raspberry Pi limitations](https://ubuntu.com/hardware/docs/boards/how-to/ubuntu_supported/raspberry-pi/)：Ubuntu 25.04 以前 libcamera stack 限制，作為 v0.1 改用 USB UVC camera 的依據。
- [NVIDIA Jetson Orin Nano Super announcement](https://developer.nvidia.com/blog/nvidia-jetson-orin-nano-developer-kit-gets-a-super-boost/)：Super mode compute/memory 規格。
- [NVIDIA Jetson developer kits](https://developer.nvidia.com/embedded/jetson-developer-kits)。
- [Luxonis OAK-D Pro](https://docs.luxonis.com/hardware/products/OAK-D%20Pro)：stereo depth、on-device compute 與相機規格。
- [RealSense documentation](https://dev.realsenseai.com/)：depth SDK、calibration、D400 guides。
- [ROBOTIS DYNAMIXEL](https://www.robotis.us/dynamixel/)：整合 motor、controller、driver、sensor、gear 與 network 的 smart actuator 系列。
- [DYNAMIXEL XL330-M288-T e-Manual](https://emanual.robotis.com/docs/en/dxl/x/xl330-m288/)：5V、扭矩/電流、feedback、limits、error 與 Bus Watchdog。
- [OpenRB-150 e-Manual](https://emanual.robotis.com/docs/en/parts/controller/openrb-150/)：4 個 TTL ports、3A DXL port current、ADC、FET 與上電預設 off。
- [Logitech C920e](https://www.logitech.com/en-hk/products/webcams/c920e-business-webcam.html)：1080p、autofocus、固定與 privacy cover。
- [ROS 2 v4l2_camera](https://index.ros.org/r/v4l2_camera/)：V4L2 camera controls、image transport 與 ROS 2 介面。
- [micro-ROS setup](https://github.com/micro-ROS/micro_ros_setup)：ROS 2 與 microcontroller build/integration 支援。

## 本專案如何使用這些技術

- URDF/TF2 解決幾何 body schema，但不包含完整語意身份、evidence、confidence 或 ownership，因此本專案增加 manifest/body graph。
- ros2_control 解決硬體抽象與 controller interface，但不讓 AI 直接控制；外層仍有 skill/safety gateway。
- Gazebo 提供 simulation/fault test 入口，實機與模擬共用 contract。
- MoveIt 2 解決 manipulation planning/collision 的一部分，不取代硬體 E-stop、watchdog 或權限。
- LeRobot 可作資料/policy 層，不取代 self-model 與安全 contract。
- VLM/LLM 是可替換的 cognition adapter；自我身份不能只存在模型 prompt/weights 中。

## 待補研究文獻

在開始學術投稿或選擇 learned self-model 方法時，另做有版本的 literature review，至少包含：robot body schema、sensorimotor contingencies、self-calibration、robot self-modeling、active system identification、tool embodiment、uncertainty calibration、safe learning/control。工程文件先以可實作 contract 為主，避免把尚未驗證的研究主張寫成系統能力。
