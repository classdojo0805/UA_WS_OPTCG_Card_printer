import os
import io
import re
import time
import requests
import zipfile
import gc
from math import floor
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, send_file, jsonify, Response
from PIL import Image

app = Flask(__name__)

# --- 全域變數：Log 系統 ---
log_queues = {}

def send_log(session_id, message):
    if session_id and session_id in log_queues:
        log_queues[session_id].put(message)
    print(f"[{session_id}] {message}")

# --- 參數設定 ---
A4_WIDTH_CM = 21.0
A4_HEIGHT_CM = 29.7
DPI = 300
CARD_WIDTH_CM = 6.48
CARD_HEIGHT_CM = 9.19

A4_WIDTH_PX = int(A4_WIDTH_CM / 2.54 * DPI)
A4_HEIGHT_PX = int(A4_HEIGHT_CM / 2.54 * DPI)
CARD_WIDTH_PX = int(CARD_WIDTH_CM / 2.54 * DPI)
CARD_HEIGHT_PX = int(CARD_HEIGHT_CM / 2.54 * DPI)

COLS = floor(A4_WIDTH_PX / CARD_WIDTH_PX)
ROWS = floor(A4_HEIGHT_PX / CARD_HEIGHT_PX)
CARDS_PER_PAGE = COLS * ROWS

# --- 下載與 PDF 處理 ---
def download_single_image(url):
    print(f"➡️ 準備下載圖片: {url}")
    if not url or "dummy" in url:
        return Image.new("RGB", (CARD_WIDTH_PX, CARD_HEIGHT_PX), "white")
    
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://ws-tcg.com/" # 簡單的 Referer 通常能騙過大部分萬代伺服器
        }
        response = requests.get(url, stream=True, timeout=15, headers=headers)
        
        if response.status_code == 200:
            image_data = io.BytesIO(response.content)
            print(f"✅ 下載成功: {url}")
            return Image.open(image_data).convert("RGB")
    except Exception as e:
        print(f"❌ 下載例外錯誤: {url} | 錯誤: {e}")
        
    return Image.new("RGB", (CARD_WIDTH_PX, CARD_HEIGHT_PX), "white")

def parallel_download_images(url_list, max_workers=10):
    images = [None] * len(url_list)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {executor.submit(download_single_image, url): i for i, url in enumerate(url_list)}
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            try:
                images[idx] = future.result()
            except:
                images[idx] = Image.new("RGB", (CARD_WIDTH_PX, CARD_HEIGHT_PX), "white")
    return images

def generate_pdf_from_pil_images(pil_images, counts, game_type="WS"):
    final_card_images = []
    for i, img in enumerate(pil_images):
        if i >= len(counts): break
        count = counts[i]
        
        # WS 名場面預設旋轉
        if game_type == "WS" and img.width > img.height:
            img = img.rotate(90, expand=True)
        
        img = img.resize((CARD_WIDTH_PX, CARD_HEIGHT_PX), Image.LANCZOS)
        for _ in range(count):
            final_card_images.append(img.copy())

    pdf_pages = []
    for i in range(0, len(final_card_images), CARDS_PER_PAGE):
        page = Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), "white")
        batch = final_card_images[i : i + CARDS_PER_PAGE]
        for idx, card_img in enumerate(batch):
            row = idx // COLS
            col = idx % COLS
            x = col * CARD_WIDTH_PX
            y = row * CARD_HEIGHT_PX
            page.paste(card_img, (x, y))
        pdf_pages.append(page)

    pdf_buffer = io.BytesIO()
    if pdf_pages:
        pdf_pages[0].save(pdf_buffer, format="PDF", save_all=True, append_images=pdf_pages[1:])
    else:
        Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), "white").save(pdf_buffer, format="PDF")
    
    pdf_buffer.seek(0)
    return pdf_buffer

# ===========================
# 邏輯區 (全 API 無頭解析)
# ===========================

def generate_ws_official_url(card_code):
    match = re.match(r"^([A-Za-z0-9]+)/([A-Za-z0-9]+)-([A-Za-z0-9]+)", card_code)
    if not match: return None
    prefix, series, number = match.groups()
    prefix, series, number = prefix.lower(), series.lower(), number.lower()
    folder1 = prefix[0]
    folder2 = f"{prefix}_{series}"
    filename = f"{prefix}_{series}_{number}.png"
    base_url = "https://ws-tcg.com/wordpress/wp-content/images/cardlist"
    return f"{base_url}/{folder1}/{folder2}/{filename}"

def process_ws_logic(url, session_id=None):
    send_log(session_id, "WS: 正在使用無頭 API 解析貓罐子牌組資料...")
    try:
        match = re.search(r'deck/([a-zA-Z0-9]+)', url)
        if not match: return None, None
        deck_id = match.group(1)

        api_url = f"https://api.bottleneko.app/decks/{deck_id}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(api_url, headers=headers, timeout=10)
        
        if response.status_code != 200: return None, None
            
        data = response.json()
        cards_array = data.get("cards", [])
        if not cards_array: return None, None
            
        card_counts = {}
        for card in cards_array:
            card_id = card.get("id")
            if card_id:
                card_counts[card_id] = card_counts.get(card_id, 0) + 1
                
        img_urls, counts = [], []
        send_log(session_id, f"WS: 成功解析 {len(card_counts)} 種卡片，準備產生官方網址...")
        for code, count in card_counts.items():
            img_url = generate_ws_official_url(code)
            if img_url:
                img_urls.append(img_url)
                counts.append(count)
            else:
                img_urls.append(None)
                counts.append(count)

        return img_urls, counts
    except Exception as e:
        send_log(session_id, f"WS 爬蟲錯誤: {e}")
        return None, None

