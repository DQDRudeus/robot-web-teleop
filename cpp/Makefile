SDK_DIR ?= /opt/hikvision-sdk
CXX ?= g++
CXXFLAGS += -std=c++17 -O2 -Wall -Wextra -I$(SDK_DIR)/incCn
LDFLAGS += -L$(SDK_DIR)/MakeAll -Wl,-rpath,$(SDK_DIR)/MakeAll -Wl,-rpath,$(SDK_DIR)/MakeAll/HCNetSDKCom
LDLIBS += -lhcnetsdk -pthread

all: hik_camera_daemon hik_encoder

hik_camera_daemon: hik_camera_daemon.cpp
	$(CXX) $(CXXFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

hik_encoder: hik_encoder.cpp
	$(CXX) $(CXXFLAGS) $< -o $@.new $(LDFLAGS) $(LDLIBS)
	mv -f $@.new $@
