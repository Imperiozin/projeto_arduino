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
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests
import serial
from serial.tools import list_ports

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


class ArduinoBridge:
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial_conn: Optional[serial.Serial] = None

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

    def _drain_input(self) -> None:
        deadline = time.time() + 1.5
        while time.time() < deadline:
            self.read_available()
            time.sleep(0.1)


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
    return parser.parse_args()


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
