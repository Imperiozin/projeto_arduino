#include <Wire.h>
#include <LiquidCrystal.h>
#include <RTClib.h>
#include <EEPROM.h>
#include <DHT.h>

const uint8_t LCD_RS = 12;
const uint8_t LCD_EN = 11;
const uint8_t LCD_D4 = 10;
const uint8_t LCD_D5 = 9;
const uint8_t LCD_D6 = 8;
const uint8_t LCD_D7 = 7;

const uint8_t BUZZER_PIN = 6;
const uint8_t DHT_PIN = 5;

const uint8_t BTN_UP_PIN = A2;
const uint8_t BTN_DOWN_PIN = A0;
const uint8_t BTN_OK_PIN = A1;

// A4/A5 = I2C do DS1307

// =============================
// Configurações gerais
// =============================
const uint8_t MAX_ALARMS = 5;
const unsigned long LONG_PRESS_MS = 700;
const unsigned long DEBOUNCE_MS = 25;
const unsigned long DISPLAY_REFRESH_MS = 200;
const unsigned long DHT_READ_MS = 2500;
const unsigned long EEPROM_SAVE_GAP_MS = 200;

#define DHTTYPE DHT11
DHT dht(DHT_PIN, DHTTYPE);
RTC_DS1307 rtc;
LiquidCrystal lcd(LCD_RS, LCD_EN, LCD_D4, LCD_D5, LCD_D6, LCD_D7);

// =============================
// Estruturas de dados
// =============================
struct AlarmData {
  uint8_t hour;
  uint8_t minute;
  uint8_t enabled;
  uint8_t repeatMask; // bit0=SEG ... bit6=DOM. 0 => tocar uma única vez na próxima ocorrência.
};

struct PersistedData {
  uint32_t magic;
  AlarmData alarms[MAX_ALARMS];
};

struct Button {
  uint8_t pin;
  bool rawState;
  bool stableState;
  unsigned long lastChangeMs;
  unsigned long pressStartMs;
  bool longFired;
  bool shortPressed;
  bool longPressed;
};

struct ExtraData {
  float outsideTemp;
  float precipitation;
  int aqi;
  bool valid;
  unsigned long lastUpdateMs;
};

struct SensorData {
  float temperature;
  float humidity;
  bool valid;
  unsigned long lastReadMs;
};

enum ScreenState {
  SCREEN_HOME_CLOCK = 0,
  SCREEN_HOME_WEATHER = 1,
  SCREEN_HOME_SENSOR = 2,
  SCREEN_MENU = 3,
  SCREEN_EDIT_TIME = 4,
  SCREEN_EDIT_ALARM = 5,
  SCREEN_ALARM_RINGING = 6
};

Button btnUp = {BTN_UP_PIN, false, false, 0, 0, false, false, false};
Button btnDown = {BTN_DOWN_PIN, false, false, 0, 0, false, false, false};
Button btnOk = {BTN_OK_PIN, false, false, 0, 0, false, false, false};

PersistedData config;
ExtraData extraData = {0.0f, 0.0f, 0, false, 0};
SensorData localSensor = {0.0f, 0.0f, false, 0};

ScreenState currentScreen = SCREEN_HOME_CLOCK;
ScreenState previousHomeScreen = SCREEN_HOME_CLOCK;
uint8_t selectedMenuIndex = 0; // 0 = data/hora, 1..MAX_ALARMS = alarmes
uint8_t currentAlarmIndex = 0;

AlarmData editingAlarm;
DateTime editingDateTime(2026, 1, 1, 0, 0, 0);
uint8_t editFieldIndex = 0;

bool alarmRinging = false;
int8_t ringingAlarmIndex = -1;
unsigned long lastDisplayMs = 0;
unsigned long lastBuzzerToggleMs = 0;
bool buzzerState = false;
unsigned long lastConfigSaveMs = 0;

unsigned long lastTriggeredMinuteByAlarm[MAX_ALARMS];
char serialBuffer[96];
uint8_t serialLen = 0;

// =============================
// Utilidades
// =============================
uint8_t dayOfWeekToMaskIndex(uint8_t rtcDow) {
  // RTClib: 0=domingo, 1=segunda, ..., 6=sábado
  switch (rtcDow) {
    case 1: return 0; // seg
    case 2: return 1; // ter
    case 3: return 2; // qua
    case 4: return 3; // qui
    case 5: return 4; // sex
    case 6: return 5; // sab
    case 0: return 6; // dom
    default: return 0;
  }
}