def process_ua_logic(url, session_id=None):
    send_log(session_id, "UA: 正在使用無頭 API 解析組牌器網址...")
    try:
        version_match = re.search(r"Version=([A-Z0-9]+)", url)
        version = version_match.group(1) if version_match else "未知"
        deck_str = url.split("Deck=")[-1]
        card_entries = deck_str.split("|")
        
        img_urls, counts = [], []
        for entry in card_entries:
            if not entry: continue
            match = re.match(r"(\d)([A-Z]+)(\d*[A-Z]*)_(\d{4})(_\d)?", entry)
            if match:
                count = int(match.group(1))
                expansion = match.group(2) + match.group(3) # EX04BT
                number = match.group(4)                     # 1040
                suffix = match.group(5)                     # _2
                
                # 將 Rugia 的異圖後綴 (_2, _3) 轉為萬代官方後綴 (_p1, _p2)
                bandai_suffix = ""
                if suffix:
                    ver_num = int(suffix.replace("_", "")) - 1
                    if ver_num > 0:
                        bandai_suffix = f"_p{ver_num}"
                        
                filename = f"{expansion}_{version}-{number[0]}-{number[1:]}{bandai_suffix}.png"
                img_url = f"https://www.unionarena-tcg.com/jp/images/cardlist/card/{filename}"
                
                img_urls.append(img_url)
                counts.append(count)
                print(f"🟢 [UA] {filename} x{count}")
                
        send_log(session_id, f"UA: 成功解析 {len(counts)} 種卡片")
        return img_urls, counts
    except Exception as e:
        send_log(session_id, f"UA 解析錯誤: {e}")
        return None, None

def process_opcg_logic(raw_text, session_id=None):
    send_log(session_id, "OPCG: 正在解析純文字牌表並自動偵測最高版本異圖...")
    try:
        matches = re.findall(r'(\d+)\s*[xX*]\s*([A-Za-z0-9-_]+)', raw_text)
        if not matches: return None, None
            
        base_url = "https://asia-tc.onepiece-cardgame.com/images/cardlist/card"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://asia-tc.onepiece-cardgame.com/"
        }

        # 內部輔助函數：負責探測每張卡片的最高版本
        def resolve_highest_version(code):
            code = code.upper().strip()
            # 若使用者已經手動指定特定異圖後綴 (例如手打 _p1)，則尊重輸入，不往下找
            if "_P" in code or "_p" in code:
                return code.replace("_P", "_p")
                
            # 依序向下探測最高版本：先找 _p2，沒有就找 _p1
            for suffix in ["_p2", "_p1"]:
                test_url = f"{base_url}/{code}{suffix}.png"
                try:
                    # 使用 head() 僅取得狀態碼，不下載圖片本體，極度節省頻寬與時間
                    res = requests.head(test_url, headers=headers, timeout=5)
                    if res.status_code == 200:
                        return f"{code}{suffix}"
                except:
                    pass
            # 若 _p2 和 _p1 都不存在，回傳原版普卡
            return code

        img_urls, counts = [], []
        # 取出所有不重複的卡號，避免同名卡重複浪費網路請求
        unique_codes = list(set([code for qty, code in matches]))
        resolved_codes = {}
        
        # 開啟 10 條執行緒並發「探測」異圖，把探測時間壓縮在 1 秒內
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_code = {executor.submit(resolve_highest_version, code): code for code in unique_codes}
            for future in as_completed(future_to_code):
                original_code = future_to_code[future]
                try:
                    resolved_codes[original_code] = future.result()
                except:
                    resolved_codes[original_code] = original_code.upper().strip()

        # 依照使用者輸入的順序進行組裝
        for qty, code in matches:
            count = int(qty)
            final_code = resolved_codes[code] # 取出探測後的最高版本卡號
            img_url = f"{base_url}/{final_code}.png"
            
            img_urls.append(img_url)
            counts.append(count)
            print(f"🟢 [OPCG] 最終採用版本: {final_code} x{count}")
            
        send_log(session_id, f"OPCG: 成功解析 {len(counts)} 種卡片 (已自動切換最高版本)")
        return img_urls, counts
    except Exception as e:
        send_log(session_id, f"OPCG 解析錯誤: {e}")
        return None, None

# ===========================
# Flask 路由
# ===========================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/stream_logs/<session_id>')
def stream_logs(session_id):
    def event_stream():
        if session_id not in log_queues:
            log_queues[session_id] = Queue()
        while True:
            message = log_queues[session_id].get()
            if message == "DONE": break
            yield f"data: {message}\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/process', methods=['POST'])
