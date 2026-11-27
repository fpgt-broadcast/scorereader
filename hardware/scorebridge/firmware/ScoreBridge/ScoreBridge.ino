// ScoreBridge firmware for the WT32-ETH01 on the ScoreBridge carrier board.
//
// Reads the scoreboard console on one of three inputs (20 mA current loop through an optocoupler, RS-232,
// RS-485/422) and streams the raw bytes, untouched, to every TCP client (ScoreReader's "TCP" source) and to the
// USB port (ScoreReader's "Serial" source at 115200). Configure it from its web page (http://scorebridge.local/
// or its IP address), or from ScoreReader: GET/POST /api/network on the network, "@SB" command lines on USB
// (see handleUsbLine below). Builds with the Arduino-ESP32 core 2.0.x or 3.x (PlatformIO: see ../platformio.ini).
//
// Pins (carrier board v1.0): current loop IO35, RS-232 IO14, RS-485 IO4; LEDs NET IO32, CLIENT IO33, DATA IO2.

#include <Arduino.h>
#include <ETH.h>
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPUpdateServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <esp_task_wdt.h>

#define FW_VERSION "1.1.1"

// ---------------------------------------------------------------- hardware
static const int PIN_IN_CL = 35;
static const int PIN_IN_232 = 14;
static const int PIN_IN_485 = 4;
static const int PIN_LED_NET = 32;
static const int PIN_LED_CLIENT = 33;
static const int PIN_LED_DATA = 2;
// WT32-ETH01 Ethernet: LAN8720, PHY address 1, MDC 23, MDIO 18, oscillator enabled by GPIO16, clock into GPIO0
static const int ETH_ADDR = 1, ETH_POWER = 16, ETH_MDC = 23, ETH_MDIO = 18;

static const int MAX_CLIENTS = 4;
static const char *INPUT_NAMES[] = {"Current loop", "RS-232", "RS-485 / RS-422"};
static const char *INPUT_KEYS[] = {"cl", "rs232", "rs485"};

// ---------------------------------------------------------------- settings (kept in flash)
struct Settings {
  uint8_t input = 0;            // 0 current loop, 1 RS-232, 2 RS-485
  uint32_t baud = 19200;
  bool invert = true;           // Daktronics current loop needs the signal inverted
  String format = "8N1";
  uint16_t tcpPort = 10001;     // same default as Lantronix serial servers
  String hostname = "scorebridge";
  String ip, mask, gateway, dns; // blank ip = DHCP
  String ssid, wifiPass;        // blank ssid = Wi-Fi off (setup access point only)
  String apPass = "scorebridge";
  String adminPass;             // blank = settings page open to anyone on the network
  bool usbMirror = true;        // copy the raw data to USB (ScoreReader "Serial" source, 115200)
} cfg;

Preferences prefs;
WebServer web(80);
HTTPUpdateServer updater;
WiFiServer *tcp = nullptr;
WiFiClient clients[MAX_CLIENTS];
uint32_t clientStall[MAX_CLIENTS];

volatile bool ethUp = false, staUp = false;
bool apOn = false, forceAp = false;
uint32_t lastNetUp = 0, apStarted = 0, bootMs = 0;
uint64_t bytesIn = 0;
uint32_t lastByteMs = 0, rateBytes = 0, rateMs = 0, rateBps = 0;
uint8_t recent[48];
size_t recentLen = 0;

String jsonStr(const String &s);
bool parseIp(const String &s, IPAddress &out);

void logln(const String &s) {
  if (!cfg.usbMirror) Serial.println(s);
}

// ---------------------------------------------------------------- settings storage
void loadSettings() {
  prefs.begin("sb", true);
  cfg.input = prefs.getUChar("input", cfg.input);
  cfg.baud = prefs.getUInt("baud", cfg.baud);
  cfg.invert = prefs.getBool("invert", cfg.invert);
  cfg.format = prefs.getString("format", cfg.format);
  cfg.tcpPort = prefs.getUShort("port", cfg.tcpPort);
  cfg.hostname = prefs.getString("host", cfg.hostname);
  cfg.ip = prefs.getString("ip", "");
  cfg.mask = prefs.getString("mask", "");
  cfg.gateway = prefs.getString("gw", "");
  cfg.dns = prefs.getString("dns", "");
  cfg.ssid = prefs.getString("ssid", "");
  cfg.wifiPass = prefs.getString("wpass", "");
  cfg.apPass = prefs.getString("appass", cfg.apPass);
  cfg.adminPass = prefs.getString("admin", "");
  cfg.usbMirror = prefs.getBool("usb", cfg.usbMirror);
  prefs.end();
  if (cfg.input > 2) cfg.input = 0;
  if (cfg.baud < 300 || cfg.baud > 1000000) cfg.baud = 19200;
}

