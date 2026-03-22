# pip install flask flask-cors pydub websocket-client
# ==========================================
# 文件名: app.py

import os
import ssl
import json
import base64
import hmac
import hashlib
import datetime
import time
import tempfile
import subprocess  # 新增：用于直接调用 ffmpeg
import _thread as thread
from urllib.parse import urlencode
from wsgiref.handlers import format_date_time
from datetime import datetime
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
# from pydub import AudioSegment  <-- 删除了这一行，不再需要它
import websocket

# ========== 1. 部署配置 ==========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 5000))
APP_ID = os.environ.get("XFYUN_APP_ID", "").strip()
API_KEY = os.environ.get("XFYUN_API_KEY", "").strip()
API_SECRET = os.environ.get("XFYUN_API_SECRET", "").strip()
ASR_MODE = os.environ.get("XFYUN_ASR_MODE", "slm").strip().lower()
ASR_LANGUAGE = os.environ.get("XFYUN_ASR_LANGUAGE", "zh_cn").strip()
ASR_ACCENT = os.environ.get("XFYUN_ASR_ACCENT", "mandarin").strip()


def has_xfyun_credentials():
    return bool(APP_ID and API_KEY and API_SECRET)


def require_xfyun_credentials():
    if has_xfyun_credentials():
        return None

    return jsonify({
        "error": "Missing XFYUN credentials",
        "message": "请在部署平台配置 XFYUN_APP_ID、XFYUN_API_KEY、XFYUN_API_SECRET 环境变量。"
    }), 500

# ========== 2. Flask 设置 ==========
app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')
CORS(app)

# ========== 3. 讯飞 ASR (语音转写) 逻辑 ==========
class WsParamASR:
    def __init__(self, APPID, APIKey, APISecret, mode="slm", language="zh_cn", accent="mandarin"):
        self.APPID = APPID
        self.APIKey = APIKey
        self.APISecret = APISecret
        self.mode = mode
        self.language = language
        self.accent = accent
        self.ws_host = "iat.xf-yun.com" if self.mode == "slm" else "iat-api.xfyun.cn"
        self.ws_path = "/v1" if self.mode == "slm" else "/v2/iat"

    def create_first_frame_payload(self, audio_base64):
        if self.mode == "slm":
            return {
                "header": {
                    "app_id": self.APPID,
                    "status": 0
                },
                "parameter": {
                    "iat": {
                        "domain": "slm",
                        "language": self.language,
                        "accent": self.accent,
                        "eos": 6000,
                        "vinfo": 1,
                        "result": {
                            "encoding": "utf8",
                            "compress": "raw",
                            "format": "json"
                        }
                    }
                },
                "payload": {
                    "audio": {
                        "encoding": "raw",
                        "sample_rate": 16000,
                        "channels": 1,
                        "bit_depth": 16,
                        "seq": 1,
                        "status": 0,
                        "audio": audio_base64
                    }
                }
            }

        return {
            "common": {"app_id": self.APPID},
            "business": {
                "domain": "iat",
                "language": self.language,
                "accent": self.accent,
                "vinfo": 1,
                "vad_eos": 10000
            },
            "data": {
                "status": 0,
                "format": "audio/L16;rate=16000",
                "audio": audio_base64,
                "encoding": "raw"
            }
        }

    def create_continue_frame_payload(self, audio_base64, seq):
        if self.mode == "slm":
            return {
                "header": {
                    "app_id": self.APPID,
                    "status": 1
                },
                "payload": {
                    "audio": {
                        "encoding": "raw",
                        "sample_rate": 16000,
                        "channels": 1,
                        "bit_depth": 16,
                        "seq": seq,
                        "status": 1,
                        "audio": audio_base64
                    }
                }
            }

        return {
            "data": {
                "status": 1,
                "format": "audio/L16;rate=16000",
                "audio": audio_base64,
                "encoding": "raw"
            }
        }

    def create_last_frame_payload(self, audio_base64, seq):
        if self.mode == "slm":
            return {
                "header": {
                    "app_id": self.APPID,
                    "status": 2
                },
                "payload": {
                    "audio": {
                        "encoding": "raw",
                        "sample_rate": 16000,
                        "channels": 1,
                        "bit_depth": 16,
                        "seq": seq,
                        "status": 2,
                        "audio": audio_base64
                    }
                }
            }

        return {
            "data": {
                "status": 2,
                "format": "audio/L16;rate=16000",
                "audio": audio_base64,
                "encoding": "raw"
            }
        }

    def create_url(self):
        url = f"wss://{self.ws_host}{self.ws_path}"
        now = datetime.now()
        date = format_date_time(time.mktime(now.timetuple()))
        signature_origin = "host: " + self.ws_host + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + self.ws_path + " HTTP/1.1"
        signature_sha = hmac.new(self.APISecret.encode('utf-8'), signature_origin.encode('utf-8'), digestmod=hashlib.sha256).digest()
        signature_sha = base64.b64encode(signature_sha).decode(encoding='utf-8')
        authorization_origin = f'api_key="{self.APIKey}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature_sha}"'
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')
        v = {"authorization": authorization, "date": date, "host": self.ws_host}
        return url + '?' + urlencode(v)

