RELÓGIO DESPERTADOR COM ARDUINO + PYTHON (USB SERIAL)

ARQUIVOS
- alarm_clock_serial.ino  -> código do Arduino
- weather_bridge.py       -> programa em Python que envia dados pela Serial

PINAGEM USADA (sem ligar periféricos nas portas 0, 1 e 13)
- LCD 16x2:
  RS = 12
  EN = 11
  D4 = 10
  D5 = 9
  D6 = 8
  D7 = 7
- Buzzer ativo 5V = 6
- DHT11 = 5
- Botão UP = A2
- Botão DOWN = A0
- Botão OK = A1
- DS1307:
  SDA = A4
  SCL = A5

OBSERVAÇÃO
- O Arduino Uno usa a USB serial internamente para conversar com o PC.
- Portanto, não há componente externo ligado nos pinos 0 e 1, mas a comunicação USB continua funcionando normalmente.

BIBLIOTECAS DO ARDUINO
Instale na IDE:
- RTClib (Adafruit)
- DHT sensor library (Adafruit)
- Adafruit Unified Sensor

BIBLIOTECAS DO PYTHON
pip install pyserial requests

COMO USAR NO ARDUINO
1) Monte o circuito conforme a pinagem acima.
2) Carregue o arquivo alarm_clock_serial.ino.
3) Na primeira inicialização, se o RTC estiver sem horário, ele usa a data/hora da compilação.
4) Você também pode ajustar a data e hora pelo menu ou pelo Python.

NAVEGAÇÃO DOS 3 BOTÕES
- Na tela inicial:
  * UP / DOWN: alterna entre relógio, clima e sensor local
  * OK curto: abre o menu
- No menu:
  * UP / DOWN: troca item
  * OK curto: entra no item
  * OK longo: volta
- No editor:
  * UP / DOWN: altera o valor
  * OK curto: próximo campo
  * OK longo: salva
- Quando o alarme toca:
  * qualquer botão para parar

ALARMES
- 5 alarmes disponíveis
- Cada alarme pode ser:
  * Uma vez: toca só na próxima ocorrência do horário e depois desativa
  * Repetição: você marca os dias da semana

COMO USAR NO PYTHON
Exemplo no Windows:
python weather_bridge.py --port COM5 --location "Bento Goncalves, BR"

Exemplo no Linux:
python3 weather_bridge.py --port /dev/ttyUSB0 --location "Bento Goncalves, BR"

Se não informar --port, o script tenta detectar automaticamente.

PROTOCOLO SERIAL
Python -> Arduino
- TIME,YYYY,MM,DD,HH,MM,SS
- WX,temp_c,precip_mm,aqi

Arduino -> Python
- HELLO,ALARM_CLOCK
- ACK,TIME
- ACK,WX
- ACK,ALARM,n
- ALARM,n
- NOW,YYYY,MM,DD,HH,MM,SS

IDEIAS DE MELHORIA
- Persistir também a tela favorita
- Adicionar snooze
- Trocar o LCD por I2C para reduzir fios
- Adicionar ícones customizados no LCD
