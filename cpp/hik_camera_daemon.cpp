#include <HCNetSDK.h>

#include <atomic>
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>

namespace {

std::atomic<bool> g_running{true};
std::atomic<unsigned long long> g_stream_bytes{0};
std::atomic<unsigned long long> g_stream_packets{0};

void on_signal(int) {
  g_running = false;
}

std::string env_or(const char* name, const std::string& fallback) {
  const char* value = std::getenv(name);
  return value && *value ? std::string(value) : fallback;
}

int env_int_or(const char* name, int fallback) {
  const char* value = std::getenv(name);
  if (!value || !*value) return fallback;
  return std::atoi(value);
}

void CALLBACK real_data_callback(LONG, DWORD data_type, BYTE* buffer, DWORD size, void*) {
  if (!buffer || size == 0) return;
  if (data_type == NET_DVR_SYSHEAD || data_type == NET_DVR_STREAMDATA) {
    g_stream_bytes += size;
    g_stream_packets++;
  }
}

struct Config {
  std::string host = env_or("CAMERA_HOST", "camera.invalid");
  int port = env_int_or("CAMERA_PORT", 8000);
  std::string user = env_or("CAMERA_USER", "admin");
  std::string password = env_or("CAMERA_PASSWORD", "");
  int channel = env_int_or("CAMERA_CHANNEL", 1);
};

void apply_arg(Config& cfg, const std::string& key, const std::string& value) {
  if (key == "--host") cfg.host = value;
  else if (key == "--port") cfg.port = std::atoi(value.c_str());
  else if (key == "--user") cfg.user = value;
  else if (key == "--password") cfg.password = value;
  else if (key == "--channel") cfg.channel = std::atoi(value.c_str());
}

Config parse_args(int argc, char** argv) {
  Config cfg;
  for (int i = 1; i + 1 < argc; i += 2) {
    apply_arg(cfg, argv[i], argv[i + 1]);
  }
  return cfg;
}

class CameraSession {
 public:
  explicit CameraSession(Config cfg) : cfg_(std::move(cfg)) {}

  bool start() {
    if (cfg_.password.empty()) {
      std::cerr << "[error] CAMERA_PASSWORD is empty\n";
      return false;
    }

    NET_DVR_Init();
    NET_DVR_SetConnectTime(3000, 1);
    NET_DVR_SetReconnect(10000, true);

    NET_DVR_USER_LOGIN_INFO login_info{};
    NET_DVR_DEVICEINFO_V40 device_info{};
    login_info.bUseAsynLogin = false;
    login_info.wPort = static_cast<WORD>(cfg_.port);
    std::strncpy(login_info.sDeviceAddress, cfg_.host.c_str(), NET_DVR_DEV_ADDRESS_MAX_LEN - 1);
    std::strncpy(login_info.sUserName, cfg_.user.c_str(), NAME_LEN - 1);
    std::strncpy(login_info.sPassword, cfg_.password.c_str(), NAME_LEN - 1);

    user_id_ = NET_DVR_Login_V40(&login_info, &device_info);
    if (user_id_ < 0) {
      std::cerr << "[error] login failed code=" << NET_DVR_GetLastError() << "\n";
      NET_DVR_Cleanup();
      return false;
    }
    std::cout << "[ok] login user_id=" << user_id_ << " host=" << cfg_.host << ":" << cfg_.port << "\n";

    NET_DVR_PREVIEWINFO preview_info{};
    preview_info.lChannel = cfg_.channel;
    preview_info.dwStreamType = 0;
    preview_info.dwLinkMode = 0;
    preview_info.hPlayWnd = 0;
    preview_info.bBlocked = 1;

    preview_handle_ = NET_DVR_RealPlay_V40(user_id_, &preview_info, real_data_callback, nullptr);
    if (preview_handle_ < 0) {
      std::cerr << "[warn] preview failed code=" << NET_DVR_GetLastError() << "\n";
    } else {
      std::cout << "[ok] preview started channel=" << cfg_.channel << "\n";
    }
    ptz_watchdog_ = std::thread(&CameraSession::watch_ptz, this);
    return true;
  }

  void stop() {
    {
      std::lock_guard<std::mutex> lock(ptz_mutex_);
      ptz_shutdown_ = true;
      stop_ptz_locked();
    }
    ptz_changed_.notify_all();
    if (ptz_watchdog_.joinable()) ptz_watchdog_.join();
    if (preview_handle_ >= 0) {
      NET_DVR_StopRealPlay(preview_handle_);
      preview_handle_ = -1;
    }
    if (user_id_ >= 0) {
      NET_DVR_Logout(user_id_);
      user_id_ = -1;
    }
    NET_DVR_Cleanup();
  }