global_asr_result = ""

def run_asr_client(audio_path):
    global global_asr_result
    global_asr_result = ""
    wsParam = WsParamASR(APP_ID, API_KEY, API_SECRET, mode=ASR_MODE, language=ASR_LANGUAGE, accent=ASR_ACCENT)
    asr_segments = {}

    def parse_slm_text(encoded_text):
        try:
            decoded = base64.b64decode(encoded_text).decode("utf-8")
            payload = json.loads(decoded)
            words = []
            for item in payload.get("ws", []):
                for candidate in item.get("cw", []):
                    word = candidate.get("w", "")
                    if word:
                        words.append(word)
            return payload.get("sn"), "".join(words)
        except Exception as e:
            print("SLM Result Parse Error:", e)
            return None, ""
    
    def on_message(ws, message):
        global global_asr_result
        try:
            msg = json.loads(message)
            header = msg.get("header", msg)
            code = header.get("code", 0)
            if code != 0:
                print(f"ASR Error: {code} - {header.get('message', '')}")
            else:
                if wsParam.mode == "slm":
                    result_payload = msg.get("payload", {}).get("result", {})
                    sn, result_text = parse_slm_text(result_payload.get("text", ""))
                    if result_text:
                        if sn is None:
                            global_asr_result += result_text
                        else:
                            asr_segments[sn] = result_text
                            global_asr_result = "".join(asr_segments[index] for index in sorted(asr_segments))
                else:
                    data = msg["data"]["result"]["ws"]
                    result = ""
                    for i in data:
                        for w in i["cw"]:
                            result += w["w"]
                    global_asr_result += result
        except Exception as e:
            print("ASR Message Error:", e)

    def on_error(ws, error): print("ASR WS Error:", error)
    def on_close(ws, a, b): pass

    def on_open(ws):
        def run(*args):
            frameSize = 5120 if wsParam.mode == "slm" else 8000
            intervel = 0.04
            status = 0
            seq = 1
            # 注意：如果 ffmpeg 转换失败，这里打开文件可能会报错，需要确保 ffmpeg 安装正确
            try:
                with open(audio_path, "rb") as fp:
                    while True:
                        buf = fp.read(frameSize)
                        if not buf: status = 2
                        audio_base64 = str(base64.b64encode(buf), 'utf-8')
                        
                        if status == 0:
                            d = wsParam.create_first_frame_payload(audio_base64)
                            ws.send(json.dumps(d))
                            status = 1
                        elif status == 1:
                            seq += 1
                            d = wsParam.create_continue_frame_payload(audio_base64, seq)
                            ws.send(json.dumps(d))
                        elif status == 2:
                            seq += 1
                            d = wsParam.create_last_frame_payload(audio_base64, seq)
                            ws.send(json.dumps(d))
                            time.sleep(1)
                            break
                        time.sleep(intervel)
                ws.close()
            except Exception as e:
                print(f"读取音频文件失败: {e}")
                ws.close()

        thread.start_new_thread(run, ())

    websocket.enableTrace(False)
    ws = websocket.WebSocketApp(wsParam.create_url(), on_message=on_message, on_error=on_error, on_close=on_close)
    ws.on_open = on_open
    ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
    return global_asr_result

