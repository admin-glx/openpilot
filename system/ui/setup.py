#!/usr/bin/env python3
import os
import re
import threading
import time
import urllib.request
from urllib.parse import urlparse
from enum import IntEnum
import shutil

import pyray as rl

from cereal import log
from openpilot.common.run import run_cmd
from openpilot.system.hardware import HARDWARE
from openpilot.system.ui.lib.scroll_panel import GuiScrollPanel
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.button import Button, ButtonStyle, ButtonRadio
from openpilot.system.ui.widgets.keyboard import Keyboard
from openpilot.system.ui.widgets.label import Label, TextAlignment
from openpilot.system.ui.widgets.network import WifiManagerUI, WifiManager

# Arabic shaping support for raylib/pyray
import arabic_reshaper
from bidi.algorithm import get_display

def shape_arabic(text):
  return get_display(arabic_reshaper.reshape(text))

NetworkType = log.DeviceState.NetworkType

MARGIN = 50
TITLE_FONT_SIZE = 116
TITLE_FONT_WEIGHT = FontWeight.MEDIUM
NEXT_BUTTON_WIDTH = 310
BODY_FONT_SIZE = 96
BUTTON_HEIGHT = 160
BUTTON_SPACING = 50

OPENPILOT_URL = "https://openpilot.comma.ai"
USER_AGENT = f"AGNOSSetup-{HARDWARE.get_os_version()}"

CONTINUE_PATH = "/data/continue.sh"
TMP_CONTINUE_PATH = "/data/continue.sh.new"
INSTALL_PATH = "/data/openpilot"
VALID_CACHE_PATH = "/data/.openpilot_cache"
INSTALLER_SOURCE_PATH = "/usr/comma/installer"
INSTALLER_DESTINATION_PATH = "/tmp/installer"
INSTALLER_URL_PATH = "/tmp/installer_url"

CONTINUE = """#!/usr/bin/env bash

cd /data/openpilot
exec ./launch_openpilot.sh
"""

class SetupState(IntEnum):
  LOW_VOLTAGE = 0
  GETTING_STARTED = 1
  NETWORK_SETUP = 2
  SOFTWARE_SELECTION = 3
  CUSTOM_SOFTWARE = 4
  DOWNLOADING = 5
  DOWNLOAD_FAILED = 6
  CUSTOM_SOFTWARE_WARNING = 7