def process():
    data = request.json
    raw_urls = data.get('url', '').strip()
    session_id = data.get('session_id')
    need_zip = data.get('need_zip', False)
    
    if not raw_urls: return jsonify({'error': '請提供內容'}), 400

    # === 💡 修正區塊：智慧判斷並合併多行 OPCG 卡表 ===
    raw_lines = [u.strip() for u in raw_urls.split('\n') if u.strip()]
    url_list = []
    opcg_buffer = []
    
    for line in raw_lines:
        lower_line = line.lower()
        # 判斷是否為網址 (WS 或 UA 的 Deck 連結)
        if line.startswith("http") or "bottleneko" in lower_line or "unionarena" in lower_line or "rugiacreation" in lower_line:
            if opcg_buffer:
                url_list.append("\n".join(opcg_buffer)) # 將前面的純文字卡表合併為一筆
                opcg_buffer = []
            url_list.append(line)
        # 判斷是否符合純文字卡表格式 (例如 1xOP17-079)
        elif re.search(r'\d+\s*[xX*]\s*[A-Za-z0-9-_]+', line):
            opcg_buffer.append(line)
        else:
            # 防呆：遇到其他無法辨識的文字，若正在讀取卡表則接續寫入
            if opcg_buffer:
                opcg_buffer.append(line)
            else:
                url_list.append(line)
                
    # 迴圈結束後，清空剩餘的卡表緩衝區
    if opcg_buffer:
        url_list.append("\n".join(opcg_buffer))
    # =================================================

    total_urls = len(url_list)
    
    output_as_zip = (total_urls > 1) or need_zip
    send_log(session_id, f"收到請求，共偵測到 {total_urls} 個連結/行數，輸出模式: {'ZIP' if output_as_zip else 'PDF'}")
    
    master_zip_buffer = io.BytesIO() if output_as_zip else None
    master_zip = zipfile.ZipFile(master_zip_buffer, 'w', zipfile.ZIP_DEFLATED) if output_as_zip else None
    single_pdf_buffer = None
    single_game_type = "WS"
    
    try:
        for index, url in enumerate(url_list, start=1):
            send_log(session_id, f"--- 正在處理第 {index}/{total_urls} 筆資料 ---")
            
            lower_input = url.lower()
            if "unionarena" in lower_input or "rugiacreation" in lower_input:
                game_type = "UA"
                img_urls, counts = process_ua_logic(url, session_id)
            elif "bottleneko" in lower_input or ".app" in lower_input:
                game_type = "WS"
                img_urls, counts = process_ws_logic(url, session_id)
            elif "xop" in lower_input or "xst" in lower_input or "xeb" in lower_input or "xprb" in lower_input or "x" in lower_input:
                game_type = "OPCG"
                img_urls, counts = process_opcg_logic(url, session_id)
            else:
                send_log(session_id, f"[{index}] ⚠️ 無法辨識格式，跳過")
                continue
                
            if not img_urls or not counts:
                send_log(session_id, f"[{index}] ⚠️ 解析失敗或查無卡片，跳過")
                continue

            send_log(session_id, f"[{index}] {game_type} 解析完畢！並發下載 {len(img_urls)} 張圖片中...")
            pil_images = parallel_download_images(img_urls, max_workers=10)
            
            send_log(session_id, f"[{index}] 圖片下載完成，正在合成 PDF...")
            pdf_buffer = generate_pdf_from_pil_images(pil_images, counts, game_type)
            
            if output_as_zip:
                pdf_filename = f"Deck_{index}_{game_type}.pdf"
                master_zip.writestr(pdf_filename, pdf_buffer.getvalue())
                
                if need_zip:
                    for i, img in enumerate(pil_images):
                        img_byte_arr = io.BytesIO()
                        img.save(img_byte_arr, format='JPEG', quality=95)
                        image_filename = f"Deck_{index}_images/{i+1:02d}.jpg"
                        master_zip.writestr(image_filename, img_byte_arr.getvalue())
            else:
                single_pdf_buffer = pdf_buffer
                single_game_type = game_type

        if output_as_zip:
            master_zip.close()
            master_zip_buffer.seek(0)
            send_log(session_id, "處理完成！開始傳輸 ZIP 壓縮檔...")
            return send_file(master_zip_buffer, as_attachment=True, download_name='Batch_Decks.zip', mimetype='application/zip')
        else:
            if single_pdf_buffer:
                single_pdf_buffer.seek(0)
                send_log(session_id, "處理完成！開始傳輸 PDF 檔案...")
                return send_file(single_pdf_buffer, as_attachment=True, download_name=f'{single_game_type}_Deck.pdf', mimetype='application/pdf')
            else:
                return jsonify({'error': '所有連結皆解析失敗'}), 400

    except Exception as e:
        send_log(session_id, f"嚴重錯誤: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if session_id in log_queues: log_queues[session_id].put("DONE")
        gc.collect()

if __name__ == '__main__':
    from waitress import serve
    print("⚡ API 無頭極速版啟動中 (已支援 WS, UA, OPCG)... 伺服器已就緒。")
    serve(app, host='0.0.0.0', port=5000)