void saveSettings() {
  prefs.begin("sb", false);
  prefs.putUChar("input", cfg.input);
  prefs.putUInt("baud", cfg.baud);
  prefs.putBool("invert", cfg.invert);
  prefs.putString("format", cfg.format);
  prefs.putUShort("port", cfg.tcpPort);
  prefs.putString("host", cfg.hostname);
  prefs.putString("ip", cfg.ip);
  prefs.putString("mask", cfg.mask);
  prefs.putString("gw", cfg.gateway);
  prefs.putString("dns", cfg.dns);
  prefs.putString("ssid", cfg.ssid);
  prefs.putString("wpass", cfg.wifiPass);
  prefs.putString("appass", cfg.apPass);
  prefs.putString("admin", cfg.adminPass);
  prefs.putBool("usb", cfg.usbMirror);
  prefs.end();
}

// Power-cycling three times within a few seconds of starting wipes the network settings and opens the setup
// access point (the BOOT pin can't be used for this: on the WT32-ETH01 it carries the Ethernet clock).
void checkResetGesture() {
  prefs.begin("sb", false);
  uint8_t boots = prefs.getUChar("boots", 0) + 1;
  prefs.putUChar("boots", boots);
  if (boots >= 3) {
    prefs.putUChar("boots", 0);
    for (const char *k : {"ip", "mask", "gw", "dns", "ssid", "wpass", "admin", "appass", "host", "port"}) prefs.remove(k);
    forceAp = true;
  }
  prefs.end();
}

// ---------------------------------------------------------------- serial input
uint32_t serialConfig(const String &f) {
  if (f == "8E1") return SERIAL_8E1;
  if (f == "8O1") return SERIAL_8O1;
  if (f == "7E1") return SERIAL_7E1;
  if (f == "7O1") return SERIAL_7O1;
  if (f == "8N2") return SERIAL_8N2;
  return SERIAL_8N1;
}

void startInput() {
  static const int pins[] = {PIN_IN_CL, PIN_IN_232, PIN_IN_485};
  Serial2.end();
  Serial2.setRxBufferSize(8192);
  // TX is not wired on the carrier; -1 keeps the core's default TX pin (IO17, unconnected).
  Serial2.begin(cfg.baud, serialConfig(cfg.format), pins[cfg.input], -1, cfg.invert);
  logln(String("Input: ") + INPUT_NAMES[cfg.input] + " " + cfg.baud + " " + cfg.format + (cfg.invert ? " inverted" : ""));
}

// ---------------------------------------------------------------- network
bool parseIp(const String &s, IPAddress &out) { return s.length() && out.fromString(s); }

void onNetEvent(arduino_event_id_t event) {
  switch (event) {
    case ARDUINO_EVENT_ETH_START: ETH.setHostname(cfg.hostname.c_str()); break;
    case ARDUINO_EVENT_ETH_GOT_IP: ethUp = true; break;
    case ARDUINO_EVENT_ETH_DISCONNECTED:
    case ARDUINO_EVENT_ETH_STOP: ethUp = false; break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP: staUp = true; break;
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
    case ARDUINO_EVENT_WIFI_STA_LOST_IP: staUp = false; break;
    default: break;
  }
}

