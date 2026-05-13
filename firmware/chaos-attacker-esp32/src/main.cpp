// chaos-attacker-esp32 — sketch for the bench-side attack device.
//
// Reads JSON commands over USB serial (115200 baud), dispatches to the
// requested fault routine, replies with one JSON object per line.
//
// This is a sketch — its job is to be the bench-side counterpart to
// HilHardwareIO's wire protocol, not a production appliance. The
// fault-emission routines (deauth_loop / wifi_jam_loop / ble_flood_loop
// / lora_carrier_loop) are stubbed to log + delay — they DO NOT emit
// real RF in this sketch. Wire them to the appropriate platform APIs
// (esp_wifi_80211_tx, NimBLE, RadioLib) before plugging this into a
// real bench. See README.md.
//
// Safety
// ------
// - Refuses every inject if CHAOS_BENCH_MODE_GPIO reads grounded.
// - Clamps power_dbm at CHAOS_MAX_TX_DBM (compile-time constant).
// - Active fault count limited to MAX_ACTIVE_FAULTS to bound stack /
//   timer pressure.

#include <Arduino.h>
#include <ArduinoJson.h>

#ifndef CHAOS_FIRMWARE_VERSION
#define CHAOS_FIRMWARE_VERSION "0.0.0-dev"
#endif
#ifndef CHAOS_DEVICE_SERIAL
#define CHAOS_DEVICE_SERIAL "AT-bench-dev"
#endif
#ifndef CHAOS_HARDWARE_REV
#define CHAOS_HARDWARE_REV "rev-?"
#endif
#ifndef CHAOS_MAX_TX_DBM
#define CHAOS_MAX_TX_DBM 14
#endif
#ifndef CHAOS_BENCH_MODE_GPIO
#define CHAOS_BENCH_MODE_GPIO 27
#endif

static constexpr size_t kMaxLineLen = 512;
static constexpr int kMaxActiveFaults = 4;

struct ActiveFault {
  bool in_use = false;
  char handle[16] = {0};
  char fault_name[40] = {0};
  unsigned long started_ms = 0;
  unsigned long duration_ms = 0;
};

static ActiveFault g_active[kMaxActiveFaults];
static int g_next_handle = 0;

static bool bench_mode() {
  // Open (HIGH via internal pull-up) = BENCH; grounded = PRODUCTION.
  return digitalRead(CHAOS_BENCH_MODE_GPIO) == HIGH;
}

static void send_reply(JsonDocument& doc) {
  serializeJson(doc, Serial);
  Serial.println();
}

static void reply_error(const char* msg) {
  JsonDocument doc;
  doc["ok"] = false;
  doc["error"] = msg;
  send_reply(doc);
}

// -----------------------------------------------------------------------
// Command handlers — each returns true on success.

static void handle_info() {
  JsonDocument doc;
  doc["ok"] = true;
  doc["firmware"] = CHAOS_FIRMWARE_VERSION;
  doc["serial"] = CHAOS_DEVICE_SERIAL;
  doc["hardware"] = CHAOS_HARDWARE_REV;
  doc["mode"] = bench_mode() ? "BENCH" : "PRODUCTION";
  send_reply(doc);
}

