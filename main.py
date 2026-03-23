import face_recognition
import cv2
import numpy as np
import os
import snap7
import time
import threading
import webbrowser
import socket
import pandas as pd
import json
from flask import Flask, render_template_string, Response, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit
import logging
import pygame
import struct
import re
import pyttsx3  # 语音合成库

# ========== 初始化 pygame 混音器 ==========
pygame.mixer.init(frequency=22050, size=-16, channels=2, buffer=512)

# ========== 日志配置 ==========
log_filename = 'plc_debug.log'
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, mode='a', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== PLC通讯配置 ==========
PLC_MAIN = {
    'ip': '192.168.8.4',
    'rack': 0,
    'slot': 1
}

PLC_IO = {
    'ip': '192.168.8.6',
    'rack': 0,
    'slot': 1
}

# 动态配置的PLC目标地址（用于写入）
PLC_TARGETS = []
PLC_UPDATE_INTERVAL = 2.0
PLC_DATA_READ_INTERVAL = 2.0
RECOGNITION_THRESHOLD = 0.6
PLC_RETRY_DELAY = 0.5

# PLC操作锁
plc_lock = threading.Lock()
face_id_lock = threading.Lock()

# 音频播放相关全局变量
last_trigger_time = 0
playing_audio = False
audio_lock = threading.Lock()
AUDIO_COOLDOWN_SECONDS = 10

# ========== 订单状态常量 ==========
ORDER_STATUS_PENDING = '待处理'
ORDER_STATUS_ACTIVE = '进行中'
ORDER_STATUS_COMPLETED = '已完成'
ORDER_STATUS_TIMEOUT = '超时'

# ========== 全局订单管理 ==========
orders = []                     # 存储所有订单
order_counter = 2190            # 订单编号计数器（从2190开始）
orders_lock = threading.Lock()
ORDER_FILE = 'order.json'

# ========== 订单完成互斥控制（使用 DB1000.DBW4 订单合格数量） ==========
last_qualified_count = 0        # 上次发送时的订单合格数量
qualified_lock = threading.Lock()  # 普通锁，用于保护 last_qualified_count

# ========== 自检状态标志 ==========
self_check_passed = False
self_check_lock = threading.Lock()

# ========== 加载物料映射表和PLC配置 ==========
print("="*60)
print("人脸识别系统 - 三端架构")
print("="*60)

mapping = {}
try:
    excel_path = 'C:/Users/A1498/Desktop/YL-Mes/Data.xlsx'
    df_raw = pd.read_excel(excel_path, header=None)
    
    # 找到配置行
    config_row_idx = -1
    for i in range(len(df_raw)):
        if df_raw.iloc[i].astype(str).str.contains('传输到的块|偏移量').any():
            config_row_idx = i
            break
    
    if config_row_idx == -1:
        raise ValueError("Excel中未找到包含'传输到的块'或'偏移量'的配置行")

    config_data = df_raw.iloc[config_row_idx]
    offset_row_data = df_raw.iloc[config_row_idx + 1] if config_row_idx + 1 < len(df_raw) else None

    if offset_row_data is not None:
        print("偏移行原始数据:", offset_row_data.values)

    db_person_str = str(config_data.iloc[0]) if pd.notna(config_data.iloc[0]) else "28"
    db_material1_str = str(config_data.iloc[1]) if pd.notna(config_data.iloc[1]) else "1000"
    db_material2_str = str(config_data.iloc[2]) if pd.notna(config_data.iloc[2]) else "1000"

    offset_person = int(offset_row_data.iloc[0]) if offset_row_data is not None and pd.notna(offset_row_data.iloc[0]) else 0
    offset_mat1 = int(offset_row_data.iloc[1]) if offset_row_data is not None and pd.notna(offset_row_data.iloc[1]) else 10
    offset_mat2 = int(offset_row_data.iloc[2]) if offset_row_data is not None and pd.notna(offset_row_data.iloc[2]) else 12

    def extract_db_num(db_str):
        match = re.search(r'DB(\d+)', str(db_str))
        return int(match.group(1)) if match else 28

    db_person = extract_db_num(db_person_str)
    db_material1 = extract_db_num(db_material1_str)
    db_material2 = extract_db_num(db_material2_str)

    PLC_TARGETS = [
        {'name': '人脸ID', 'db': db_person, 'offset': offset_person},
        {'name': '物料1', 'db': db_material1, 'offset': offset_mat1},
        {'name': '物料2', 'db': db_material2, 'offset': offset_mat2}
    ]

    print("解析到的PLC写入目标：")
    for t in PLC_TARGETS:
        print(f"  {t['name']}: DB{t['db']}.DBW{t['offset']}")

    data_df = df_raw.iloc[:config_row_idx]
    data_df.columns = data_df.iloc[0]
    data_df = data_df.drop(data_df.index[0]).reset_index(drop=True)
    data_df.columns = data_df.columns.str.strip()

    required_cols = ['人', '物料1', '物料2']
    for col in required_cols:
        if col not in data_df.columns:
            raise KeyError(f"缺少必需的列: '{col}'")

    for _, row in data_df.iterrows():
        try:
            face_id = int(float(row['人']))
            mat1 = int(float(row['物料1']))
            mat2 = int(float(row['物料2']))
            mapping[face_id] = (mat1, mat2)
        except (ValueError, TypeError, KeyError):
            continue

    print(f"✅ 物料映射表加载完成，共 {len(mapping)} 条记录。")

except Exception as e:
    logger.error(f"加载Excel失败: {e}")
    print(f"❌ 加载失败: {e}")
    print("   程序将使用默认配置进行测试。")
    PLC_TARGETS = [
        {'name': '人脸ID', 'db': 28, 'offset': 0},
        {'name': '物料1', 'db': 28, 'offset': 0},
        {'name': '物料2', 'db': 1000, 'offset': 12}
    ]
    mapping = {
        1: (1, 2),
        2: (1, 1),
        3: (2, 1)
    }

# ========== 加载已知人脸 ==========
known_face_encodings = []
known_face_names = []

if not os.path.exists('D:/Pypg'):
    os.makedirs('D:/Pypg')

for filename in os.listdir('D:/Pypg'):
    if not filename.lower().endswith(('.jpg', '.jpeg', '.png')):
        continue
    if filename == 'camera_result.jpg':
        continue

    name = os.path.splitext(filename)[0]
    if not name.isdigit():
        continue

    try:
        image = face_recognition.load_image_file(os.path.join('D:/Pypg', filename))
        encodings = face_recognition.face_encodings(image)
        if encodings:
            known_face_encodings.append(encodings[0])
            known_face_names.append(int(name))
    except Exception as e:
        print(f"  ❌ 错误: {e}")

print(f"\n📊 共加载 {len(known_face_names)} 个人脸: {sorted(known_face_names)}\n")

# ========== PLC连接 ==========
plc_client = None
plc_connected = False
try:
    plc_client = snap7.client.Client()
    plc_client.connect(PLC_MAIN['ip'], PLC_MAIN['rack'], PLC_MAIN['slot'])
    if plc_client.get_connected():
        plc_connected = True
        print(f"  ✅ 主PLC连接成功！")
    else:
        print("  ❌ 主PLC连接失败")
except Exception as e:
    logger.error(f"连接主PLC时发生异常: {e}")
    print(f"  ❌ 连接错误: {e}")

io_client = None
io_connected = False
try:
    io_client = snap7.client.Client()
    io_client.connect(PLC_IO['ip'], PLC_IO['rack'], PLC_IO['slot'])
    if io_client.get_connected():
        io_connected = True
        print(f"  ✅ IO监控PLC连接成功！")
    else:
        print("  ❌ IO监控PLC连接失败")
except Exception as e:
    logger.error(f"连接IO监控PLC时发生异常: {e}")
    print(f"  ❌ 连接错误: {e}")

# ========== PLC 读写辅助函数 ==========
def write_int_to_plc(db_number, start_byte, value, retry=3):
    if not plc_connected or not plc_client:
        return False
    for attempt in range(retry):
        try:
            with plc_lock:
                data = bytearray([(value >> 8) & 0xFF, value & 0xFF])
                plc_client.db_write(db_number, start_byte, data)
            logger.debug(f"写入成功: DB{db_number}.DBW{start_byte} = {value}")
            return True
        except Exception as e:
            logger.error(f"写入失败 DB{db_number}.DBW{start_byte}: {e}")
            if attempt < retry-1:
                time.sleep(PLC_RETRY_DELAY)
                continue
    return False

def read_int_from_plc(db_number, start_byte, retry=2):
    if not plc_connected or not plc_client:
        return 0
    for attempt in range(retry):
        try:
            with plc_lock:
                data = plc_client.db_read(db_number, start_byte, 2)
            value = (data[0] << 8) | data[1]
            return value
        except Exception as e:
            logger.error(f"读取失败 DB{db_number}.DBW{start_byte}: {e}")
            if attempt < retry-1:
                time.sleep(PLC_RETRY_DELAY)
                continue
    return 0

def read_real_from_plc(db_number, start_byte, retry=2):
    if not plc_connected or not plc_client:
        return 0.0
    for attempt in range(retry):
        try:
            with plc_lock:
                data = plc_client.db_read(db_number, start_byte, 4)
            value = struct.unpack('>f', bytes(data))[0]
            return value
        except Exception as e:
            logger.error(f"读取失败 DB{db_number}.DBD{start_byte}: {e}")
            if attempt < retry-1:
                time.sleep(PLC_RETRY_DELAY)
                continue
    return 0.0

