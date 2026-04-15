#!/usr/bin/env python3
"""
Bridge Serial <-> Internet para o relógio despertador com Arduino.

Funções:
- Descobre coordenadas de uma cidade com Open-Meteo Geocoding.
- Busca temperatura externa e precipitação atual.
- Busca AQI atual.
- Envia os dados periodicamente pela Serial USB ao Arduino.
- Também sincroniza a data/hora do RTC do Arduino.

Protocolo enviado ao Arduino:
  TIME,YYYY,MM,DD,HH,MM,SS\n
  WX,temp_c,precip_mm,aqi\n
Exemplo:
  TIME,2026,4,7,14,30,0
  WX,22.4,0.0,37
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import random
import sys
import time
from dataclasses import dataclass
from typing import Optional, List

import requests
import serial
from serial.tools import list_ports
import matplotlib.pyplot as plt
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"


@dataclass
class Location:
    name: str
    latitude: float
    longitude: float
    timezone: str


@dataclass
class ExtraWeather:
    temperature_c: float
    precipitation_mm: float
    aqi: int


@dataclass
class SensorData:
    timestamp: dt.datetime
    temperature: float
    humidity: float


class ArduinoBridge:
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial_conn: Optional[serial.Serial] = None
        self.sensor_data: List[SensorData] = []
        self._email_config: dict = {}
        self.last_weather: Optional[ExtraWeather] = None

    def connect(self) -> None:
        logging.info("Abrindo serial em %s (%d bps)", self.port, self.baudrate)
        self.serial_conn = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        time.sleep(2.0)  # tempo para reset do Arduino Uno ao abrir a serial
        self._drain_input()

    def close(self) -> None:
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
        self.serial_conn = None

    def ensure_connected(self) -> None:
        if self.serial_conn and self.serial_conn.is_open:
            return
        self.connect()

    def send_line(self, line: str) -> None:
        self.ensure_connected()
        assert self.serial_conn is not None
        payload = (line.strip() + "\n").encode("utf-8")
        logging.info("-> %s", line)
        self.serial_conn.write(payload)
        self.serial_conn.flush()

    def read_available(self) -> None:
        if not self.serial_conn or not self.serial_conn.is_open:
            return
        while self.serial_conn.in_waiting:
            line = self.serial_conn.readline().decode("utf-8", errors="replace").strip()
            if line:
                logging.info("<- %s", line)
                self._process_incoming_line(line)

    def _drain_input(self) -> None:
        deadline = time.time() + 1.5
        while time.time() < deadline:
            self.read_available()
            time.sleep(0.1)

    def _process_incoming_line(self, line: str) -> None:
        parts = line.split(",")
        if not parts:
            return
        cmd = parts[0]
        if cmd == "SENSOR" and len(parts) >= 3:
            try:
                temp = float(parts[1])
                hum = float(parts[2])
                now = dt.datetime.now()
                self.sensor_data.append(SensorData(now, temp, hum))
                # Manter apenas últimas 24h
                cutoff = now - dt.timedelta(hours=24)
                self.sensor_data = [d for d in self.sensor_data if d.timestamp > cutoff]
                logging.info("Sensor data received: temp=%.1f, hum=%.1f", temp, hum)
            except ValueError:
                pass
        elif cmd == "ALARM" and len(parts) >= 2:
            try:
                alarm_num = parts[1]
                logging.info("Alarm triggered: ALARM,%s", alarm_num)
                self._send_alarm_email(alarm_num)
            except (ValueError, IndexError) as e:
                logging.error("Error processing alarm: %s", e)
    def _send_alarm_email(self, alarm_num: str) -> None:
        logging.debug("[EMAIL] Iniciando envio de email para alarme %s", alarm_num)
        
        if not self.sensor_data:
            logging.warning("[EMAIL] Nenhum dado de sensor para enviar no email.")
            return
        
        logging.debug("[EMAIL] Dados de sensor disponíveis: %d registros", len(self.sensor_data))

        config = self._email_config
        
        if not config or not config.get('enabled'):
            logging.warning("[EMAIL] Email não configurado ou desabilitado")
            return
        
        logging.debug("[EMAIL] Configurações de email carregadas: smtp=%s, to=%s", config.get('smtp'), config.get('to'))
        logging.debug("[EMAIL] Criando mensagem de email")
        msg = MIMEMultipart()
        msg['From'] = config['user']
        msg['To'] = config['to']
        msg['Subject'] = f"Alarme {alarm_num} disparado - Dados de Sensor"
        logging.debug("[EMAIL] Subject: %s", msg['Subject'])

        # Criar tabela HTML com dados do Arduino e da API
        table_html = "<table border='1' style='border-collapse:collapse; width:100%;'>"
        table_html += "<tr style='background-color:#f0f0f0;'><th>Data/Hora</th><th>Temp Local (°C)</th><th>Umidade (%)</th><th>Temp Externa (°C)</th><th>Precipitação (mm)</th><th>AQI</th></tr>"
        
        for data in self.sensor_data[-20:]:  # Últimos 20 registros
            temp_ext = self.last_weather.temperature_c if self.last_weather else 0.0
            precip = self.last_weather.precipitation_mm if self.last_weather else 0.0
            aqi = self.last_weather.aqi if self.last_weather else 0
            
            table_html += f"<tr><td>{data.timestamp.strftime('%Y-%m-%d %H:%M:%S')}</td>"
            table_html += f"<td>{data.temperature:.1f}</td>"
            table_html += f"<td>{data.humidity:.1f}</td>"
            table_html += f"<td>{temp_ext:.1f}</td>"
            table_html += f"<td>{precip:.1f}</td>"
            table_html += f"<td>{aqi}</td></tr>"
        
        table_html += "</table>"

        body = f"<h2>Alarme {alarm_num} disparou</h2>"
        body += "<p><strong>Dados de Sensor (últimas 24 horas):</strong></p>"
        body += table_html
        
        if self.last_weather:
            body += f"<br><p><strong>Dados Atuais da API:</strong></p>"
            body += f"<ul>"
            body += f"<li>Temperatura Externa: {self.last_weather.temperature_c:.1f}°C</li>"
            body += f"<li>Precipitação: {self.last_weather.precipitation_mm:.1f}mm</li>"
            body += f"<li>AQI: {self.last_weather.aqi}</li>"
            body += f"</ul>"
        
        msg.attach(MIMEText(body, 'html'))

        # Criar gráfico com 2 séries de temperatura (local + externa)
        if len(self.sensor_data) > 0:
            timestamps = [d.timestamp for d in self.sensor_data]
            temps_local = [d.temperature for d in self.sensor_data]
            
            # Criar série de temperatura externa com mesma quantidade de pontos
            temps_externa = []
            if self.last_weather:
                temps_externa = [self.last_weather.temperature_c for _ in temps_local]
            else:
                temps_externa = [0.0 for _ in temps_local]
            
            plt.figure(figsize=(12, 6))
            plt.plot(timestamps, temps_local, label='Temperatura Local (Arduino)', marker='o', linewidth=2, color='#FF6B6B')
            plt.plot(timestamps, temps_externa, label='Temperatura Externa (API)', marker='s', linewidth=2, color='#4ECDC4', linestyle='--')
            plt.title('Comparação de Temperaturas', fontsize=14, fontweight='bold')
            plt.xlabel('Tempo', fontsize=12)
            plt.ylabel('Temperatura (°C)', fontsize=12)
            plt.legend(fontsize=11)
            plt.grid(True, alpha=0.3)
            plt.xticks(rotation=45)
            plt.tight_layout()
            plt.savefig('sensor_plot.png', dpi=100, bbox_inches='tight')
            plt.close()

            with open('sensor_plot.png', 'rb') as f:
                img = MIMEImage(f.read())
                img.add_header('Content-ID', '<sensor_plot>')
                msg.attach(img)

        try:
            logging.debug("[EMAIL] Conectando ao servidor SMTP: %s:%d", config['smtp'], config['port'])
            server = smtplib.SMTP_SSL(config['smtp'], config['port'], timeout=10)
            
            logging.debug("[EMAIL] Autenticando com usuário: %s", config['user'])
            server.login(config['user'], config['pass'])
            
            logging.debug("[EMAIL] Preparando conteúdo da mensagem")
            text = msg.as_string()
            
            logging.debug("[EMAIL] Enviando email para: %s", config['to'])
            server.sendmail(config['user'], config['to'], text)
            server.quit()
            logging.info("[EMAIL] ✓ Email enviado com sucesso para alarme %s", alarm_num)
        except TimeoutError as e:
            logging.error("[EMAIL] ✗ Timeout ao conectar ao servidor SMTP (%s:%d): %s", config['smtp'], config['port'], e)
            logging.warning("[EMAIL] Possíveis causas: firewall, ISP bloqueando porta SMTP, servidor indisponível ou conexão instável")
        except smtplib.SMTPAuthenticationError as e:
            logging.error("[EMAIL] ✗ Erro de autenticação SMTP: %s", e)
            logging.warning("[EMAIL] Verifique: usuário, senha, ou se é necessário ativar 'apps menos seguros' no Gmail")
        except smtplib.SMTPException as e:
            logging.error("[EMAIL] ✗ Erro SMTP: %s", e)
        except ConnectionRefusedError as e:
            logging.error("[EMAIL] ✗ Conexão recusada pelo servidor SMTP: %s", e)
            logging.warning("[EMAIL] Servidor pode estar offline ou porta incorreta")
        except OSError as e:
            logging.error("[EMAIL] ✗ Erro de rede ao conectar ao SMTP: %s", e)
            logging.warning("[EMAIL] Verifique conectividade de internet e configurações de firewall")
        except Exception as e:
            logging.error("[EMAIL] ✗ Erro inesperado ao enviar email: %s", e)
            import traceback
            logging.debug("[EMAIL] Traceback: %s", traceback.format_exc())


def auto_detect_port() -> Optional[str]:
    keywords = ("arduino", "ch340", "usb serial", "wchusb", "cp210", "uno")
    ports = list(list_ports.comports())

    for port in ports:
        text = " ".join(filter(None, [port.device, port.description, port.manufacturer, port.product])).lower()
        if any(keyword in text for keyword in keywords):
            return port.device

    return ports[0].device if ports else None


def geocode_location(name: str, language: str = "pt") -> Location:
    params = {
        "name": name,
        "count": 1,
        "language": language,
        "format": "json",
    }
    response = requests.get(GEOCODING_URL, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    results = data.get("results") or []
    if not results:
        raise RuntimeError(f"Nenhuma localizacao encontrada para: {name}")

    item = results[0]
    return Location(
        name=item.get("name", name),
        latitude=float(item["latitude"]),
        longitude=float(item["longitude"]),
        timezone=item.get("timezone", "auto"),
    )


def fetch_weather(location: Location) -> ExtraWeather:
    weather_resp = requests.get(
        WEATHER_URL,
        params={
            "latitude": location.latitude,
            "longitude": location.longitude,
            "current": "temperature_2m,precipitation",
            "timezone": "auto",
        },
        timeout=20,
    )
    weather_resp.raise_for_status()
    weather_data = weather_resp.json()
    current_weather = weather_data.get("current", {})

    air_resp = requests.get(
        AIR_QUALITY_URL,
        params={
            "latitude": location.latitude,
            "longitude": location.longitude,
            "current": "us_aqi",
            "timezone": "auto",
        },
        timeout=20,
    )
    air_resp.raise_for_status()
    air_data = air_resp.json()
    current_air = air_data.get("current", {})

    return ExtraWeather(
        temperature_c=float(current_weather.get("temperature_2m", 0.0)),
        precipitation_mm=float(current_weather.get("precipitation", 0.0)),
        aqi=int(round(float(current_air.get("us_aqi", 0)))),
    )


def format_time_command(now: dt.datetime) -> str:
    return f"TIME,{now.year},{now.month},{now.day},{now.hour},{now.minute},{now.second}"


def format_weather_command(weather: ExtraWeather) -> str:
    return f"WX,{weather.temperature_c:.1f},{weather.precipitation_mm:.1f},{weather.aqi}"


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(message)s")
    # Desabilitar logs DEBUG desnecessários do matplotlib e PIL
    logging.getLogger('matplotlib').setLevel(logging.WARNING)
    logging.getLogger('PIL').setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Envia clima/AQI para um relógio com Arduino via USB Serial.")
    parser.add_argument("--port", help="Porta serial (ex.: COM5, /dev/ttyUSB0). Se omitido, tenta detectar.")
    parser.add_argument("--baudrate", type=int, default=9600, help="Baudrate da conexão serial.")
    parser.add_argument(
        "--location",
        default="Bento Gonçalves",
        help="Localizacao usada para clima e qualidade do ar.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Intervalo, em segundos, entre atualizacoes do clima/AQI.",
    )
    parser.add_argument(
        "--time-sync-interval",
        type=int,
        default=3600,
        help="Intervalo, em segundos, entre sincronizacoes do RTC.",
    )
    parser.add_argument("--verbose", action="store_true", help="Mostra logs detalhados.")
    parser.add_argument(
        "--sensor-interval", 
        type=int, 
        default=30, 
        help="Intervalo em minutos para coleta de dados de sensor (padrão 30)."
    )
    parser.add_argument(
        "--email-enabled", 
        action="store_true", 
        default=True,
        help="Habilitar envio de email quando alarme dispara."
    )
    parser.add_argument(
        "--email-smtp", 
        type=str,
        default="smtp.gmail.com",
        help="Servidor SMTP para envio de email.")
    parser.add_argument(
        "--email-port", 
        type=int, 
        default=465, 
        help="Porta SMTP."
    )
    parser.add_argument(
        "--email-user",
        type=str,
        default="guibasalvati@gmail.com",
        help="Usuário do email."
    )
    parser.add_argument(
        "--email-pass",
        type=str,
        default="hazh rhpg otys njeg",         
        help="Senha do email.")
    parser.add_argument(
        "--email-to",
        type=str,
        default="guibasalvati@gmail.com",
        help="Email destinatário."
    )
    parser.add_argument(
        "--simulate-sensor-data",
        action="store_true",
        default=False,
        help="Simular dados de sensor das últimas 24 horas para teste."
    )
    return parser.parse_args()


def _simulate_sensor_data(bridge: ArduinoBridge, interval_minutes: int) -> None:
    now = dt.datetime.now()
    start = now - dt.timedelta(hours=24)
    interval = dt.timedelta(minutes=interval_minutes)
    current = start
    while current <= now:
        temp = 20 + random.uniform(-5, 10)  # Temp entre 15-30°C
        hum = 40 + random.uniform(-10, 20)   # Hum entre 30-60%
        bridge.sensor_data.append(SensorData(current, temp, hum))
        current += interval
    logging.info("Simulados %d registros de sensor para as últimas 24 horas.", len(bridge.sensor_data))


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)

    try:
        location = geocode_location(args.location)
    except Exception as exc:
        logging.error("Falha ao resolver localizacao: %s", exc)
        return 1

    port = args.port or auto_detect_port()
    if not port:
        logging.error("Nenhuma porta serial encontrada.")
        return 1

    logging.info(
        "Localizacao resolvida: %s (lat=%.5f, lon=%.5f, tz=%s)",
        location.name,
        location.latitude,
        location.longitude,
        location.timezone,
    )
    logging.info("Usando porta serial: %s", port)

    bridge = ArduinoBridge(port=port, baudrate=args.baudrate)
    email_config = {
        'enabled': args.email_enabled,
        'smtp': args.email_smtp,
        'port': args.email_port,
        'user': args.email_user,
        'pass': args.email_pass,
        'to': args.email_to,
    }
    bridge._email_config = email_config

    if args.simulate_sensor_data:
        _simulate_sensor_data(bridge, args.sensor_interval)

    last_weather_sync = 0.0
    last_time_sync = 0.0

    while True:
        try:
            bridge.ensure_connected()
            bridge.read_available()

            now = time.time()

            if now - last_time_sync >= args.time_sync_interval:
                current_time = dt.datetime.now()
                bridge.send_line(format_time_command(current_time))
                last_time_sync = now
                time.sleep(0.2)
                bridge.read_available()

            if now - last_weather_sync >= args.interval:
                weather = fetch_weather(location)
                bridge.last_weather = weather
                bridge.send_line(format_weather_command(weather))
                last_weather_sync = now
                time.sleep(0.2)
                bridge.read_available()

            time.sleep(1.0)

        except KeyboardInterrupt:
            logging.info("Encerrado pelo usuario.")
            bridge.close()
            return 0
        except (serial.SerialException, OSError) as exc:
            logging.warning("Falha na serial (%s). Tentando reconectar...", exc)
            bridge.close()
            time.sleep(3.0)
        except requests.RequestException as exc:
            logging.warning("Falha ao consultar APIs: %s", exc)
            time.sleep(10.0)
        except Exception as exc:
            logging.exception("Erro inesperado: %s", exc)
            time.sleep(5.0)


if __name__ == "__main__":
    sys.exit(main())