const char* shortDayName(uint8_t idx) {
  static const char* names[7] = {"SEG", "TER", "QUA", "QUI", "SEX", "SAB", "DOM"};
  if (idx < 7) return names[idx];
  return "---";
}

void writePadded2(char* buf, int value) {
  sprintf(buf, "%02d", value);
}

void saveConfig() {
  if (millis() - lastConfigSaveMs < EEPROM_SAVE_GAP_MS) {
    return;
  }
  EEPROM.put(0, config);
  lastConfigSaveMs = millis();
}

void loadConfig() {
  EEPROM.get(0, config);
  if (config.magic != 0xA1A2A3A4UL) {
    config.magic = 0xA1A2A3A4UL;
    for (uint8_t i = 0; i < MAX_ALARMS; i++) {
      config.alarms[i].hour = 6;
      config.alarms[i].minute = 0;
      config.alarms[i].enabled = 0;
      config.alarms[i].repeatMask = 0;
      lastTriggeredMinuteByAlarm[i] = 0;
    }
    saveConfig();
  } else {
    for (uint8_t i = 0; i < MAX_ALARMS; i++) {
      lastTriggeredMinuteByAlarm[i] = 0;
    }
  }
}

void beepOff() {
  noTone(BUZZER_PIN);
  buzzerState = false;
}

void stopAlarm() {
  alarmRinging = false;
  ringingAlarmIndex = -1;
  beepOff();
  currentScreen = previousHomeScreen;
}

void startAlarm(uint8_t alarmIndex) {
  alarmRinging = true;
  ringingAlarmIndex = alarmIndex;
  previousHomeScreen = currentScreen;
  currentScreen = SCREEN_ALARM_RINGING;
  lastBuzzerToggleMs = 0;
}

bool isAlarmDue(const AlarmData& alarm, const DateTime& now, uint8_t alarmIndex) {
  if (!alarm.enabled) return false;
  if (now.hour() != alarm.hour || now.minute() != alarm.minute) return false;

  unsigned long currentMinute = now.unixtime() / 60UL;
  if (lastTriggeredMinuteByAlarm[alarmIndex] == currentMinute) {
    return false;
  }

  if (alarm.repeatMask != 0) {
    uint8_t bitIndex = dayOfWeekToMaskIndex(now.dayOfTheWeek());
    if ((alarm.repeatMask & (1 << bitIndex)) == 0) {
      return false;
    }
  }

  return true;
}

void markAlarmTriggered(uint8_t alarmIndex, const DateTime& now) {
  lastTriggeredMinuteByAlarm[alarmIndex] = now.unixtime() / 60UL;

  // Alarme sem repetição: desativa após tocar uma vez.
  if (config.alarms[alarmIndex].repeatMask == 0) {
    config.alarms[alarmIndex].enabled = 0;
    saveConfig();
  }
}

void updateButton(Button& b) {
  bool reading = (digitalRead(b.pin) == LOW);

  if (reading != b.rawState) {
    b.rawState = reading;
    b.lastChangeMs = millis();
  }

  if ((millis() - b.lastChangeMs) > DEBOUNCE_MS && reading != b.stableState) {
    b.stableState = reading;
    if (b.stableState) {
      b.pressStartMs = millis();
      b.longFired = false;
    } else {
      if (!b.longFired) {
        b.shortPressed = true;
      }
    }
  }

  if (b.stableState && !b.longFired && (millis() - b.pressStartMs >= LONG_PRESS_MS)) {
    b.longFired = true;
    b.longPressed = true;
  }
}

bool consumeShort(Button& b) {
  if (b.shortPressed) {
    b.shortPressed = false;
    return true;
  }
  return false;
}

bool consumeLong(Button& b) {
  if (b.longPressed) {
    b.longPressed = false;
    return true;
  }
  return false;
}

void lcdPrint2Lines(const char* l1, const char* l2) {
  lcd.clear();
  lcd.setCursor(0, 0);
  lcd.print(l1);
  lcd.setCursor(0, 1);
  lcd.print(l2);
}