def read_dint_from_plc(db_number, start_byte, retry=2):
    if not plc_connected or not plc_client:
        return 0
    for attempt in range(retry):
        try:
            with plc_lock:
                data = plc_client.db_read(db_number, start_byte, 4)
            value = struct.unpack('>i', bytes(data))[0]
            return value
        except Exception as e:
            logger.error(f"读取失败 DB{db_number}.DBD{start_byte}: {e}")
            if attempt < retry-1:
                time.sleep(PLC_RETRY_DELAY)
                continue
    return 0

def read_bool_from_plc(db_number, start_byte, bit_position, retry=2):
    if not plc_connected or not plc_client:
        return False
    for attempt in range(retry):
        try:
            with plc_lock:
                data = plc_client.db_read(db_number, start_byte, 1)
            mask = 1 << bit_position
            return bool(data[0] & mask)
        except Exception as e:
            logger.error(f"读取失败 DB{db_number}.DBX{start_byte}.{bit_position}: {e}")
            if attempt < retry-1:
                time.sleep(PLC_RETRY_DELAY)
                continue
    return False

def write_bool_to_plc(db_number, start_byte, bit_position, value, retry=3):
    if not plc_connected or not plc_client:
        return False
    for attempt in range(retry):
        try:
            with plc_lock:
                current_byte = plc_client.db_read(db_number, start_byte, 1)[0]
                if value:
                    current_byte |= (1 << bit_position)
                else:
                    current_byte &= ~(1 << bit_position)
                plc_client.db_write(db_number, start_byte, bytearray([current_byte]))
            logger.debug(f"写入成功: DB{db_number}.DBX{start_byte}.{bit_position} = {value}")
            return True
        except Exception as e:
            logger.error(f"写入失败 DB{db_number}.DBX{start_byte}.{bit_position}: {e}")
            if attempt < retry-1:
                time.sleep(PLC_RETRY_DELAY)
                continue
    return False

def read_input_byte(byte_offset):
    if not io_connected or not io_client:
        return 0
    try:
        data = io_client.read_area(snap7.types.Areas.PE, 0, byte_offset, 1)
        if data:
            return data[0]
    except Exception as e:
        logger.error(f"读取输入字节错误: {e}")
    return 0

# ========== 音频播放函数 ==========
def play_audio_for_face(face_id):
    global playing_audio
    audio_file = f"C:/Users/A1498/Desktop/YL-Mes/m4a/{face_id:02d}.mp3"
    if not os.path.exists(audio_file):
        logger.warning(f"音频文件不存在: {audio_file}")
        with audio_lock:
            playing_audio = False
        return
    try:
        sound = pygame.mixer.Sound(audio_file)
        sound.set_volume(1.0)
        sound.play()
        start_time = time.time()
        while pygame.mixer.get_busy() and (time.time() - start_time) < 10:
            time.sleep(0.1)
        logger.info(f"音频播放完成: {audio_file}")
    except Exception as e:
        logger.error(f"播放音频失败: {e}")
    finally:
        with audio_lock:
            playing_audio = False

def check_trigger_condition():
    global last_trigger_time, playing_audio, last_input_byte, current_face_id
    db6_value = read_int_from_plc(6, 0)
    if db6_value <= 25:
        return
    current_byte = read_input_byte(45)
    rising_edge_mask = current_byte & (~last_input_byte) & 0xFF
    if rising_edge_mask == 0:
        last_input_byte = current_byte
        return
    with audio_lock:
        current_time = time.time()
        if current_time - last_trigger_time < AUDIO_COOLDOWN_SECONDS:
            last_input_byte = current_byte
            return
        if playing_audio:
            last_input_byte = current_byte
            return
        playing_audio = True
        last_trigger_time = current_time
        last_input_byte = current_byte
        current_fid = current_face_id
    if current_fid == 0:
        with audio_lock:
            playing_audio = False
        return
    def delayed_play():
        time.sleep(1.0)
        play_audio_for_face(current_fid)
    threading.Thread(target=delayed_play, daemon=True).start()

# ========== 语音合成（TTS）==========
tts_engine = None

def init_tts():
    global tts_engine
    try:
        tts_engine = pyttsx3.init()
        # 选择中文语音（Windows 下 Huihui 是中文女声）
        voices = tts_engine.getProperty('voices')
        for voice in voices:
            if 'Chinese' in voice.name or 'zh-CN' in str(voice.languages):
                tts_engine.setProperty('voice', voice.id)
                logger.info(f"已选择中文语音: {voice.name}")
                break
        tts_engine.setProperty('rate', 170)
        tts_engine.setProperty('volume', 0.9)
        logger.info("TTS 引擎初始化成功")
    except Exception as e:
        logger.error(f"TTS 引擎初始化失败: {e}")

def speak_text(text):
    """异步播报文本"""
    if tts_engine is None:
        return
    def _speak():
        try:
            tts_engine.say(text)
            tts_engine.runAndWait()
            logger.info(f"语音播报: {text}")
        except Exception as e:
            logger.error(f"语音播报失败: {e}")
    threading.Thread(target=_speak, daemon=True).start()

# ========== 辅助函数 ==========
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

def select_camera():
    for idx in [1, 0, 2, 3, 4]:
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if cap.isOpened():
            ret, frame = cap.read()
            if ret and frame is not None:
                cap.release()
                print(f"✅ 选择摄像头索引 {idx}")
                return idx
            cap.release()
    print("❌ 没有找到可用的摄像头")
    return None

def location_to_letter(location_num):
    """将库位数字转换为字母（1->A, 2->B, ..., 9->I）"""
    if 1 <= location_num <= 9:
        return chr(ord('A') + location_num - 1)
    return str(location_num)

# ========== 摄像头 ==========
camera_index = select_camera()
if camera_index is None:
    exit(1)

camera = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
camera.set(cv2.CAP_PROP_FPS, 30)
camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

# ========== 全局状态 ==========
current_frame = None
current_face_id = 0
current_material1 = 0
current_material2 = 0
plc_current_values = [0] * len(PLC_TARGETS)
plc_read_data = {
    'barcode': '',
    'rfid1': [],
    'rfid2': [],
    'mes_order': {},
    'smart_meter': {},
    'environment': {},
    'storage_status': {}
}
frame_lock = threading.Lock()
last_input_byte = 0