# ========== 4. 讯飞 TTS (语音合成) 逻辑 ==========
class WsParamTTS:
    def __init__(self, APPID, APIKey, APISecret, Text):
        self.APPID = APPID
        self.APIKey = APIKey
        self.APISecret = APISecret
        self.Text = Text
        self.CommonArgs = {"app_id": self.APPID}
        self.BusinessArgs = {"aue": "lame", "sfl": 1, "auf": "audio/L16;rate=16000", "vcn": "x2_SuhCn_XiXi", "tte": "utf8"}
        self.Data = {"status": 2, "text": str(base64.b64encode(self.Text.encode('utf-8')), "UTF8")}

    def create_url(self):
        url = 'wss://tts-api.xfyun.cn/v2/tts'
        now = datetime.now()
        date = format_date_time(time.mktime(now.timetuple()))
        signature_origin = "host: " + "ws-api.xfyun.cn" + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + "/v2/tts " + "HTTP/1.1"
        signature_sha = hmac.new(self.APISecret.encode('utf-8'), signature_origin.encode('utf-8'), digestmod=hashlib.sha256).digest()
        signature_sha = base64.b64encode(signature_sha).decode(encoding='utf-8')
        authorization_origin = f'api_key="{self.APIKey}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature_sha}"'
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')
        v = {"authorization": authorization, "date": date, "host": "ws-api.xfyun.cn"}
        return url + '?' + urlencode(v)

def run_tts_client(text, output_file):
    wsParam = WsParamTTS(APP_ID, API_KEY, API_SECRET, text)
    def on_message(ws, message):
        try:
            msg = json.loads(message)
            if msg["code"] != 0: print("TTS Error:", msg["message"])
            else:
                data = msg["data"]["audio"]
                audio = base64.b64decode(data)
                with open(output_file, 'ab') as f: f.write(audio)
                if msg["data"]["status"] == 2: ws.close()
        except: pass
    ws = websocket.WebSocketApp(wsParam.create_url(), on_message=on_message, on_open=lambda ws: ws.send(json.dumps({"common": wsParam.CommonArgs, "business": wsParam.BusinessArgs, "data": wsParam.Data})))
    ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})