  bool ptz(const std::string& name, int speed, int duration_ms) {
    std::lock_guard<std::mutex> lock(ptz_mutex_);
    if (ptz_shutdown_) return false;
    if (name == "stop") return stop_ptz_locked();
    static const std::map<std::string, DWORD> commands = {
        {"up", TILT_UP},
        {"down", TILT_DOWN},
        {"left", PAN_LEFT},
        {"right", PAN_RIGHT},
        {"zoom_in", ZOOM_IN},
        {"zoom_out", ZOOM_OUT},
    };

    auto it = commands.find(name);
    if (it == commands.end()) {
      std::cerr << "[error] unknown ptz command=" << name << "\n";
      return false;
    }

    speed = std::max(1, std::min(7, speed));
    duration_ms = std::max(50, std::min(3000, duration_ms));

    DWORD cmd = it->second;
    if (active_command_ != cmd || active_speed_ != speed) {
      if (!stop_ptz_locked()) return false;
      if (!NET_DVR_PTZControlWithSpeed_Other(user_id_, cfg_.channel, cmd, 0, speed)) {
        std::cerr << "[error] ptz start failed code=" << NET_DVR_GetLastError() << "\n";
        return false;
      }
      active_command_ = cmd;
      active_speed_ = speed;
      std::cout << "[ok] ptz command=" << name << " speed=" << speed << "\n";
    }

    // Repeated hold commands renew the deadline without restarting the motor.
    ptz_deadline_ = std::chrono::steady_clock::now() + std::chrono::milliseconds(duration_ms);
    ptz_changed_.notify_one();
    return true;
  }

  void status() const {
    std::lock_guard<std::mutex> lock(ptz_mutex_);
    std::cout << "[status] user_id=" << user_id_
              << " preview_handle=" << preview_handle_
              << " stream_bytes=" << g_stream_bytes.load()
              << " stream_packets=" << g_stream_packets.load() << "\n";
  }

 private:
  bool stop_ptz_locked() {
    if (!active_command_) return true;
    if (!NET_DVR_PTZControlWithSpeed_Other(user_id_, cfg_.channel,
                                          active_command_, 1, active_speed_)) {
      std::cerr << "[error] ptz stop failed code=" << NET_DVR_GetLastError() << "\n";
      ptz_deadline_ = std::chrono::steady_clock::now() + std::chrono::milliseconds(100);
      return false;
    }
    active_command_ = 0;
    std::cout << "[ok] ptz stopped\n";
    return true;
  }

  void watch_ptz() {
    std::unique_lock<std::mutex> lock(ptz_mutex_);
    while (!ptz_shutdown_) {
      if (!active_command_) {
        ptz_changed_.wait(lock, [this] { return ptz_shutdown_ || active_command_ != 0; });
      } else if (std::chrono::steady_clock::now() >= ptz_deadline_) {
        stop_ptz_locked();
      } else {
        const auto deadline = ptz_deadline_;
        ptz_changed_.wait_until(lock, deadline);
      }
    }
  }

  Config cfg_;
  LONG user_id_ = -1;
  LONG preview_handle_ = -1;
  mutable std::mutex ptz_mutex_;
  std::condition_variable ptz_changed_;
  std::thread ptz_watchdog_;
  bool ptz_shutdown_ = false;
  DWORD active_command_ = 0;
  int active_speed_ = 1;
  std::chrono::steady_clock::time_point ptz_deadline_;
};

}  // namespace

int main(int argc, char** argv) {
  std::cout << std::unitbuf;
  std::cerr << std::unitbuf;
  std::signal(SIGINT, on_signal);
  std::signal(SIGTERM, on_signal);

  Config cfg = parse_args(argc, argv);
  CameraSession camera(cfg);
  if (!camera.start()) return 1;

  std::cout << "[ready] ptz_mode=continuous commands: ptz <left|right|up|down|zoom_in|zoom_out|stop> <speed 1-7> <timeout_ms>, status, quit\n";

  std::string line;
  while (g_running && std::getline(std::cin, line)) {
    std::istringstream iss(line);
    std::string op;
    iss >> op;

    if (op == "quit" || op == "exit") {
      break;
    }
    if (op == "status") {
      camera.status();
      continue;
    }
    if (op == "ptz") {
      std::string name;
      int speed = 4;
      int duration_ms = 250;
      iss >> name >> speed >> duration_ms;
      camera.ptz(name, speed, duration_ms);
      continue;
    }
    if (!op.empty()) {
      std::cerr << "[error] unknown command line=" << line << "\n";
    }
  }

  camera.stop();
  std::cout << "[ok] stopped\n";
  return 0;
}