# ========== PLC读取配置 ==========
PLC_READ_CONFIG = {
    'DB11': {
        # 条码字符数组（6个字节，DB11.DBB0~5）
        'barcode_chars': {'type': 'bytes', 'offset': 0, 'length': 6}
    },
    'DB9': {
        # RFID1: 偏移0~14 (8个int)
        'rfid1_1': {'type': 'int', 'offset': 0},
        'rfid1_2': {'type': 'int', 'offset': 2},
        'rfid1_3': {'type': 'int', 'offset': 4},
        'rfid1_4': {'type': 'int', 'offset': 6},
        'rfid1_5': {'type': 'int', 'offset': 8},
        'rfid1_6': {'type': 'int', 'offset': 10},
        'rfid1_7': {'type': 'int', 'offset': 12},
        'rfid1_8': {'type': 'int', 'offset': 14},
        # RFID2: 偏移32~46 (8个int)
        'rfid2_1': {'type': 'int', 'offset': 32},
        'rfid2_2': {'type': 'int', 'offset': 34},
        'rfid2_3': {'type': 'int', 'offset': 36},
        'rfid2_4': {'type': 'int', 'offset': 38},
        'rfid2_5': {'type': 'int', 'offset': 40},
        'rfid2_6': {'type': 'int', 'offset': 42},
        'rfid2_7': {'type': 'int', 'offset': 44},
        'rfid2_8': {'type': 'int', 'offset': 46},
    },
    'DB1000': {
        # ... 原有配置保持不变 ...
        'order_no': {'type': 'int', 'offset': 0},
        'prod_qty': {'type': 'int', 'offset': 2},
        'qualified_qty': {'type': 'int', 'offset': 4},
        'unqualified_qty': {'type': 'int', 'offset': 6},
        'location': {'type': 'int', 'offset': 8},
        'large_balls': {'type': 'int', 'offset': 10},
        'small_balls': {'type': 'int', 'offset': 12},
        'order_qty': {'type': 'int', 'offset': 14},
        'product_qualified': {'type': 'int', 'offset': 16},
        'product_unqualified': {'type': 'int', 'offset': 18},
        # 订单数据备用数组 (3个DInt)
        'order_reserve_1': {'type': 'dint', 'offset': 20},
        'order_reserve_2': {'type': 'dint', 'offset': 24},
        'order_reserve_3': {'type': 'dint', 'offset': 28},
        # 设备控制位
        'device_start': {'type': 'bool', 'offset': 32, 'bit': 0},
        'device_reset': {'type': 'bool', 'offset': 32, 'bit': 1},
        'device_stop': {'type': 'bool', 'offset': 32, 'bit': 2},
        # 智能电表数据
        'a_voltage': {'type': 'real', 'offset': 74},
        'a_current': {'type': 'real', 'offset': 78},
        'a_active_power': {'type': 'real', 'offset': 82},
        'a_reactive_power': {'type': 'real', 'offset': 86},
        'a_apparent_power': {'type': 'real', 'offset': 90},
        'a_power_factor': {'type': 'real', 'offset': 94},
        'frequency': {'type': 'real', 'offset': 98},
        'pos_active_energy': {'type': 'dint', 'offset': 102},
        'neg_active_energy': {'type': 'dint', 'offset': 106},
        'pos_reactive_energy': {'type': 'dint', 'offset': 110},
        'neg_reactive_energy': {'type': 'dint', 'offset': 114},
        'voltage_alarm': {'type': 'bool', 'offset': 118, 'bit': 0},
        'current_alarm': {'type': 'bool', 'offset': 118, 'bit': 1},
        # 环境及产线变量数据
        'humidity': {'type': 'real', 'offset': 160},
        'temperature': {'type': 'real', 'offset': 164},
        'pressure': {'type': 'real', 'offset': 168},
        'co2': {'type': 'int', 'offset': 172},
        'noise': {'type': 'real', 'offset': 174},
        'air_pressure': {'type': 'real', 'offset': 178},
        'vibration_sum': {'type': 'real', 'offset': 182},
        'vibration_z': {'type': 'real', 'offset': 186},
        'vibration_y': {'type': 'real', 'offset': 190},
        'vibration_x': {'type': 'real', 'offset': 194},
        # 料盒供料输送单元
        'feed_status': {'type': 'int', 'offset': 238},
        'feed_sequence': {'type': 'int', 'offset': 240},
        'feed_encoder': {'type': 'real', 'offset': 242},
        'feed_weight': {'type': 'real', 'offset': 246},
        # RFID1 结构体 (先取前4个整数)
        'rfid1_data1': {'type': 'int', 'offset': 250},
        'rfid1_data2': {'type': 'int', 'offset': 252},
        'rfid1_data3': {'type': 'int', 'offset': 254},
        'rfid1_data4': {'type': 'int', 'offset': 256},
        # RFID2 结构体
        'rfid2_data1': {'type': 'int', 'offset': 278},
        'rfid2_data2': {'type': 'int', 'offset': 280},
        'rfid2_data3': {'type': 'int', 'offset': 282},
        'rfid2_data4': {'type': 'int', 'offset': 284},
        # 转盘料芯供料单元
        'turntable_status': {'type': 'int', 'offset': 346},
        'turntable_sequence': {'type': 'int', 'offset': 348},
        'turntable_angle': {'type': 'real', 'offset': 350},
        'turntable_speed': {'type': 'real', 'offset': 354},
        # 模拟量高度检测单元
        'height_status': {'type': 'int', 'offset': 398},
        'height_sequence': {'type': 'int', 'offset': 400},
        'height_value': {'type': 'real', 'offset': 402},
        # 大小钢珠装配单元
        'assembly_status': {'type': 'int', 'offset': 446},
        'assembly_sequence': {'type': 'int', 'offset': 448},
        'small_ball_drop': {'type': 'int', 'offset': 450},
        'large_ball_drop': {'type': 'int', 'offset': 452},
        # 分拣检测单元
        'sort_status': {'type': 'int', 'offset': 494},
        'sort_sequence': {'type': 'int', 'offset': 496},
        'vision_qualified': {'type': 'int', 'offset': 498},
        'scan_info': {'type': 'str', 'offset': 500, 'length': 256},
        # 料盒搬运料盖装配单元
        'cover_status': {'type': 'int', 'offset': 796},
        'cover_sequence': {'type': 'int', 'offset': 798},
        # 伺服搬运单元
        'servo_status': {'type': 'int', 'offset': 840},
        'servo_sequence': {'type': 'int', 'offset': 842},
        'servo_pos': {'type': 'real', 'offset': 844},
        'servo_speed': {'type': 'real', 'offset': 848},
        # 龙门2轴搬运单元
        'gantry_status': {'type': 'int', 'offset': 892},
        'gantry_sequence': {'type': 'int', 'offset': 894},
        'gantry_x_pos': {'type': 'real', 'offset': 896},
        'gantry_x_speed': {'type': 'real', 'offset': 900},
        'gantry_y_pos': {'type': 'real', 'offset': 904},
        'gantry_y_speed': {'type': 'real', 'offset': 908},
        # 智能仓储单元
        'storage_location': {'type': 'int', 'offset': 952},
        'location_a_detect': {'type': 'bool', 'offset': 954, 'bit': 0},
        'location_b_detect': {'type': 'bool', 'offset': 954, 'bit': 1},
        'location_c_detect': {'type': 'bool', 'offset': 954, 'bit': 2},
        'location_d_detect': {'type': 'bool', 'offset': 954, 'bit': 3},
        'location_e_detect': {'type': 'bool', 'offset': 954, 'bit': 4},
        'location_f_detect': {'type': 'bool', 'offset': 954, 'bit': 5},
        'location_g_detect': {'type': 'bool', 'offset': 954, 'bit': 6},
        'location_h_detect': {'type': 'bool', 'offset': 954, 'bit': 7},
        'location_i_detect': {'type': 'bool', 'offset': 955, 'bit': 0},
    }
}

def read_plc_data():
    global plc_read_data
    new_data = {}
    
    # 读取条码字符数组（DB11.DBB0~5）
    try:
        with plc_lock:
            raw = plc_client.db_read(11, 0, 6)
        barcode = ''.join(chr(b) for b in raw if 32 <= b <= 126)
        new_data['barcode'] = barcode.strip()
    except Exception as e:
        logger.error(f"读取条码失败: {e}")
        new_data['barcode'] = ''
    
    # 读取 RFID1 (DB9.DBW0~14)
    rfid1 = []
    for offset in range(0, 16, 2):
        try:
            val = read_int_from_plc(9, offset)
            rfid1.append(val)
        except:
            rfid1.append(0)
    new_data['rfid1'] = rfid1
    
    # 读取 RFID2 (DB9.DBW32~46)
    rfid2 = []
    for offset in range(32, 48, 2):
        try:
            val = read_int_from_plc(9, offset)
            rfid2.append(val)
        except:
            rfid2.append(0)
    new_data['rfid2'] = rfid2
    
    # 读取 DB1000 所有字段
    for key, cfg in PLC_READ_CONFIG['DB1000'].items():
        try:
            if cfg['type'] == 'int':
                new_data[key] = read_int_from_plc(1000, cfg['offset'])
            elif cfg['type'] == 'real':
                new_data[key] = round(read_real_from_plc(1000, cfg['offset']), 2)
            elif cfg['type'] == 'dint':
                new_data[key] = read_dint_from_plc(1000, cfg['offset'])
            elif cfg['type'] == 'bool':
                new_data[key] = read_bool_from_plc(1000, cfg['offset'], cfg['bit'])
            elif cfg['type'] == 'str':
                length = cfg.get('length', 100)
                with plc_lock:
                    raw = plc_client.db_read(1000, cfg['offset'], length)
                raw = raw.split(b'\x00')[0]
                s = raw.decode('ascii', errors='ignore')
                new_data[key] = ''.join(c for c in s if 32 <= ord(c) <= 126)
        except:
            if cfg['type'] in ('int','dint'):
                new_data[key] = 0
            elif cfg['type'] == 'real':
                new_data[key] = 0.0
            elif cfg['type'] == 'str':
                new_data[key] = ''
            else:
                new_data[key] = False
    
    # 分组整理
    new_data['mes_order'] = {
        'order_no': new_data.get('order_no', 0),
        'prod_qty': new_data.get('prod_qty', 0),
        'qualified_qty': new_data.get('qualified_qty', 0),
        'unqualified_qty': new_data.get('unqualified_qty', 0),
        'location': new_data.get('location', 0),
        'large_balls': new_data.get('large_balls', 0),
        'small_balls': new_data.get('small_balls', 0),
        'order_qty': new_data.get('order_qty', 0),
        'product_qualified': new_data.get('product_qualified', 0),
        'product_unqualified': new_data.get('product_unqualified', 0),
        'reserve': [new_data.get('order_reserve_1', 0),
                    new_data.get('order_reserve_2', 0),
                    new_data.get('order_reserve_3', 0)]
    }
    
    new_data['smart_meter'] = {
        'voltage': new_data.get('a_voltage', 0.0),
        'current': new_data.get('a_current', 0.0),
        'active_power': new_data.get('a_active_power', 0.0),
        'reactive_power': new_data.get('a_reactive_power', 0.0),
        'apparent_power': new_data.get('a_apparent_power', 0.0),
        'power_factor': new_data.get('a_power_factor', 0.0),
        'frequency': new_data.get('frequency', 0.0),
        'energy_pos': new_data.get('pos_active_energy', 0),
        'energy_neg': new_data.get('neg_active_energy', 0),
        'reactive_pos': new_data.get('pos_reactive_energy', 0),
        'reactive_neg': new_data.get('neg_reactive_energy', 0),
        'voltage_alarm': new_data.get('voltage_alarm', False),
        'current_alarm': new_data.get('current_alarm', False),
    }
    
    new_data['environment'] = {
        'temp': new_data.get('temperature', 0.0),
        'humidity': new_data.get('humidity', 0.0),
        'pressure': new_data.get('pressure', 0.0),
        'noise': new_data.get('noise', 0.0),
        'co2': new_data.get('co2', 0),
        'air_pressure': new_data.get('air_pressure', 0.0),
        'vibration_sum': new_data.get('vibration_sum', 0.0),
        'vibration_x': new_data.get('vibration_x', 0.0),
        'vibration_y': new_data.get('vibration_y', 0.0),
        'vibration_z': new_data.get('vibration_z', 0.0),
    }
    
    new_data['device_controls'] = {
        'start': new_data.get('device_start', False),
        'reset': new_data.get('device_reset', False),
        'stop': new_data.get('device_stop', False),
    }
    
    new_data['units_status'] = {
        'feed': {
            'status': new_data.get('feed_status', 0),
            'sequence': new_data.get('feed_sequence', 0),
            'encoder': new_data.get('feed_encoder', 0.0),
            'weight': new_data.get('feed_weight', 0.0),
        },
        'turntable': {
            'status': new_data.get('turntable_status', 0),
            'sequence': new_data.get('turntable_sequence', 0),
            'angle': new_data.get('turntable_angle', 0.0),
            'speed': new_data.get('turntable_speed', 0.0),
        },
        'height': {
            'status': new_data.get('height_status', 0),
            'sequence': new_data.get('height_sequence', 0),
            'value': new_data.get('height_value', 0.0),
        },
        'assembly': {
            'status': new_data.get('assembly_status', 0),
            'sequence': new_data.get('assembly_sequence', 0),
            'small_drop': new_data.get('small_ball_drop', 0),
            'large_drop': new_data.get('large_ball_drop', 0),
        },
        'sorting': {
            'status': new_data.get('sort_status', 0),
            'sequence': new_data.get('sort_sequence', 0),
            'vision_qualified': new_data.get('vision_qualified', 0),
            'scan_info': new_data.get('scan_info', ''),
        },
        'cover': {
            'status': new_data.get('cover_status', 0),
            'sequence': new_data.get('cover_sequence', 0),
        },
        'servo': {
            'status': new_data.get('servo_status', 0),
            'sequence': new_data.get('servo_sequence', 0),
            'position': new_data.get('servo_pos', 0.0),
            'speed': new_data.get('servo_speed', 0.0),
        },
        'gantry': {
            'status': new_data.get('gantry_status', 0),
            'sequence': new_data.get('gantry_sequence', 0),
            'x_pos': new_data.get('gantry_x_pos', 0.0),
            'x_speed': new_data.get('gantry_x_speed', 0.0),
            'y_pos': new_data.get('gantry_y_pos', 0.0),
            'y_speed': new_data.get('gantry_y_speed', 0.0),
        },
    }
    
    det = {}
    for k in ['location_a_detect','location_b_detect','location_c_detect','location_d_detect',
              'location_e_detect','location_f_detect','location_g_detect','location_h_detect','location_i_detect']:
        det[k[-1].upper()] = new_data.get(k, False)
    new_data['storage_status'] = {
        'current_location': new_data.get('storage_location', 0),
        'detections': det
    }
    
    plc_read_data = new_data