void startNetwork() {
  WiFi.onEvent(onNetEvent);
  WiFi.setHostname(cfg.hostname.c_str());
  if (cfg.ssid.length()) {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
  }
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  ETH.begin(ETH_PHY_LAN8720, ETH_ADDR, ETH_MDC, ETH_MDIO, ETH_POWER, ETH_CLOCK_GPIO0_IN);
#else
  ETH.begin(ETH_ADDR, ETH_POWER, ETH_MDC, ETH_MDIO, ETH_PHY_LAN8720, ETH_CLOCK_GPIO0_IN);
#endif
  IPAddress ip, mask, gw, dns;
  if (parseIp(cfg.ip, ip)) {
    if (!parseIp(cfg.mask, mask)) mask = IPAddress(255, 255, 255, 0);
    if (!parseIp(cfg.gateway, gw)) gw = IPAddress(0, 0, 0, 0);
    if (!parseIp(cfg.dns, dns)) dns = gw;
    ETH.config(ip, gw, mask, dns);  // the static address is for Ethernet; Wi-Fi always uses DHCP
  }
  if (cfg.ssid.length()) WiFi.begin(cfg.ssid.c_str(), cfg.wifiPass.c_str());
  MDNS.begin(cfg.hostname.c_str());
  MDNS.addService("http", "tcp", 80);
  MDNS.addService("scorebridge", "tcp", cfg.tcpPort);
  tcp = new WiFiServer(cfg.tcpPort, MAX_CLIENTS);
  tcp->begin();
  tcp->setNoDelay(true);
}

String apName() {
  uint64_t mac = ESP.getEfuseMac();
  char buf[24];
  snprintf(buf, sizeof buf, "ScoreBridge-%02X%02X", (uint8_t)(mac >> 32), (uint8_t)(mac >> 40));
  return buf;
}

void manageAccessPoint() {
  bool netUp = ethUp || staUp;
  uint32_t now = millis();
  if (netUp) lastNetUp = now;
  bool want = forceAp || (!netUp && now - lastNetUp > 15000 && now - bootMs > 15000);
  if (want && !apOn) {
    WiFi.mode(cfg.ssid.length() ? WIFI_AP_STA : WIFI_AP);
    String pass = cfg.apPass.length() >= 8 ? cfg.apPass : String("scorebridge");
    WiFi.softAP(apName().c_str(), pass.c_str());
    apOn = true;
    apStarted = now;
    logln("Setup access point " + apName() + " at " + WiFi.softAPIP().toString());
  }
  // once the real network is up, close the access point (after 2 minutes, and only when nobody is on it)
  if (apOn && !forceAp && netUp && now - apStarted > 120000 && WiFi.softAPgetStationNum() == 0) {
    WiFi.softAPdisconnect(true);
    WiFi.mode(cfg.ssid.length() ? WIFI_STA : WIFI_OFF);
    apOn = false;
  }
}

// ---------------------------------------------------------------- TCP clients
int clientCount() {
  int n = 0;
  for (auto &c : clients) n += c.connected() ? 1 : 0;
  return n;
}

void acceptClients() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  WiFiClient nc = tcp->accept();
#else
  WiFiClient nc = tcp->available();
#endif
  if (!nc) return;
  for (int i = 0; i < MAX_CLIENTS; i++) {
    if (!clients[i].connected()) {
      clients[i].stop();
      clients[i] = nc;
      clients[i].setNoDelay(true);
#if ESP_ARDUINO_VERSION_MAJOR >= 3
      clients[i].setTimeout(500);
#else
      clients[i].setTimeout(1);
#endif
      clientStall[i] = 0;
      logln("Client " + nc.remoteIP().toString() + " connected");
      return;
    }
  }
  nc.stop();  // full
}

void sendToClients(const uint8_t *buf, size_t n) {
  uint32_t now = millis();
  for (int i = 0; i < MAX_CLIENTS; i++) {
    WiFiClient &c = clients[i];
    if (!c.connected()) continue;
    while (c.available()) c.read();  // ScoreReader never sends anything; drop it
    size_t w = c.write(buf, n);
    if (w == n) {
      clientStall[i] = 0;
    } else if (!clientStall[i]) {
      clientStall[i] = now;
    } else if (now - clientStall[i] > 3000) {
      c.stop();  // a client that stopped reading must not hold everyone up
    }
  }
}

// ---------------------------------------------------------------- network settings (web API and USB commands)
String netJson() {
  String j = "{";
  j += "\"hostname\":" + jsonStr(cfg.hostname);
  j += ",\"dhcp\":" + String(cfg.ip.length() ? "false" : "true");
  j += ",\"ip\":" + jsonStr(cfg.ip);
  j += ",\"mask\":" + jsonStr(cfg.mask);
  j += ",\"gateway\":" + jsonStr(cfg.gateway);
  j += ",\"dns\":" + jsonStr(cfg.dns);
  j += ",\"tcp_port\":" + String(cfg.tcpPort);
  j += ",\"current_ip\":" + jsonStr(ethUp ? ETH.localIP().toString() : String(""));
  j += ",\"link\":" + String(ETH.linkUp() ? "true" : "false");
  j += ",\"mac\":" + jsonStr(ETH.macAddress());
  j += ",\"version\":" + jsonStr(FW_VERSION);
  j += "}";
  return j;
}

