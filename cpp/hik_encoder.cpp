// Short-lived HCNetSDK configuration helper; never opens a preview or sends PTZ.
#include <HCNetSDK.h>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

static std::string env(const char* name, const char* fallback = "") {
  const char* text = std::getenv(name);
  return text && *text ? text : fallback;
}
static std::string quote(const std::string& text) {
  std::string result = "\"";
  const char* hex = "0123456789abcdef";
  for (unsigned char c : text) {
    if (c == '"' || c == '\\') { result += '\\'; result += c; }
    else if (c < 32) { result += "\\u00"; result += hex[c >> 4]; result += hex[c & 15]; }
    else result += c;
  }
  return result + '"';
}
static DWORD bitrate_value(DWORD code) {
  static const DWORD rates[] = {0,16,32,48,64,80,96,128,160,192,224,256,320,384,448,512,640,768,896,1024,1280,1536,1792,2048,3072,4096,8192,16384};
  if (code & 0x80000000u) return code & 0x7fffffffu;
  return code < sizeof(rates)/sizeof(rates[0]) ? rates[code] : 0;
}
static DWORD bitrate_code(DWORD kbps) {
  for (DWORD i = 1; i <= 27; ++i) if (bitrate_value(i) == kbps) return i;
  return 0x80000000u | kbps;
}
static bool read_config(LONG user, LONG channel, NET_DVR_COMPRESSIONCFG_V30& cfg) {
  std::memset(&cfg, 0, sizeof(cfg));
  cfg.dwSize = sizeof(cfg);
  DWORD returned = 0;
  return NET_DVR_GetDVRConfig(user, NET_DVR_GET_COMPRESSCFG_V30, channel, &cfg, sizeof(cfg), &returned);
}
static int failure(const char* stage, DWORD code, bool rollback = true) {
  std::cout << "{\"ok\":false,\"stage\":" << quote(stage) << ",\"sdk_error\":" << code
            << ",\"rollback_verified\":" << (rollback ? "true" : "false") << "}\n";
  return 1;
}
int main(int argc, char** argv) {
  if (argc != 2 && argc != 5) return failure("arguments", 0);
  const bool setting = std::string(argv[1]) == "set";
  if ((!setting && std::string(argv[1]) != "inspect") || (setting ? argc != 5 : argc != 2)) return failure("arguments", 0);
  int resolution = 0, fps = 0, kbps = 0;
  if (setting) {
    try {
      for (int i = 2; i < 5; ++i) if (std::string(argv[i]).find_first_not_of("0123456789") != std::string::npos) return failure("arguments", 0);
      resolution = std::stoi(argv[2]); fps = std::stoi(argv[3]); kbps = std::stoi(argv[4]);
    } catch (...) { return failure("arguments", 0); }
    if (resolution < 0 || resolution > 254 || fps < 5 || fps > 33 || kbps < 64 || kbps > 8192) return failure("arguments", 0);
  }
  if (env("CAMERA_PASSWORD").empty()) return failure("credentials", 0);
  NET_DVR_Init();
  NET_DVR_SetConnectTime(3000, 1);
  NET_DVR_USER_LOGIN_INFO login{};
  NET_DVR_DEVICEINFO_V40 device{};
  std::strncpy(login.sDeviceAddress, env("CAMERA_HOST", "camera.invalid").c_str(), sizeof(login.sDeviceAddress)-1);
  std::strncpy(login.sUserName, env("CAMERA_USER", "admin").c_str(), sizeof(login.sUserName)-1);
  std::strncpy(login.sPassword, env("CAMERA_PASSWORD").c_str(), sizeof(login.sPassword)-1);
  login.wPort = static_cast<WORD>(std::atoi(env("CAMERA_PORT", "8000").c_str()));
  login.bUseAsynLogin = false;
  LONG user = NET_DVR_Login_V40(&login, &device);
  if (user < 0) { DWORD code = NET_DVR_GetLastError(); NET_DVR_Cleanup(); return failure("login", code); }
  const LONG channel = std::atoi(env("CAMERA_CHANNEL", "1").c_str());
  NET_DVR_COMPRESSIONCFG_V30 original{};
  if (!read_config(user, channel, original)) {
    DWORD code = NET_DVR_GetLastError(); NET_DVR_Logout(user); NET_DVR_Cleanup(); return failure("get", code);
  }
  auto current = original;
  if (setting) {
    current.struNormHighRecordPara.byResolution = static_cast<BYTE>(resolution);
    current.struNormHighRecordPara.dwVideoFrameRate = static_cast<DWORD>(fps);
    current.struNormHighRecordPara.dwVideoBitrate = bitrate_code(kbps);
    bool wrote = NET_DVR_SetDVRConfig(user, NET_DVR_SET_COMPRESSCFG_V30, channel, &current, sizeof(current));
    DWORD code = wrote ? 0 : NET_DVR_GetLastError();
    NET_DVR_COMPRESSIONCFG_V30 actual{};
    bool verified = wrote && read_config(user, channel, actual) &&
      actual.struNormHighRecordPara.byResolution == resolution &&
      actual.struNormHighRecordPara.dwVideoFrameRate == static_cast<DWORD>(fps) &&
      bitrate_value(actual.struNormHighRecordPara.dwVideoBitrate) == static_cast<DWORD>(kbps);
    if (!verified) {
      if (!code) code = NET_DVR_GetLastError();
      bool restored = NET_DVR_SetDVRConfig(user, NET_DVR_SET_COMPRESSCFG_V30, channel, &original, sizeof(original));
      NET_DVR_COMPRESSIONCFG_V30 back{};
      restored = restored && read_config(user, channel, back) &&
        std::memcmp(&original.struNormHighRecordPara, &back.struNormHighRecordPara, sizeof(original.struNormHighRecordPara)) == 0;
      NET_DVR_Logout(user); NET_DVR_Cleanup(); return failure("set_or_readback", code, restored);
    }
    current = actual;
  }
  std::string ability;
  DWORD ability_error = 0;
  if (!setting) {
    std::ostringstream input;
    input << "<AudioVideoCompressInfo><AudioChannelNumber>" << channel << "</AudioChannelNumber><VoiceTalkChannelNumber>1</VoiceTalkChannelNumber><VideoChannelNumber>" << channel << "</VideoChannelNumber></AudioVideoCompressInfo>";
    std::string request = input.str();
    std::vector<char> buffer(1024 * 1024, 0);
    if (NET_DVR_GetDeviceAbility(user, DEVICE_ENCODE_ALL_ABILITY_V20, const_cast<char*>(request.c_str()), static_cast<DWORD>(request.size()), buffer.data(), static_cast<DWORD>(buffer.size()-1))) ability = buffer.data();
    else ability_error = NET_DVR_GetLastError();
  }
  const auto& info = current.struNormHighRecordPara;
  std::cout << "{\"ok\":true,\"source\":\"hcnetsdk\",\"resolution_code\":" << static_cast<int>(info.byResolution)
            << ",\"fps_code\":" << info.dwVideoFrameRate << ",\"kbps\":" << bitrate_value(info.dwVideoBitrate)
            << ",\"codec\":" << static_cast<int>(info.byVideoEncType) << ",\"ability_error\":" << ability_error
            << ",\"capabilities_xml\":" << quote(ability) << "}\n";
  NET_DVR_Logout(user);
  NET_DVR_Cleanup();
  return 0;
}