void formatClockLine1(const DateTime& now, char* out) {
  sprintf(out, "%02d/%02d %02d:%02d:%02d", now.day(), now.month(), now.hour(), now.minute(), now.second());
}

void formatNextAlarm(char* out) {
  for (uint8_t i = 0; i < MAX_ALARMS; i++) {
    if (config.alarms[i].enabled) {
      sprintf(out, "A%d %02d:%02d %s", i + 1, config.alarms[i].hour, config.alarms[i].minute,
              config.alarms[i].repeatMask ? "REP" : "1x ");
      return;
    }
  }
  strcpy(out, "OK abre menu");
}

void renderClockHome() {
  DateTime now = rtc.now();
  char l1[17];
  char l2[17];
  formatClockLine1(now, l1);
  formatNextAlarm(l2);
  lcdPrint2Lines(l1, l2);
}

void renderWeatherHome() {
  char l1[17];
  char l2[17];
  char tempStr[4];
  char precStr[4];

  if (!extraData.valid) {
    strcpy(l1, "Sem dados USB");
    strcpy(l2, "Python off :(");
  } else {
    dtostrf(extraData.outsideTemp, 2, 1, tempStr);
    dtostrf(extraData.precipitation, 2, 1, precStr);

    snprintf(l1, sizeof(l1), "T:%sC P:%s", tempStr, precStr);
    snprintf(l2, sizeof(l2), "AQI:%3d USB OK", extraData.aqi);
  }

  lcdPrint2Lines(l1, l2);
}

void renderSensorHome() {
  char l1[17];
  char l2[17];
  char tempStr[4];
  char humStr[4];

  if (!localSensor.valid) {
    strcpy(l1, "DHT11 sem leitura");
    strcpy(l2, "Verifique sensor");
  } else {
    dtostrf(localSensor.temperature, 2, 1, tempStr);
    dtostrf(localSensor.humidity, 2, 1, humStr);

    snprintf(l1, sizeof(l1), "Amb:%s C", tempStr);
    snprintf(l2, sizeof(l2), "Umidade:%s %%", humStr);
  }
  lcdPrint2Lines(l1, l2);
}

void renderMenu() {
  char l1[17];
  char l2[17];
  if (selectedMenuIndex == 0) {
    strcpy(l1, "Menu: Data/Hora");
  } else {
    uint8_t idx = selectedMenuIndex - 1;
    snprintf(l1, sizeof(l1), "Menu: Alarme %d", idx + 1);
  }
  strcpy(l2, "U/D | OK entra");
  lcdPrint2Lines(l1, l2);
}

void renderEditTime() {
  char l1[17];
  char l2[17];
  switch (editFieldIndex) {
    case 0: strcpy(l1, "Ajuste Ano"); snprintf(l2, sizeof(l2), "%04d", editingDateTime.year()); break;
    case 1: strcpy(l1, "Ajuste Mes"); snprintf(l2, sizeof(l2), "%02d", editingDateTime.month()); break;
    case 2: strcpy(l1, "Ajuste Dia"); snprintf(l2, sizeof(l2), "%02d", editingDateTime.day()); break;
    case 3: strcpy(l1, "Ajuste Hora"); snprintf(l2, sizeof(l2), "%02d", editingDateTime.hour()); break;
    case 4: strcpy(l1, "Ajuste Minuto"); snprintf(l2, sizeof(l2), "%02d", editingDateTime.minute()); break;
    default: strcpy(l1, "Ajuste Segundo"); snprintf(l2, sizeof(l2), "%02d", editingDateTime.second()); break;
  }
  lcdPrint2Lines(l1, l2);
}

void formatRepeatMask(char* out, uint8_t mask) {
  if (mask == 0) {
    strcpy(out, "Uma vez");
    return;
  }

  out[0] = '\0';
  for (uint8_t i = 0; i < 7; i++) {
    if (mask & (1 << i)) {
      if (strlen(out) > 0) strcat(out, " ");
      strcat(out, shortDayName(i));
    }
  }
}