bool validHostname(const String &h) {
  if (!h.length() || h.length() > 32 || h[0] == '-') return false;
  for (char c : h)
    if (!isalnum((unsigned char)c) && c != '-') return false;
  return true;
}

// Checks and stores new Ethernet settings. ip blank = DHCP. Returns "" when saved, else the reason.
// *changed tells the caller to restart (the network stack only takes a new address at start-up).
String applyNetwork(String host, String ip, String mask, String gw, String dns, bool *changed) {
  IPAddress a;
  host.trim(); ip.trim(); mask.trim(); gw.trim(); dns.trim();
  if (!host.length()) host = cfg.hostname;
  if (!validHostname(host)) return "name: letters, digits and - only (max 32)";
  if (ip.length()) {
    if (!parseIp(ip, a)) return "IP address is not valid";
    if (!mask.length()) mask = "255.255.255.0";
    if (!parseIp(mask, a)) return "subnet mask is not valid";
    if (gw.length() && !parseIp(gw, a)) return "gateway is not valid";
    if (dns.length() && !parseIp(dns, a)) return "DNS is not valid";
  } else {
    mask = gw = dns = "";  // DHCP: nothing else applies
  }
  *changed = host != cfg.hostname || ip != cfg.ip || mask != cfg.mask || gw != cfg.gateway || dns != cfg.dns;
  cfg.hostname = host; cfg.ip = ip; cfg.mask = mask; cfg.gateway = gw; cfg.dns = dns;
  if (*changed) saveSettings();
  return "";
}

bool restartPending = false;
uint32_t restartAt = 0;
void restartSoon() { restartPending = true; restartAt = millis() + 700; }

// ---------------------------------------------------------------- USB commands
// ScoreReader sends one text line: "@SB GET" or "@SB NET host=scorebridge ip=192.168.1.50 mask=255.255.255.0
// gw=192.168.1.1 dns=192.168.1.1" (ip= blank = DHCP). The reply is one JSON object between "\r\n@SB>" and
// "<@SB\r\n", written in one piece so it never splits the console data around it.
String usbLine;

String argOf(const String &line, const char *key) {
  String k = String(" ") + key + "=";
  int i = line.indexOf(k);
  if (i < 0) return "";
  i += k.length();
  int e = line.indexOf(' ', i);
  return e < 0 ? line.substring(i) : line.substring(i, e);
}

void usbReply(const String &json) { Serial.print("\r\n@SB>" + json + "<@SB\r\n"); }

void handleUsbLine(String line) {
  line.trim();
  if (!line.startsWith("@SB ")) return;
  String cmd = line.substring(4);
  int sp = cmd.indexOf(' ');
  String verb = sp < 0 ? cmd : cmd.substring(0, sp);
  if (verb == "GET") {
    usbReply("{\"ok\":true,\"network\":" + netJson() + "}");
  } else if (verb == "NET") {
    String args = " " + (sp < 0 ? String("") : cmd.substring(sp + 1));
    bool changed = false;
    String err = applyNetwork(argOf(args, "host"), argOf(args, "ip"), argOf(args, "mask"), argOf(args, "gw"),
                              argOf(args, "dns"), &changed);
    if (err.length()) {
      usbReply("{\"ok\":false,\"error\":" + jsonStr(err) + "}");
      return;
    }
    usbReply("{\"ok\":true,\"restarting\":" + String(changed ? "true" : "false") + ",\"network\":" + netJson() + "}");
    if (changed) restartSoon();
  } else {
    usbReply("{\"ok\":false,\"error\":\"unknown command\"}");
  }
}

void pollUsb() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (usbLine.length()) handleUsbLine(usbLine);
      usbLine = "";
    } else if (usbLine.length() < 300) {
      usbLine += c;
    }
  }
}

// ---------------------------------------------------------------- web page
bool authorised() {
  if (!cfg.adminPass.length()) return true;
  if (web.authenticate("admin", cfg.adminPass.c_str())) return true;
  web.requestAuthentication();
  return false;
}