def normalize_text(text):
    return "".join(ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff" or ch.isdigit())


def build_ai_commentary(payload):
    original_text = payload.get("originalText", "")
    recognized_text = payload.get("recognizedText", "")
    score = int(payload.get("score", 0) or 0)
    line_character = payload.get("lineCharacter", "小演员")
    script_title = payload.get("scriptTitle", "当前剧本")

    clean_original = normalize_text(original_text)
    clean_recognized = normalize_text(recognized_text)
    original_len = len(clean_original)
    recognized_len = len(clean_recognized)

    match_count = sum(1 for char in clean_recognized if char in clean_original)
    match_ratio = (match_count / original_len) if original_len else 0
    length_gap = abs(original_len - recognized_len)
    is_mandarin_suspect = bool(clean_original and clean_recognized and clean_original == clean_recognized)
    low_confidence = recognized_len == 0 or (recognized_len < max(1, original_len // 3))

    suggestions = []
    if low_confidence:
        summary = f"{line_character}这一句收音有点少，AI先按鼓励模式给建议。"
        suggestions.append("下一次录音时把嘴靠近一点麦克风，开头先停半秒再说。")
        suggestions.append("声音再放大一点，句尾不要收得太快。")
    elif is_mandarin_suspect:
        summary = f"{line_character}台词很清楚，不过现在更像普通话版《{script_title}》。"
        suggestions.append("保留现在的清晰度，再把儿化音和普通话口型收一收。")
        suggestions.append("先听一遍示范，再模仿节奏和拖腔，会更有方言味。")
    elif score >= 90:
        summary = f"{line_character}这一句的乡音味出来了，节奏和情绪都比较稳。"
        suggestions.append("保持现在的停顿和语气，下一句继续把尾音放开一点。")
        suggestions.append("如果想更像舞台表演，可以把关键词再强调一下。")
    elif score >= 70:
        summary = f"{line_character}这一句已经不错，方言感和清晰度基本兼顾住了。"
        suggestions.append("可以把重点词再咬得更清楚一点，整句会更有戏。")
        suggestions.append("句子中间少一点犹豫停顿，连贯度会更好。")
    else:
        summary = f"{line_character}这一句已经有参与感了，接下来重点把字头和节奏稳住。"
        suggestions.append("先跟着完整试听读一遍，再开始录音会更容易进状态。")
        suggestions.append("把语速放慢一点，每个关键词说完整。")

    if length_gap >= 3 and not low_confidence:
        suggestions.append("这次和参考台词长度差得有点多，可能漏了几个字，建议重录一次。")
    elif match_ratio < 0.45 and not low_confidence:
        suggestions.append("识别结果和参考句差异偏大，可能是咬字太快，也可能是方言味太重导致识别偏差。")

    confidence_note = "AI点评仅供练习参考，若识别受环境噪音影响，评分会自动偏保守。"
    if low_confidence:
        confidence_note = "这次识别信息较少，AI已切换为宽松点评，建议优先优化收音。"
    elif is_mandarin_suspect:
        confidence_note = "识别非常接近原句，AI判断清晰度高，但方言味可能偏弱。"

    return {
        "summary": summary,
        "suggestions": suggestions[:3],
        "confidenceNote": confidence_note,
        "tags": {
            "isMandarinSuspect": is_mandarin_suspect,
            "lowConfidence": low_confidence,
            "matchRatio": round(match_ratio, 2),
        },
    }

# ========== 5. Web 接口路由 ==========

@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))


@app.route('/api/health', methods=['GET'])
def api_health():
    return jsonify({
        'status': 'ok',
        'hasXfyunCredentials': has_xfyun_credentials(),
        'xfyunAsrMode': ASR_MODE,
        'xfyunAsrLanguage': ASR_LANGUAGE,
        'xfyunAsrAccent': ASR_ACCENT,
    })

@app.route('/api/recognize', methods=['POST'])
def api_recognize():
    """接收浏览器录音 -> 直接调用 ffmpeg 转 PCM -> 讯飞识别"""
    credential_error = require_xfyun_credentials()
    if credential_error:
        return credential_error

    if 'audio' not in request.files: return jsonify({'error': 'No audio'}), 400
    file = request.files['audio']
    
    ts = str(int(time.time()))
    webm_path = os.path.join(tempfile.gettempdir(), f"temp_{ts}.webm")
    pcm_path = os.path.join(tempfile.gettempdir(), f"temp_{ts}.pcm")
    
    try:
        file.save(webm_path)
        
        # [修改] 使用 subprocess 直接调用 ffmpeg，彻底绕过 pydub/audioop
        # 讯飞要求: 采样率16000, 单声道(ac 1), s16le格式
        cmd = [
            'ffmpeg', '-y',          # -y 覆盖输出文件
            '-i', webm_path,         # 输入文件
            '-ac', '1',              # 单声道
            '-ar', '16000',          # 16k 采样率
            '-f', 's16le',           # PCM 格式 (signed 16-bit little endian)
            pcm_path                 # 输出文件
        ]
        
        # 执行命令，如果报错会捕获异常
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # 识别
        text = run_asr_client(pcm_path)
        
        # 清理
        try:
            os.remove(webm_path)
            os.remove(pcm_path)
        except: pass
        
        return jsonify({'text': text})
        
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg Error: {e}")
        return jsonify({'error': 'Audio conversion failed. Is FFmpeg installed?'}), 500
    except Exception as e:
        print(f"General Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/synthesize', methods=['POST'])
def api_synthesize():
    credential_error = require_xfyun_credentials()
    if credential_error:
        return credential_error

    data = request.json
    text = data.get('text', '')
    if not text: return jsonify({'error': 'No text'}), 400
    
    ts = str(int(time.time()))
    mp3_path = os.path.join(tempfile.gettempdir(), f"tts_{ts}.mp3")
    
    try:
        run_tts_client(text, mp3_path)
        return send_file(mp3_path, mimetype="audio/mpeg")
    except Exception as e:
        print(f"TTS Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/ai-commentary', methods=['POST'])
def api_ai_commentary():
    data = request.json or {}
    return jsonify(build_ai_commentary(data))

if __name__ == '__main__':
    if not os.path.exists(os.path.join(BASE_DIR, 'images')):
        os.makedirs(os.path.join(BASE_DIR, 'images'))
    print("----------------------------------------------------------------")
    print(f"🚀 服务已启动。请访问: http://127.0.0.1:{PORT}")
    print("⚠️ 请确保你的电脑已经安装了 FFmpeg 并在命令行可以运行 'ffmpeg -version'")
    if not has_xfyun_credentials():
        print("⚠️ 当前未检测到讯飞环境变量，语音识别与合成功能将不可用。")
    print("----------------------------------------------------------------")
    app.run(host='0.0.0.0', port=PORT)