static void handle_inject(JsonDocument& req) {
  if (!bench_mode()) {
    reply_error("production-mode: chaos commands disabled");
    return;
  }
  const char* fault = req["fault"] | "";
  int duration_s = req["duration_seconds"] | 0;
  if (duration_s <= 0 || duration_s > 600) {
    reply_error("duration_seconds must be 1..600");
    return;
  }

  // Find a free slot.
  ActiveFault* slot = nullptr;
  for (auto& f : g_active) {
    if (!f.in_use) { slot = &f; break; }
  }
  if (slot == nullptr) {
    reply_error("max active faults reached");
    return;
  }

  // Power-dBm clamping (only some faults expose it; harmless if absent).
  int power = req["params"]["power_dbm"] | 0;
  bool clamped = false;
  if (power > CHAOS_MAX_TX_DBM) {
    power = CHAOS_MAX_TX_DBM;
    clamped = true;
  }

  // Dispatch to the appropriate fault routine. ALL OF THESE ARE STUBS;
  // wire them to platform APIs (esp_wifi_80211_tx, NimBLE, RadioLib)
  // before plugging into a real bench.
  if (strcmp(fault, "wifi.deauth") == 0) {
    // TODO: trigger deauth_loop() with target_bssid, channel, intensity.
  } else if (strcmp(fault, "wifi.jam") == 0) {
    // TODO: trigger wifi_jam_loop() with channel, sweep_period_ms, power.
  } else if (strcmp(fault, "ble.advertising_flood") == 0) {
    // TODO: trigger ble_flood_loop() with rate_per_second, spoofed_macs.
  } else if (strcmp(fault, "lora.jam") == 0) {
    // TODO: trigger lora_carrier_loop() with frequency_hz, bandwidth_hz,
    //       spreading_factor, power_dbm.
  } else {
    reply_error("unknown fault");
    return;
  }

  snprintf(slot->handle, sizeof(slot->handle), "h-%05d", ++g_next_handle);
  strncpy(slot->fault_name, fault, sizeof(slot->fault_name) - 1);
  slot->started_ms = millis();
  slot->duration_ms = (unsigned long)duration_s * 1000;
  slot->in_use = true;

  JsonDocument resp;
  resp["ok"] = true;
  resp["handle"] = slot->handle;
  if (clamped) resp["clamped"] = true;
  send_reply(resp);
}

static void handle_cleanup(JsonDocument& req) {
  const char* handle = req["handle"] | "";
  for (auto& f : g_active) {
    if (f.in_use && strcmp(f.handle, handle) == 0) {
      // TODO: stop the active fault routine.
      f.in_use = false;
      JsonDocument resp;
      resp["ok"] = true;
      send_reply(resp);
      return;
    }
  }
  // Idempotent — already-gone is a success at the Python side. Surface
  // it as a soft error so the audit trail records the disposition.
  JsonDocument resp;
  resp["ok"] = false;
  resp["error"] = "handle already gone";
  send_reply(resp);
}

static void handle_reset() {
  for (auto& f : g_active) f.in_use = false;
  // TODO: stop every active fault routine; reset any hardware state.
  JsonDocument resp;
  resp["ok"] = true;
  send_reply(resp);
}

// -----------------------------------------------------------------------
// Auto-expire faults whose duration has elapsed.

static void poll_expirations() {
  unsigned long now = millis();
  for (auto& f : g_active) {
    if (!f.in_use) continue;
    if (now - f.started_ms >= f.duration_ms) {
      // TODO: stop the corresponding fault routine.
      f.in_use = false;
    }
  }
}

// -----------------------------------------------------------------------
// Line-based serial reader.

static char g_line[kMaxLineLen];
static size_t g_line_len = 0;

static void process_line(const char* line) {
  JsonDocument req;
  DeserializationError err = deserializeJson(req, line);
  if (err) {
    reply_error("bad json");
    return;
  }
  const char* cmd = req["cmd"] | "";
  if (strcmp(cmd, "info") == 0) handle_info();
  else if (strcmp(cmd, "inject") == 0) handle_inject(req);
  else if (strcmp(cmd, "cleanup") == 0) handle_cleanup(req);
  else if (strcmp(cmd, "reset") == 0) handle_reset();
  else reply_error("unknown cmd");
}

void setup() {
  Serial.begin(115200);
  pinMode(CHAOS_BENCH_MODE_GPIO, INPUT_PULLUP);
  // Bench operators expect to see a banner on connection.
  delay(100);
  Serial.println("# chaos-attacker-esp32 ready");
}

void loop() {
  while (Serial.available()) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\r') continue;  // strip CR
    if (c == '\n') {
      g_line[g_line_len] = '\0';
      if (g_line_len > 0) process_line(g_line);
      g_line_len = 0;
      continue;
    }
    if (g_line_len < sizeof(g_line) - 1) {
      g_line[g_line_len++] = (char)c;
    } else {
      // Overflow — discard the rest of the line.
      g_line_len = 0;
      reply_error("line too long");
      // Skip until newline.
      while (Serial.available()) {
        int d = Serial.read();
        if (d == '\n') break;
      }
    }
  }
  poll_expirations();
}