void renderEditAlarm() {
  char l1[17];
  char l2[17];

  switch (editFieldIndex) {
    case 0:
      snprintf(l1, sizeof(l1), "A%d Habilitado?", currentAlarmIndex + 1);
      strcpy(l2, editingAlarm.enabled ? "Sim" : "Nao");
      break;
    case 1:
      snprintf(l1, sizeof(l1), "A%d Hora", currentAlarmIndex + 1);
      snprintf(l2, sizeof(l2), "%02d", editingAlarm.hour);
      break;
    case 2:
      snprintf(l1, sizeof(l1), "A%d Minuto", currentAlarmIndex + 1);
      snprintf(l2, sizeof(l2), "%02d", editingAlarm.minute);
      break;
    case 3:
      snprintf(l1, sizeof(l1), "A%d Repeticao", currentAlarmIndex + 1);
      strcpy(l2, editingAlarm.repeatMask ? "Repetir" : "Uma vez");
      break;
    default: {
      uint8_t dayIndex = editFieldIndex - 4;
      snprintf(l1, sizeof(l1), "%s ativo?", shortDayName(dayIndex));
      strcpy(l2, (editingAlarm.repeatMask & (1 << dayIndex)) ? "Sim" : "Nao");
      break;
    }
  }
  lcdPrint2Lines(l1, l2);
}

void renderAlarmRinging() {
  char l1[17];
  char l2[17];
  if (ringingAlarmIndex >= 0) {
    snprintf(l1, sizeof(l1), "ALARME %d TOCANDO", ringingAlarmIndex + 1);
    snprintf(l2, sizeof(l2), "%02d:%02d - aperte", config.alarms[ringingAlarmIndex].hour,
             config.alarms[ringingAlarmIndex].minute);
  } else {
    strcpy(l1, "ALARME TOCANDO");
    strcpy(l2, "Aperte botao");
  }
  lcdPrint2Lines(l1, l2);
}

void refreshDisplay() {
  if (millis() - lastDisplayMs < DISPLAY_REFRESH_MS) return;
  lastDisplayMs = millis();

  switch (currentScreen) {
    case SCREEN_HOME_CLOCK: renderClockHome(); break;
    case SCREEN_HOME_WEATHER: renderWeatherHome(); break;
    case SCREEN_HOME_SENSOR: renderSensorHome(); break;
    case SCREEN_MENU: renderMenu(); break;
    case SCREEN_EDIT_TIME: renderEditTime(); break;
    case SCREEN_EDIT_ALARM: renderEditAlarm(); break;
    case SCREEN_ALARM_RINGING: renderAlarmRinging(); break;
  }
}

void readDHTIfNeeded() {
  if (millis() - localSensor.lastReadMs < DHT_READ_MS) return;
  localSensor.lastReadMs = millis();

  float h = dht.readHumidity();
  float t = dht.readTemperature();

  if (!isnan(h) && !isnan(t)) {
    localSensor.humidity = h;
    localSensor.temperature = t;
    localSensor.valid = true;
  }
}

void processAlarmBuzzer() {
  if (!alarmRinging) return;

  unsigned long nowMs = millis();
  if (nowMs - lastBuzzerToggleMs >= 250) {
    lastBuzzerToggleMs = nowMs;
    buzzerState = !buzzerState;
    if (buzzerState) {
      tone(BUZZER_PIN, 2000);
    } else {
      noTone(BUZZER_PIN);
    }
  }
}

void applyTimeFieldDelta(int delta) {
  int y = editingDateTime.year();
  int mo = editingDateTime.month();
  int d = editingDateTime.day();
  int h = editingDateTime.hour();
  int mi = editingDateTime.minute();
  int s = editingDateTime.second();

  switch (editFieldIndex) {
    case 0:
      y += delta;
      if (y < 2024) y = 2035;
      if (y > 2035) y = 2024;
      break;
    case 1:
      mo += delta;
      if (mo < 1) mo = 12;
      if (mo > 12) mo = 1;
      break;
    case 2:
      d += delta;
      if (d < 1) d = 31;
      if (d > 31) d = 1;
      break;
    case 3:
      h += delta;
      if (h < 0) h = 23;
      if (h > 23) h = 0;
      break;
    case 4:
      mi += delta;
      if (mi < 0) mi = 59;
      if (mi > 59) mi = 0;
      break;
    default:
      s += delta;
      if (s < 0) s = 59;
      if (s > 59) s = 0;
      break;
  }

  editingDateTime = DateTime(y, mo, d, h, mi, s);
}