// The browser must be talking to this box by its own address: an IP, a plain name or a .local name. A web page that
// points its own domain at the box (DNS rebinding) sends that domain as Host and is refused.
bool hostOk() {
  String h = web.hostHeader();
  int c = h.indexOf(':');
  if (c >= 0) h = h.substring(0, c);
  h.toLowerCase();
  if (!h.length()) return true;  // HTTP/1.0 tools
  if (h.indexOf('.') < 0 || h.endsWith(".local")) return true;
  for (char ch : h) if (!(isdigit((unsigned char)ch) || ch == '.')) return false;
  return true;  // an IPv4 address
}

// For anything that changes the box: a form or fetch on another site's page (CSRF) carries that site's Origin, and
// would otherwise work with no password, or with the password the browser remembers. ScoreReader and curl send none.
bool allowedChange() {
  if (!hostOk() || (web.hasHeader("Origin") && web.header("Origin") != String("http://") + web.hostHeader())) {
    web.send(403, "text/plain", "Refused: changes must come from this ScoreBridge's own page or from ScoreReader");
    return false;
  }
  return authorised();
}

String esc(const String &s) {
  String o;
  for (char c : s) {
    if (c == '<') o += "&lt;";
    else if (c == '>') o += "&gt;";
    else if (c == '"') o += "&quot;";
    else if (c == '&') o += "&amp;";
    else o += c;
  }
  return o;
}

String jsonStr(const String &s) {
  String o = "\"";
  for (char c : s) {
    if (c == '"' || c == '\\') { o += '\\'; o += c; }
    else if ((uint8_t)c < 0x20) o += ' ';
    else o += c;
  }
  return o + "\"";
}

String statusJson() {
  String clientsJson = "[";
  bool first = true;
  for (auto &c : clients) {
    if (!c.connected()) continue;
    clientsJson += (first ? "" : ",") + jsonStr(c.remoteIP().toString());
    first = false;
  }
  clientsJson += "]";
  String hex;
  for (size_t i = 0; i < recentLen; i++) {
    char b[4];
    snprintf(b, sizeof b, "%02X ", recent[i]);
    hex += b;
  }
  String j = "{";
  j += "\"version\":" + jsonStr(FW_VERSION);
  j += ",\"input\":" + jsonStr(INPUT_KEYS[cfg.input]);
  j += ",\"baud\":" + String(cfg.baud);
  j += ",\"invert\":" + String(cfg.invert ? "true" : "false");
  j += ",\"format\":" + jsonStr(cfg.format);
  j += ",\"tcp_port\":" + String(cfg.tcpPort);
  j += ",\"ethernet\":" + jsonStr(ethUp ? ETH.localIP().toString() : (ETH.linkUp() ? String("link, no IP") : String("")));
  j += ",\"wifi\":" + jsonStr(staUp ? WiFi.localIP().toString() : String(""));
  j += ",\"access_point\":" + jsonStr(apOn ? apName() + " " + WiFi.softAPIP().toString() : String(""));
  j += ",\"clients\":" + clientsJson;
  j += ",\"bytes\":" + String((double)bytesIn, 0);
  j += ",\"bytes_per_sec\":" + String(rateBps);
  j += ",\"last_byte_ms\":" + String(lastByteMs ? millis() - lastByteMs : 0);
  j += ",\"uptime_s\":" + String(millis() / 1000);
  j += ",\"recent_hex\":" + jsonStr(hex);
  j += "}";
  return j;
}