class Setup(Widget):
  def __init__(self):
    super().__init__()
    self.state = SetupState.GETTING_STARTED
    self.network_check_thread = None
    self.network_connected = threading.Event()
    self.wifi_connected = threading.Event()
    self.stop_network_check_thread = threading.Event()
    self.failed_url = ""
    self.failed_reason = ""
    self.download_url = ""
    self.download_progress = 0
    self.download_thread = None
    self.wifi_ui = WifiManagerUI(WifiManager())
    self.keyboard = Keyboard()
    self.selected_radio = None
    self.warning = gui_app.texture("icons/warning.png", 150, 150)
    self.checkmark = gui_app.texture("icons/circled_check.png", 100, 100)

    self._low_voltage_title_label = Label(
      shape_arabic("تحذير: الجهد منخفض"),
      TITLE_FONT_SIZE,
      FontWeight.MEDIUM,
      TextAlignment.LEFT,
      text_color=rl.Color(255, 89, 79, 255)
    )

    self._low_voltage_body_label = Label(
      shape_arabic("قم بتشغيل الجهاز داخل السيارة باستخدام الخرطوم أو تابع على مسؤوليتك الخاصة."),
      BODY_FONT_SIZE,
      text_alignment=TextAlignment.LEFT
    )

    self._low_voltage_continue_button = Button(
      shape_arabic("متابعة"),
      self._low_voltage_continue_button_callback
    )

    self._low_voltage_poweroff_button = Button(
      shape_arabic("إيقاف التشغيل"),
      HARDWARE.shutdown
    )

    self._getting_started_button = Button(
      "",
      self._getting_started_button_callback,
      button_style=ButtonStyle.PRIMARY,
      border_radius=0
    )

    self._getting_started_title_label = Label(
      shape_arabic("لنبدأ"),
      TITLE_FONT_SIZE,
      FontWeight.BOLD,
      TextAlignment.LEFT
    )

    self._getting_started_body_label = Label(
      shape_arabic("قبل الانطلاق، لنكمل التثبيت ونراجع بعض التفاصيل."),
      BODY_FONT_SIZE,
      text_alignment=TextAlignment.LEFT
    )

    self._software_selection_openpilot_button = ButtonRadio(
      shape_arabic("القائد الآلي"),
      self.checkmark,
      font_size=BODY_FONT_SIZE,
      text_padding=80
    )

    self._software_selection_custom_software_button = ButtonRadio(
      shape_arabic("برنامج مخصّص"),
      self.checkmark,
      font_size=BODY_FONT_SIZE,
      text_padding=80
    )

    self._software_selection_continue_button = Button(
      shape_arabic("متابعة"),
      self._software_selection_continue_button_callback,
      button_style=ButtonStyle.PRIMARY
    )

    self._software_selection_continue_button.set_enabled(False)

    self._software_selection_back_button = Button(
      shape_arabic("رجوع"),
      self._software_selection_back_button_callback
    )

    self._software_selection_title_label = Label(
      shape_arabic("اختر البرنامج المستخدم"),
      TITLE_FONT_SIZE,
      FontWeight.BOLD,
      TextAlignment.LEFT
    )

    self._download_failed_reboot_button = Button(
      shape_arabic("إعادة تشغيل الجهاز"),
      HARDWARE.reboot
    )

    self._download_failed_startover_button = Button(
      shape_arabic("البدء من جديد"),
      self._download_failed_startover_button_callback,
      button_style=ButtonStyle.PRIMARY
    )

    self._download_failed_title_label = Label(
      shape_arabic("فشل التنزيل"),
      TITLE_FONT_SIZE,
      FontWeight.BOLD,
      TextAlignment.LEFT
    )

    self._download_failed_url_label = Label(
      "",
      64,
      FontWeight.NORMAL,
      TextAlignment.LEFT
    )

    self._download_failed_body_label = Label(
      "",
      BODY_FONT_SIZE,
      text_alignment=TextAlignment.LEFT
    )

    self._network_setup_back_button = Button(
      shape_arabic("رجوع"),
      self._network_setup_back_button_callback
    )

    self._network_setup_continue_button = Button(
      shape_arabic("في انتظار الإنترنت"),
      self._network_setup_continue_button_callback,
      button_style=ButtonStyle.PRIMARY
    )

    self._network_setup_continue_button.set_enabled(False)

    self._network_setup_title_label = Label(
      shape_arabic("الاتصال بشبكة Wi-Fi"),
      TITLE_FONT_SIZE,
      FontWeight.BOLD,
      TextAlignment.LEFT
    )

    self._custom_software_warning_continue_button = Button(
      shape_arabic("مرّر للمتابعة"),
      self._custom_software_warning_continue_button_callback,
      button_style=ButtonStyle.PRIMARY
    )

    self._custom_software_warning_continue_button.set_enabled(False)

    self._custom_software_warning_back_button = Button(
      shape_arabic("رجوع"),
      self._custom_software_warning_back_button_callback
    )

    self._custom_software_warning_title_label = Label(
      shape_arabic("تحذير: برنامج مخصّص"),
      100,
      FontWeight.BOLD,
      TextAlignment.LEFT,
      text_color=rl.Color(255,89,79,255),
      text_padding=60
    )

    self._custom_software_warning_body_label = Label(
      shape_arabic(
        "استخدم الحذر عند تثبيت برامج خارجية.\n\n"
        + "⚠️ لم يتم اختباره بواسطة NMK.\n\n"
        + "⚠️ قد لا يتوافق مع معايير السلامة المعتمدة.\n\n"
        + "⚠️ قد يسبب ضرراً للجهاز و/أو المركبة.\n\n"
        + "إذا أردت المتابعة، استخدم https://flash.nmk.ai "
        + "لاستعادة الجهاز إلى حالة المصنع لاحقاً."
      ),
      85,
      text_alignment=TextAlignment.LEFT,
      text_padding=60
    )

    self._custom_software_warning_body_scroll_panel = GuiScrollPanel()

    self._downloading_body_label = Label(
      shape_arabic("جارٍ التنزيل..."),
      TITLE_FONT_SIZE,
      FontWeight.MEDIUM
    )

    try:
      with open("/sys/class/hwmon/hwmon1/in1_input") as f:
        voltage = float(f.read().strip()) / 1000.0
        if voltage < 7:
          self.state = SetupState.LOW_VOLTAGE
    except (FileNotFoundError, ValueError):
      self.state = SetupState.LOW_VOLTAGE

  # بقية الكود يبقى كما هو بدون تعديل...

def main():
  try:
    gui_app.init_window(shape_arabic("إعداد القائد الآلي"), 20)
    setup = Setup()
    for _ in gui_app.render():
      setup.render(rl.Rectangle(0, 0, gui_app.width, gui_app.height))
    setup.close()
  except Exception as e:
    print(f"Setup error: {e}")
  finally:
    gui_app.close()


if __name__ == "__main__":
  main()