void applyAlarmFieldDelta(int delta) {
  switch (editFieldIndex) {
    case 0:
      editingAlarm.enabled = !editingAlarm.enabled;
      break;
    case 1: {
      int v = (int)editingAlarm.hour + delta;
      if (v < 0) v = 23;
      if (v > 23) v = 0;
      editingAlarm.hour = v;
      break;
    }
    case 2: {
      int v = (int)editingAlarm.minute + delta;
      if (v < 0) v = 59;
      if (v > 59) v = 0;
      editingAlarm.minute = v;
      break;
    }
    case 3:
      if (editingAlarm.repeatMask == 0) {
        editingAlarm.repeatMask = 0b01111100; // seg-sex por padrão ao habilitar repetição
      } else {
        editingAlarm.repeatMask = 0;
      }
      break;
    default: {
      uint8_t dayIndex = editFieldIndex - 4;
      if (dayIndex < 7) {
        editingAlarm.repeatMask ^= (1 << dayIndex);
      }
      break;
    }
  }
}

void enterTimeEditor() {
  DateTime now = rtc.now();
  editingDateTime = now;
  editFieldIndex = 0;
  currentScreen = SCREEN_EDIT_TIME;
}

void enterAlarmEditor(uint8_t alarmIndex) {
  currentAlarmIndex = alarmIndex;
  editingAlarm = config.alarms[alarmIndex];
  editFieldIndex = 0;
  currentScreen = SCREEN_EDIT_ALARM;
}

void handleHomeNavigation() {
  if (consumeShort(btnUp)) {
    if (currentScreen == SCREEN_HOME_CLOCK) currentScreen = SCREEN_HOME_SENSOR;
    else currentScreen = (ScreenState)(currentScreen - 1);
  }

  if (consumeShort(btnDown)) {
    if (currentScreen == SCREEN_HOME_SENSOR) currentScreen = SCREEN_HOME_CLOCK;
    else currentScreen = (ScreenState)(currentScreen + 1);
  }

  if (consumeShort(btnOk)) {
    selectedMenuIndex = 0;
    previousHomeScreen = currentScreen;
    currentScreen = SCREEN_MENU;
  }
}

void handleMenu() {
  if (consumeShort(btnUp)) {
    if (selectedMenuIndex == 0) selectedMenuIndex = MAX_ALARMS;
    else selectedMenuIndex--;
  }
  if (consumeShort(btnDown)) {
    selectedMenuIndex++;
    if (selectedMenuIndex > MAX_ALARMS) selectedMenuIndex = 0;
  }
  if (consumeShort(btnOk)) {
    if (selectedMenuIndex == 0) {
      enterTimeEditor();
    } else {
      enterAlarmEditor(selectedMenuIndex - 1);
    }
  }
  if (consumeLong(btnOk)) {
    currentScreen = previousHomeScreen;
  }
}

void handleTimeEditor() {
  if (consumeShort(btnUp)) applyTimeFieldDelta(+1);
  if (consumeShort(btnDown)) applyTimeFieldDelta(-1);
  if (consumeShort(btnOk)) {
    editFieldIndex++;
    if (editFieldIndex > 5) editFieldIndex = 0;
  }
  if (consumeLong(btnOk)) {
    rtc.adjust(editingDateTime);
    currentScreen = SCREEN_MENU;
    Serial.println(F("ACK,TIME"));
  }
}

void handleAlarmEditor() {
  uint8_t maxField = (editingAlarm.repeatMask == 0) ? 3 : 10;

  if (consumeShort(btnUp)) applyAlarmFieldDelta(+1);
  if (consumeShort(btnDown)) applyAlarmFieldDelta(-1);
  if (consumeShort(btnOk)) {
    editFieldIndex++;
    if (editFieldIndex > maxField) editFieldIndex = 0;
  }
  if (consumeLong(btnOk)) {
    config.alarms[currentAlarmIndex] = editingAlarm;
    saveConfig();
    currentScreen = SCREEN_MENU;
    Serial.print(F("ACK,ALARM,"));
    Serial.println(currentAlarmIndex + 1);
  }
}