const char PAGE_HEAD[] PROGMEM = R"HTML(<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ScoreBridge</title><style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--mut:#5b6474;--acc:#0b63ce;--bd:#d9dde4}
@media(prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--fg:#e8ebf0;--mut:#9aa3b2;--acc:#5aa2ff;--bd:#323844}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px}h1{font-size:22px;margin:4px 0 14px}
section{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;margin-bottom:14px}
h2{font-size:15px;margin:0 0 10px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
label{display:block;margin:8px 0 3px;color:var(--mut);font-size:13px}
input,select{width:100%;box-sizing:border-box;padding:8px;border:1px solid var(--bd);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
input[type=checkbox]{width:auto;margin-right:6px}.row{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
button{padding:9px 14px;border:0;border-radius:6px;background:var(--acc);color:#fff;font:inherit;cursor:pointer;margin:10px 8px 0 0}
button.alt{background:var(--mut)}.kv{display:grid;grid-template-columns:150px 1fr;gap:4px 10px}.kv div:nth-child(odd){color:var(--mut)}
code{font-size:12px;word-break:break-all}.presets button{background:transparent;color:var(--acc);border:1px solid var(--acc);margin:4px 6px 0 0;padding:6px 10px}
@media(max-width:560px){.row{grid-template-columns:1fr}.kv{grid-template-columns:110px 1fr}}
</style></head><body><main><h1>ScoreBridge</h1>)HTML";

void handleRoot() {
  if (!authorised()) return;
  String h = FPSTR(PAGE_HEAD);
  h += F("<section><h2>Status</h2><div class=kv id=st><div>Loading</div><div></div></div></section>");
  h += F("<form method=post action=/save><section><h2>Console input</h2><div class=presets>"
         "<button type=button onclick=\"p(0,19200,1,'8N1')\">Daktronics current loop</button>"
         "<button type=button onclick=\"p(0,2400,1,'8N1')\">Electro-Mech current loop</button>"
         "<button type=button onclick=\"p(1,115200,0,'8N1')\">Colorado Timing V2</button>"
         "<button type=button onclick=\"p(1,9600,0,'8N1')\">Colorado Timing V1</button>"
         "<button type=button onclick=\"p(1,19200,0,'8N1')\">Daktronics RTD (J6)</button></div><div class=row>");
  h += "<div><label>Input</label><select name=input id=input>";
  for (int i = 0; i < 3; i++)
    h += String("<option value=") + i + (cfg.input == i ? " selected" : "") + ">" + INPUT_NAMES[i] + "</option>";
  h += "</select></div><div><label>Baud</label><input name=baud id=baud type=number min=300 max=1000000 value=" + String(cfg.baud) + "></div>";
  h += "<div><label>Format</label><select name=format id=format>";
  for (const char *f : {"8N1", "8E1", "8O1", "7E1", "7O1", "8N2"})
    h += String("<option") + (cfg.format == f ? " selected" : "") + ">" + f + "</option>";
  h += "</select></div><div><label>&nbsp;</label><label><input type=checkbox name=invert id=invert value=1" +
       String(cfg.invert ? " checked" : "") + ">Invert signal (current loop usually needs this)</label></div></div>";
  h += "<label><input type=checkbox name=usb value=1" + String(cfg.usbMirror ? " checked" : "") +
       ">Also send the data to USB (115200 baud)</label></section>";
  h += "<section><h2>Network</h2><div class=row>";
  h += "<div><label>Name (name.local)</label><input name=host value=\"" + esc(cfg.hostname) + "\"></div>";
  h += "<div><label>TCP port for ScoreReader</label><input name=port type=number value=" + String(cfg.tcpPort) + "></div>";
  h += "<div><label>Ethernet static IP (blank = DHCP)</label><input name=ip value=\"" + esc(cfg.ip) + "\"></div>";
  h += "<div><label>Subnet mask</label><input name=mask placeholder=255.255.255.0 value=\"" + esc(cfg.mask) + "\"></div>";
  h += "<div><label>Gateway</label><input name=gw value=\"" + esc(cfg.gateway) + "\"></div>";
  h += "<div><label>DNS</label><input name=dns value=\"" + esc(cfg.dns) + "\"></div>";
  h += "<div><label>Wi-Fi network (blank = Ethernet only)</label><input name=ssid value=\"" + esc(cfg.ssid) + "\"></div>";
  h += "<div><label>Wi-Fi password</label><input name=wpass type=password placeholder=\"(unchanged)\"></div>";
  h += "<div><label>Setup access point password (8+ chars)</label><input name=appass type=password placeholder=\"(unchanged)\"></div>";
  h += "<div><label>Settings password (user: admin)</label><input name=admin type=password placeholder=\"" +
       String(cfg.adminPass.length() ? "(unchanged)" : "(none)") + "\"></div>";
  h += F("</div><button>Save</button><button type=button class=alt onclick=\"if(confirm('Restart?'))fetch('/reboot',{method:'POST'})\">Restart</button>"
         "<button type=button class=alt onclick=\"location='/update'\">Firmware update</button></section></form>");
  h += F("<script>function p(i,b,v,f){input.value=i;baud.value=b;invert.checked=!!v;format.value=f}"
         "async function s(){try{const j=await(await fetch('/api/status')).json();const r=["
         "['Ethernet',j.ethernet||'not connected'],['Wi-Fi',j.wifi||'off'],['Setup AP',j.access_point||'off'],"
         "['ScoreReader',j.clients.length?j.clients.join(', '):'none connected'],"
         "['Input',j.input+' '+j.baud+' '+j.format+(j.invert?' inverted':'')],"
         "['Data',j.bytes+' bytes, '+j.bytes_per_sec+' B/s'+(j.bytes?', last '+(j.last_byte_ms/1000).toFixed(1)+' s ago':'')],"
         "['TCP port',j.tcp_port],['Last bytes','<code>'+(j.recent_hex||'-')+'</code>'],['Firmware',j.version+', up '+j.uptime_s+' s']];"
         "st.innerHTML=r.map(x=>'<div>'+x[0]+'</div><div>'+x[1]+'</div>').join('')}catch(e){}}s();setInterval(s,1000)</script>");
  h += F("</main></body></html>");
  web.send(200, "text/html", h);
}

void applySerialArgs() {
  if (web.hasArg("input")) {
    String v = web.arg("input");
    for (int i = 0; i < 3; i++) if (v == INPUT_KEYS[i] || v == String(i)) cfg.input = i;
  }
  if (web.hasArg("baud")) {
    uint32_t b = web.arg("baud").toInt();
    if (b >= 300 && b <= 1000000) cfg.baud = b;
  }
  if (web.hasArg("format")) cfg.format = web.arg("format");
}

void handleSave() {
  if (!allowedChange()) return;
  String oldNet = cfg.hostname + cfg.tcpPort + cfg.ip + cfg.mask + cfg.gateway + cfg.dns + cfg.ssid;
  applySerialArgs();
  cfg.invert = web.hasArg("invert");
  cfg.usbMirror = web.hasArg("usb");
  cfg.hostname = web.arg("host").length() ? web.arg("host") : String("scorebridge");
  uint32_t port = web.arg("port").toInt();
  if (port > 0 && port < 65536) cfg.tcpPort = port;
  cfg.ip = web.arg("ip"); cfg.mask = web.arg("mask"); cfg.gateway = web.arg("gw"); cfg.dns = web.arg("dns");
  cfg.ssid = web.arg("ssid");
  if (web.arg("wpass").length()) cfg.wifiPass = web.arg("wpass");
  if (web.arg("appass").length() >= 8) cfg.apPass = web.arg("appass");
  if (web.arg("admin").length()) cfg.adminPass = web.arg("admin");
  saveSettings();
  startInput();
  bool reboot = oldNet != cfg.hostname + cfg.tcpPort + cfg.ip + cfg.mask + cfg.gateway + cfg.dns + cfg.ssid;
  web.sendHeader("Location", "/");
  web.send(303, "text/plain", reboot ? "Saved, restarting" : "Saved");
  if (reboot) { delay(500); ESP.restart(); }
}

// POST /api/serial?input=cl|rs232|rs485&baud=19200&invert=1&format=8N1[&save=1] (for ScoreReader to set the
// input for the chosen scoreboard; without save=1 the change lasts until the next restart)
void handleApiSerial() {
  if (!allowedChange()) return;
  applySerialArgs();
  if (web.hasArg("invert")) cfg.invert = web.arg("invert") == "1" || web.arg("invert") == "true";
  if (web.arg("save") == "1") saveSettings();
  startInput();
  web.send(200, "application/json", statusJson());
}

// GET /api/network: the Ethernet settings. POST /api/network host=&ip=&mask=&gw=&dns= (ip blank = DHCP):
// saves them and restarts when they changed (ScoreReader then finds the box at its new address).
void handleApiNetwork() {
  web.sendHeader("Access-Control-Allow-Origin", "*");
  if (web.method() == HTTP_GET) {
    web.send(200, "application/json", netJson());
    return;
  }
  if (!allowedChange()) return;
  bool changed = false;
  String err = applyNetwork(web.arg("host"), web.arg("ip"), web.arg("mask"), web.arg("gw"), web.arg("dns"), &changed);
  if (err.length()) {
    web.send(400, "application/json", "{\"ok\":false,\"error\":" + jsonStr(err) + "}");
    return;
  }
  web.send(200, "application/json",
           "{\"ok\":true,\"restarting\":" + String(changed ? "true" : "false") + ",\"network\":" + netJson() + "}");
  if (changed) restartSoon();
}

void startWeb() {
  web.on("/", HTTP_GET, handleRoot);
  web.on("/save", HTTP_POST, handleSave);
  web.on("/api/status", HTTP_GET, [] { web.sendHeader("Access-Control-Allow-Origin", "*"); web.send(200, "application/json", statusJson()); });
  web.on("/api/serial", HTTP_POST, handleApiSerial);
  web.on("/api/network", HTTP_ANY, handleApiNetwork);
  web.on("/reboot", HTTP_POST, [] { if (!allowedChange()) return; web.send(200, "text/plain", "Restarting"); delay(300); ESP.restart(); });
  // Firmware over the network only with a settings password: without one, anyone on the network (or on the setup
  // access point, whose default password is public) could load their own firmware. USB flashing always works.
  if (cfg.adminPass.length()) updater.setup(&web, "/update", "admin", cfg.adminPass.c_str());
  else web.on("/update", HTTP_ANY, [] {
    web.send(403, "text/plain", "Set a settings password on the main page and restart the ScoreBridge to update its "
                                "firmware over the network. Over USB always works.");
  });
  static const char *headerKeys[] = {"Origin"};
  web.collectHeaders(headerKeys, 1);
  web.onNotFound([] { web.send(404, "text/plain", "Not found"); });
  web.begin();
}

// ---------------------------------------------------------------- LEDs
void updateLeds() {
  uint32_t now = millis();
  bool netUp = ethUp || staUp;
  digitalWrite(PIN_LED_NET, netUp ? HIGH : (apOn ? ((now / 500) & 1) : LOW));  // blinks in setup-AP mode
  digitalWrite(PIN_LED_CLIENT, clientCount() > 0 ? HIGH : LOW);
  digitalWrite(PIN_LED_DATA, (lastByteMs && now - lastByteMs < 30) ? HIGH : LOW);
}

// ---------------------------------------------------------------- main
void setup() {
  bootMs = millis();
  pinMode(PIN_LED_NET, OUTPUT);
  pinMode(PIN_LED_CLIENT, OUTPUT);
  pinMode(PIN_LED_DATA, OUTPUT);
  loadSettings();
  Serial.begin(115200);
  checkResetGesture();
  if (forceAp) {
    cfg = Settings();
    loadSettings();
  }
  logln(String("\nScoreBridge ") + FW_VERSION);
  startInput();
  startNetwork();
  startWeb();
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_config_t wdt = {.timeout_ms = 15000, .idle_core_mask = 0, .trigger_panic = true};
  esp_task_wdt_reconfigure(&wdt);
#else
  esp_task_wdt_init(15, true);
#endif
  esp_task_wdt_add(NULL);
}

void loop() {
  static uint8_t buf[1024];
  static bool bootsCleared = false;
  esp_task_wdt_reset();

  int avail = Serial2.available();
  if (avail > 0) {
    size_t n = Serial2.read(buf, min((size_t)avail, sizeof buf));
    if (n) {
      bytesIn += n;
      rateBytes += n;
      lastByteMs = millis();
      if (n >= sizeof recent) {
        memcpy(recent, buf + n - sizeof recent, sizeof recent);
        recentLen = sizeof recent;
      } else {
        size_t keep = min(recentLen, sizeof recent - n);
        memmove(recent, recent + recentLen - keep, keep);
        memcpy(recent + keep, buf, n);
        recentLen = keep + n;
      }
      sendToClients(buf, n);
      if (cfg.usbMirror) Serial.write(buf, n);
    }
  }
  uint32_t now = millis();
  if (now - rateMs >= 1000) {
    rateBps = rateBytes * 1000 / (now - rateMs);
    rateBytes = 0;
    rateMs = now;
  }
  acceptClients();
  web.handleClient();
  pollUsb();
  if (restartPending && (int32_t)(now - restartAt) >= 0) {
    Serial.flush();
    ESP.restart();
  }
  manageAccessPoint();
  updateLeds();
  if (!bootsCleared && now - bootMs > 8000) {  // ran long enough: this wasn't a reset gesture
    prefs.begin("sb", false);
    prefs.putUChar("boots", 0);
    prefs.end();
    bootsCleared = true;
  }
  if (!avail) delay(1);
}