# ========== 订单保存辅助函数 ==========
def _save_orders():
    try:
        with open(ORDER_FILE, 'w', encoding='utf-8') as f:
            json.dump(orders, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存订单失败: {e}")

def load_orders():
    global orders, order_counter
    if os.path.exists(ORDER_FILE):
        try:
            with open(ORDER_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                orders = loaded
                if orders:
                    max_no = max(o['order_no'] for o in orders)
                    order_counter = max_no + 1
                logger.info(f"已加载 {len(orders)} 个历史订单，下一个订单号: {order_counter}")
        except Exception as e:
            logger.error(f"加载订单文件失败: {e}")

load_orders()

def create_order(face_id, mat1, mat2, location, status=ORDER_STATUS_PENDING):
    global order_counter
    logger.info(f"尝试创建订单，人脸ID: {face_id}, 物料: {mat1},{mat2}, 库位: {location}")
    with orders_lock:
        order = {
            'order_no': order_counter,
            'face_id': face_id,
            'location': location,
            'large_balls': mat1,
            'small_balls': mat2,
            'order_qty': 0,
            'status': status,
            'barcode': '',
            'rfid1': [],
            'rfid2': [],
            'create_time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'complete_time': '',
            'create_timestamp': time.time()
        }
        orders.append(order)
        order_counter += 1
        _save_orders()
        socketio.emit('orders_update', orders)
        logger.info(f"✅ 新订单创建成功: {order['order_no']}，状态: {status}")
        return order

def increment_order_qty():
    if not plc_connected:
        logger.error("PLC未连接，无法增加下单数量")
        return 0
    current = read_int_from_plc(1000, 14)
    new_qty = current + 1
    if write_int_to_plc(1000, 14, new_qty):
        logger.info(f"下单数量已更新: {new_qty}")
        return new_qty
    else:
        logger.error("下单数量写入失败")
        return current

def send_order_material_to_plc(order):
    global last_qualified_count
    if not plc_connected:
        logger.error("PLC未连接，无法发送订单")
        return False

    face_id = order['face_id']
    mat1 = order['large_balls']
    mat2 = order['small_balls']
    order_no = order['order_no']

    current_prod = read_int_from_plc(1000, 2)
    new_location = current_prod + 1

    success = True
    for target in PLC_TARGETS:
        value = 0
        if target['name'] == '人脸ID':
            value = face_id
        elif target['name'] == '物料1':
            value = mat1
        elif target['name'] == '物料2':
            value = mat2
        else:
            continue
        if not write_int_to_plc(target['db'], target['offset'], value, retry=3):
            success = False
            logger.error(f"写入{target['name']}失败")
            break
    if not success:
        return False

    if not write_int_to_plc(1000, 0, order_no, retry=3):
        logger.error("写入订单编号失败")
        return False

    if not write_int_to_plc(1000, 8, new_location, retry=3):
        logger.error("写入库位失败")
        return False

    new_prod_qty = current_prod + 1
    if not write_int_to_plc(1000, 2, new_prod_qty, retry=3):
        logger.error("写入产品数量失败")
        return False
    logger.info(f"产品数量已更新: {current_prod} -> {new_prod_qty}, 库位: {new_location}")

    if not write_bool_to_plc(1000, 32, 0, True, retry=3):
        logger.warning("启动信号置位失败")
    time.sleep(1)
    write_bool_to_plc(1000, 32, 0, False, retry=3)

    with orders_lock:
        order['location'] = new_location
        order['status'] = ORDER_STATUS_ACTIVE
        order['start_time'] = time.time()
        _save_orders()
        socketio.emit('orders_update', orders)

    with qualified_lock:
        last_qualified_count = read_int_from_plc(1000, 4)

    logger.info(f"订单 {order_no} 已发送到PLC，库位 {new_location} ({location_to_letter(new_location)})，产品数量 {new_prod_qty}")
    return True

def complete_active_order_and_start_next():
    completed_order = None
    next_order = None

    with orders_lock:
        for order in orders:
            if order['status'] == ORDER_STATUS_ACTIVE:
                # 记录完成时的条码和RFID
                order['barcode'] = plc_read_data.get('barcode', '')
                order['rfid1'] = plc_read_data.get('rfid1', [])
                order['rfid2'] = plc_read_data.get('rfid2', [])
                order['status'] = ORDER_STATUS_COMPLETED
                order['complete_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
                completed_order = order
                logger.info(f"订单 {order['order_no']} 标记为已完成，条码: {order['barcode']}, RFID1: {order['rfid1']}, RFID2: {order['rfid2']}")
                break

        for order in orders:
            if order['status'] == ORDER_STATUS_PENDING:
                next_order = order
                break

        _save_orders()
        socketio.emit('orders_update', orders)

    if completed_order:
        logger.info(f"准备播放订单完成音频，人脸ID: {completed_order['face_id']}")
        threading.Thread(
            target=play_audio_for_face,
            args=(completed_order['face_id'],),
            daemon=True
        ).start()

        # 语音播报取药提示
        patient_id = completed_order['face_id']
        location_num = completed_order.get('location', 0)
        location_letter = location_to_letter(location_num)
        message = f"请 {patient_id} 号患者到 {location_letter} 号库位取药"
        speak_text(message)
        logger.info(f"语音播报: {message}")

    if next_order:
        logger.info(f"自动发送下一个订单物料: {next_order['order_no']}")
        try:
            send_order_material_to_plc(next_order)
        except Exception as e:
            logger.error(f"发送下一个订单 {next_order['order_no']} 失败: {e}")
    else:
        logger.info("没有待处理订单，等待新订单创建")

# ========== Flask & SocketIO ==========
app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# 人脸端页面（摄像头 + 按钮）保持不变
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>人脸识别动态PLC控制</title>
    <script src="https://cdn.socket.io/4.5.0/socket.io.min.js"></script>
    <style>
        body { margin: 0; padding: 0; background: #1a1a1a; font-family: 'Microsoft YaHei'; }
        .container { position: relative; width: 100vw; height: 100vh; overflow: hidden; }
        #videoFeed { width: 100%; height: 100%; object-fit: contain; background: black; }
        .overlay { 
            position: absolute; 
            bottom: 30px; 
            right: 30px; 
            z-index: 100; 
            display: flex;
            flex-direction: column;
            align-items: flex-end;
        }
        .info-panel {
            position: absolute; top: 20px; left: 20px;
            background: rgba(0,0,0,0.7); color: white;
            padding: 20px; border-radius: 10px;
            min-width: 300px;
        }
        .info-item { margin: 8px 0; }
        .plc-value { font-size: 20px; font-weight: bold; color: #ff9800; }
        .material-value { font-size: 24px; font-weight: bold; color: #4caf50; }
        .btn {
            width: 250px;
            margin-bottom: 10px;
            padding: 12px 20px;
            font-size: 16px;
            font-weight: bold;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.3s;
            box-sizing: border-box;
        }
        .btn-send-zeros {
            background: linear-gradient(135deg, #9E9E9E, #616161);
            color: white;
        }
        .btn-send-data {
            background: linear-gradient(135deg, #667eea, #764ba2);
            color: white;
        }
        .btn-self-check {
            background: linear-gradient(135deg, #ff9800, #f57c00);
            color: white;
        }
        .btn:hover:not(:disabled) {
            transform: translateY(-2px);
            box-shadow: 0 6px 15px rgba(102,126,234,0.5);
        }
        .btn:disabled { 
            opacity: 0.5; 
            cursor: not-allowed; 
        }
        .status-connected { color: #4caf50; }
        .status-disconnected { color: #f44336; }
        .face-id { font-size: 32px; font-weight: bold; color: #4caf50; }
        table { width: 100%; margin-top: 10px; border-collapse: collapse; }
        td { padding: 5px; border-bottom: 1px solid #444; }
        .label { color: #aaa; }
        .self-check-status {
            margin-top: 10px;
            font-size: 14px;
            color: #ff9800;
        }
    </style>
</head>
<body>
    <div class="container">
        <img id="videoFeed" src="/video_feed" alt="视频流">
        <div class="info-panel">
            <div class="info-item">当前人脸ID: <span id="faceId" class="face-id">0</span></div>
            <div class="info-item">PLC状态: <span id="plcStatus">检查中...</span></div>
                <table>
                    <tr><td class="label">物料1(待发送):</td><td><span id="mat1" class="material-value">0</span></td></tr>
                    <tr><td class="label">物料2(待发送):</td><td><span id="mat2" class="material-value">0</span></td></tr>
                    <tr><td class="label" id="addr0_label">地址0:</td><td><span id="plc0" class="plc-value">0</span></td></tr>
                    <tr><td class="label" id="addr1_label">地址1:</td><td><span id="plc1" class="plc-value">0</span></td></tr>
                    <tr><td class="label" id="addr2_label">地址2:</td><td><span id="plc2" class="plc-value">0</span></td></tr>
                </table>
            <div class="self-check-status" id="selfCheckStatus">自检状态: 未进行</div>
        </div>
        <div class="overlay">
            <button class="btn btn-self-check" id="selfCheckBtn" onclick="selfCheck()">🔧 设备自检</button>
            <button class="btn btn-send-zeros" id="sendZerosBtn" onclick="sendZerosToPLC()">🔄 发送零值到PLC</button>
            <button class="btn btn-send-data" id="sendBtn" onclick="sendToPLC()" disabled>📤 发送人脸&物料到PLC</button>
        </div>
    </div>
    <script>
        const host = window.location.hostname;
        const port = window.location.port;
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const socket = io(protocol + '//' + host + ':' + port);

        const faceIdSpan = document.getElementById('faceId');
        const plcStatusSpan = document.getElementById('plcStatus');
        const mat1Span = document.getElementById('mat1');
        const mat2Span = document.getElementById('mat2');
        const plcValueSpans = [document.getElementById('plc0'), document.getElementById('plc1'), document.getElementById('plc2')];
        const addrLabels = [document.getElementById('addr0_label'), document.getElementById('addr1_label'), document.getElementById('addr2_label')];
        const sendBtn = document.getElementById('sendBtn');
        const sendZerosBtn = document.getElementById('sendZerosBtn');
        const selfCheckBtn = document.getElementById('selfCheckBtn');
        const selfCheckStatus = document.getElementById('selfCheckStatus');

        socket.on('connect', () => console.log('WebSocket connected'));

        socket.on('update', (data) => {
            faceIdSpan.textContent = data.face_id;
            mat1Span.textContent = data.mat1;
            mat2Span.textContent = data.mat2;
            
            data.plc_addresses.forEach((addr, index) => {
                if (addrLabels[index]) {
                    addrLabels[index].textContent = `${addr}当前值:`;
                }
            });
            
            data.plc_current_values.forEach((value, index) => {
                if (plcValueSpans[index]) {
                    plcValueSpans[index].textContent = value;
                }
            });
            
            if (data.plc_connected) {
                plcStatusSpan.innerHTML = '<span class="status-connected">✅ 已连接</span>';
            } else {
                plcStatusSpan.innerHTML = '<span class="status-disconnected">❌ 未连接</span>';
            }
            sendZerosBtn.disabled = !data.plc_connected;
        });

        function selfCheck() {
            selfCheckBtn.disabled = true;
            selfCheckStatus.textContent = '自检状态: 进行中...';
            fetch('/self_check', { method: 'POST' })
                .then(res => res.json())
                .then(data => {
                    if (data.success) {
                        selfCheckStatus.textContent = '自检状态: ✅ 通过，可以发送订单';
                        sendBtn.disabled = false;
                    } else {
                        selfCheckStatus.textContent = '自检状态: ❌ 失败 - ' + data.message;
                        sendBtn.disabled = true;
                    }
                })
                .catch(err => {
                    selfCheckStatus.textContent = '自检状态: ❌ 请求异常';
                    console.error(err);
                })
                .finally(() => {
                    selfCheckBtn.disabled = false;
                });
        }

        function sendToPLC() {
            fetch('/send_to_plc', { method: 'POST' })
                .then(res => res.json())
                .then(data => alert(data.message))
                .catch(err => alert('发送失败: ' + err));
        }

        function sendZerosToPLC() {
            fetch('/send_zeros', { method: 'POST' })
                .then(res => res.json())
                .then(data => alert(data.message))
                .catch(err => alert('发送零值失败: ' + err));
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

# 客户端页面（显示库位字母）
CLIENT_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>客户端 - 取药提示</title>
    <script src="https://cdn.socket.io/4.5.0/socket.io.min.js"></script>
    <style>
        body {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            font-family: 'Microsoft YaHei', sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            color: white;
        }
        .container {
            text-align: center;
            background: rgba(0,0,0,0.6);
            padding: 40px;
            border-radius: 30px;
            backdrop-filter: blur(10px);
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
        }
        h1 {
            font-size: 2.5rem;
            margin-bottom: 20px;
        }
        .location {
            font-size: 8rem;
            font-weight: bold;
            margin: 30px 0;
            background: white;
            color: #667eea;
            display: inline-block;
            width: 200px;
            height: 200px;
            line-height: 200px;
            border-radius: 50%;
            box-shadow: 0 0 30px rgba(0,0,0,0.3);
        }
        .info {
            font-size: 1.2rem;
            margin-top: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🏥 智慧药房</h1>
        <div class="location" id="locationLetter">-</div>
        <div class="info">当前库位字母</div>
    </div>
    <script>
        const socket = io();
        const locationSpan = document.getElementById('locationLetter');

        socket.on('update', (data) => {
            const location = data.plc_read_data?.mes_order?.location;
            let letter = '-';
            if (location >= 1 && location <= 9) {
                letter = String.fromCharCode(64 + location);
            }
            locationSpan.innerText = letter;
        });
    </script>
</body>
</html>
"""

@app.route('/client')
def client():
    return render_template_string(CLIENT_TEMPLATE)

# 后台管理页面（修改后的完整版，包含RFID完整显示）
BACKEND_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>智慧配药系统 MES</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <script src="https://cdn.socket.io/4.5.0/socket.io.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', 'Microsoft YaHei', system-ui, sans-serif; }
        body { background: #f0f2f8; min-height: 100vh; }
        .app-container { display: flex; height: 100vh; overflow: hidden; }
        .sidebar { width: 260px; background: #fff; box-shadow: 2px 0 12px rgba(0,0,0,0.05); display: flex; flex-direction: column; z-index: 10; }
        .logo-area { padding: 24px 20px; border-bottom: 1px solid #eef2f8; margin-bottom: 20px; }
        .logo-area h2 { font-size: 20px; font-weight: 600; background: linear-gradient(135deg, #1e6fdf, #3b82f6); -webkit-background-clip: text; background-clip: text; color: transparent; }
        .nav-menu { flex: 1; padding: 0 16px; }
        .nav-item { display: flex; align-items: center; gap: 12px; padding: 12px 16px; margin-bottom: 8px; border-radius: 12px; color: #4a5568; cursor: pointer; transition: all 0.2s; font-weight: 500; }
        .nav-item i { width: 24px; font-size: 18px; }
        .nav-item:hover { background: #f1f5f9; color: #2563eb; }
        .nav-item.active { background: #eef2ff; color: #2563eb; }
        .main-content { flex: 1; overflow-y: auto; padding: 24px 28px; background: #f8fafc; }
        .status-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 28px; background: white; padding: 12px 24px; border-radius: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        .conn-status { display: flex; gap: 20px; align-items: center; }
        .badge { padding: 4px 12px; border-radius: 30px; font-size: 13px; font-weight: 500; }
        .badge.connected { background: #d1fae5; color: #065f46; }
        .badge.disconnected { background: #fee2e2; color: #991b1b; }
        .badge.plc-online { background: #dbeafe; color: #1e40af; }
        .badge.plc-offline { background: #f3f4f6; color: #4b5563; }
        .card { background: white; border-radius: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); padding: 20px 24px; margin-bottom: 24px; border: 1px solid #eef2f8; }
        .card-title { font-size: 18px; font-weight: 600; margin-bottom: 18px; display: flex; align-items: center; gap: 8px; color: #1e293b; border-left: 4px solid #3b82f6; padding-left: 12px; }
        .data-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
        .data-item { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid #f1f5f9; }
        .data-label { color: #5b6e8c; font-size: 14px; }
        .data-value { font-weight: 600; color: #0f172a; }
        .orders-table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .orders-table th { text-align: left; padding: 12px 8px; background: #f8fafc; color: #334155; font-weight: 600; border-bottom: 1px solid #e2e8f0; }
        .orders-table td { padding: 10px 8px; border-bottom: 1px solid #f0f2f5; color: #1e293b; }
        .orders-table tr:hover td { background: #fef9e3; }
        .status-badge { display: inline-block; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 500; }
        .status-active { background: #dbeafe; color: #1e40af; }
        .status-completed { background: #dcfce7; color: #166534; }
        .status-pending { background: #fff3e3; color: #b45309; }
        .status-timeout { background: #ffe4e2; color: #b91c1c; }
        .refresh-btn { background: #3b82f6; border: none; padding: 6px 16px; border-radius: 30px; color: white; font-size: 13px; font-weight: 500; cursor: pointer; transition: 0.2s; }
        .refresh-btn:hover { background: #2563eb; }
        @media (max-width: 768px) { .sidebar { width: 200px; } .main-content { padding: 16px; } .data-grid { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
<div class="app-container">
    <div class="sidebar">
        <div class="logo-area"><h2>🏭 智慧配药 MES</h2></div>
        <div class="nav-menu">
            <div class="nav-item active" data-module="dashboard"><i class="fas fa-tachometer-alt"></i><span>数据看板</span></div>
            <div class="nav-item" data-module="orders"><i class="fas fa-list-ul"></i><span>订单列表</span></div>
        </div>
    </div>
    <div class="main-content">
        <div class="status-bar">
            <div class="conn-status">
                <span class="badge" id="socketStatus">连接中...</span>
                <span class="badge" id="plcStatus">PLC状态未知</span>
            </div>
            <button class="refresh-btn" id="manualRefreshBtn">刷新订单</button>
        </div>

        <!-- 数据看板模块 -->
        <div id="dashboardModule">
            <!-- 条码/RFID -->
            <div class="card"><div class="card-title"><i class="fas fa-qrcode"></i> 条码 & RFID</div>
                <div class="data-item"><span class="data-label">条码 (DB11):</span><span id="barcode" class="data-value">-</span></div>
                <div class="data-item"><span class="data-label">RFID1原始:</span><span id="rfid1_raw" class="data-value">-</span></div>
                <div class="data-item"><span class="data-label">RFID2原始:</span><span id="rfid2_raw" class="data-value">-</span></div>
            </div>
            <!-- MES订单 -->
            <div class="card"><div class="card-title"><i class="fas fa-clipboard-list"></i> MES订单 & 生产统计</div>
                <div class="data-grid">
                    <div class="data-item"><span class="data-label">订单编号:</span><span id="order-no" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">库位:</span><span id="location" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">大钢珠数量:</span><span id="large-balls" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">小钢珠数量:</span><span id="small-balls" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">产品合格数量:</span><span id="product-qualified" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">产品不合格数量:</span><span id="product-unqualified" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">订单合格数量:</span><span id="qualified-qty" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">订单不合格数量:</span><span id="unqualified-qty" class="data-value">-</span></div>
                </div>
            </div>
            <!-- 智能电表 (完整) -->
            <div class="card"><div class="card-title"><i class="fas fa-bolt"></i> 智能电表</div>
                <div class="data-grid">
                    <div class="data-item"><span class="data-label">A相电压:</span><span id="a-voltage" class="data-value">- V</span></div>
                    <div class="data-item"><span class="data-label">A相电流:</span><span id="a-current" class="data-value">- A</span></div>
                    <div class="data-item"><span class="data-label">有功功率:</span><span id="a-active-power" class="data-value">- kW</span></div>
                    <div class="data-item"><span class="data-label">无功功率:</span><span id="a-reactive-power" class="data-value">- kVar</span></div>
                    <div class="data-item"><span class="data-label">视在功率:</span><span id="a-apparent-power" class="data-value">- kVA</span></div>
                    <div class="data-item"><span class="data-label">功率因数:</span><span id="a-power-factor" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">频率:</span><span id="frequency" class="data-value">- Hz</span></div>
                    <div class="data-item"><span class="data-label">正向有功电能:</span><span id="pos-active-energy" class="data-value">- Wh</span></div>
                    <div class="data-item"><span class="data-label">反向有功电能:</span><span id="neg-active-energy" class="data-value">- Wh</span></div>
                    <div class="data-item"><span class="data-label">正向无功电能:</span><span id="pos-reactive-energy" class="data-value">- Varh</span></div>
                    <div class="data-item"><span class="data-label">反向无功电能:</span><span id="neg-reactive-energy" class="data-value">- Varh</span></div>
                    <div class="data-item"><span class="data-label">电压异常报警:</span><span id="voltage-alarm" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">电流异常报警:</span><span id="current-alarm" class="data-value">-</span></div>
                </div>
            </div>
            <!-- 环境 & 振动 -->
            <div class="card"><div class="card-title"><i class="fas fa-temperature-high"></i> 环境 & 振动</div>
                <div class="data-grid">
                    <div class="data-item"><span class="data-label">温度:</span><span id="temperature" class="data-value">- ℃</span></div>
                    <div class="data-item"><span class="data-label">湿度:</span><span id="humidity" class="data-value">- %RH</span></div>
                    <div class="data-item"><span class="data-label">大气压力:</span><span id="pressure" class="data-value">- KPa</span></div>
                    <div class="data-item"><span class="data-label">噪声:</span><span id="noise" class="data-value">- dB</span></div>
                    <div class="data-item"><span class="data-label">CO₂:</span><span id="co2" class="data-value">- ppm</span></div>
                    <div class="data-item"><span class="data-label">设备气压:</span><span id="air-pressure" class="data-value">- MPa</span></div>
                    <div class="data-item"><span class="data-label">矢量和振动:</span><span id="vibration-sum" class="data-value">- mm/s</span></div>
                    <div class="data-item"><span class="data-label">X轴振动:</span><span id="vibration-x" class="data-value">- mm/s</span></div>
                    <div class="data-item"><span class="data-label">Y轴振动:</span><span id="vibration-y" class="data-value">- mm/s</span></div>
                    <div class="data-item"><span class="data-label">Z轴振动:</span><span id="vibration-z" class="data-value">- mm/s</span></div>
                </div>
            </div>
            <!-- 单元状态 (折叠为简洁卡片) -->
            <div class="card"><div class="card-title"><i class="fas fa-microchip"></i> 单元状态</div>
                <div class="data-grid">
                    <div class="data-item"><span class="data-label">料盒供料状态/时序:</span><span id="feed-status-seq" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">编码器/称重:</span><span id="feed-encoder-weight" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">转盘状态/角度/速度:</span><span id="turntable-data" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">高度检测状态/值:</span><span id="height-data" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">装配单元落料(小/大):</span><span id="assembly-drop" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">分拣视觉合格:</span><span id="vision-qualified" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">扫码信息:</span><span id="scan-info" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">伺服位置/速度:</span><span id="servo-pos-speed" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">龙门X/Y位置:</span><span id="gantry-pos" class="data-value">-</span></div>
                </div>
            </div>
            <!-- 仓储 -->
            <div class="card"><div class="card-title"><i class="fas fa-warehouse"></i> 智能仓储</div>
                <div class="data-grid">
                    <div class="data-item"><span class="data-label">当前库位:</span><span id="storage-location" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">库位A-I:</span><span id="storage-detections" class="data-value">-</span></div>
                </div>
            </div>
            <!-- 设备控制 -->
            <div class="card"><div class="card-title"><i class="fas fa-power-off"></i> 设备控制</div>
                <div class="data-grid">
                    <div class="data-item"><span class="data-label">启动信号:</span><span id="device-start" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">复位信号:</span><span id="device-reset" class="data-value">-</span></div>
                    <div class="data-item"><span class="data-label">停止信号:</span><span id="device-stop" class="data-value">-</span></div>
                </div>
            </div>
        </div>

        <!-- 订单列表模块 -->
        <div id="ordersModule" style="display: none;">
            <div class="card"><div class="card-title"><i class="fas fa-list"></i> 历史订单</div>
                <div style="overflow-x: auto;">
                    <table class="orders-table">
                        <thead><tr><th>订单编号</th><th>就诊号</th><th>库位</th><th>大钢珠</th><th>小钢珠</th><th>条码</th><th>RFID1数据</th><th>RFID2数据</th><th>状态</th><th>创建时间</th><th>完成时间</th></tr></thead>
                        <tbody id="ordersTableBody"><tr><td colspan="11" style="text-align:center;">暂无订单</td></tr></tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
    const socket = io();
    const socketStatusSpan = document.getElementById('socketStatus');
    const plcStatusSpan = document.getElementById('plcStatus');
    const dashboardDiv = document.getElementById('dashboardModule');
    const ordersDiv = document.getElementById('ordersModule');
    const manualRefreshBtn = document.getElementById('manualRefreshBtn');

    // 模块切换
    const navItems = document.querySelectorAll('.nav-item');
    function switchModule(module) {
        dashboardDiv.style.display = module === 'dashboard' ? 'block' : 'none';
        ordersDiv.style.display = module === 'orders' ? 'block' : 'none';
        navItems.forEach(item => {
            const mod = item.getAttribute('data-module');
            if (mod === module) item.classList.add('active');
            else item.classList.remove('active');
        });
    }
    navItems.forEach(item => {
        item.addEventListener('click', () => switchModule(item.getAttribute('data-module')));
    });
    switchModule('dashboard');

    // 订单渲染
    function renderOrders(orders) {
        const tbody = document.getElementById('ordersTableBody');
        if (!orders || orders.length === 0) { tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;">暂无订单</td></tr>'; return; }
        let html = '';
        orders.forEach(order => {
            let cls = '';
            if (order.status === '进行中') cls = 'status-active';
            else if (order.status === '已完成') cls = 'status-completed';
            else if (order.status === '待处理') cls = 'status-pending';
            else if (order.status === '超时') cls = 'status-timeout';
            const rfid1 = (order.rfid1 || []).join(',');
            const rfid2 = (order.rfid2 || []).join(',');
            html += `<tr>
                <td>${order.order_no}</td><td>${order.face_id}</td><td>${order.location}</td>
                <td>${order.large_balls}</td><td>${order.small_balls}</td>
                <td>${order.barcode || ''}</td><td>${rfid1}</td><td>${rfid2}</td>
                <td><span class="status-badge ${cls}">${order.status}</span></td>
                <td>${order.create_time}</td><td>${order.complete_time || ''}</td>
            </tr>`;
        });
        tbody.innerHTML = html;
    }
    function refreshOrders() { fetch('/api/orders').then(res => res.json()).then(orders => renderOrders(orders)).catch(err => console.error(err)); }
    manualRefreshBtn.addEventListener('click', refreshOrders);

    // Socket 事件
    socket.on('connect', () => { socketStatusSpan.textContent = '✅ 已连接'; socketStatusSpan.className = 'badge connected'; });
    socket.on('disconnect', () => { socketStatusSpan.textContent = '❌ 未连接'; socketStatusSpan.className = 'badge disconnected'; });
    socket.on('connect_error', () => { socketStatusSpan.textContent = '⚠️ 连接失败'; socketStatusSpan.className = 'badge disconnected'; });

    socket.on('update', (data) => {
        const plcData = data.plc_read_data || {};
        // 条码/RFID
        document.getElementById('barcode').innerText = plcData.barcode || 'N/A';
        const rfid1 = plcData.rfid1 || [];
        const rfid2 = plcData.rfid2 || [];
        document.getElementById('rfid1_raw').innerText = rfid1.join(',');
        document.getElementById('rfid2_raw').innerText = rfid2.join(',');

        // MES订单
        const mes = plcData.mes_order || {};
        document.getElementById('order-no').innerText = mes.order_no ?? '-';
        document.getElementById('location').innerText = mes.location ?? '-';
        document.getElementById('large-balls').innerText = mes.large_balls ?? '-';
        document.getElementById('small-balls').innerText = mes.small_balls ?? '-';
        document.getElementById('product-qualified').innerText = mes.product_qualified ?? '-';
        document.getElementById('product-unqualified').innerText = mes.product_unqualified ?? '-';
        document.getElementById('qualified-qty').innerText = mes.qualified_qty ?? '-';
        document.getElementById('unqualified-qty').innerText = mes.unqualified_qty ?? '-';

        // 智能电表
        const meter = plcData.smart_meter || {};
        document.getElementById('a-voltage').innerHTML = (meter.voltage ?? '-') + ' V';
        document.getElementById('a-current').innerHTML = (meter.current ?? '-') + ' A';
        document.getElementById('a-active-power').innerHTML = (meter.active_power ?? '-') + ' kW';
        document.getElementById('a-reactive-power').innerHTML = (meter.reactive_power ?? '-') + ' kVar';
        document.getElementById('a-apparent-power').innerHTML = (meter.apparent_power ?? '-') + ' kVA';
        document.getElementById('a-power-factor').innerHTML = (meter.power_factor ?? '-');
        document.getElementById('frequency').innerHTML = (meter.frequency ?? '-') + ' Hz';
        document.getElementById('pos-active-energy').innerHTML = (meter.energy_pos ?? '-') + ' Wh';
        document.getElementById('neg-active-energy').innerHTML = (meter.energy_neg ?? '-') + ' Wh';
        document.getElementById('pos-reactive-energy').innerHTML = (meter.reactive_pos ?? '-') + ' Varh';
        document.getElementById('neg-reactive-energy').innerHTML = (meter.reactive_neg ?? '-') + ' Varh';
        document.getElementById('voltage-alarm').innerText = meter.voltage_alarm ? '⚠️异常' : '正常';
        document.getElementById('current-alarm').innerText = meter.current_alarm ? '⚠️异常' : '正常';

        // 环境 & 振动
        const env = plcData.environment || {};
        document.getElementById('temperature').innerHTML = (env.temp ?? '-') + ' ℃';
        document.getElementById('humidity').innerHTML = (env.humidity ?? '-') + ' %RH';
        document.getElementById('pressure').innerHTML = (env.pressure ?? '-') + ' KPa';
        document.getElementById('noise').innerHTML = (env.noise ?? '-') + ' dB';
        document.getElementById('co2').innerHTML = env.co2 ?? '-';
        document.getElementById('air-pressure').innerHTML = (env.air_pressure ?? '-') + ' MPa';
        document.getElementById('vibration-sum').innerHTML = (env.vibration_sum ?? '-') + ' mm/s';
        document.getElementById('vibration-x').innerHTML = (env.vibration_x ?? '-') + ' mm/s';
        document.getElementById('vibration-y').innerHTML = (env.vibration_y ?? '-') + ' mm/s';
        document.getElementById('vibration-z').innerHTML = (env.vibration_z ?? '-') + ' mm/s';

        // 单元状态
        const units = plcData.units_status || {};
        const feed = units.feed || {};
        document.getElementById('feed-status-seq').innerText = `状态${feed.status} 时序${feed.sequence}`;
        document.getElementById('feed-encoder-weight').innerText = `编码器${feed.encoder} 称重${feed.weight}`;
        const turntable = units.turntable || {};
        document.getElementById('turntable-data').innerText = `状态${turntable.status} 角度${turntable.angle}° 速度${turntable.speed}`;
        const height = units.height || {};
        document.getElementById('height-data').innerText = `状态${height.status} 高度${height.value}mm`;
        const assembly = units.assembly || {};
        document.getElementById('assembly-drop').innerText = `小钢珠${assembly.small_drop} 大钢珠${assembly.large_drop}`;
        const sorting = units.sorting || {};
        document.getElementById('vision-qualified').innerText = sorting.vision_qualified ?? '-';
        document.getElementById('scan-info').innerText = sorting.scan_info || '-';
        const servo = units.servo || {};
        document.getElementById('servo-pos-speed').innerText = `位置${servo.position} 速度${servo.speed}`;
        const gantry = units.gantry || {};
        document.getElementById('gantry-pos').innerText = `X:${gantry.x_pos} Y:${gantry.y_pos}`;

        // 仓储
        const storage = plcData.storage_status || {};
        document.getElementById('storage-location').innerText = storage.current_location ?? '-';
        const det = storage.detections || {};
        const detStr = Object.entries(det).map(([k,v]) => `${k}:${v?'占用':'空闲'}`).join(' | ');
        document.getElementById('storage-detections').innerText = detStr;

        // 设备控制
        const ctrl = plcData.device_controls || {};
        document.getElementById('device-start').innerText = ctrl.start ? '已启动' : '未启动';
        document.getElementById('device-reset').innerText = ctrl.reset ? '复位中' : '空闲';
        document.getElementById('device-stop').innerText = ctrl.stop ? '已停止' : '运行中';

        // PLC连接状态
        const isConnected = data.plc_connected;
        plcStatusSpan.innerText = isConnected ? '✅ PLC 在线' : '❌ PLC 离线';
        plcStatusSpan.className = isConnected ? 'badge plc-online' : 'badge plc-offline';
    });

    socket.on('orders_update', (orders) => renderOrders(orders));
    refreshOrders();
</script>
</body>
</html>
"""

@app.route('/backend')
def backend():
    return render_template_string(BACKEND_TEMPLATE)

def generate_frames():
    global current_frame
    while True:
        with frame_lock:
            if current_frame is None:
                time.sleep(0.03)
                continue
            ret, jpeg = cv2.imencode('.jpg', current_frame)
            frame_bytes = jpeg.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.03)

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/self_check', methods=['POST'])
def self_check():
    global self_check_passed
    if not plc_connected:
        return jsonify(success=False, message='PLC未连接')
    
    try:
        reset_light = read_bool_from_plc(1, 72, 6)
    except Exception as e:
        logger.error(f"读取复位指示灯失败: {e}")
        return jsonify(success=False, message='读取复位指示灯失败')
    
    if not reset_light:
        logger.info("复位指示灯不亮，发送复位信号 DB1000.DBX32.1")
        if not write_bool_to_plc(1000, 32, 1, True):
            return jsonify(success=False, message='发送复位信号失败')
        time.sleep(1)
        write_bool_to_plc(1000, 32, 1, False)
        for _ in range(25):
            time.sleep(0.2)
            try:
                if read_bool_from_plc(1, 72, 6):
                    reset_light = True
                    break
            except:
                pass
        if not reset_light:
            return jsonify(success=False, message='等待复位指示灯超时')
    else:
        logger.info("复位指示灯已亮")
    
    with self_check_lock:
        self_check_passed = True
    return jsonify(success=True, message='自检通过，设备就绪')

@app.route('/send_to_plc', methods=['POST'])
def send_to_plc():
    if not plc_connected:
        return jsonify(success=False, message='PLC未连接')
    if current_face_id == 0:
        return jsonify(success=False, message='未识别到有效人脸')

    with self_check_lock:
        if not self_check_passed:
            return jsonify(success=False, message='请先进行设备自检')

    current_location = read_int_from_plc(1000, 8)

    order = create_order(current_face_id, current_material1, current_material2, current_location)

    increment_order_qty()

    with orders_lock:
        active_exists = any(o['status'] == ORDER_STATUS_ACTIVE for o in orders)

    if not active_exists:
        if send_order_material_to_plc(order):
            return jsonify(success=True, message=f'✅ 订单 {order["order_no"]} 已创建，下单数量已增加，物料已发送')
        else:
            return jsonify(success=False, message='订单已创建，下单数量已增加，但物料发送失败，将等待后续发送')
    else:
        return jsonify(success=True, message=f'✅ 订单 {order["order_no"]} 已创建，下单数量已增加，物料将自动发送')

@app.route('/send_zeros', methods=['POST'])
def send_zeros():
    if not plc_connected:
        return jsonify(success=False, message='PLC未连接')
    
    success_list = []
    fail_list = []
    for target in PLC_TARGETS:
        ok = write_int_to_plc(target['db'], target['offset'], 0)
        if ok:
            success_list.append(f"{target['name']}(DB{target['db']}.DBW{target['offset']})")
        else:
            fail_list.append(f"{target['name']}(DB{target['db']}.DBW{target['offset']})")
    
    if len(fail_list) == 0:
        msg = f'✅ 零值已全部发送到PLC。'
        return jsonify(success=True, message=msg)
    else:
        msg = f'⚠️ 部分零值写入失败: {", ".join(fail_list)}'
        return jsonify(success=False, message=msg)

# ========== API 接口 ==========
@app.route('/api/orders', methods=['GET'])
def api_orders():
    with orders_lock:
        return jsonify(orders)

# ========== SocketIO 事件处理 ==========
@socketio.on('get_orders')
def handle_get_orders():
    emit('orders_update', orders)

# ========== 为 web 文件夹提供静态文件服务（可选） ==========
@app.route('/<path:filename>')
def serve_static_html(filename):
    api_paths = ['/send_to_plc', '/send_zeros', '/video_feed', '/self_check', '/api/orders']
    if any(filename.startswith(path.lstrip('/')) for path in api_paths):
        from flask import abort
        abort(404)
    try:
        return send_from_directory('web', filename)
    except FileNotFoundError:
        from flask import abort
        abort(404)

# ========== 人脸识别线程 ==========
def recognition_thread():
    global current_frame, current_face_id, current_material1, current_material2, plc_current_values
    process_this_frame = True
    face_locations = []
    face_names = []
    last_plc_read = 0

    while True:
        ret, frame = camera.read()
        if not ret:
            time.sleep(0.1)
            continue

        small_frame = cv2.resize(frame, (0,0), fx=0.25, fy=0.25)
        rgb_small = cv2.cvtColor(small_frame, cv2.COLOR_BGR2RGB)

        if process_this_frame:
            face_locations = face_recognition.face_locations(rgb_small)
            face_encodings = face_recognition.face_encodings(small_frame, face_locations)

            face_names = []
            face_ids = []
            for encoding in face_encodings:
                if known_face_encodings:
                    distances = face_recognition.face_distance(known_face_encodings, encoding)
                    best = np.argmin(distances)
                    if distances[best] < RECOGNITION_THRESHOLD:
                        fid = known_face_names[best]
                        name = str(fid)
                        if fid in mapping:
                            current_material1, current_material2 = mapping[fid]
                        else:
                            current_material1 = current_material2 = 0
                        with face_id_lock:
                            current_face_id = fid
                    else:
                        name = "Unknown"
                        fid = 0
                else:
                    name = "Unknown"
                    fid = 0
                face_names.append(name)
                face_ids.append(fid)

            valid = [fid for fid in face_ids if fid != 0]
            if not valid:
                with face_id_lock:
                    current_face_id = 0
                current_material1 = current_material2 = 0

        process_this_frame = not process_this_frame

        for (top, right, bottom, left), name in zip(face_locations, face_names):
            top *= 4; right *= 4; bottom *= 4; left *= 4
            color = (0,255,0) if name!="Unknown" else (0,0,255)
            cv2.rectangle(frame, (left,top), (right,bottom), color, 2)
            label = f"ID:{name}" if name!="Unknown" else "Unknown"
            cv2.putText(frame, label, (left+6, bottom-6), cv2.FONT_HERSHEY_DUPLEX, 0.5, (255,255,255), 1)

        cv2.putText(frame, f"ID: {current_face_id}", (10,30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2)

        with frame_lock:
            current_frame = frame.copy()

        if time.time() - last_plc_read >= PLC_UPDATE_INTERVAL:
            for i, target in enumerate(PLC_TARGETS):
                plc_current_values[i] = read_int_from_plc(target['db'], target['offset'])
                time.sleep(0.1)
            last_plc_read = time.time()

        socketio.emit('update', {
            'face_id': current_face_id,
            'mat1': current_material1,
            'mat2': current_material2,
            'plc_current_values': plc_current_values,
            'plc_addresses': [f"DB{t['db']}.DBW{t['offset']}" for t in PLC_TARGETS],
            'plc_connected': plc_connected,
            'plc_read_data': plc_read_data
        })

# ========== IO监控线程 ==========
def io_monitor_thread():
    while True:
        if io_connected:
            try:
                check_trigger_condition()
            except Exception as e:
                logger.error(f"监控线程异常: {e}")
        time.sleep(0.05)

# ========== PLC数据读取线程 ==========
def plc_data_reader_thread():
    while True:
        if plc_connected:
            try:
                read_plc_data()
            except Exception as e:
                logger.error(f"PLC数据读取线程异常: {e}")
        time.sleep(PLC_DATA_READ_INTERVAL)

# ========== 订单完成监控线程 ==========
def monitor_order_complete():
    global last_qualified_count

    while True:
        if plc_connected:
            try:
                current_qualified = read_int_from_plc(1000, 4)
                logger.debug(f"当前合格数量: {current_qualified}, 上次记录: {last_qualified_count}")

                need_process = False
                diff = 0

                with qualified_lock:
                    if current_qualified > last_qualified_count:
                        diff = current_qualified - last_qualified_count
                        last_qualified_count = current_qualified
                        need_process = True
                        logger.info(f"合格数量增加 {diff}，当前合格数: {current_qualified}")

                if need_process:
                    for i in range(diff):
                        logger.info(f"处理第 {i+1}/{diff} 个订单完成")
                        try:
                            complete_active_order_and_start_next()
                        except Exception as e:
                            logger.error(f"处理订单完成时出错: {e}")

            except Exception as e:
                logger.error(f"订单完成监控异常: {e}")

        time.sleep(1)

# ========== 超时监控线程 ==========
def timeout_monitor():
    while True:
        if plc_connected:
            with orders_lock:
                now = time.time()
                updated = False
                for order in orders:
                    if order['status'] == ORDER_STATUS_ACTIVE:
                        create_ts = order.get('create_timestamp', 0)
                        if create_ts and (now - create_ts > 600):
                            order['status'] = ORDER_STATUS_TIMEOUT
                            logger.warning(f"订单 {order['order_no']} 超时")
                            updated = True
                if updated:
                    _save_orders()
                    socketio.emit('orders_update', orders)
        time.sleep(30)

# 初始化合格数量
if plc_connected:
    last_qualified_count = read_int_from_plc(1000, 4)

# 启动 TTS
init_tts()

# 启动线程
threading.Thread(target=recognition_thread, daemon=True).start()
threading.Thread(target=io_monitor_thread, daemon=True).start()
threading.Thread(target=plc_data_reader_thread, daemon=True).start()
threading.Thread(target=monitor_order_complete, daemon=True).start()
threading.Thread(target=timeout_monitor, daemon=True).start()

# ========== 启动Web服务 ==========
if __name__ == '__main__':
    port = 8080
    local_ip = get_local_ip()
    print(f"\n🌐 Web服务启动于 http://{local_ip}:{port}")
    print(f"   摄像头页面: http://{local_ip}:{port}/")
    print(f"   客户端页面: http://{local_ip}:{port}/client")
    print(f"   后台管理端: http://{local_ip}:{port}/backend")
    webbrowser.open(f"http://localhost:{port}")
    try:
        socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\n正在关闭...")
    finally:
        if camera:
            camera.release()
        if plc_client and plc_connected:
            plc_client.disconnect()
        if io_client and io_connected:
            io_client.disconnect()
        pygame.mixer.quit()