void handleAlarmRinging() {
  if (consumeShort(btnUp) || consumeShort(btnDown) || consumeShort(btnOk) || consumeLong(btnOk)) {
    stopAlarm();
  }
}

void checkAlarms() {
  if (alarmRinging) return;

  DateTime now = rtc.now();
  for (uint8_t i = 0; i < MAX_ALARMS; i++) {
    if (isAlarmDue(config.alarms[i], now, i)) {
      markAlarmTriggered(i, now);
      startAlarm(i);
      Serial.print(F("ALARM,"));
      Serial.println(i + 1);
      break;
    }
  }
}

void processCommand(char* line) {
  char* cmd = strtok(line, ",");
  if (!cmd) return;

  if (strcmp(cmd, "TIME") == 0) {
    char* sy = strtok(NULL, ",");
    char* smo = strtok(NULL, ",");
    char* sd = strtok(NULL, ",");
    char* sh = strtok(NULL, ",");
    char* smin = strtok(NULL, ",");
    char* ss = strtok(NULL, ",");
    if (sy && smo && sd && sh && smin && ss) {
      DateTime dt(atoi(sy), atoi(smo), atoi(sd), atoi(sh), atoi(smin), atoi(ss));
      rtc.adjust(dt);
      Serial.println(F("ACK,TIME"));
    }
  } else if (strcmp(cmd, "WX") == 0) {
    char* st = strtok(NULL, ",");
    char* sp = strtok(NULL, ",");
    char* saqi = strtok(NULL, ",");
    if (st && sp && saqi) {
      extraData.outsideTemp = atof(st);
      extraData.precipitation = atof(sp);
      extraData.aqi = atoi(saqi);
      extraData.valid = true;
      extraData.lastUpdateMs = millis();
      Serial.println(F("ACK,WX"));
    }
  } else if (strcmp(cmd, "PING") == 0) {
    Serial.println(F("PONG"));
  } else if (strcmp(cmd, "STATUS") == 0) {
    DateTime now = rtc.now();
    Serial.print(F("NOW,"));
    Serial.print(now.year()); Serial.print(',');
    Serial.print(now.month()); Serial.print(',');
    Serial.print(now.day()); Serial.print(',');
    Serial.print(now.hour()); Serial.print(',');
    Serial.print(now.minute()); Serial.print(',');
    Serial.println(now.second());
  }
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;

    if (c == '\n') {
      serialBuffer[serialLen] = '\0';
      if (serialLen > 0) {
        processCommand(serialBuffer);
      }
      serialLen = 0;
    } else if (serialLen < sizeof(serialBuffer) - 1) {
      serialBuffer[serialLen++] = c;
    } else {
      serialLen = 0;
    }
  }
}

void setup() {
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(BTN_UP_PIN, INPUT_PULLUP);
  pinMode(BTN_DOWN_PIN, INPUT_PULLUP);
  pinMode(BTN_OK_PIN, INPUT_PULLUP);

  lcd.begin(16, 2);
  Wire.begin();
  dht.begin();
  Serial.begin(9600);

  if (!rtc.begin()) {
    lcdPrint2Lines("RTC nao achado", "Verifique DS1307");
    while (true) {
      delay(100);
    }
  }

  if (!rtc.isrunning()) {
    rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
  }

  loadConfig();
  lcdPrint2Lines("Relogio Serial", "Iniciando...");
  delay(1200);

  Serial.println(F("HELLO,ALARM_CLOCK"));
  Serial.println(F("INFO,Comandos: TIME/WX/PING/STATUS"));
}

void loop() {
  updateButton(btnUp);
  updateButton(btnDown);
  updateButton(btnOk);

  readSerialCommands();
  readDHTIfNeeded();
  checkAlarms();
  processAlarmBuzzer();

  switch (currentScreen) {
    case SCREEN_HOME_CLOCK:
    case SCREEN_HOME_WEATHER:
    case SCREEN_HOME_SENSOR:
      handleHomeNavigation();
      break;
    case SCREEN_MENU:
      handleMenu();
      break;
    case SCREEN_EDIT_TIME:
      handleTimeEditor();
      break;
    case SCREEN_EDIT_ALARM:
      handleAlarmEditor();
      break;
    case SCREEN_ALARM_RINGING:
      handleAlarmRinging();
      break;
  }

  refreshDisplay();
}
