# Robot Web Teleop

浏览器远程控制台：海康摄像头 PTZ、ROS 2 turtlesim / AM3008 底盘、键盘 / 手柄 / 触屏输入、操控权租约、速度挡位、松手与超时停车，以及源视频质量管理。

这是从维护主线生成的脱敏开源快照。没有包含部署网址、真实设备或服务器地址、个人路径、密码、密钥、现场日志、截图、历史备份或旧 Git 历史。代码中的回环地址仅用于本机进程通信；`.invalid` 主机名是不可用占位符，必须自行配置。

## 架构

浏览器通过 `/ws/control` 连接 FastAPI 后端；后端连接 HCNetSDK 云台进程和独立 ROS 2 worker。视频由摄像头 RTSP 经 FFmpeg、SSH 隧道传到自建 ZLMediaKit，再由浏览器接收。视频与运动通道独立管理。

## Windows / Linux：本机界面与后端

需要 Python 3.10+。在本仓库根目录运行以下命令，只安装 Python 依赖：

```sh
python -m pip install -r requirements-control.txt
```

随后在当前终端私下设置 `OPERATOR_TOKEN` 为自行生成的强口令，再运行：

```sh
python backend/app.py
```

后端默认只绑定本机回环，端口 8090。浏览器访问本机该端口即可看到界面；没有摄像头 SDK 或 ROS 2 时，相关设备显示未连接。没有配置口令时不能获取高级操控权。低权限体验口令默认关闭，仅在私下设置 `TEST_OPERATOR_TOKEN` 后启用；体验角色仍能发出低速运动指令。

## NX / Linux：设备集成

设备端需要 ROS 2、匹配架构的 HCNetSDK、C++ 编译器、FFmpeg 和 SSH。厂商 SDK、头文件、二进制库及 ZLMediaKit 播放器未随仓库分发，请自行取得并遵守各自许可。编译自己的 SDK 适配程序：

```sh
make -C cpp SDK_DIR=/opt/hikvision-sdk
```

SDK 目录应包含 `incCn` 和 `MakeAll`；按实际 SDK 布局调整。成功后生成云台和编码参数 helper。这一步未在 Windows 上验证原生编译。

`deploy/stack.py configure` 是面向 NX 的交互配置入口；在设备端自己的终端运行，将询问摄像头主机、账号、SSH 目标、ROS 环境和隐藏口令，保存为权限受限的私有配置。示例主机名不可直接使用。该管理器保留 Jetson 的 `nvidia` 运行账号约定；其他 Linux 账号需要先适配账号检查和主目录。

后端直接运行可通过 `CAMERA_HOST`、`CAMERA_USER`、`CAMERA_PASSWORD`、`MOTION_TARGET` 等环境变量配置；ROS 目标支持 `none`、`turtlesim`、`am3008`。不要同时启动多个底盘速度发布源。

公网接入需要自行设计 TLS、反向代理鉴权、SSH 密钥和 ZLMediaKit 配置。仓库不提供原服务器配置，`public-control/gateway_routes.py` 仅为已有 Nginx 配置的兼容路由工具，使用前需单独审查。服务安装、启用和重启均需操作者自行进行。

## 验证与边界

```sh
python -m pip install httpx
python -m unittest test_control test_turtle test_video_quality test_am3008_start test_motion_modes test_motion_deploy test_desktop_launcher test_camera_repair
```

这些是本地模拟测试，不代表真实设备、原生 SDK 编译、远程部署、弱网链路或制动能力已经验收。先在仿真或受控条件下验证运动方向、速度、松手/断线/过期停车，再连接真实底盘。网页 STOP 不能替代独立的硬件急停。

## 许可

项目自有代码采用 MIT 许可；Lucide 图标保留 `web/lucide-LICENSE.txt` 的 ISC 许可。外部 SDK、播放器和其他依赖不属于本项目 MIT 授权范围。
